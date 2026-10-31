"""Weighted hypothesis scoring (README#confidence).

    raw(h, d) = sum_m q_m * ( abnormal[h][m] * s_m + healthy[h][m] * (1 - s_m) )
    S(h, d)   = raw(h, d) / Z(h)
    S(normal) = 1 - worst quality-weighted severity
    P(h)      = softmax(S(h) / T)
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from netcoop.config import Settings
from netcoop.protocol.vocabulary import Hypothesis
from netcoop.scoring.evidence import MetricSummary

FAULTS: list[Hypothesis] = [h for h in Hypothesis if h is not Hypothesis.NORMAL]


@dataclass(frozen=True)
class Profile:
    abnormal: dict[str, float]
    healthy: dict[str, float]

    @property
    def z(self) -> float:
        return sum(w for w in self.abnormal.values() if w > 0) + sum(self.healthy.values())

    def metrics(self) -> set[str]:
        return set(self.abnormal) | set(self.healthy)

    def owned_z(self, owned: set[str]) -> float:
        return sum(w for m, w in self.abnormal.items() if w > 0 and m in owned) + sum(
            w for m, w in self.healthy.items() if m in owned
        )

    def abnormal_share(self, owned: set[str]) -> float:
        total = sum(w for w in self.abnormal.values() if w > 0)
        mine = sum(w for m, w in self.abnormal.items() if w > 0 and m in owned)
        return mine / total if total else 0.0

    def weight(self, metric: str) -> float:
        """Total positive weight of a metric in this profile (for coverage)."""
        return max(self.abnormal.get(metric, 0.0), 0.0) + self.healthy.get(metric, 0.0)


def profiles(settings: Settings) -> dict[Hypothesis, Profile]:
    table = settings.weights["hypotheses"]
    return {
        Hypothesis(name): Profile(
            abnormal={k: float(v) for k, v in (spec.get("abnormal") or {}).items()},
            healthy={k: float(v) for k, v in (spec.get("healthy") or {}).items()},
        )
        for name, spec in table.items()
    }


def raw_contributions(summaries: Iterable[MetricSummary], settings: Settings) -> dict[Hypothesis, float]:
    """This caller's share of raw(h, d) for one device."""
    raw: dict[Hypothesis, float] = {h: 0.0 for h in FAULTS}
    for prof_h, prof in profiles(settings).items():
        total = 0.0
        for s in summaries:
            a = prof.abnormal.get(s.metric, 0.0)
            b = prof.healthy.get(s.metric, 0.0)
            if a or b:
                total += s.quality_factor * (a * s.severity + b * (1.0 - s.severity))
        raw[prof_h] = round(total, 6)
    return raw


def normal_score(summaries: Iterable[MetricSummary], settings: Settings) -> float | None:
    """1 - worst severity. Deliberately NOT discounted by quality: weak data may weaken evidence
    for a fault, but must never make the system more confident that everything is fine."""
    sevs = [s.severity for s in summaries if s.quality_factor > 0]
    if not sevs:
        return None
    return round(1.0 - float(settings.scoring["normal"]["worst_weight"]) * max(sevs), 6)


def has_abnormal_support(prof: Profile, summaries: Iterable[MetricSummary]) -> bool:
    """Healthy-weight evidence may support a hypothesis but never create one on its own."""
    return any(prof.abnormal.get(s.metric, 0.0) > 0 and s.severity > 0 and s.quality_factor > 0 for s in summaries)


def min_share(settings: Settings) -> float:
    return float(settings.scoring.get("agent_min_share", 0.35))


def local_scores(
    raw: dict[Hypothesis, float], owned: set[str], settings: Settings,
    summaries: Iterable[MetricSummary] | None = None,
) -> dict[Hypothesis, float]:
    """Agent-local normalized scores, only for hypotheses the agent can meaningfully judge
    (it owns at least `agent_min_share` of the hypothesis' abnormal weight)."""
    items = list(summaries) if summaries is not None else None
    out: dict[Hypothesis, float] = {}
    for h, prof in profiles(settings).items():
        if prof.abnormal_share(owned) >= min_share(settings):
            z = prof.owned_z(owned)
            supported = items is None or has_abnormal_support(prof, items)
            out[h] = round(raw.get(h, 0.0) / z, 6) if z and supported else 0.0
    return out


def eligible(h: Hypothesis, owned: set[str], settings: Settings) -> bool:
    if h is Hypothesis.NORMAL:
        return True
    prof = profiles(settings).get(h)
    return bool(prof and prof.abnormal_share(owned) >= min_share(settings))


def softmax(scores: dict[Hypothesis, float], temperature: float) -> dict[Hypothesis, float]:
    if not scores:
        return {}
    top = max(scores.values())
    exps = {h: math.exp((s - top) / temperature) for h, s in scores.items()}
    total = sum(exps.values())
    return {h: e / total for h, e in exps.items()}
