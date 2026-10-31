"""Bus behaviour: routing, retained capabilities, error replies, contained agent crashes, logging."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from netcoop.agents.base import BaseAgent
from netcoop.bus import MessageBus, MessageLog
from netcoop.protocol.envelope import Envelope
from netcoop.protocol.payloads import Ack, ErrorPayload
from netcoop.protocol.vocabulary import ErrorCode, MessageType


class Recorder(BaseAgent):
    name = "recorder"
    consumes = frozenset({MessageType.ACK, MessageType.ERROR, MessageType.CAPABILITY_ANNOUNCE})

    def __init__(self, bus: MessageBus, name: str = "recorder", crash: bool = False) -> None:
        self.name = name  # type: ignore[misc]
        super().__init__(bus)
        self.crash = crash
        self.received: list[Envelope] = []

    async def handle(self, envelope: Envelope) -> None:
        self.received.append(envelope)
        if self.crash and envelope.type is MessageType.ACK:
            raise RuntimeError("boom")


def run(coro):
    return asyncio.run(coro)


def test_broadcast_filtering_direct_delivery_and_sender_exclusion() -> None:
    async def scenario() -> None:
        bus = MessageBus()
        a, b = Recorder(bus, "a"), Recorder(bus, "b")
        await a.start()
        await b.start()
        await a.send(MessageType.ACK, Ack(acked_msg_id="1"), correlation_id="INC-1")
        await a.send(MessageType.HEARTBEAT, a.heartbeat("busy"))  # b does not consume heartbeats
        await b.send(MessageType.ACK, Ack(acked_msg_id="2"), to="a", correlation_id="INC-1")
        assert await bus.drain(5)

        b_types = [m.type for m in b.received]
        assert MessageType.ACK in b_types and MessageType.HEARTBEAT not in b_types
        assert not any(m.from_agent == "b" for m in b.received)
        assert [m.payload.acked_msg_id for m in a.received if m.type is MessageType.ACK] == ["2"]
        # each recipient decoded its own copy: no shared Python objects
        assert all(m is not bus.log.entries[i] for i, m in enumerate(b.received) if i < len(bus.log.entries))
        await bus.close()

    run(scenario())


def test_late_joiner_receives_retained_capabilities() -> None:
    async def scenario() -> None:
        bus = MessageBus()
        early = Recorder(bus, "early")
        await early.start()
        late = Recorder(bus, "late")
        await late.start()
        assert await bus.drain(5)
        caps = [m for m in late.received if m.type is MessageType.CAPABILITY_ANNOUNCE]
        assert [m.payload.agent for m in caps] == ["early"]
        await bus.close()

    run(scenario())


def test_invalid_wire_message_gets_structured_error_reply() -> None:
    async def scenario() -> None:
        bus = MessageBus()
        rec = Recorder(bus, "ext")
        await rec.start()
        bad = json.dumps({"protocol": "netcoop/9.0", "from_agent": "ext", "correlation_id": "INC-9"})
        assert await bus.publish_wire(bad) is None
        assert await bus.drain(5)
        errors = [m.payload for m in rec.received if m.type is MessageType.ERROR]
        assert len(errors) == 1 and isinstance(errors[0], ErrorPayload)
        assert errors[0].code is ErrorCode.E_UNSUPPORTED_VERSION
        assert bus.stats["rejected"] == 1
        await bus.close()

    run(scenario())


def test_agent_crash_is_contained_and_reported() -> None:
    async def scenario() -> None:
        bus = MessageBus()
        crasher, observer = Recorder(bus, "crasher", crash=True), Recorder(bus, "observer")
        await crasher.start()
        await observer.start()
        await observer.send(MessageType.ACK, Ack(acked_msg_id="x"), to="crasher", correlation_id="INC-2")
        assert await bus.drain(5)
        errors = [m.payload for m in observer.received if m.type is MessageType.ERROR]
        assert errors and errors[0].code is ErrorCode.E_INTERNAL
        assert "boom" in errors[0].message
        # the crashed agent keeps working
        await observer.send(MessageType.ACK, Ack(acked_msg_id="y"), to="crasher", correlation_id="INC-2")
        assert await bus.drain(5)
        assert len([m for m in crasher.received if m.type is MessageType.ACK]) == 2
        await bus.close()

    run(scenario())


def test_jsonl_log_per_incident_is_replayable(tmp_path: Path) -> None:
    async def scenario() -> None:
        bus = MessageBus(MessageLog(tmp_path))
        a, b = Recorder(bus, "a"), Recorder(bus, "b")
        await a.start()
        await b.start()
        await a.send(MessageType.ACK, Ack(acked_msg_id="1"), correlation_id="INC-7")
        assert await bus.drain(5)
        await bus.close()

    run(scenario())
    lines = MessageLog.read_file(tmp_path / "INC-7" / "messages.jsonl")
    assert len(lines) == 1 and lines[0]["type"] == "ACK"
    assert (tmp_path / "system" / "messages.jsonl").exists()


def test_unknown_recipient_goes_to_dead_letters() -> None:
    async def scenario() -> None:
        bus = MessageBus()
        a = Recorder(bus, "a")
        await a.start()
        await a.send(MessageType.ACK, Ack(acked_msg_id="1"), to="nobody")
        assert await bus.drain(5)
        assert any("nobody" in d.reason for d in bus.dead_letters)
        await bus.close()

    run(scenario())
