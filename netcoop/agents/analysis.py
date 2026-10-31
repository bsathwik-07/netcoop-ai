"""Shared behaviour of the analysis agents: TASK_ASSIGNMENT -> FINDING, QUERY -> RESPONSE."""

from __future__ import annotations

from typing import ClassVar

from netcoop.agents.base import BaseAgent
from netcoop.protocol.envelope import Envelope
from netcoop.protocol.payloads import (
    Claim,
    EvidenceItem,
    Finding,
    HypothesisScore,
    Query,
    QueryPredicate,
    Response,
    TaskAssignment,
)
from netcoop.protocol.vocabulary import Hypothesis, MessageType
from netcoop.scoring.evidence import MetricSummary, series_by_device, summarize_device, to_evidence
from netcoop.scoring.hypotheses import local_scores, normal_score, profiles, raw_contributions

Summaries = dict[str, dict[str, MetricSummary]]  # device -> metric -> summary


class AnalysisAgent(BaseAgent):
    consumes = frozenset({MessageType.TASK_ASSIGNMENT, MessageType.QUERY})
    produces = frozenset({MessageType.FINDING, MessageType.RESPONSE, MessageType.ERROR})
    query_predicates: ClassVar[tuple[QueryPredicate, ...]] = (QueryPredicate.METRIC_ABOVE_THRESHOLD,)

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self._memory: dict[str, Summaries] = {}  # incident -> summaries, for answering QUERYs

    # -- message handling -------------------------------------------------------------------

    async def handle(self, envelope: Envelope) -> None:
        if envelope.type is MessageType.TASK_ASSIGNMENT:
            task: TaskAssignment = envelope.payload
            finding = self.analyze(task, envelope.correlation_id)
            await self.reply(envelope, MessageType.FINDING, finding)
        elif envelope.type is MessageType.QUERY:
            query: Query = envelope.payload
            await self.reply(envelope, MessageType.RESPONSE, self.answer(query, envelope.correlation_id))

    # -- analysis ---------------------------------------------------------------------------

    def summarize(self, task: TaskAssignment) -> Summaries:
        wanted = set(self.inputs) | set(self.metrics)
        return {
            device: summarize_device(device, series, wanted, self.settings)
            for device, series in series_by_device(task.records).items()
        }

    def analyze(self, task: TaskAssignment, incident: str) -> Finding:
        summaries = self.summarize(task)
        self._memory[incident] = summaries
        owned = set(self.metrics)
        scores: list[HypothesisScore] = []
        evidence: list[EvidenceItem] = []
        best: tuple[float, Hypothesis, str | None] = (-1.0, Hypothesis.NORMAL, None)
        worst_severity = 0.0
        flagged: list[str] = []
        limitations: list[str] = []
        claims: list[Claim] = []
        for device, by_metric in sorted(summaries.items()):
            mine = [s for m, s in by_metric.items() if m in owned]
            if not mine:
                continue
            evidence.extend(to_evidence(s, self.name) for s in mine)
            raw = raw_contributions(mine, self.settings)
            local = local_scores(raw, owned, self.settings, mine)
            for h, value in raw.items():
                if value:
                    scores.append(HypothesisScore(hypothesis=h, device_id=device, raw=value,
                                                  score=local.get(h, 0.0)))
            for h, value in local.items():
                if value > best[0]:
                    best = (value, h, device)
            device_worst = max(s.severity * s.quality_factor for s in mine)
            worst_severity = max(worst_severity, device_worst)
            if device_worst > 0:
                flagged.append(device)
            claims.extend(Claim(subject=device, predicate=f"{s.metric}_above_threshold", value=True,
                                source_agent=self.name)
                          for s in mine if s.severity >= 1.0)

        agent_normal = normal_score([s for d in summaries.values() for m, s in d.items() if m in owned], self.settings)
        min_score = float(self.settings.scoring.get("agent_min_score", 0.5))
        top: Hypothesis | None
        if agent_normal is None:
            top = None
        elif best[0] >= min_score and best[0] > agent_normal:
            top = best[1]
        elif agent_normal >= 0.5:
            top = Hypothesis.NORMAL
        else:
            top = None  # abnormal signals, but none of the hypotheses this agent can judge explains them
            limitations.append(f"{self.name} sees abnormal signals it cannot attribute on its own")
        seen = {m for d in summaries.values() for m in d}
        missing = [m for m in self.inputs if m not in seen]
        coverage = 1.0 - len(missing) / len(self.inputs) if self.inputs else 1.0
        if not summaries:
            limitations.append(f"{self.name} received no usable records")
        return Finding(
            task_id=task.task_id,
            agent=self.name,
            agent_version=self.version,
            hypothesis_scores=scores,
            top_hypothesis=top,
            evidence=evidence,
            devices_flagged=flagged,
            metrics_analyzed=sorted(seen & (set(self.inputs) | owned)),
            metrics_missing=missing,
            data_coverage=round(max(coverage, 0.0), 4),
            claims=claims,
            limitations=limitations,
        )

    # -- challenge-round answers --------------------------------------------------------------

    def answer(self, query: Query, incident: str) -> Response:
        summaries = self._memory.get(incident, {})
        if query.predicate is QueryPredicate.METRIC_ABOVE_THRESHOLD:
            return self._answer_metric(query, summaries)
        return self.answer_other(query, summaries)

    def answer_other(self, query: Query, summaries: Summaries) -> Response:
        return Response(query_id=query.query_id, answer=None,
                        explanation=f"{self.name} does not answer {query.predicate} queries")

    def _answer_metric(self, query: Query, summaries: Summaries) -> Response:
        device = str(query.params.get("device_id", ""))
        metric = str(query.params.get("metric", ""))
        summary = summaries.get(device, {}).get(metric)
        if summary is None or summary.quality_factor == 0:
            return Response(query_id=query.query_id, answer=None,
                            explanation=f"no usable {metric} data for {device}; cannot confirm or refute")
        abnormal = summary.severity > 0
        supports, refutes = [], []
        for h, prof in profiles(self.settings).items():
            a, b = prof.abnormal.get(metric, 0.0), prof.healthy.get(metric, 0.0)
            if abnormal and a > 0 or not abnormal and b > 0:
                supports.append(h)
            if abnormal and a < 0 or not abnormal and a >= 0.8:
                refutes.append(h)
        level = "critical" if summary.severity >= 1 else ("above warning" if abnormal else "within normal range")
        return Response(
            query_id=query.query_id,
            answer=abnormal,
            value=summary.value,
            unit=summary.unit,
            explanation=f"{metric} on {device} is {summary.value:g} {summary.unit or ''}: {level} "
                        f"(severity {summary.severity:.2f}, quality {summary.quality})".replace("  ", " "),
            evidence=[to_evidence(summary, self.name)],
            supports=supports,
            refutes=refutes,
        )
