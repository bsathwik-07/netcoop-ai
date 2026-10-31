"""Protocol contract tests: round-trips, JSON Schema validation, migrations, error codes."""

from __future__ import annotations

import json

import jsonschema
import pytest

from netcoop.protocol.envelope import ProtocolError, decode_message, make_message
from netcoop.protocol.migrations import migration_path
from netcoop.protocol.payloads import PAYLOAD_MODELS, Finding, Heartbeat
from netcoop.protocol.schema_export import export_all, message_schema, protocol_tables
from netcoop.protocol.vocabulary import PROTOCOL, ErrorCode, MessageType, Unit
from tests.conftest import sample_payloads

MESSAGE_SCHEMA = message_schema()
ALL_TYPES = list(MessageType)


def test_every_message_type_has_a_payload_model() -> None:
    assert set(PAYLOAD_MODELS) == set(MessageType)
    assert set(sample_payloads()) == set(MessageType)


@pytest.mark.parametrize("message_type", ALL_TYPES, ids=str)
def test_round_trip_through_wire_and_json_schema(message_type: MessageType) -> None:
    payload = sample_payloads()[message_type]
    msg = make_message(message_type, payload, from_agent="tester", correlation_id="INC-0001")
    wire = msg.to_wire()

    jsonschema.validate(json.loads(wire), MESSAGE_SCHEMA)  # external agents rely on this schema
    decoded = decode_message(wire)

    assert decoded.type is message_type
    assert decoded.payload_schema == payload.SCHEMA
    assert decoded.payload == payload
    assert decoded.to_wire() == wire


def test_make_message_rejects_wrong_payload_type() -> None:
    with pytest.raises(TypeError):
        make_message(MessageType.FINDING, Heartbeat(agent="a", version="1"), from_agent="x")


def _wire(**overrides: object) -> dict:
    msg = make_message(MessageType.HEARTBEAT, Heartbeat(agent="a", version="1"), from_agent="a")
    data = json.loads(msg.to_wire())
    data.update(overrides)
    return data


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("{not json", ErrorCode.E_SCHEMA_INVALID),
        ('{"protocol": "netcoop/2.0", "x": NaN}', ErrorCode.E_SCHEMA_INVALID),
        ("[1, 2, 3]", ErrorCode.E_SCHEMA_INVALID),
        ('{"msg_id": "1"}', ErrorCode.E_MISSING_REQUIRED),
        (json.dumps(_wire(protocol="netcoop/3.0")), ErrorCode.E_UNSUPPORTED_VERSION),
        (json.dumps(_wire(protocol="netcoop/0.9")), ErrorCode.E_UNSUPPORTED_VERSION),
        (json.dumps(_wire(protocol="otherproto/2.0")), ErrorCode.E_SCHEMA_INVALID),
        (json.dumps(_wire(type="NOT_A_TYPE")), ErrorCode.E_SCHEMA_INVALID),
        (json.dumps(_wire(ttl_s="soon")), ErrorCode.E_SCHEMA_INVALID),
        (json.dumps({k: v for k, v in _wire().items() if k != "from_agent"}), ErrorCode.E_MISSING_REQUIRED),
        (b"\xff\xfe\x00garbage", ErrorCode.E_SCHEMA_INVALID),
    ],
)
def test_invalid_messages_produce_the_right_error_code(raw: str | bytes, code: ErrorCode) -> None:
    with pytest.raises(ProtocolError) as info:
        decode_message(raw)
    assert info.value.code is code


def test_payload_must_match_message_type() -> None:
    data = _wire(type="FINDING")  # heartbeat payload under a FINDING type
    with pytest.raises(ProtocolError) as info:
        decode_message(data)
    assert info.value.code in (ErrorCode.E_MISSING_REQUIRED, ErrorCode.E_SCHEMA_INVALID)


def test_newer_minor_version_is_accepted_and_unknown_fields_ignored() -> None:
    data = _wire(protocol="netcoop/2.7", priority="urgent")
    data["payload"]["battery_pct"] = 80
    decoded = decode_message(data)
    assert decoded.protocol == "netcoop/2.7"
    assert decoded.type is MessageType.HEARTBEAT


LEGACY_V1_0_FINDING = {
    "protocol": "netcoop/1.0",
    "id": "legacy-1",
    "incident": "INC-0042",
    "sender": "old_perf_agent",
    "recipient": "*",
    "msg_type": "FINDING",
    "ts": 1792000000.0,
    "body": {
        "task_id": "T9",
        "hypothesis_scores": {"link_congestion": 2.5, "normal": 0.1},
        "evidence": [{"metric": "link_utilization_pct", "value": 94, "interpretation": "High utilization"}],
    },
}


def test_v1_0_finding_migrates_to_v2() -> None:
    msg = decode_message(LEGACY_V1_0_FINDING)
    assert msg.protocol == PROTOCOL
    assert msg.correlation_id == "INC-0042"
    assert msg.from_agent == "old_perf_agent"
    assert msg.to_agent == "broadcast"
    assert msg.payload_schema == Finding.SCHEMA
    finding = msg.payload
    assert isinstance(finding, Finding)
    assert finding.agent == "old_perf_agent"
    assert {s.hypothesis.value: s.score for s in finding.hypothesis_scores} == {"link_congestion": 2.5, "normal": 0.1}
    assert finding.evidence[0].unit is Unit.PERCENT  # unit filled from the vocabulary
    assert finding.evidence[0].source_agent == "old_perf_agent"
    assert msg.timestamp.year == 2026


def test_v1_1_message_migrates_with_reply_link() -> None:
    legacy = {
        "protocol": "netcoop/1.1",
        "id": "legacy-2",
        "incident": "INC-0042",
        "sender": "a",
        "recipient": "correlation_agent",
        "msg_type": "ACK",
        "ts": "2026-10-14T07:05:49Z",
        "in_reply_to": "legacy-1",
        "ttl": 30,
        "body": {"acked_msg_id": "legacy-1"},
    }
    msg = decode_message(legacy)
    assert msg.causation_id == "legacy-1"
    assert msg.to_agent == "correlation_agent"
    assert msg.ttl_s == 30
    assert migration_path("netcoop/1.0") == ["netcoop/1.0", "netcoop/1.1", "netcoop/2.0"]


def test_schema_export_and_tables(tmp_path) -> None:
    paths = export_all(tmp_path)
    names = {p.name for p in paths}
    assert {"message.schema.json", "report.schema.json", "vocabulary.json"} <= names
    assert len(list((tmp_path / "payloads").glob("*.json"))) == len(MessageType)
    tables = protocol_tables()
    assert "| `correlation_id` |" in tables
    assert "### FINDING (`finding/1.0`)" in tables
