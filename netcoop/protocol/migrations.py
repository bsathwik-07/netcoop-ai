"""Version migrations for bus messages: netcoop/1.0 -> 1.1 -> 2.0.

History of the envelope (documented in PROTOCOL.md):

* 1.0  keys: id, incident, sender, recipient ('*' = all), msg_type, ts (epoch seconds), body.
       FINDING bodies carried `hypothesis_scores` as {hypothesis: score} and evidence without units.
* 1.1  adds in_reply_to and ttl; ts becomes an ISO-8601 string.
* 2.0  FIPA-style names (from_agent, to_agent, correlation_id, causation_id), explicit
       payload_schema, typed payloads, explicit unit on every evidence item.

Within major 2, minor versions are fully compatible (additive, must-ignore), so no
migration step is needed: a 2.3 message validates as 2.0 with its new fields ignored.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from netcoop.protocol.payloads import PAYLOAD_MODELS
from netcoop.protocol.vocabulary import METRICS, PROTOCOL, MessageType, parse_protocol

Migration = Callable[[dict[str, Any]], dict[str, Any]]


def _v1_0_to_v1_1(msg: dict[str, Any]) -> dict[str, Any]:
    out = dict(msg)
    ts = out.get("ts")
    if isinstance(ts, int | float):
        out["ts"] = datetime.fromtimestamp(float(ts), tz=UTC).isoformat().replace("+00:00", "Z")
    out.setdefault("in_reply_to", None)
    out.setdefault("ttl", 300)
    out["protocol"] = "netcoop/1.1"
    return out


def _migrate_finding_body(body: dict[str, Any], sender: str) -> dict[str, Any]:
    out = dict(body)
    scores = out.get("hypothesis_scores")
    if isinstance(scores, dict):
        out["hypothesis_scores"] = [{"hypothesis": h, "score": s} for h, s in scores.items()]
    evidence = []
    for item in out.get("evidence", []) or []:
        item = dict(item)
        spec = METRICS.get(str(item.get("metric")))
        if item.get("unit") is None and spec is not None:
            item["unit"] = spec.unit.value
        item.setdefault("source_agent", sender)
        evidence.append(item)
    out["evidence"] = evidence
    out.setdefault("agent", sender)
    return out


def _v1_1_to_v2_0(msg: dict[str, Any]) -> dict[str, Any]:
    msg_type = MessageType(msg["msg_type"])
    recipient = msg.get("recipient", "*")
    body = dict(msg.get("body") or {})
    if msg_type is MessageType.FINDING:
        body = _migrate_finding_body(body, str(msg.get("sender", "")))
    out: dict[str, Any] = {
        "protocol": PROTOCOL,
        "msg_id": msg["id"],
        "correlation_id": msg.get("incident") or "system",
        "causation_id": msg.get("in_reply_to"),
        "from_agent": msg["sender"],
        "to_agent": "broadcast" if recipient in (None, "*", "all") else recipient,
        "type": msg_type.value,
        "timestamp": msg["ts"],
        "ttl_s": int(msg.get("ttl", 300)),
        "payload_schema": PAYLOAD_MODELS[msg_type].SCHEMA,
        "payload": body,
    }
    return out


# (major, minor) -> step that moves a message one version forward
_STEPS: dict[tuple[int, int], Migration] = {
    (1, 0): _v1_0_to_v1_1,
    (1, 1): _v1_1_to_v2_0,
}


def migrate_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Apply migration steps until the message is at major 2. Returns a new dict."""
    current = dict(msg)
    for _ in range(len(_STEPS) + 1):
        version = parse_protocol(current["protocol"])
        if version[0] >= 2:
            return current
        step = _STEPS.get(version)
        if step is None:
            raise ValueError(f"no migration path from netcoop/{version[0]}.{version[1]}")
        current = step(current)
    raise ValueError("migration did not converge")


# ---------------------------------------------------------------------------------------------
# Telemetry record schemas (input data, not bus messages)
#
#   organizer_v1  no schema_version, metrics nested under "metrics", status Healthy|Warning|Critical
#   legacy_v1_0   schema_version 1/1.0, flat metrics at top level
#   legacy_v1_1   schema_version 1.1, flat metrics, camelCase ids, epoch `ts`, `<field>_unit` siblings
#   canonical_v2  schema_version 2.x, metrics as {"value": x, "unit": u}
# The adapter maps all of them onto canonical v2 field by field (aliases, units, status).
# ---------------------------------------------------------------------------------------------


def detect_telemetry_schema(version: Any, nested_metrics: bool) -> tuple[str, int | None]:
    """(schema name, major version) of one raw telemetry record; major None = unparseable."""
    if version is None or (isinstance(version, str) and not version.strip()):
        return ("organizer_v1" if nested_metrics else "legacy_v1_0"), 1
    text = str(version).strip().lower().removeprefix("v")
    major_text, _, minor_text = text.partition(".")
    try:
        major = int(float(major_text))
        minor = int(minor_text or 0) if minor_text.isdigit() or not minor_text else 0
    except ValueError:
        return f"unknown_{version}", None
    if major == 2:
        return "canonical_v2", 2
    if major == 1:
        if minor >= 1:
            return "legacy_v1_1", 1
        return ("organizer_v1" if nested_metrics else "legacy_v1_0"), 1
    return f"unknown_{version}", major


def migration_path(protocol: str) -> list[str]:
    """Human-readable chain of versions a message passes through, for docs and logs."""
    path = [protocol]
    version = parse_protocol(protocol)
    while version[0] < 2:
        if version not in _STEPS:
            break
        version = (1, 1) if version == (1, 0) else (2, 0)
        path.append(f"netcoop/{version[0]}.{version[1]}")
    return path
