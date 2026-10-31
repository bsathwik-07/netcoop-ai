"""The LLM is optional garnish: off by default, never changes the decision, falls back silently."""

from __future__ import annotations

from netcoop.llm import narrator
from netcoop.pipeline import run_scenario


def test_llm_is_off_by_default(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("NETCOOP_LLM", raising=False)
    assert narrator.optional_hooks() == {}
    report = run_scenario("hardware_failure", seed=1).report
    assert report is not None and "LLM" not in report.narrative  # deterministic template


def test_failed_llm_falls_back_to_the_template_and_keeps_the_decision(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("NETCOOP_LLM", "1")
    monkeypatch.setattr(narrator, "_ask", lambda *a, **k: None)  # unreachable / refused / no key
    baseline = run_scenario("link_congestion", seed=1).report
    assert baseline is not None
    assert baseline.root_cause_code.value == "link_congestion" and "LLM" not in baseline.narrative


def test_inconsistent_llm_narrative_is_rejected(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("NETCOOP_LLM", "1")
    monkeypatch.setattr(narrator, "_ask", lambda *a, **k: "Everything is fine, it was DNS all along.")
    report = run_scenario("link_congestion", seed=1).report
    assert report is not None and "DNS all along" not in report.narrative


def test_consistent_llm_narrative_is_used_and_labelled(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("NETCOOP_LLM", "1")
    monkeypatch.setattr(narrator, "_ask", lambda *a, **k: (
        "HOSTEL_CORE_SW_01 is congested; confidence is high."))
    report = run_scenario("link_congestion", seed=1).report
    assert report is not None and report.narrative.startswith("HOSTEL_CORE_SW_01 is congested")
    assert "written by an LLM" in report.narrative and report.root_cause_code.value == "link_congestion"


def test_field_mapper_only_accepts_known_names(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(narrator, "_ask", lambda *a, **k: "latency_ms")
    assert narrator.llm_field_mapper("roundTripMillis", ["latency_ms", "jitter_ms"]) == "latency_ms"
    monkeypatch.setattr(narrator, "_ask", lambda *a, **k: "made_up_metric")
    assert narrator.llm_field_mapper("x", ["latency_ms"]) is None
