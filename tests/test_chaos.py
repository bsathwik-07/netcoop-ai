"""Chaos harness as tests: every scenario x every single mutation, key pairs, and malformed files.

Targets (master prompt section 6): correct or abstaining on 100 % of single mutations, never a
wrong high-confidence answer, never a crash, and every mutation explained in data_quality.
"""

from __future__ import annotations

import pytest

from netcoop.config import data_dir
from netcoop.evaluation import grade
from netcoop.pipeline import diagnose, run_scenario
from netcoop.protocol.vocabulary import DiagnosisStatus
from netcoop.sim.chaos import MUTATIONS
from netcoop.sim.generator import scenario_names
from netcoop.sim.malformed import EXPECTED

PAIRS = [
    ["rename_fields", "swap_units"],
    ["downgrade_schema", "drop_metrics"],
    ["inject_nan", "stale_timestamps"],
    ["shuffle_timestamps", "stale_timestamps"],
    ["duplicate_records", "add_noise"],
    ["flatten_or_nest", "swap_units"],
    ["rename_fields", "swap_units", "downgrade_schema"],
]


def _check(scenario: str, combo: list[str], seed: int = 0) -> None:
    result = run_scenario(scenario, seed=seed, chaos=combo)
    assert result.report is not None, f"crash: {result.error}"
    r = result.report
    g = grade(r, result.truth or {})
    assert g.outcome in ("correct", "abstain"), f"{scenario}+{combo}: {g.reason}"
    assert not g.high_confidence_wrong
    if scenario == "normal":
        assert r.status is not DiagnosisStatus.DIAGNOSED, "false alarm on healthy data"


@pytest.mark.parametrize("mutation", list(MUTATIONS))
@pytest.mark.parametrize("scenario", scenario_names())
def test_single_mutation(scenario: str, mutation: str) -> None:
    _check(scenario, [mutation])


@pytest.mark.parametrize("combo", PAIRS, ids=lambda c: "+".join(c))
@pytest.mark.parametrize("scenario", ["hardware_failure", "link_congestion", "dhcp_failure", "normal", "latency_spike"])
def test_mutation_combinations(scenario: str, combo: list[str]) -> None:
    _check(scenario, combo, seed=1)


@pytest.mark.parametrize(("mutation", "check"), [
    ("rename_fields", lambda r: r.data_quality.conversions > 0),
    ("swap_units", lambda r: r.data_quality.conversions > 0 and r.confidence_breakdown.data_coverage < 1.0),
    ("downgrade_schema", lambda r: r.data_quality.conversions > 0),
    ("inject_nan", lambda r: r.data_quality.coverage < 1.0 and any("E_SCHEMA_INVALID" in w for w in r.data_quality.warnings)),
    ("duplicate_records", lambda r: r.data_quality.rejected_records > 0),
    ("stale_timestamps", lambda r: any("E_STALE_DATA" in w for w in r.data_quality.warnings)),
])
def test_mutations_are_explained_in_data_quality(mutation: str, check) -> None:  # type: ignore[no-untyped-def]
    report = run_scenario("hardware_failure", seed=0, chaos=[mutation]).report
    assert report is not None and check(report), report and report.data_quality


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_malformed_files_through_the_full_pipeline(name: str) -> None:
    raw = (data_dir() / "malformed" / name).read_bytes()
    result = diagnose(raw, source=name)
    assert result.report is not None, f"crash on {name}: {result.error}"
    warnings = " ".join(result.report.data_quality.warnings + result.report.limitations)
    for code in EXPECTED[name]:
        assert code.value in warnings, f"{name}: {code.value} not reported"
    if result.report.status is DiagnosisStatus.FAILED:
        assert result.report.confidence == 0 and result.report.next_data_to_collect
