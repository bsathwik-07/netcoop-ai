"""DNS/DHCP Service agent.

Registered only through the `netcoop.agents` entry point in pyproject.toml (no core code
imports it), to show that new agents plug in without touching the orchestrator.
"""

from __future__ import annotations

from netcoop.agents.analysis import AnalysisAgent, Summaries
from netcoop.protocol.payloads import Query, QueryPredicate, Response, Skill
from netcoop.protocol.vocabulary import Hypothesis
from netcoop.scoring.evidence import to_evidence

DNS_METRICS = ("dns_success_pct", "dns_latency_ms")
DHCP_METRICS = ("dhcp_success_pct", "dhcp_ack_ms", "dhcp_pool_utilization_pct", "apipa_clients")


class ServicesAgent(AnalysisAgent):
    name = "services_agent"
    description = "Analyzes DNS resolution and DHCP lease health (success rates, timings, pool, APIPA clients)."
    inputs = (*DNS_METRICS, *DHCP_METRICS)
    metrics = (*DNS_METRICS, *DHCP_METRICS)
    hypotheses = (Hypothesis.DNS_FAILURE, Hypothesis.DHCP_FAILURE)
    query_predicates = (QueryPredicate.METRIC_ABOVE_THRESHOLD, QueryPredicate.SERVICE_HEALTH)
    skills = (
        Skill(id="dns-health", name="DNS health", description="Resolution success and latency", tags=["dns"]),
        Skill(id="dhcp-health", name="DHCP health", description="Lease success, DORA timing, pool, APIPA",
              tags=["dhcp"]),
    )

    def answer_other(self, query: Query, summaries: Summaries) -> Response:
        if query.predicate is not QueryPredicate.SERVICE_HEALTH:
            return super().answer_other(query, summaries)
        service = str(query.params.get("service", "dns")).lower()
        wanted = DNS_METRICS if service == "dns" else DHCP_METRICS
        found = [s for by_metric in summaries.values() for m, s in by_metric.items() if m in wanted]
        if not found:
            return Response(query_id=query.query_id, answer=None, explanation=f"no {service} probe data")
        failing = [s for s in found if s.severity > 0]
        hyp = Hypothesis.DNS_FAILURE if service == "dns" else Hypothesis.DHCP_FAILURE
        return Response(
            query_id=query.query_id,
            answer=not failing,
            explanation=f"{service.upper()} {'healthy' if not failing else 'failing'}: "
                        + ", ".join(f"{s.device_id}.{s.metric}={s.value:g}" for s in (failing or found)[:4]),
            evidence=[to_evidence(s, self.name) for s in (failing or found)[:4]],
            supports=[hyp] if failing else [],
            refutes=[] if failing else [hyp],
        )
