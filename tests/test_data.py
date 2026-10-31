"""Phase 2: topology, organizer generator copy, scenario generator, fixtures."""

from __future__ import annotations

import pytest

from netcoop.config import get_settings
from netcoop.sim.chaos import MUTATIONS, mutate
from netcoop.sim.generator import HEALTHY, SCENARIOS, generate
from netcoop.sim.malformed import EXPECTED, FIXTURES
from netcoop.sim.organizer import ORGANIZER_FAULT_TYPES, generate_batch
from netcoop.topology import default_topology


def test_topology_tree_and_lca() -> None:
    topo = default_topology()
    assert all(f"SW_CAMPUS_{i}" in topo for i in range(1, 11))
    assert topo.lowest_common_ancestor(["SW_CAMPUS_1", "SW_CAMPUS_3"]) == "HOSTEL_CORE_SW_01"
    assert topo.lowest_common_ancestor(["SW_CAMPUS_1", "SW_CAMPUS_5"]) == "CAMPUS_CORE_RTR_01"
    assert topo.lowest_common_ancestor(["HOSTEL_A_AP_02"]) == "HOSTEL_A_AP_02"
    assert "HOSTEL_A_AP_01" in topo.descendants("HOSTEL_CORE_SW_01")


@pytest.mark.parametrize("fault", ORGANIZER_FAULT_TYPES)
def test_organizer_copy_is_seeded_and_matches_notebook_ranges(fault: str) -> None:
    a, b = generate_batch(fault, 50, seed=3), generate_batch(fault, 50, seed=3)
    assert [r["metrics"] for r in a] == [r["metrics"] for r in b]
    for rec in a:
        m = rec["metrics"]
        assert rec["device_id"].startswith("SW_CAMPUS_")
        assert rec["timestamp"].endswith("Z")
        if fault == "hardware_failure":
            assert rec["status"] == "Critical" and "jitter_ms" not in m and 100 <= m["interface_errors"] <= 500
        elif fault == "latency_spike":
            assert rec["status"] == "Warning" and 150 <= m["latency_ms"] <= 400
        else:
            assert rec["status"] == "Healthy" and m["packet_loss_pct"] == 0.0
        assert all(round(v, 2) == v for v in m.values() if isinstance(v, float))


def test_healthy_ranges_never_cross_a_warn_threshold() -> None:
    thresholds = get_settings().thresholds
    for metric, (lo, hi) in HEALTHY.items():
        t = thresholds[metric]
        assert t.severity(lo) == 0.0 and t.severity(hi) == 0.0, metric


@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_generator_is_deterministic_and_has_truth(scenario: str) -> None:
    r1, truth = generate(scenario, seed=5, duration=10)
    r2, _ = generate(scenario, seed=5, duration=10)
    r3, _ = generate(scenario, seed=6, duration=10)
    assert r1 == r2 and r1
    assert r1 != r3
    assert truth["scenario"] == scenario
    assert truth["expected_status"] in ("diagnosed", "normal", "insufficient_evidence")
    if truth["expected_status"] == "diagnosed":
        assert truth["root_cause"] in truth["acceptable_root_causes"]


def test_organizer_format_output() -> None:
    records, _ = generate("hardware_failure", seed=1, duration=4, fmt="organizer")
    faulty = [r for r in records if r["device_id"] == "SW_CAMPUS_4" and r.get("status") == "Critical"]
    assert faulty and set(faulty[0]["metrics"]) == {
        "cpu_utilization_pct", "link_utilization_pct", "packet_loss_pct", "latency_ms", "queue_drops", "interface_errors",
    }


@pytest.mark.parametrize("name", list(MUTATIONS))
def test_every_mutation_returns_new_records(name: str) -> None:
    records, _ = generate("link_congestion", seed=1, duration=3)
    before = repr(records)
    mutated = mutate(records, [name], seed=1)
    assert repr(records) == before  # input untouched
    assert mutated


def test_unknown_mutation_is_a_clear_error() -> None:
    with pytest.raises(ValueError, match="unknown mutation"):
        mutate([], ["melt_cables"])


def test_every_malformed_fixture_has_an_expectation() -> None:
    assert set(FIXTURES) == set(EXPECTED)
