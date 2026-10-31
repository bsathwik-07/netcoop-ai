"""Connectivity & Topology agent: blast radius, fault localization, shared-upstream signal.

Localization follows the Sherlock idea (Bahl et al., SIGCOMM 2007): prefer the single upstream
fault that explains the most symptoms. Affected devices are grouped into clusters by walking
up while the parent is also affected; each cluster's top device is a root candidate and the
rest of the cluster is symptomatic, not causal.
"""

from __future__ import annotations

from collections import defaultdict

from netcoop.agents.analysis import AnalysisAgent, Summaries
from netcoop.agents.registry import register_agent
from netcoop.config import Settings
from netcoop.protocol.payloads import (
    EvidenceItem,
    Finding,
    HypothesisScore,
    Localization,
    Query,
    QueryPredicate,
    Response,
    Skill,
    TaskAssignment,
)
from netcoop.protocol.vocabulary import Hypothesis, Quality
from netcoop.scoring.evidence import (
    DEVICE_STATUS,
    SHARED_UPSTREAM,
    MetricSummary,
    series_by_device,
    summarize_device,
    to_evidence,
)
from netcoop.scoring.hypotheses import local_scores, normal_score, raw_contributions
from netcoop.topology import Topology, default_topology

HEALTH_METRICS = ("latency_ms", "jitter_ms", "packet_loss_pct")


@register_agent
class ConnectivityAgent(AnalysisAgent):
    name = "connectivity_agent"
    description = "Maps symptoms onto the campus topology: blast radius, root vs symptomatic devices, partitions."
    inputs = HEALTH_METRICS
    metrics = (SHARED_UPSTREAM,)
    hypotheses = (Hypothesis.UPSTREAM_ISSUE,)
    query_predicates = (QueryPredicate.FAULT_LOCALIZATION,)
    skills = (
        Skill(id="localization", name="Topology-aware fault localization",
              description="Lowest-common-ancestor localization, blast radius and partition detection",
              tags=["topology", "localization"]),
    )

    def __init__(self, bus, settings: Settings | None = None, topology: Topology | None = None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(bus, settings)
        try:
            self.topology: Topology | None = topology or default_topology()
        except (OSError, ValueError, KeyError):
            self.topology = None

    def summarize(self, task: TaskAssignment) -> Summaries:
        wanted = {*HEALTH_METRICS, DEVICE_STATUS}
        return {d: summarize_device(d, s, wanted, self.settings) for d, s in series_by_device(task.records).items()}

    @staticmethod
    def degraded(by_metric: dict[str, MetricSummary]) -> bool:
        return any(s.severity > 0 for m, s in by_metric.items() if m in (*HEALTH_METRICS, DEVICE_STATUS))

    def analyze(self, task: TaskAssignment, incident: str) -> Finding:
        summaries = self.summarize(task)
        limitations: list[str] = []
        topo = self.topology
        if topo is None:
            self._memory[incident] = summaries
            return Finding(task_id=task.task_id, agent=self.name, top_hypothesis=None, data_coverage=0.0,
                           limitations=["no topology available: localization and blast radius not computed"])

        affected = sorted(d for d, by in summaries.items() if self.degraded(by))
        unknown = sorted(d for d in summaries if d not in topo)
        if unknown:
            limitations.append(f"devices not in topology, not localized: {', '.join(unknown[:5])}")

        # shared-upstream signal per device: share of its peers (siblings + parent) that are degraded
        evidence: list[EvidenceItem] = []
        scores: list[HypothesisScore] = []
        best = (-1.0, Hypothesis.NORMAL)
        derived: list[MetricSummary] = []
        for device in sorted(summaries):
            if device not in topo:
                continue
            peers = [p for p in [*topo.siblings(device), topo.parent(device)] if p and p in summaries]
            if not peers:
                continue
            share = sum(1 for p in peers if self.degraded(summaries[p])) / len(peers)
            threshold = self.settings.thresholds[SHARED_UPSTREAM]
            sev = threshold.severity(share)
            summary = MetricSummary(device, SHARED_UPSTREAM, round(share, 4), None, Quality.OK, 1.0, round(sev, 4),
                                    len(peers), threshold=threshold.warn)
            summaries[device][SHARED_UPSTREAM] = summary
            derived.append(summary)
            if device in affected or sev > 0:
                ev = to_evidence(summary, self.name)
                ev.interpretation += f" ({sum(1 for p in peers if self.degraded(summaries[p]))}/{len(peers)} peers degraded)"
                evidence.append(ev)
                raw = raw_contributions([summary], self.settings)
                local = local_scores(raw, set(self.metrics), self.settings, [summary])
                scores.extend(HypothesisScore(hypothesis=h, device_id=device, raw=v, score=local.get(h, 0.0))
                              for h, v in raw.items() if v)
                if device in affected and local.get(Hypothesis.UPSTREAM_ISSUE, 0.0) > best[0]:
                    best = (local[Hypothesis.UPSTREAM_ISSUE], Hypothesis.UPSTREAM_ISSUE)
        self._memory[incident] = summaries

        localization = self.localize(affected, summaries, topo)
        top: Hypothesis | None
        affected_known = [d for d in affected if d in topo]
        if not derived:
            top = None
            limitations.append("no neighbouring devices reported data: cannot tell whether the fault is local or upstream")
        elif best[0] >= 0.5:
            top = best[1]
        else:
            top = Hypothesis.NORMAL if normal_score(derived, self.settings) is not None else None
        return Finding(
            task_id=task.task_id,
            agent=self.name,
            agent_version=self.version,
            hypothesis_scores=scores,
            top_hypothesis=top,
            evidence=evidence,
            devices_flagged=affected_known,
            metrics_analyzed=sorted({m for by in summaries.values() for m in by}),
            data_coverage=round(len([d for d in summaries if d in topo]) / max(len(summaries), 1), 4),
            localization=localization,
            limitations=limitations,
        )

    def localize(self, affected: list[str], summaries: Summaries, topo: Topology) -> Localization:
        known = [d for d in affected if d in topo]
        if not known:
            return Localization(method="no affected devices in topology")
        affected_set = set(known)
        clusters: dict[str, list[str]] = defaultdict(list)
        for device in known:
            top = device
            while (parent := topo.parent(top)) and parent in affected_set:
                top = parent
            clusters[top].append(device)
        # several clusters under one parent without telemetry: that parent is the inferred suspect
        by_parent: dict[str, list[str]] = defaultdict(list)
        for root in clusters:
            parent = topo.parent(root)
            if parent and parent not in summaries:
                by_parent[parent].append(root)
        for parent, roots in by_parent.items():
            if len(roots) >= 2:
                clusters[parent] = [d for r in roots for d in clusters.pop(r)]

        def weight(root: str) -> tuple[int, float]:
            worst = max((s.severity for s in summaries.get(root, {}).values()), default=0.0)
            return len(clusters[root]), worst

        root = max(sorted(clusters), key=weight)
        members = sorted(set(clusters[root]) - {root})
        down = [d for d, by in summaries.items()
                if (st := by.get(DEVICE_STATUS)) and st.severity >= 1 and d in topo]
        partitioned = sorted({c for d in down for c in topo.descendants(d) if c not in summaries})
        zones = sorted({z for d in [root, *members] if (z := topo.zone(d))})
        inferred = root not in summaries
        return Localization(
            root_device=root,
            symptomatic_devices=members,
            affected_devices=sorted(set(known) | {root}),
            affected_segments=zones,
            partitioned_devices=partitioned,
            method=("inferred common upstream (no telemetry from it)" if inferred else
                    "cluster of affected devices rooted at their highest affected ancestor")
                   + (f"; {len(clusters) - 1} other independent cluster(s)" if len(clusters) > 1 else ""),
        )

    def answer_other(self, query: Query, summaries: Summaries) -> Response:
        if query.predicate is not QueryPredicate.FAULT_LOCALIZATION:
            return super().answer_other(query, summaries)
        device = str(query.params.get("device_id", ""))
        shared = summaries.get(device, {}).get(SHARED_UPSTREAM)
        topo = self.topology
        if shared is None or topo is None:
            return Response(query_id=query.query_id, answer=None,
                            explanation=f"no neighbour data around {device}: cannot tell local from upstream")
        parent = topo.parent(device) or "the core"
        above = shared.severity > 0
        return Response(
            query_id=query.query_id,
            answer=above,
            value=shared.value,
            explanation=(f"{shared.value:.0%} of devices sharing upstream {parent} with {device} are degraded: "
                         + ("fault is at or above the shared upstream" if above else "fault is local to the device")),
            evidence=[to_evidence(shared, self.name)],
            supports=[Hypothesis.UPSTREAM_ISSUE] if above else [],
            refutes=[] if above else [Hypothesis.UPSTREAM_ISSUE],
        )
