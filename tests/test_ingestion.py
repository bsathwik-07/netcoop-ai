"""Schema adapter: aliases, units, heuristics, schema versions, validation, quality tags."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from netcoop.config import data_dir, get_settings
from netcoop.ingest.adapter import TelemetryAdapter, norm_key
from netcoop.protocol.payloads import MappingMethod
from netcoop.protocol.vocabulary import DeviceStatus, ErrorCode, Quality
from netcoop.sim.malformed import EXPECTED, FIXTURES
from netcoop.sim.organizer import ORGANIZER_FAULT_TYPES, generate_batch
from netcoop.topology import default_topology


@pytest.fixture(scope="module")
def adapter() -> TelemetryAdapter:
    return TelemetryAdapter(get_settings(), default_topology())


@pytest.mark.parametrize(("raw", "expected"), [
    ("deviceId", "device_id"), ("pktLoss", "pkt_loss"), ("link-util", "link_util"),
    ("CPU Load", "cpu_load"), ("metrics.latency_ms", "metrics_latency_ms"), ("dnsSuccessRate", "dns_success_rate"),
])
def test_key_normalization(raw: str, expected: str) -> None:
    assert norm_key(raw) == expected


@pytest.mark.parametrize("fault", ORGANIZER_FAULT_TYPES)
def test_organizer_format_is_ingested_with_zero_conversion_errors(adapter: TelemetryAdapter, fault: str) -> None:
    records = generate_batch(fault, 20, seed=1)
    res = adapter.ingest(json.dumps(records))
    assert res.report.records_accepted == 20 and not res.report.rejections
    rec = res.records[0]
    assert rec.source_format == "organizer_v1"
    assert rec.status_original in ("Healthy", "Warning", "Critical")
    assert rec.status in (DeviceStatus.UP, DeviceStatus.DEGRADED, DeviceStatus.DOWN)
    assert all(mv.quality is Quality.OK for r in res.records for mv in r.metrics.values())
    if fault == "hardware_failure":
        assert "jitter_ms" not in rec.metrics  # absent means missing, never zero


def test_aliases_units_and_legacy_schema(adapter: TelemetryAdapter) -> None:
    legacy = [{"schema_version": "1.1", "deviceId": "SW_CAMPUS_3", "ts": 1791961200, "state": "Warning",
               "rtt": 0.25, "rtt_unit": "s", "link-util": 0.94, "cpuLoad": 0.5, "out-discards": 160,
               "in-errors": 0, "rssi": 72, "goodput_kbps": 250000, "mystery_field": 7}]
    res = adapter.ingest(json.dumps(legacy))
    rec = res.records[0]
    m = rec.metrics
    assert rec.device_id == "SW_CAMPUS_3" and rec.status is DeviceStatus.DEGRADED
    assert m["latency_ms"].value == pytest.approx(250.0) and m["latency_ms"].quality is Quality.CONVERTED
    assert m["link_utilization_pct"].value == pytest.approx(94.0)  # fraction heuristic
    assert m["cpu_utilization_pct"].value == pytest.approx(50.0)
    assert m["queue_drops"].value == 160 and m["interface_errors"].value == 0
    assert m["wifi_rssi_dbm"].value == -72  # positive RSSI magnitude
    assert m["throughput_mbps"].value == pytest.approx(250.0)
    assert rec.unmapped == {"mystery_field": 7}
    assert "legacy_v1_1" in res.report.schema_versions
    methods = {c.method for c in res.report.conversions}
    assert {MappingMethod.ALIAS, MappingMethod.UNIT_CONVERSION, MappingMethod.HEURISTIC, MappingMethod.MIGRATION} <= methods
    assert any(r.code is ErrorCode.E_FIELD_UNMAPPED for r in res.report.rejections)


def test_small_packet_loss_is_not_mistaken_for_a_fraction(adapter: TelemetryAdapter) -> None:
    res = adapter.ingest(json.dumps([{"device_id": "SW_CAMPUS_1", "timestamp": "2026-10-14T07:00:00Z",
                                      "metrics": {"packet_loss_pct": 0.45, "latency_ms": 12}}]))
    assert res.records[0].metrics["packet_loss_pct"].value == pytest.approx(0.45)


def test_fuzzy_match_of_a_typo(adapter: TelemetryAdapter) -> None:
    res = adapter.ingest({"device_id": "SW_CAMPUS_1", "timestamp": "2026-10-14T07:00:00Z", "latncy_ms": 12})
    mv = res.records[0].metrics["latency_ms"]
    assert mv.quality is Quality.CONVERTED and mv.provenance[0].method is MappingMethod.FUZZY


def test_unknown_unit_is_rejected_not_guessed(adapter: TelemetryAdapter) -> None:
    res = adapter.ingest({"device_id": "SW_CAMPUS_1", "timestamp": "2026-10-14T07:00:00Z",
                          "metrics": {"latency_ms": {"value": 3, "unit": "fortnights"}}})
    assert res.records[0].metrics["latency_ms"].quality is Quality.REJECTED
    assert any(r.code is ErrorCode.E_UNIT_UNKNOWN for r in res.report.rejections)


def test_staleness_is_relative_to_the_batch(adapter: TelemetryAdapter) -> None:
    old = "2020-01-01T00:00:00Z"  # years old, but the whole batch is: not stale
    res = adapter.ingest([{"device_id": "SW_CAMPUS_1", "timestamp": old, "metrics": {"latency_ms": 9}}])
    assert res.records[0].record_quality is Quality.OK
    mixed = [{"device_id": "SW_CAMPUS_1", "timestamp": "2026-10-14T07:00:00Z", "metrics": {"latency_ms": 9}},
             {"device_id": "SW_CAMPUS_2", "timestamp": "2026-10-14T03:00:00Z", "metrics": {"latency_ms": 9}}]
    res = adapter.ingest(mixed)
    stale = [r for r in res.records if r.device_id == "SW_CAMPUS_2"][0]
    assert stale.record_quality is Quality.STALE
    assert stale.metrics["latency_ms"].quality is Quality.STALE


def test_jsonl_and_csv(adapter: TelemetryAdapter) -> None:
    lines = "\n".join(json.dumps(r) for r in generate_batch("normal", 3, seed=2))
    assert adapter.ingest(lines).report.records_accepted == 3
    csv_text = "device,time,cpu,latency\nSW_CAMPUS_1,2026-10-14T07:00:00Z,20,9\nSW_CAMPUS_2,2026-10-14T07:00:00Z,25,11\n"
    res = adapter.ingest(csv_text)
    assert res.report.records_accepted == 2 and res.records[0].metrics["cpu_utilization_pct"].value == 20


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_every_malformed_fixture_is_reported_with_the_expected_code(adapter: TelemetryAdapter, name: str) -> None:
    path = data_dir() / "malformed" / name
    raw = path.read_bytes() if path.exists() else FIXTURES[name]
    res = adapter.ingest(raw if isinstance(raw, bytes) else raw.encode(), source=name)
    codes = {r.code for r in res.report.rejections}
    for code in EXPECTED[name]:
        assert code in codes, f"{name}: expected {code}, got {codes}"
    assert all(r.reason for r in res.report.rejections)


def test_adapter_never_raises_on_hostile_input(adapter: TelemetryAdapter) -> None:
    for raw in [b"", b"\x00" * 50, "[]", "{}", "[1,2,3]", "null", '{"records": "nope"}', "[[]]",
                '{"device_id": {"nested": 1}}', '{"device_id": "X", "metrics": [1, 2]}', "a,b\n1\n"]:
        res = adapter.ingest(raw)
        assert res.report is not None


def test_data_files_exist() -> None:
    assert (Path(data_dir()) / "organizer_samples" / "single_hardware_failure.json").exists()
