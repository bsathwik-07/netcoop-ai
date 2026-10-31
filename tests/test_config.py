from __future__ import annotations

import pytest

from netcoop.config import get_settings
from netcoop.protocol.vocabulary import Hypothesis


def test_all_config_files_load() -> None:
    s = get_settings()
    assert "latency_ms" in s.thresholds
    assert s.scoring["decision"]["decide_threshold"] == pytest.approx(0.55)


@pytest.mark.parametrize(
    ("metric", "value", "expected"),
    [
        ("latency_ms", 10, 0.0),
        ("latency_ms", 100, 0.5),
        ("latency_ms", 900, 1.0),
        ("wifi_rssi_dbm", -60, 0.0),
        ("wifi_rssi_dbm", -71, 0.5),
        ("wifi_rssi_dbm", -82, 1.0),
        ("dns_success_pct", 100, 0.0),
        ("dns_success_pct", 50, 1.0),
    ],
)
def test_severity_is_piecewise_linear_in_both_directions(metric: str, value: float, expected: float) -> None:
    assert get_settings().thresholds[metric].severity(value) == pytest.approx(expected)


def test_every_threshold_cites_a_source() -> None:
    assert all(t.source for t in get_settings().thresholds.values())


def test_weights_cover_every_fault_hypothesis() -> None:
    weights = get_settings().weights["hypotheses"]
    faults = {h.value for h in Hypothesis} - {"normal"}
    assert set(weights) == faults
