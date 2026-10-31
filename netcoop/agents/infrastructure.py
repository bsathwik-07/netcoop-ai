"""Infrastructure Health agent: device status, CPU, memory, interface errors, queue drops, flaps, reboots."""

from __future__ import annotations

from netcoop.agents.analysis import AnalysisAgent, Summaries
from netcoop.agents.registry import register_agent
from netcoop.protocol.payloads import Query, QueryPredicate, Response, Skill
from netcoop.protocol.vocabulary import Hypothesis
from netcoop.scoring.evidence import DEVICE_STATUS, UPTIME_RESET, to_evidence

INFRA_INPUTS = ("cpu_utilization_pct", "memory_utilization_pct", "queue_drops", "interface_errors",
                "interface_flaps", "uptime_s")


@register_agent
class InfrastructureAgent(AnalysisAgent):
    name = "infrastructure_agent"
    description = "Examines device status, CPU/memory, interface errors, egress queue drops, flaps and reboots."
    inputs = INFRA_INPUTS
    metrics = (*INFRA_INPUTS[:-1], DEVICE_STATUS, UPTIME_RESET)
    hypotheses = (Hypothesis.HARDWARE_FAILURE, Hypothesis.INTERFACE_FAULT, Hypothesis.DEVICE_OVERLOAD,
                  Hypothesis.LINK_CONGESTION)
    query_predicates = (QueryPredicate.METRIC_ABOVE_THRESHOLD, QueryPredicate.DEVICE_STATUS)
    skills = (
        Skill(id="device-health", name="Device health analysis",
              description="CPU/memory load, interface error counters, queue drops, flaps, reboots",
              tags=["hardware", "interface", "cpu"]),
    )

    def answer_other(self, query: Query, summaries: Summaries) -> Response:
        if query.predicate is not QueryPredicate.DEVICE_STATUS:
            return super().answer_other(query, summaries)
        device = str(query.params.get("device_id", ""))
        status = summaries.get(device, {}).get(DEVICE_STATUS)
        if status is None:
            return Response(query_id=query.query_id, answer=None, explanation=f"no status reported for {device}")
        down = status.severity > 0
        return Response(query_id=query.query_id, answer=down, value=status.value,
                        explanation=f"{device} status severity {status.severity:.1f}",
                        evidence=[to_evidence(status, self.name)],
                        supports=[Hypothesis.HARDWARE_FAILURE] if status.severity >= 1 else [])
