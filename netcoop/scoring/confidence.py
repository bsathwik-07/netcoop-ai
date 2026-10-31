"""Documented confidence formula and abstention rule (README#confidence).

    confidence = P(top) * data_coverage * agreement_factor

* data_coverage: share of the top hypothesis' weight profile observed on the root device,
  each metric counted with its quality factor.
* agreement_factor: weighted share of eligible agents whose own top hypothesis matches.
* abstain (insufficient_evidence) if confidence < decide_threshold, or P(top)-P(second) <
  min_margin, or a required corroborating signal is missing, or ingestion was fatal.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from netcoop.config import Settings
from netcoop.protocol.vocabulary import ConfidenceCategory, DiagnosisStatus, Hypothesis
from netcoop.scoring.evidence import MetricSummary
from netcoop.scoring.hypotheses import profiles


def category(settings: Settings, confidence: float) -> ConfidenceCategory:
    cats = settings.scoring["confidence_categories"]
    if confidence >= float(cats["high"]):
        return ConfidenceCategory.HIGH
    if confidence >= float(cats["medium"]):
        return ConfidenceCategory.MEDIUM
    return ConfidenceCategory.LOW


def metric_coverage(h: Hypothesis, observed: dict[str, MetricSummary], settings: Settings) -> float:
    """Share of the profile weight of `h` that was observed, quality-weighted."""
    prof = profiles(settings).get(h)
    if prof is None or prof.z == 0:
        return 0.0
    seen = sum(prof.weight(m) * s.quality_factor for m, s in observed.items() if prof.weight(m) > 0)
    return round(min(seen / prof.z, 1.0), 4)


def missing_corroboration(h: Hypothesis, observed: dict[str, MetricSummary], settings: Settings) -> list[str]:
    """Signals that must be observed before `h` may be claimed (scoring.yaml)."""
    required = settings.scoring.get("corroboration_required", {}).get(h.value, [])
    if not required:
        return []
    return [] if any(m in observed and observed[m].quality_factor > 0 for m in required) else list(required)


@dataclass
class AgentVote:
    agent: str
    top: Hypothesis | None
    eligible_for_final: bool
    resolved: bool = False


@dataclass
class Decision:
    status: DiagnosisStatus
    confidence: float
    category: ConfidenceCategory
    p_top: float
    p_second: float
    margin: float
    coverage: float
    agreement: float
    reasons: list[str] = field(default_factory=list)


def agreement_factor(final: Hypothesis, votes: list[AgentVote], settings: Settings) -> float:
    """Agents vote only on what they can judge. A dissent answered by refuting evidence in the
    challenge round counts with partial credit (scoring.yaml: agreement.resolved_dissent_credit)."""
    credit = float(settings.scoring["agreement"]["resolved_dissent_credit"])
    counted: list[float] = []
    for v in votes:
        if v.top is None:
            continue
        agrees = v.top is final
        active_dissent = v.top is not final and v.top is not Hypothesis.NORMAL
        if not (v.eligible_for_final or active_dissent):
            continue  # e.g. infrastructure says 'normal' about a DNS fault: not a dissent
        if agrees:
            counted.append(1.0)
        elif v.resolved:
            counted.append(credit)
        else:
            counted.append(0.0)
    return round(sum(counted) / len(counted), 4) if counted else 1.0


def decide(
    probabilities: dict[Hypothesis, float],
    coverage: float,
    agreement: float,
    settings: Settings,
    *,
    missing_required: list[str] | None = None,
    fatal: bool = False,
) -> tuple[Hypothesis, Decision]:
    ranked = sorted(probabilities.items(), key=lambda kv: kv[1], reverse=True)
    top, p_top = ranked[0]
    p_second = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = p_top - p_second
    confidence = round(p_top * coverage * agreement, 4)
    rules = settings.scoring["decision"]
    reasons: list[str] = []
    if fatal:
        return top, Decision(DiagnosisStatus.FAILED, 0.0, ConfidenceCategory.LOW, p_top, p_second, margin,
                             coverage, agreement, ["no usable telemetry survived ingestion"])
    if confidence < float(rules["decide_threshold"]):
        reasons.append(f"confidence {confidence:.2f} < decide_threshold {float(rules['decide_threshold']):.2f}")
    if margin < float(rules["min_margin"]):
        reasons.append(f"margin P(top)-P(second) = {margin:.2f} < min_margin {float(rules['min_margin']):.2f}")
    if missing_required:
        reasons.append(f"'{top.value}' cannot be confirmed yet: {'; '.join(missing_required)}")
        # a claim that cannot be verified must not keep a high confidence (scoring.yaml)
        confidence = round(confidence * float(rules.get("unverified_confidence_factor", 0.3)), 4)
    if reasons:
        status = DiagnosisStatus.INSUFFICIENT_EVIDENCE
    else:
        status = DiagnosisStatus.NORMAL if top is Hypothesis.NORMAL else DiagnosisStatus.DIAGNOSED
    return top, Decision(status, confidence, category(settings, confidence), round(p_top, 4), round(p_second, 4),
                         round(margin, 4), coverage, agreement, reasons)
