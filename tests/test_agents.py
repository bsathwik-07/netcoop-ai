"""Agents in isolation and the orchestration's failure handling (timeouts, crashes, routing)."""

from __future__ import annotations

import asyncio
import dataclasses
import json

from netcoop.agents.analysis import AnalysisAgent
from netcoop.agents.connectivity import ConnectivityAgent
from netcoop.agents.infrastructure import InfrastructureAgent
from netcoop.agents.performance import PerformanceAgent
from netcoop.agents.registry import discover
from netcoop.bus import MessageBus
from netcoop.config import get_settings
from netcoop.ingest import TelemetryAdapter
from netcoop.pipeline import diagnose, run_scenario_async
from netcoop.protocol.envelope import Envelope
from netcoop.protocol.payloads import Query, QueryPredicate, TaskAssignment
from netcoop.protocol.vocabulary import DiagnosisStatus, Hypothesis, MessageType
from netcoop.sim.generator import generate
from netcoop.sim.organizer import generate_batch
from netcoop.topology import default_topology


def _task(records: list[dict]) -> TaskAssignment:
    res = TelemetryAdapter(get_settings(), default_topology()).ingest(json.dumps(records))
    return TaskAssignment(task_id="T", deadline_s=3, records=res.records)


def _agent(cls):  # agents are only analyzed here, never started
    return cls(MessageBus())


def test_registry_discovers_all_six_roles_plus_plugin() -> None:
    names = set(discover())
    assert {"ingestion_agent", "performance_agent", "infrastructure_agent", "connectivity_agent",
            "correlation_agent", "reporting_agent", "services_agent"} <= names


def test_infrastructure_agent_on_organizer_hardware_record() -> None:
    async def go() -> None:
        agent = InfrastructureAgent(MessageBus())
        finding = agent.analyze(_task(generate_batch("hardware_failure", 1, seed=3)), "INC-T")
        assert finding.top_hypothesis is Hypothesis.HARDWARE_FAILURE
        assert any(e.metric == "interface_errors" and e.severity == 1.0 for e in finding.evidence)
        answer = agent.answer(Query(query_id="q", question="?", predicate=QueryPredicate.METRIC_ABOVE_THRESHOLD,
                                    params={"device_id": finding.evidence[0].device_id, "metric": "interface_errors"}),
                              "INC-T")
        assert answer.answer is True and Hypothesis.HARDWARE_FAILURE in answer.supports

    asyncio.run(go())


def test_performance_agent_sees_congestion_and_answers_path_queries() -> None:
    async def go() -> None:
        agent = PerformanceAgent(MessageBus())
        records, _ = generate("link_congestion", seed=1, duration=10)
        finding = agent.analyze(_task(records), "INC-T")
        assert finding.top_hypothesis is Hypothesis.LINK_CONGESTION
        resp = agent.answer(Query(query_id="q", question="?", predicate=QueryPredicate.PATH_HEALTHY,
                                  params={"device_id": "DNS_SRV_01"}), "INC-T")
        assert resp.answer is True

    asyncio.run(go())


def test_connectivity_agent_localizes_the_congested_uplink() -> None:
    async def go() -> None:
        agent = ConnectivityAgent(MessageBus())
        records, _ = generate("link_congestion", seed=1, duration=10)
        finding = agent.analyze(_task(records), "INC-T")
        loc = finding.localization
        assert loc is not None and loc.root_device == "HOSTEL_CORE_SW_01"
        assert "SW_CAMPUS_1" in loc.symptomatic_devices and "hostel_zone" in loc.affected_segments

    asyncio.run(go())


def test_agent_without_topology_degrades_gracefully() -> None:
    async def go() -> None:
        agent = ConnectivityAgent(MessageBus())
        agent.topology = None
        finding = agent.analyze(_task(generate_batch("normal", 2, seed=1)), "INC-T")
        assert finding.limitations and "topology" in finding.limitations[0]

    asyncio.run(go())


class _SlowPerformance(PerformanceAgent):
    async def handle(self, envelope: Envelope) -> None:
        if envelope.type is MessageType.TASK_ASSIGNMENT:
            await asyncio.sleep(30)  # never answers in time
        await super().handle(envelope)


class _CrashingInfra(InfrastructureAgent):
    def analyze(self, task, incident):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated crash")


def test_missing_and_crashed_agents_become_limitations_not_failures() -> None:
    settings = get_settings()
    fast = dataclasses.replace(settings, orchestration=dict(settings.orchestration, agent_timeout_s=0.5,
                                                                  query_timeout_s=0.3))
    result = asyncio.run(run_scenario_async(
        "device_overload", settings=fast,
        agent_overrides={"performance_agent": _SlowPerformance, "infrastructure_agent": _CrashingInfra}))
    r = result.report
    assert r is not None
    assert r.status is not DiagnosisStatus.FAILED
    joined = " ".join(r.limitations)
    assert "performance_agent" in joined and "infrastructure_agent" in joined
    assert {s.agent for s in r.agent_findings_summary if not s.responded} == {"performance_agent", "infrastructure_agent"}
    errors = [m.payload.code.value for m in result.messages if m.type is MessageType.ERROR]
    assert "E_AGENT_TIMEOUT" in errors and "E_INTERNAL" in errors


def test_capability_routing_skips_agents_with_nothing_to_do() -> None:
    result = diagnose(generate_batch("normal", 1, seed=1))
    tasks = {m.to_agent for m in result.messages if m.type is MessageType.TASK_ASSIGNMENT}
    assert "services_agent" not in tasks  # organizer data has no DNS/DHCP metrics
    assert {"performance_agent", "infrastructure_agent"} <= tasks


def test_every_message_in_a_run_is_valid_on_the_wire() -> None:
    from netcoop.protocol.envelope import decode_message

    result = diagnose(generate_batch("hardware_failure", 1, seed=5))
    assert result.messages
    for env in result.messages:
        assert decode_message(env.to_wire()).msg_id == env.msg_id
    causes = {m.msg_id for m in result.messages}
    assert all(m.causation_id is None or m.causation_id in causes or m.type is MessageType.ERROR
               for m in result.messages if m.type in (MessageType.FINDING, MessageType.RESPONSE))


def test_a_new_agent_plugs_in_without_double_counting() -> None:
    import importlib.util

    from netcoop.config import REPO_ROOT

    spec = importlib.util.spec_from_file_location("custom_agent", REPO_ROOT / "examples" / "custom_agent.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    base = asyncio.run(run_scenario_async("weak_wifi", seed=3))
    extended = asyncio.run(run_scenario_async("weak_wifi", seed=3,
                                              agent_overrides={"wireless_agent": module.WirelessAgent}))
    assert extended.report is not None and base.report is not None
    summary = {s.agent: s.top_hypothesis for s in extended.report.agent_findings_summary}
    assert summary["wireless_agent"] is Hypothesis.WEAK_WIFI
    assert extended.report.root_cause_code is Hypothesis.WEAK_WIFI
    # same evidence, observed by two agents, gives the same probability (no double counting)
    assert extended.report.confidence_breakdown.p_top == base.report.confidence_breakdown.p_top


def test_analysis_agents_share_the_base_class() -> None:
    assert issubclass(PerformanceAgent, AnalysisAgent) and issubclass(ConnectivityAgent, AnalysisAgent)
    assert _agent(PerformanceAgent).capability().inputs
