"""Grade a report against a ground-truth file: correct, abstain or wrong."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from netcoop.protocol.payloads import DiagnosticReport
from netcoop.protocol.vocabulary import ConfidenceCategory, DiagnosisStatus


@dataclass(frozen=True)
class Grade:
    outcome: str  # "correct" | "abstain" | "wrong"
    reason: str
    high_confidence_wrong: bool = False

    @property
    def ok(self) -> bool:
        """Correct, or an abstention the ground truth allows."""
        return self.outcome == "correct" or (self.outcome == "abstain" and "allowed" in self.reason)


def grade(report: DiagnosticReport | None, truth: dict[str, Any]) -> Grade:
    if report is None:
        return Grade("wrong", "no report produced (system failure)")
    expected = truth.get("expected_status", "diagnosed")
    acceptable = set(truth.get("acceptable_root_causes", []))
    abstain_ok = bool(truth.get("abstain_ok", False))
    status = report.status
    code = report.root_cause_code.value if report.root_cause_code else None
    high = report.confidence_category is ConfidenceCategory.HIGH

    if expected == "normal":
        if status is DiagnosisStatus.NORMAL:
            return Grade("correct", "normal recognised")
        if status is DiagnosisStatus.INSUFFICIENT_EVIDENCE:
            return Grade("abstain", "abstained on healthy data (not allowed)")
        return Grade("wrong", f"false alarm: {status.value} {code}", status is DiagnosisStatus.DIAGNOSED and high)

    if expected == "insufficient_evidence":
        if status is DiagnosisStatus.INSUFFICIENT_EVIDENCE:
            return Grade("correct", "honest abstention")
        if status is DiagnosisStatus.DIAGNOSED and code in acceptable:
            return Grade("correct", f"acceptable diagnosis {code} with {report.confidence_category.value} confidence")
        return Grade("wrong", f"claimed {status.value} {code} without sufficient evidence",
                     status is DiagnosisStatus.DIAGNOSED and high)

    # expected a diagnosis
    if status is DiagnosisStatus.DIAGNOSED:
        if code in acceptable:
            return Grade("correct", f"{code}")
        return Grade("wrong", f"diagnosed {code}, expected one of {sorted(acceptable)}", high)
    if status is DiagnosisStatus.INSUFFICIENT_EVIDENCE:
        return Grade("abstain", "abstained (allowed)" if abstain_ok else "abstained (counted as abstention)")
    return Grade("wrong", f"status {status.value}")
