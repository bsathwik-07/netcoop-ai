"""Telemetry & Performance agent: latency, jitter, loss, throughput, utilization, Wi-Fi quality."""

from __future__ import annotations

from netcoop.agents.analysis import AnalysisAgent, Summaries
from netcoop.agents.registry import register_agent
from netcoop.protocol.payloads import Query, QueryPredicate, Response, Skill
from netcoop.protocol.vocabulary import Hypothesis
from netcoop.scoring.evidence import to_evidence

PERFORMANCE_METRICS = (
    "latency_ms", "jitter_ms", "packet_loss_pct", "throughput_mbps", "bandwidth_mbps",
    "link_utilization_pct", "retransmission_pct", "wifi_rssi_dbm", "wifi_snr_db",
)


@register_agent
class PerformanceAgent(AnalysisAgent):
    name = "performance_agent"
    description = "Analyzes latency, jitter, packet loss, throughput, link utilization and Wi-Fi signal quality."
    inputs = PERFORMANCE_METRICS
    metrics = PERFORMANCE_METRICS
    hypotheses = (Hypothesis.LINK_CONGESTION, Hypothesis.WEAK_WIFI, Hypothesis.UPSTREAM_ISSUE,
                  Hypothesis.INTERFACE_FAULT)
    query_predicates = (QueryPredicate.METRIC_ABOVE_THRESHOLD, QueryPredicate.PATH_HEALTHY)
    skills = (
        Skill(id="path-performance", name="Path performance analysis",
              description="Threshold and baseline analysis of latency, jitter, loss and utilization",
              tags=["latency", "loss", "congestion"]),
        Skill(id="wifi-quality", name="Wi-Fi quality analysis", description="RSSI, SNR and retransmissions",
              tags=["wifi", "rf"]),
    )

    def answer_other(self, query: Query, summaries: Summaries) -> Response:
        if query.predicate is not QueryPredicate.PATH_HEALTHY:
            return super().answer_other(query, summaries)
        device = str(query.params.get("device_id", ""))
        by_metric = summaries.get(device, {})
        path = [by_metric[m] for m in ("latency_ms", "packet_loss_pct") if m in by_metric]
        if not path:
            return Response(query_id=query.query_id, answer=None,
                            explanation=f"no latency/loss data for {device}; path health unknown")
        healthy = all(s.severity == 0 for s in path)
        return Response(
            query_id=query.query_id,
            answer=healthy,
            explanation=(f"IP path to {device} is healthy (" if healthy else f"IP path to {device} is degraded (")
            + ", ".join(f"{s.metric}={s.value:g}{s.unit}" for s in path) + ")",
            evidence=[to_evidence(s, self.name) for s in path],
            supports=[Hypothesis.DNS_FAILURE, Hypothesis.DHCP_FAILURE] if healthy else [Hypothesis.LINK_CONGESTION],
            refutes=[Hypothesis.UPSTREAM_ISSUE, Hypothesis.LINK_CONGESTION] if healthy else [],
        )
