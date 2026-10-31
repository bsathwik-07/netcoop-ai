"""The message envelope, typed per message type, plus wire encode/decode.

Field mapping to FIPA ACL (SC00061G): type ~ performative, from_agent ~ sender,
to_agent ~ receiver, correlation_id ~ conversation-id, causation_id ~ in-reply-to,
payload_schema ~ ontology, protocol ~ protocol.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, Union
from uuid import uuid4

from pydantic import Field, TypeAdapter, ValidationError, create_model

from netcoop.protocol.payloads import PAYLOAD_MODELS, NCModel, Payload, Timestamp, utcnow
from netcoop.protocol.vocabulary import (
    PROTOCOL,
    SUPPORTED_MAJORS,
    ErrorCode,
    MessageType,
    parse_protocol,
)

BROADCAST = "broadcast"
SYSTEM_CORRELATION = "system"


class ProtocolError(Exception):
    """Raised by `decode_message`; always carries a protocol error code."""

    def __init__(self, code: ErrorCode, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.detail = detail or {}


class Envelope(NCModel):
    protocol: str = Field(PROTOCOL, pattern=r"^netcoop/\d+\.\d+$", description="family/major.minor")
    msg_id: str = Field(default_factory=lambda: str(uuid4()), description="UUID4, unique per message")
    correlation_id: str = Field(description="Incident id; ties all messages of one incident together")
    causation_id: str | None = Field(None, description="msg_id of the message that caused this one")
    from_agent: str
    to_agent: str = Field(BROADCAST, description="Agent name, or 'broadcast'")
    type: MessageType
    timestamp: Timestamp = Field(default_factory=utcnow, description="ISO-8601 UTC")
    ttl_s: int = Field(300, ge=1, description="Seconds after `timestamp` the message stays deliverable")
    payload_schema: str = Field(description="Payload schema name/version, e.g. 'finding/1.0'")
    payload: Any

    def to_wire(self) -> str:
        return self.model_dump_json()

    @property
    def summary(self) -> str:
        return f"{self.type} {self.from_agent} -> {self.to_agent}"


def _typed_envelope(message_type: MessageType, payload_model: type[Payload]) -> type[Envelope]:
    name = payload_model.__name__.removesuffix("Payload") + "Message"
    # Literal[message_type] is built at runtime, which static checkers cannot follow.
    literal: Any = Literal[message_type]  # type: ignore[valid-type]
    model: type[Envelope] = create_model(name, __base__=Envelope, type=(literal, ...), payload=(payload_model, ...))
    return model


MESSAGE_MODELS: dict[MessageType, type[Envelope]] = {
    mt: _typed_envelope(mt, model) for mt, model in PAYLOAD_MODELS.items()
}

# The discriminated union over all typed messages, built at runtime (hence Any for static checkers).
AnyMessage: Any = Annotated[Union[tuple(MESSAGE_MODELS.values())], Field(discriminator="type")]  # noqa: UP007
_MESSAGE_ADAPTER: TypeAdapter[Envelope] = TypeAdapter(AnyMessage)


def make_message(
    message_type: MessageType,
    payload: Payload,
    *,
    from_agent: str,
    correlation_id: str = SYSTEM_CORRELATION,
    to_agent: str = BROADCAST,
    causation_id: str | None = None,
    ttl_s: int = 300,
) -> Envelope:
    expected = PAYLOAD_MODELS[message_type]
    if not isinstance(payload, expected):
        raise TypeError(f"{message_type} needs a {expected.__name__} payload, got {type(payload).__name__}")
    model = MESSAGE_MODELS[message_type]
    return model(
        type=message_type,
        payload=payload,
        payload_schema=payload.SCHEMA,
        from_agent=from_agent,
        to_agent=to_agent,
        correlation_id=correlation_id,
        causation_id=causation_id,
        ttl_s=ttl_s,
    )


def _reject_nan(value: str) -> float:
    raise ValueError(f"non-finite number {value!r} is not valid JSON")


def decode_message(raw: str | bytes | dict[str, Any]) -> Envelope:
    """Parse, version-check, migrate and validate a wire message.

    Never raises anything but ProtocolError, so a receiver can always answer
    with a structured ERROR instead of crashing.
    """
    # Local import: migrations needs vocabulary only, but keep the import graph acyclic.
    from netcoop.protocol.migrations import migrate_message

    try:
        if isinstance(raw, dict):
            data = raw
        else:
            text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            data = json.loads(text, parse_constant=_reject_nan)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ProtocolError(ErrorCode.E_SCHEMA_INVALID, f"message is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise ProtocolError(ErrorCode.E_SCHEMA_INVALID, "message must be a JSON object")

    protocol = data.get("protocol")
    if not isinstance(protocol, str):
        raise ProtocolError(ErrorCode.E_MISSING_REQUIRED, "missing 'protocol' field")
    try:
        major, _minor = parse_protocol(protocol)
    except ValueError as exc:
        raise ProtocolError(ErrorCode.E_SCHEMA_INVALID, str(exc)) from None
    if major not in SUPPORTED_MAJORS:
        raise ProtocolError(
            ErrorCode.E_UNSUPPORTED_VERSION,
            f"protocol {protocol} not supported; supported majors: {list(SUPPORTED_MAJORS)}",
            {"received": protocol, "supported": ",".join(f"netcoop/{m}.x" for m in SUPPORTED_MAJORS)},
        )

    try:
        data = migrate_message(data)
    except (KeyError, TypeError, ValueError) as exc:
        raise ProtocolError(ErrorCode.E_SCHEMA_INVALID, f"cannot migrate {protocol} message: {exc}") from None

    try:
        return _MESSAGE_ADAPTER.validate_python(data)
    except ValidationError as exc:
        errors = exc.errors(include_url=False)
        missing = [".".join(str(p) for p in e["loc"]) for e in errors if e["type"] == "missing"]
        first = errors[0]
        where = ".".join(str(p) for p in first["loc"])
        code = ErrorCode.E_MISSING_REQUIRED if missing and len(missing) == len(errors) else ErrorCode.E_SCHEMA_INVALID
        raise ProtocolError(
            code,
            f"{len(errors)} validation error(s); first at '{where}': {first['msg']}",
            {"errors": len(errors), "first_location": where, "missing": ",".join(missing)},
        ) from None
