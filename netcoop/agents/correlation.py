"""Fault Correlation agent: orchestrator (orchestrator-workers) and judge (evaluator loop).

1. TELEMETRY_RAW from a client -> forwarded to the agent that normalizes telemetry.
2. TASK_ASSIGNMENT to every analysis agent whose declared inputs match the batch (capability routing).
3. Waits for FINDINGs with a per-agent timeout; missing agents become limitations.
4. Conflict -> challenge round(s): targeted QUERYs to the owning agents, RESPONSEs re-score.
5. Decide or abstain -> DIAGNOSIS to the reporting agent.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass, field

from netcoop.agents.base import BaseAgent
from netcoop.agents.registry import register_agent
from netcoop.bus.store import StateStore
from netcoop.config import Settings
from netcoop.protocol.envelope import Envelope
from netcoop.protocol.payloads import (
    Ack,
    CapabilityAnnounce,
    ConflictResolution,
    Diagnosis,
    Finding,
    IngestionReport,
    Response,
    Skill,
    TaskAssignment,
    TelemetryNormalized,
    TelemetryRaw,
)
from netcoop.protocol.vocabulary import DiagnosisStatus, ErrorCode, MessageType
from netcoop.scoring.judge import Judge
from netcoop.topology import Topology, default_topology

INCIDENT_PATTERN = re.compile(r"^INC-[A-Za-z0-9_-]{1,40}$")


def new_incident_id() -> str:
    return f"INC-{uuid.uuid4().hex[:8].upper()}"


@dataclass
class _Incident:
    incident_id: str
    request: Envelope
    normalized: asyncio.Future[TelemetryNormalized]
    expected: set[str] = field(default_factory=set)
    findings: dict[str, Finding] = field(default_factory=dict)
    failed_agents: set[str] = field(default_factory=set)
    all_in: asyncio.Event = field(default_factory=asyncio.Event)
    responses: dict[str, asyncio.Future[Response]] = field(default_factory=dict)
    task_msg_ids: dict[str, str] = field(default_factory=dict)


@register_agent
class CorrelationAgent(BaseAgent):
    name = "correlation_agent"
    description = "Orchestrates the incident, combines findings, runs the challenge round and decides or abstains."
    consumes = frozenset({
        MessageType.TELEMETRY_RAW, MessageType.TELEMETRY_NORMALIZED, MessageType.FINDING,
        MessageType.RESPONSE, MessageType.ERROR, MessageType.CAPABILITY_ANNOUNCE, MessageType.TASK_ASSIGNMENT,
    })
    produces = frozenset({MessageType.TELEMETRY_RAW, MessageType.TASK_ASSIGNMENT, MessageType.QUERY,
                          MessageType.DIAGNOSIS, MessageType.ACK, MessageType.ERROR})
    skills = (Skill(id="correlation", name="Fault correlation",
                    description="Weighted evidence fusion, conflict resolution, calibrated abstention",
                    tags=["orchestration", "root-cause"]),)

    def __init__(self, bus, settings: Settings | None = None, topology: Topology | None = None,  # type: ignore[no-untyped-def]
                 store: StateStore | None = None, pause_after: str | None = None) -> None:
        super().__init__(bus, settings)
        if pause_after not in (None, "ingestion", "correlation"):
            raise ValueError("pause_after must be 'ingestion' or 'correlation'")
        self.store = store or StateStore(":memory:")
        self.pause_after = pause_after
        try:
            self.topology: Topology | None = topology or default_topology()
        except (OSError, ValueError, KeyError):
            self.topology = None
        self.directory: dict[str, CapabilityAnnounce] = {}
        self.incidents: dict[str, _Incident] = {}
        self.completed: dict[str, Diagnosis] = {}
        orch = self.settings.orchestration
        self.agent_timeout = float(orch["agent_timeout_s"])
        self.query_timeout = float(orch["query_timeout_s"])
        self.max_rounds = int(orch["max_challenge_rounds"])

    # -- inbound --------------------------------------------------------------------------------

    async def handle(self, envelope: Envelope) -> None:
        t = envelope.type
        if t is MessageType.CAPABILITY_ANNOUNCE:
            cap: CapabilityAnnounce = envelope.payload
            if cap.agent != self.name:
                self.directory[cap.agent] = cap
            return
        if t is MessageType.TASK_ASSIGNMENT and envelope.to_agent == self.name:
            task: TaskAssignment = envelope.payload
            if task.kind == "resume_incident":
                incident_id = envelope.correlation_id
                resumed = _Incident(incident_id, envelope, asyncio.get_running_loop().create_future())
                self.incidents[incident_id] = resumed
                self.pause_after = None  # a resumed run goes to the end
                self.spawn(self._resume(resumed), name=f"resume:{incident_id}")
            return
        if t is MessageType.TELEMETRY_RAW and envelope.to_agent == self.name:
            incident_id = envelope.correlation_id if INCIDENT_PATTERN.match(envelope.correlation_id) else new_incident_id()
            if incident_id in self.incidents or incident_id in self.completed:
                incident_id = new_incident_id()
            new_state = _Incident(incident_id, envelope, asyncio.get_running_loop().create_future())
            self.incidents[incident_id] = new_state
            self.spawn(self._run(new_state), name=f"incident:{incident_id}")
            return
        state = self.incidents.get(envelope.correlation_id)
        if state is None:
            return
        if t is MessageType.TELEMETRY_NORMALIZED and not state.normalized.done():
            state.normalized.set_result(envelope.payload)
        elif t is MessageType.FINDING:
            state.findings[envelope.from_agent] = envelope.payload
            self._check_all_in(state)
        elif t is MessageType.RESPONSE:
            resp: Response = envelope.payload
            fut = state.responses.get(resp.query_id)
            if fut and not fut.done():
                fut.set_result(resp)
        elif t is MessageType.ERROR and envelope.from_agent in state.expected:
            # an analysis agent crashed on its task: stop waiting for it
            if envelope.causation_id == state.task_msg_ids.get(envelope.from_agent):
                state.failed_agents.add(envelope.from_agent)
                self._check_all_in(state)

    @staticmethod
    def _check_all_in(state: _Incident) -> None:
        if state.expected and state.expected <= set(state.findings) | state.failed_agents:
            state.all_in.set()

    # -- orchestration --------------------------------------------------------------------------

    def _agents_that(self, consumes: MessageType, produces: MessageType | None = None) -> list[str]:
        return sorted(a for a, cap in self.directory.items()
                      if consumes in cap.consumes and (produces is None or produces in cap.produces))

    async def _run(self, state: _Incident) -> None:
        incident = state.incident_id
        limitations: list[str] = []
        raw: TelemetryRaw = state.request.payload
        self.store.set_phase(incident, "received", source=raw.source)
        try:
            normalized = await self._normalize(state, limitations)
            if normalized is None:
                report = IngestionReport(fatal=True, warnings=["telemetry could not be normalized"])
                await self._finish(state, Judge(self.settings, self.topology, {}, {}, report).diagnose(
                    ConflictResolution(), limitations, []))
                return
            self.store.save(incident, "normalized", normalized.model_dump_json())
            self.store.set_phase(incident, "normalized")
            if self.pause_after == "ingestion":
                await self._pause(state, "ingestion")
                return
            await self._analyze_and_decide(state, normalized, limitations)
        except Exception as exc:  # never leave a client hanging
            await self._crashed(state, exc)

    async def _analyze_and_decide(self, state: _Incident, normalized: TelemetryNormalized,
                                  limitations: list[str]) -> None:
        if normalized.report.fatal:
            judge = Judge(self.settings, self.topology, {}, self.directory, normalized.report)
            await self._finish(state, judge.diagnose(ConflictResolution(), limitations, []))
            return
        missing = await self._dispatch_and_collect(state, normalized, limitations)
        self.store.save(state.incident_id, "findings",
                        {a: f.model_dump(mode="json") for a, f in state.findings.items()})
        self.store.set_phase(state.incident_id, "analyzed")
        judge = Judge(self.settings, self.topology, dict(state.findings), dict(self.directory), normalized.report)
        conflict = await self._challenge(state, judge)
        if not conflict.conflict_detected:
            judge.apply_rules()
        diagnosis = judge.diagnose(conflict, limitations, missing)
        self.store.save(state.incident_id, "diagnosis", diagnosis.model_dump_json())
        self.store.set_phase(state.incident_id, "diagnosed")
        if self.pause_after == "correlation":
            await self._pause(state, "correlation")
            return
        await self._finish(state, diagnosis)

    async def _crashed(self, state: _Incident, exc: Exception) -> None:
        self.log.exception("incident %s failed", state.incident_id)
        self.store.set_phase(state.incident_id, "failed")
        await self.send_error(ErrorCode.E_INTERNAL, f"correlation failed: {type(exc).__name__}: {exc}",
                              correlation_id=state.incident_id)
        await self._finish(state, Diagnosis(status=DiagnosisStatus.FAILED,
                                            limitations=[f"internal error in correlation: {exc}"]))

    # -- pause / resume -------------------------------------------------------------------------

    async def _pause(self, state: _Incident, after: str) -> None:
        self.store.set_phase(state.incident_id, "paused", paused_after=after)
        self.incidents.pop(state.incident_id, None)
        await self.reply(state.request, MessageType.ACK,
                         Ack(acked_msg_id=state.request.msg_id, note=f"paused after {after}"))

    async def _resume(self, state: _Incident) -> None:
        """Continue from the last durable checkpoint (written by this or an earlier process)."""
        incident = state.incident_id
        try:
            info = self.store.incident(incident)
            diagnosis_json = self.store.load(incident, "diagnosis")
            normalized_json = self.store.load(incident, "normalized")
            if info is None or normalized_json is None:
                await self.send_error(ErrorCode.E_MISSING_REQUIRED,
                                      f"nothing to resume for {incident}: no checkpoint in the state store",
                                      original=state.request)
                await self._finish(state, Diagnosis(status=DiagnosisStatus.FAILED,
                                                    limitations=[f"no checkpoint for {incident}"]))
                return
            if diagnosis_json is not None and info.get("paused_after") == "correlation":
                await self._finish(state, Diagnosis.model_validate_json(diagnosis_json))
                return
            # paused (or crashed) before a decision: replay analysis from the normalized telemetry
            normalized = TelemetryNormalized.model_validate_json(normalized_json)
            await self._analyze_and_decide(state, normalized, [f"resumed from checkpoint '{info['phase']}'"])
        except Exception as exc:
            await self._crashed(state, exc)

    async def _normalize(self, state: _Incident, limitations: list[str]) -> TelemetryNormalized | None:
        normalizers = self._agents_that(MessageType.TELEMETRY_RAW, MessageType.TELEMETRY_NORMALIZED)
        if not normalizers:
            limitations.append("no ingestion agent available on the bus")
            return None
        raw: TelemetryRaw = state.request.payload
        await self.send(MessageType.TELEMETRY_RAW, raw, correlation_id=state.incident_id, to=normalizers[0],
                        causation_id=state.request.msg_id)
        try:
            return await asyncio.wait_for(state.normalized, timeout=self.agent_timeout * 3)
        except TimeoutError:
            await self.send_error(ErrorCode.E_AGENT_TIMEOUT, f"{normalizers[0]} did not normalize the telemetry in time",
                                  correlation_id=state.incident_id)
            limitations.append(f"{normalizers[0]} timed out")
            return None

    async def _dispatch_and_collect(self, state: _Incident, normalized: TelemetryNormalized,
                                    limitations: list[str]) -> list[str]:
        present = {m for r in normalized.records for m, v in r.metrics.items() if v.value is not None}
        has_status = any(r.status.value != "unknown" for r in normalized.records)
        workers = self._agents_that(MessageType.TASK_ASSIGNMENT, MessageType.FINDING)
        for agent in workers:
            cap = self.directory[agent]
            wanted = set(cap.inputs)
            if not (wanted & present or (has_status and "device_status" in cap.metrics)):
                continue  # capability-based routing: nothing in this batch for that agent
            records = [
                r.model_copy(update={"metrics": {m: v for m, v in r.metrics.items() if m in wanted}})
                for r in normalized.records
                if any(m in wanted for m in r.metrics) or "device_status" in cap.metrics
            ]
            task = TaskAssignment(task_id=f"{state.incident_id}-{agent}", deadline_s=self.agent_timeout,
                                  records=records, metrics_of_interest=sorted(wanted & present),
                                  context={"topology": self.topology.name if self.topology else "none"})
            env = await self.send(MessageType.TASK_ASSIGNMENT, task, correlation_id=state.incident_id, to=agent)
            state.expected.add(agent)
            state.task_msg_ids[agent] = env.msg_id
        if not state.expected:
            limitations.append("no analysis agent declared the metrics present in this batch")
            return []
        self._check_all_in(state)
        try:
            await asyncio.wait_for(state.all_in.wait(), timeout=self.agent_timeout)
        except TimeoutError:
            pass
        missing = sorted(state.expected - set(state.findings))
        for agent in missing:
            reason = "crashed while analyzing" if agent in state.failed_agents else f"no FINDING within {self.agent_timeout:.1f} s"
            await self.send_error(ErrorCode.E_AGENT_TIMEOUT if agent not in state.failed_agents else ErrorCode.E_INTERNAL,
                                  f"{agent}: {reason}; continuing without it", correlation_id=state.incident_id,
                                  detail={"agent": agent})
        for f in state.findings.values():
            limitations.extend(f.limitations)
        return missing

    async def _challenge(self, state: _Incident, judge: Judge) -> ConflictResolution:
        reasons = judge.conflict_reasons()
        conflict = ConflictResolution(conflict_detected=bool(reasons), reasons=reasons)
        while reasons and judge.rounds < self.max_rounds:
            planned = judge.plan_queries(state.incident_id)
            if not planned:
                break
            judge.rounds += 1
            loop = asyncio.get_running_loop()
            for p in planned:
                state.responses[p.query.query_id] = loop.create_future()
                await self.send(MessageType.QUERY, p.query, correlation_id=state.incident_id, to=p.agent)
            for p in planned:
                try:
                    resp = await asyncio.wait_for(state.responses[p.query.query_id], timeout=self.query_timeout)
                except TimeoutError:
                    resp = None
                    await self.send_error(ErrorCode.E_AGENT_TIMEOUT, f"{p.agent} did not answer {p.query.query_id}",
                                          correlation_id=state.incident_id)
                judge.absorb(p.query, resp)
            judge.apply_rules()
            reasons = judge.conflict_reasons()
        conflict.rounds = judge.rounds
        conflict.queries = judge.queries_text
        if judge.log:
            conflict.reasons = conflict.reasons + [f"[round log] {line}" for line in judge.log]
        return conflict

    async def _finish(self, state: _Incident, diagnosis: Diagnosis) -> None:
        self.completed[state.incident_id] = diagnosis
        self.incidents.pop(state.incident_id, None)
        reporters = self._agents_that(MessageType.DIAGNOSIS)
        targets = reporters or ["broadcast"]
        for target in targets[:1]:
            await self.send(MessageType.DIAGNOSIS, diagnosis, correlation_id=state.incident_id, to=target,
                            causation_id=state.request.msg_id)
