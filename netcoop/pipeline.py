
"""Run one incident end to end: start every registered agent on a bus, submit telemetry, await the report."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from netcoop.agents.base import BaseAgent
from netcoop.agents.correlation import new_incident_id
from netcoop.agents.registry import discover
from netcoop.bus import MessageBus, MessageLog
from netcoop.bus.store import StateStore
from netcoop.config import Settings, get_settings
from netcoop.protocol.envelope import Envelope
from netcoop.protocol.payloads import (
    ApprovalDecision,
    ApprovalRequest,
    DiagnosticReport,
    Finding,
    IngestionReport,
    Report,
    TaskAssignment,
    TelemetryRaw,
)
from netcoop.protocol.vocabulary import MessageType
from netcoop.sim.chaos import mutate
from netcoop.sim.generator import generate
from netcoop.topology import Topology, default_topology

OPERATOR = "operator"


class OperatorClient(BaseAgent):
    """The human/CLI side of the conversation: submits telemetry, receives the report, decides approvals."""

    name = OPERATOR
    description = "Human operator console (CLI or dashboard)"
    consumes = frozenset({MessageType.REPORT, MessageType.APPROVAL_REQUEST, MessageType.ACK, MessageType.ERROR})
    produces = frozenset({MessageType.TELEMETRY_RAW, MessageType.TASK_ASSIGNMENT, MessageType.APPROVAL_DECISION})

    def __init__(self, bus: MessageBus, settings: Settings | None = None) -> None:
        super().__init__(bus, settings)
        self.reports: dict[str, asyncio.Future[Report | str]] = {}
        self.acks: dict[str, asyncio.Future[str]] = {}
        self.approval_requests: list[ApprovalRequest] = []

    async def handle(self, envelope: Envelope) -> None:
        if envelope.type is MessageType.REPORT:
            self._resolve(self.reports, envelope.correlation_id, envelope.payload)
        elif envelope.type is MessageType.APPROVAL_REQUEST:
            self.approval_requests.append(envelope.payload)
        elif envelope.type is MessageType.ACK:
            note = envelope.payload.note
            if note.startswith("paused after"):
                self._resolve(self.reports, envelope.correlation_id, note)
            if envelope.causation_id:
                self._resolve(self.acks, envelope.causation_id, note)
        elif envelope.type is MessageType.ERROR and envelope.causation_id in self.acks:
            self._resolve(self.acks, envelope.causation_id, f"error: {envelope.payload.message}")

    @staticmethod
    def _resolve(table: dict, key: str, value: Any) -> None:  # type: ignore[type-arg]
        fut = table.get(key)
        if fut is None:
            fut = table[key] = asyncio.get_running_loop().create_future()
        if not fut.done():
            fut.set_result(value)

    def expect(self, incident_id: str) -> asyncio.Future[Report | str]:
        return self.reports.setdefault(incident_id, asyncio.get_running_loop().create_future())

    def expect_ack(self, msg_id: str) -> asyncio.Future[str]:
        return self.acks.setdefault(msg_id, asyncio.get_running_loop().create_future())


@dataclass
class RunResult:
    incident_id: str
    report: DiagnosticReport | None
    messages: list[Envelope] = field(default_factory=list)
    ingestion: IngestionReport | None = None
    findings: dict[str, Finding] = field(default_factory=dict)
    error: str | None = None
    report_path: str | None = None
    truth: dict[str, Any] | None = None
    paused: str | None = None  # e.g. "paused after ingestion"
    approval_requests: list[ApprovalRequest] = field(default_factory=list)


def _as_text(raw: str | bytes | dict | list) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, bytes):
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            return raw.decode("latin-1")  # lossless; the adapter rejects it as binary
    return json.dumps(raw)


def build_agents(bus: MessageBus, settings: Settings, topology: Topology | None, out_dir: Path | None,
                 exclude: set[str] | None = None, overrides: dict[str, type[BaseAgent]] | None = None,
                 extra: dict[str, Any] | None = None) -> list[BaseAgent]:
    classes = discover()
    classes.update(overrides or {})
    agents: list[BaseAgent] = []
    offered = {"settings": settings, "topology": topology, "out_dir": out_dir, **(extra or {})}
    for name, cls in sorted(classes.items()):
        if exclude and name in exclude:
            continue
        params = inspect.signature(cls.__init__).parameters
        kwargs = {k: v for k, v in offered.items() if k in params}
        agents.append(cls(bus, **kwargs))
    return agents


async def run_incident_async(
    raw: str | bytes | dict | list,
    *,
    source: str = "inline",
    content_type: str = "auto",
    incident_id: str | None = None,
    settings: Settings | None = None,
    topology: Topology | None = None,
    out_dir: Path | None = None,
    on_message: Callable[[Envelope], None] | None = None,
    exclude_agents: set[str] | None = None,
    agent_overrides: dict[str, type[BaseAgent]] | None = None,
    agent_extra: dict[str, Any] | None = None,
    timeout_s: float = 60.0,
    store: StateStore | None = None,
    pause_after: str | None = None,
    resume: bool = False,
) -> RunResult:
    """Run one incident. With `resume=True`, `raw` is ignored and `incident_id` is continued from
    its last checkpoint in `store` (written by an earlier, paused or crashed, run)."""
    settings = settings or get_settings()
    if topology is None:
        try:
            topology = default_topology()
        except (OSError, ValueError, KeyError):
            topology = None
    if resume and not incident_id:
        raise ValueError("resume needs the incident_id to continue")
    incident_id = incident_id or new_incident_id()
    store = store or StateStore(":memory:")
    bus = MessageBus(MessageLog(out_dir))
    if on_message:
        bus.add_tap(on_message)
    from netcoop.llm.narrator import optional_hooks

    extra = {"store": store, "pause_after": pause_after, **optional_hooks(), **(agent_extra or {})}
    agents = build_agents(bus, settings, topology, out_dir, exclude_agents, agent_overrides, extra)
    operator = OperatorClient(bus, settings)
    result = RunResult(incident_id=incident_id, report=None)
    try:
        for agent in [*agents, operator]:
            await agent.start()
        await bus.drain(5)
        waiter = operator.expect(incident_id)
        if resume:
            await operator.send(MessageType.TASK_ASSIGNMENT,
                                TaskAssignment(task_id=incident_id, kind="resume_incident", deadline_s=timeout_s,
                                               records=[]),
                                correlation_id=incident_id, to="correlation_agent")
        else:
            await operator.send(MessageType.TELEMETRY_RAW,
                                TelemetryRaw(source=source, content_type=content_type, content=_as_text(raw)),  # type: ignore[arg-type]
                                correlation_id=incident_id, to="correlation_agent")
        outcome = await asyncio.wait_for(waiter, timeout=timeout_s)
        await bus.drain(5)
        if isinstance(outcome, str):
            result.paused = outcome
        else:
            result.report = outcome.report
            result.report_path = outcome.json_path
        result.approval_requests = list(operator.approval_requests)
    except TimeoutError:
        result.error = f"no report within {timeout_s:.0f} s"
    except Exception as exc:  # the runner reports, it never crashes the caller
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        result.messages = bus.log.for_incident(incident_id)
        for env in result.messages:
            if env.type is MessageType.TELEMETRY_NORMALIZED:
                result.ingestion = env.payload.report
            elif env.type is MessageType.FINDING:
                result.findings[env.from_agent] = env.payload
        await bus.close()
    return result


def diagnose(raw: str | bytes | dict | list, **kwargs: Any) -> RunResult:
    return asyncio.run(run_incident_async(raw, **kwargs))


def resume_incident(incident_id: str, store: StateStore, **kwargs: Any) -> RunResult:
    return asyncio.run(run_incident_async("", incident_id=incident_id, store=store, resume=True, **kwargs))


async def decide_action_async(
    incident_id: str, action_id: str, approved: bool, store: StateStore, *, reason: str = "",
    decided_by: str = "human_operator", settings: Settings | None = None, out_dir: Path | None = None,
    timeout_s: float = 120.0,
) -> dict[str, Any]:
    """A human approves or rejects one proposed action. The decision travels as an APPROVAL_DECISION
    on the bus to the remediation agent; only approved actions are simulated (dry run)."""
    from netcoop.agents.remediation import RemediationAgent

    settings = settings or get_settings()
    bus = MessageBus(MessageLog(out_dir))
    agent = RemediationAgent(bus, settings, store=store, out_dir=out_dir)
    operator = OperatorClient(bus, settings)
    try:
        await agent.start()
        await operator.start()
        env = operator.message(MessageType.APPROVAL_DECISION,
                               ApprovalDecision(incident_id=incident_id, action_id=action_id, approved=approved,
                                                decided_by=decided_by, reason=reason),
                               correlation_id=incident_id, to=RemediationAgent.name)
        waiter = operator.expect_ack(env.msg_id)
        await bus.publish(env)
        note = await asyncio.wait_for(waiter, timeout=timeout_s)
        await bus.drain(5)
    finally:
        await bus.close()
    row = next((a for a in store.approvals(incident_id) if a["action_id"] == action_id), None)
    return {"note": note, "approval": row}


def decide_action(incident_id: str, action_id: str, approved: bool, store: StateStore, **kwargs: Any) -> dict[str, Any]:
    return asyncio.run(decide_action_async(incident_id, action_id, approved, store, **kwargs))


def scenario_input(scenario: str, seed: int = 42, fmt: str = "canonical", chaos: str | list[str] | None = None,
                   duration: int = 20, chaos_seed: int | None = None) -> tuple[list[dict], dict[str, Any]]:
    records, truth = generate(scenario, seed=seed, duration=duration, fmt=fmt)
    if chaos:
        records = mutate(records, chaos, seed=seed if chaos_seed is None else chaos_seed)
        truth = {**truth, "mutations": [*truth.get("mutations", []),
                                        *(chaos.split(",") if isinstance(chaos, str) else chaos)]}
    return records, truth


async def run_scenario_async(scenario: str, seed: int = 42, fmt: str = "canonical",
                             chaos: str | list[str] | None = None, duration: int = 20, **kwargs: Any) -> RunResult:
    records, truth = scenario_input(scenario, seed, fmt, chaos, duration)
    text = json.dumps(records)  # NaN stays NaN, like a real broken collector would send
    result = await run_incident_async(text, source=f"scenario:{scenario}", **kwargs)
    result.truth = truth
    return result


def run_scenario(scenario: str, **kwargs: Any) -> RunResult:
    return asyncio.run(run_scenario_async(scenario, **kwargs))
