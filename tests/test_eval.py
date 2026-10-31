"""Evaluation harness: grading, calibration maths and the multi-seed runner."""

from __future__ import annotations

import pytest

from netcoop.benchmark import EvalResult, jobs_for, run_eval, write_outputs


def _row(confidence: float, correct: int, eligible: int = 1, **kw) -> dict:  # type: ignore[no-untyped-def]
    base = {"group": "g", "status": "diagnosed", "confidence": confidence, "top_correct": correct,
            "calibration_eligible": eligible, "outcome": "correct" if correct else "wrong",
            "high_confidence_wrong": 0, "expected_label": "x", "predicted_label": "x"}
    return {**base, **kw}


def test_ece_is_zero_for_perfect_calibration_and_large_for_overconfidence() -> None:
    perfect = EvalResult([_row(0.95, 1)] * 19 + [_row(0.95, 0)], 0.0)
    assert perfect.calibration()["ece"] == pytest.approx(0.0, abs=1e-9)
    overconfident = EvalResult([_row(0.95, 0)] * 10, 0.0)
    assert overconfident.calibration()["ece"] == pytest.approx(0.95)


def test_ambiguous_truth_is_excluded_from_calibration() -> None:
    res = EvalResult([_row(0.3, 1, eligible=0), _row(0.9, 1)], 0.0)
    cal = res.calibration()
    assert cal["runs"] == 1 and cal["excluded_ambiguous"] == 1


def test_job_list_covers_scenarios_chaos_and_organizer() -> None:
    kinds = {j.kind for j in jobs_for(2)}
    assert kinds == {"scenario", "chaos", "organizer", "organizer_batch"}


def test_small_end_to_end_eval_meets_targets(tmp_path) -> None:  # type: ignore[no-untyped-def]
    result = run_eval(seeds=1, workers=1, chaos_seeds=1)
    h = result.headline()
    assert h["crashes"] == 0 and h["high_confidence_wrong"] == 0 and h["normal_false_alarms"] == 0
    assert h["correct_or_abstain"] >= 0.95
    names = {p.name for p in write_outputs(result, tmp_path)}
    assert {"summary.json", "summary.md", "runs.csv", "confusion_matrix.png", "reliability.png"} <= names
