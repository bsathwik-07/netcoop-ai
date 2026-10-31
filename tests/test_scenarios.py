"""End-to-end: every scenario and every organizer sample is correct or honestly abstains."""

from __future__ import annotations

import json

import pytest

from netcoop.config import data_dir
from netcoop.evaluation import grade
from netcoop.pipeline import diagnose, run_scenario
from netcoop.protocol.vocabulary import DiagnosisStatus, Hypothesis
from netcoop.sim.generator import scenario_names
from netcoop.sim.organizer import generate_batch


@pytest.mark.parametrize("scenario", scenario_names())
@pytest.mark.parametrize("seed", [42, 7])
def test_scenario_matches_ground_truth(scenario: str, seed: int) -> None:
    result = run_scenario(scenario, seed=seed)
    assert result.report is not None, result.error
    g = grade(result.report, result.truth or {})
    assert g.ok, f"{scenario}/{seed}: {g.outcome} - {g.reason}"
    assert not g.high_confidence_wrong


def _organizer_samples() -> list[str]:
    folder = data_dir() / "organizer_samples"
    return sorted(p.stem for p in folder.glob("*.json") if not p.name.endswith(".truth.json"))


@pytest.mark.parametrize("name", _organizer_samples())
def test_organizer_samples(name: str) -> None:
    folder = data_dir() / "organizer_samples"
    truth = json.loads((folder / f"{name}.truth.json").read_text())
    result = diagnose((folder / f"{name}.json").read_bytes(), source=name)
    assert result.report is not None, result.error
    assert grade(result.report, truth).ok


@pytest.mark.parametrize("seed", range(10))
def test_organizer_generator_many_seeds(seed: int) -> None:
    """Fresh organizer output (single records) for every fault type and many seeds."""
    expectations = {
        "normal": {DiagnosisStatus.NORMAL},
        "hardware_failure": {DiagnosisStatus.DIAGNOSED},
        "latency_spike": {DiagnosisStatus.INSUFFICIENT_EVIDENCE, DiagnosisStatus.DIAGNOSED},
    }
    for fault, allowed in expectations.items():
        record = generate_batch(fault, 1, seed=seed)[0]
        report = diagnose(record).report
        assert report is not None and report.status in allowed, (fault, seed, report and report.status)
        if fault == "hardware_failure":
            assert report.root_cause_code in (Hypothesis.HARDWARE_FAILURE, Hypothesis.INTERFACE_FAULT)
            assert report.conflict_resolution.conflict_detected  # congestion vs hardware: visible challenge
            assert {a.cause for a in report.alternative_causes} & {Hypothesis.LINK_CONGESTION, Hypothesis.INTERFACE_FAULT}
        if fault == "latency_spike":
            assert report.status is DiagnosisStatus.INSUFFICIENT_EVIDENCE  # no neighbour data: must abstain
            assert report.next_data_to_collect and len(report.alternative_causes) >= 2


def test_report_keeps_every_organizer_field_name() -> None:
    report = run_scenario("link_congestion").report
    assert report is not None
    data = json.loads(report.model_dump_json())
    for key in ["incident_id", "status", "suspected_root_cause", "confidence", "affected_devices",
                "supporting_evidence", "alternative_causes", "recommended_actions", "evidence_sources", "limitations"]:
        assert key in data
    assert {"metric", "value", "interpretation"} <= set(data["supporting_evidence"][0])
    assert report.root_device == "HOSTEL_CORE_SW_01"
    assert {"SW_CAMPUS_1", "HOSTEL_A_AP_02"} <= set(report.symptomatic_devices)


def test_multi_fault_reports_a_secondary_incident() -> None:
    report = run_scenario("multi_fault").report
    assert report is not None and report.status is DiagnosisStatus.DIAGNOSED
    causes = {report.root_cause_code} | {s.root_cause_code for s in report.secondary_incidents}
    assert causes >= {Hypothesis.LINK_CONGESTION, Hypothesis.DNS_FAILURE}
