"""BaseAgent: bus membership, capability announcement, heartbeats, contained errors."""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Coroutine
from typing import Any, ClassVar

from netcoop.bus.bus import MessageBus
from netcoop.config import Settings, get_settings
from netcoop.protocol.envelope import BROADCAST, SYSTEM_CORRELATION, Envelope, make_message
from netcoop.protocol.payloads import (
    CapabilityAnnounce,
    ErrorPayload,
    Heartbeat,
    Payload,
    QueryPredicate,
    Skill,
)
from netcoop.protocol.vocabulary import PROTOCOL, ErrorCode, Hypothesis, MessageType

# Protocol versions every BaseAgent can read (older ones are migrated on decode).
DEFAULT_SUPPORTED_PROTOCOLS = ("netcoop/1.0", "netcoop/1.1", PROTOCOL)


class BaseAgent(ABC):
    name: ClassVar[str]
    version: ClassVar[str] = "1.0.0"
    description: ClassVar[str] = ""
    consumes: ClassVar[frozenset[MessageType]] = frozenset()
    produces: ClassVar[frozenset[MessageType]] = frozenset()
    inputs: ClassVar[tuple[str, ...]] = ()
    metrics: ClassVar[tuple[str, ...]] = ()
    hypotheses: ClassVar[tuple[Hypothesis, ...]] = ()
    query_predicates: ClassVar[tuple[QueryPredicate, ...]] = ()
    skills: ClassVar[tuple[Skill, ...]] = ()
    supported_protocols: ClassVar[tuple[str, ...]] = DEFAULT_SUPPORTED_PROTOCOLS

    def __init__(self, bus: MessageBus, settings: Settings | None = None) -> None:
        self.bus = bus
        self.settings = settings or get_settings()
        self.log = logging.getLogger(f"netcoop.agent.{self.name}")
        self._started_at = time.monotonic()

    # -- lifecycle --------------------------------------------------------------------------

    async def start(self) -> None:
        self._started_at = time.monotonic()
        self.bus.subscribe(self.name, self._on_message, self.consumes)
        await self.send(MessageType.CAPABILITY_ANNOUNCE, self.capability())
        await self.send(MessageType.HEARTBEAT, self.heartbeat("ready"))

    async def stop(self) -> None:
        await self.send(MessageType.HEARTBEAT, self.heartbeat("stopping"))
        await self.bus.unsubscribe(self.name)

    def capability(self) -> CapabilityAnnounce:
        return CapabilityAnnounce(
            agent=self.name,
            version=self.version,
            description=self.description,
            supported_protocols=list(self.supported_protocols),
            consumes=sorted(self.consumes),
            produces=sorted(self.produces),
            inputs=list(self.inputs),
            metrics=list(self.metrics),
            hypotheses=list(self.hypotheses),
            query_predicates=list(self.query_predicates),
            skills=list(self.skills),
        )

    def heartbeat(self, state: str) -> Heartbeat:
        return Heartbeat(
            agent=self.name,
            version=self.version,
            state=state,  # type: ignore[arg-type]
            uptime_s=round(time.monotonic() - self._started_at, 3),
        )

    # -- messaging --------------------------------------------------------------------------

    async def _on_message(self, envelope: Envelope) -> None:
        try:
            await self.handle(envelope)
        except Exception as exc:  # contain every failure and report it on the bus
            self.log.exception("unhandled error while handling %s", envelope.type)
            await self.send_error(
                ErrorCode.E_INTERNAL,
                f"{self.name} failed while handling {envelope.type}: {type(exc).__name__}: {exc}",
                original=envelope,
            )

    @abstractmethod
    async def handle(self, envelope: Envelope) -> None:
        """React to one message. Exceptions are caught and turned into E_INTERNAL errors."""

    def message(
        self,
        message_type: MessageType,
        payload: Payload,
        *,
        correlation_id: str = SYSTEM_CORRELATION,
        to: str = BROADCAST,
        causation_id: str | None = None,
    ) -> Envelope:
        return make_message(
            message_type,
            payload,
            from_agent=self.name,
            to_agent=to,
            correlation_id=correlation_id,
            causation_id=causation_id,
            ttl_s=int(self.settings.orchestration.get("message_ttl_s", 300)),
        )

    async def send(
        self,
        message_type: MessageType,
        payload: Payload,
        *,
        correlation_id: str = SYSTEM_CORRELATION,
        to: str = BROADCAST,
        causation_id: str | None = None,
    ) -> Envelope:
        envelope = self.message(
            message_type, payload, correlation_id=correlation_id, to=to, causation_id=causation_id
        )
        await self.bus.publish(envelope)
        return envelope

    async def reply(self, original: Envelope, message_type: MessageType, payload: Payload) -> Envelope:
        return await self.send(
            message_type,
            payload,
            correlation_id=original.correlation_id,
            to=original.from_agent,
            causation_id=original.msg_id,
        )

    async def send_error(
        self,
        code: ErrorCode,
        text: str,
        *,
        original: Envelope | None = None,
        correlation_id: str | None = None,
        detail: dict[str, Any] | None = None,
        recoverable: bool = True,
    ) -> Envelope:
        payload = ErrorPayload(
            code=code,
            message=text,
            detail={k: v if isinstance(v, str | int | float | bool) or v is None else str(v)
                    for k, v in (detail or {}).items()},
            offending_msg_id=original.msg_id if original else None,
            recoverable=recoverable,
        )
        return await self.send(
            MessageType.ERROR,
            payload,
            # Errors are broadcast so the correlator and observers see them, not only the sender.
            correlation_id=correlation_id or (original.correlation_id if original else SYSTEM_CORRELATION),
            to=BROADCAST,
            causation_id=original.msg_id if original else None,
        )

    def spawn(self, coro: Coroutine[Any, Any, Any], name: str | None = None) -> None:
        """Run long work (e.g. orchestration that waits for replies) without blocking delivery."""
        self.bus.spawn(coro, name=name or f"{self.name}:task")
