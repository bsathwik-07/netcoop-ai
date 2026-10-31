"""Seeded scenario generator: multi-device time series with ground truth.

Output is canonical v2 (`{"value": x, "unit": u}` per metric) or organizer v1 (flat numbers,
Healthy/Warning/Critical). Faults start at `onset` so earlier samples form a baseline.
Healthy ranges sit strictly below every `warn` threshold, so `normal` never raises an alarm.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from netcoop.protocol.vocabulary import METRICS, DeviceStatus, DiagnosisStatus, Hypothesis
from netcoop.sim.chaos import mutate
from netcoop.topology import Topology, default_topology

BASE_TIME = datetime(2026, 10, 14, 7, 0, tzinfo=UTC)
INTERVAL_S = 60

Range = tuple[float, float]

SWITCH_METRICS = [
    "latency_ms", "jitter_ms", "packet_loss_pct", "link_utilization_pct", "throughput_mbps",
    "cpu_utilization_pct", "memory_utilization_pct", "queue_drops", "interface_errors",
    "interface_flaps", "uptime_s",
]
AP_METRICS = [*SWITCH_METRICS, "wifi_rssi_dbm", "wifi_snr_db", "retransmission_pct"]
DNS_METRICS = ["latency_ms", "packet_loss_pct", "cpu_utilization_pct", "memory_utilization_pct",
               "dns_success_pct", "dns_latency_ms"]
DHCP_METRICS = ["latency_ms", "packet_loss_pct", "cpu_utilization_pct", "memory_utilization_pct",
                "dhcp_success_pct", "dhcp_ack_ms", "dhcp_pool_utilization_pct", "apipa_clients"]
ORGANIZER_LATENCY_SPIKE_METRICS = ["cpu_utilization_pct", "link_utilization_pct", "packet_loss_pct",
                                   "latency_ms", "jitter_ms", "queue_drops"]
ORGANIZER_HARDWARE_METRICS = ["cpu_utilization_pct", "link_utilization_pct", "packet_loss_pct",
                              "latency_ms", "queue_drops", "interface_errors"]

HEALTHY: dict[str, Range] = {
    "latency_ms": (2, 18),
    "jitter_ms": (0.5, 4),
    "packet_loss_pct": (0.0, 0.05),
    "link_utilization_pct": (15, 55),
    "cpu_utilization_pct": (10, 40),
    "memory_utilization_pct": (30, 60),
    "queue_drops": (0, 20),
    "interface_errors": (0, 2),
    "interface_flaps": (0, 0),
    "wifi_rssi_dbm": (-62, -45),
    "wifi_snr_db": (30, 45),
    "retransmission_pct": (1, 6),
    "dns_success_pct": (99.6, 100),
    "dns_latency_ms": (5, 40),
    "dhcp_success_pct": (99.2, 100),
    "dhcp_ack_ms": (50, 400),
    "dhcp_pool_utilization_pct": (30, 70),
    "apipa_clients": (0, 0),
}
COUNT_METRICS = {"queue_drops", "interface_errors", "interface_flaps", "apipa_clients"}

ORGANIZER_STATUS = {DeviceStatus.UP: "Healthy", DeviceStatus.DEGRADED: "Warning", DeviceStatus.DOWN: "Critical"}


@dataclass
class Overlay:
    """Fault ranges applied to one device from `onset` on."""

    ranges: dict[str, Range]
    status: DeviceStatus = DeviceStatus.DEGRADED
    metric_set: list[str] | None = None  # replaces the device's metric set (organizer patterns)
    ramp: dict[str, Range] = field(default_factory=dict)  # metric -> (start, end) rising linearly


@dataclass
class ScenarioSpec:
    name: str
    description: str
    expected_status: DiagnosisStatus
    root_cause: Hypothesis | None
    acceptable: list[Hypothesis]
    abstain_ok: bool
    root_device: str | None
    overlays: dict[str, Overlay]
    symptomatic: list[str] = field(default_factory=list)
    devices: list[str] | None = None  # None = every device in the topology
    secondary: list[Hypothesis] = field(default_factory=list)
    mutations: list[str] = field(default_factory=list)
    drop: dict[str, list[str]] = field(default_factory=dict)  # device -> metrics removed entirely


def _congestion_overlays() -> tuple[dict[str, Overlay], list[str]]:
    root = Overlay({
        "link_utilization_pct": (93, 99.5), "queue_drops": (1500, 4000), "latency_ms": (90, 200),
        "jitter_ms": (10, 30), "packet_loss_pct": (0.3, 1.5),
    })
    downstream = ["SW_CAMPUS_1", "SW_CAMPUS_2", "SW_CAMPUS_3", "SW_CAMPUS_4",
                  "HOSTEL_A_AP_01", "HOSTEL_A_AP_02", "HOSTEL_B_AP_01", "HOSTEL_B_AP_02"]
    symptom = Overlay({"latency_ms": (70, 170), "jitter_ms": (8, 25), "packet_loss_pct": (0.2, 0.9)},
                      status=DeviceStatus.UP)
    overlays = {"HOSTEL_CORE_SW_01": root, **{d: symptom for d in downstream}}
    return overlays, downstream


def _dns_overlay() -> Overlay:
    return Overlay({"dns_success_pct": (40, 75), "dns_latency_ms": (800, 3000)})


def _specs() -> dict[str, ScenarioSpec]:
    congestion, congestion_symptoms = _congestion_overlays()
    specs = [
        ScenarioSpec(
            "normal", "Every device within expected ranges.",
            DiagnosisStatus.NORMAL, Hypothesis.NORMAL, [Hypothesis.NORMAL], False, None, {},
        ),
        ScenarioSpec(
            "link_congestion",
            "Hostel aggregation uplink saturated: high utilization, rising queue drops, latency up "
            "on the switch and on every device downstream of it.",
            DiagnosisStatus.DIAGNOSED, Hypothesis.LINK_CONGESTION, [Hypothesis.LINK_CONGESTION], False,
            "HOSTEL_CORE_SW_01", congestion, congestion_symptoms,
        ),
        ScenarioSpec(
            "weak_wifi",
            "Clients of one hostel AP see poor RSSI/SNR, many retransmissions and loss; wired path healthy.",
            DiagnosisStatus.DIAGNOSED, Hypothesis.WEAK_WIFI, [Hypothesis.WEAK_WIFI], False, "HOSTEL_A_AP_02",
            {"HOSTEL_A_AP_02": Overlay({
                "wifi_rssi_dbm": (-86, -78), "wifi_snr_db": (6, 13), "retransmission_pct": (26, 42),
                "packet_loss_pct": (1.2, 4), "jitter_ms": (15, 40), "latency_ms": (20, 60),
            })},
        ),
        ScenarioSpec(
            "interface_fault",
            "Rising CRC/interface errors and link flaps on one academic access switch; utilization normal.",
            DiagnosisStatus.DIAGNOSED, Hypothesis.INTERFACE_FAULT, [Hypothesis.INTERFACE_FAULT], False,
            "SW_CAMPUS_6",
            {"SW_CAMPUS_6": Overlay(
                {"interface_flaps": (1, 5), "packet_loss_pct": (1.5, 6), "latency_ms": (20, 60)},
                ramp={"interface_errors": (60, 450)},
            )},
        ),
        ScenarioSpec(
            "dns_failure", "Resolver failing: DNS probes time out while ICMP latency stays healthy everywhere.",
            DiagnosisStatus.DIAGNOSED, Hypothesis.DNS_FAILURE, [Hypothesis.DNS_FAILURE], False, "DNS_SRV_01",
            {"DNS_SRV_01": _dns_overlay()},
        ),
        ScenarioSpec(
            "dhcp_failure", "DHCP pool exhausted: lease failures, slow DORA and clients falling back to APIPA.",
            DiagnosisStatus.DIAGNOSED, Hypothesis.DHCP_FAILURE, [Hypothesis.DHCP_FAILURE], False, "DHCP_SRV_01",
            {"DHCP_SRV_01": Overlay({
                "dhcp_success_pct": (30, 70), "dhcp_ack_ms": (3000, 8000),
                "dhcp_pool_utilization_pct": (99, 100), "apipa_clients": (8, 30),
            })},
        ),
        ScenarioSpec(
            "device_overload", "CPU and memory pegged on one switch; control-plane drops and added latency.",
            DiagnosisStatus.DIAGNOSED, Hypothesis.DEVICE_OVERLOAD, [Hypothesis.DEVICE_OVERLOAD], False,
            "SW_CAMPUS_7",
            {"SW_CAMPUS_7": Overlay({
                "cpu_utilization_pct": (92, 99), "memory_utilization_pct": (88, 97), "latency_ms": (40, 120),
                "jitter_ms": (10, 25), "queue_drops": (200, 800),
            })},
        ),
        ScenarioSpec(
            "hardware_failure",
            "Exact organizer hardware_failure pattern on one switch (CPU 99.9, link 100 %, 15-30 % loss, "
            "huge queue drops, 100-500 interface errors, no jitter metric).",
            DiagnosisStatus.DIAGNOSED, Hypothesis.HARDWARE_FAILURE,
            [Hypothesis.HARDWARE_FAILURE, Hypothesis.INTERFACE_FAULT], False, "SW_CAMPUS_4",
            {"SW_CAMPUS_4": Overlay({
                "cpu_utilization_pct": (99.9, 99.9), "link_utilization_pct": (100, 100),
                "packet_loss_pct": (15, 30), "latency_ms": (500, 1000), "queue_drops": (5000, 15000),
                "interface_errors": (100, 500),
            }, status=DeviceStatus.DOWN, metric_set=ORGANIZER_HARDWARE_METRICS)},
        ),
        ScenarioSpec(
            "latency_spike",
            "Exact organizer latency_spike pattern on one switch: high latency and jitter, moderate "
            "utilization, few drops. No single clear cause; honest uncertainty expected.",
            DiagnosisStatus.INSUFFICIENT_EVIDENCE, None,
            [Hypothesis.LINK_CONGESTION, Hypothesis.UPSTREAM_ISSUE], True, "SW_CAMPUS_9",
            {"SW_CAMPUS_9": Overlay({
                "cpu_utilization_pct": (40, 70), "link_utilization_pct": (60, 85), "packet_loss_pct": (0.1, 1.5),
                "latency_ms": (150, 400), "jitter_ms": (30, 80), "queue_drops": (0, 50),
            }, metric_set=ORGANIZER_LATENCY_SPIKE_METRICS)},
        ),
        ScenarioSpec(
            "interoperability_failure",
            "The link_congestion incident delivered by a legacy collector: renamed fields, latency in "
            "seconds, utilization as fractions, schema v1.1 flat records, some metrics missing.",
            DiagnosisStatus.DIAGNOSED, Hypothesis.LINK_CONGESTION, [Hypothesis.LINK_CONGESTION], True,
            "HOSTEL_CORE_SW_01", congestion, congestion_symptoms,
            mutations=["rename_fields", "swap_units", "downgrade_schema"],
            drop={"*": ["memory_utilization_pct", "interface_flaps", "uptime_s"]},
        ),
        ScenarioSpec(
            "multi_fault", "Hostel uplink congestion and, independently, a DNS resolver failure.",
            DiagnosisStatus.DIAGNOSED, Hypothesis.LINK_CONGESTION,
            [Hypothesis.LINK_CONGESTION, Hypothesis.DNS_FAILURE], True, "HOSTEL_CORE_SW_01",
            {**congestion, "DNS_SRV_01": _dns_overlay()}, congestion_symptoms,
            secondary=[Hypothesis.DNS_FAILURE],
        ),
        ScenarioSpec(
            "subtle_degradation",
            "Latency and jitter on one academic switch drift well above the device's own history but stay "
            "below every alert threshold. Expected: an early-warning abstention, never 'normal' and never a "
            "confident root cause.",
            DiagnosisStatus.INSUFFICIENT_EVIDENCE, None, [], True, "SW_CAMPUS_8",
            {"SW_CAMPUS_8": Overlay({"latency_ms": (30, 42), "jitter_ms": (9, 15), "packet_loss_pct": (0.04, 0.08)},
                                    status=DeviceStatus.UP)},
        ),
        ScenarioSpec(
            "insufficient_data",
            "A single device reports only latency and loss. No reliable root cause can be claimed.",
            DiagnosisStatus.INSUFFICIENT_EVIDENCE, None, [], True, "SW_CAMPUS_3",
            {"SW_CAMPUS_3": Overlay({"latency_ms": (120, 200), "packet_loss_pct": (0.3, 0.8)},
                                    metric_set=["latency_ms", "packet_loss_pct"], status=DeviceStatus.UNKNOWN)},
            devices=["SW_CAMPUS_3"],
        ),
    ]
    return {s.name: s for s in specs}


SCENARIOS: dict[str, ScenarioSpec] = _specs()


def _metric_set(node_type: str) -> list[str]:
    if node_type == "access_point":
        return AP_METRICS
    if node_type == "dns_server":
        return DNS_METRICS
    if node_type == "dhcp_server":
        return DHCP_METRICS
    return SWITCH_METRICS


def _draw(rng: random.Random, metric: str, lo: float, hi: float) -> float:
    if metric in COUNT_METRICS:
        return float(rng.randint(int(lo), int(hi)))
    return round(rng.uniform(lo, hi), 2)


def _sample(
    rng: random.Random, metric: str, overlay: Overlay | None, active: bool, step: int, steps: int,
    uptime0: float, t: int,
) -> float:
    if metric == "uptime_s":
        return float(int(uptime0 + t * INTERVAL_S))
    if active and overlay is not None:
        if metric in overlay.ramp:
            start, end = overlay.ramp[metric]
            frac = step / max(steps - 1, 1)
            centre = start + (end - start) * frac
            return _draw(rng, metric, centre * 0.9, centre * 1.1)
        if metric in overlay.ranges:
            lo, hi = overlay.ranges[metric]
            return _draw(rng, metric, lo, hi)
    lo, hi = HEALTHY[metric]
    return _draw(rng, metric, lo, hi)


def generate(
    scenario: str,
    seed: int = 42,
    duration: int = 20,
    fmt: str = "canonical",
    devices: int | None = None,
    onset_fraction: float = 0.5,
    topology: Topology | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return (records, truth) for one scenario."""
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}; choose from {', '.join(SCENARIOS)}")
    spec = SCENARIOS[scenario]
    topo = topology or default_topology()
    rng = random.Random(f"{scenario}:{seed}")

    device_ids = spec.devices or list(topo.nodes)
    if devices is not None:  # keep fault-related devices, then fill with controls
        involved = [d for d in device_ids if d in spec.overlays]
        controls = [d for d in device_ids if d not in spec.overlays]
        device_ids = involved + controls[: max(devices - len(involved), 0)]

    onset = int(duration * onset_fraction) if spec.overlays else duration
    records: list[dict[str, Any]] = []
    uptimes = {d: float(rng.randint(100_000, 9_000_000)) for d in device_ids}
    for t in range(duration):
        ts = (BASE_TIME + timedelta(seconds=t * INTERVAL_S)).isoformat().replace("+00:00", "Z")
        for device in device_ids:
            node = topo.nodes[device]
            overlay = spec.overlays.get(device)
            active = overlay is not None and t >= onset
            metrics_for = (overlay.metric_set if overlay and overlay.metric_set else _metric_set(node.type))
            dropped = set(spec.drop.get("*", [])) | set(spec.drop.get(device, []))
            values: dict[str, float] = {}
            util: float | None = None
            for metric in metrics_for:
                if metric in dropped or metric == "throughput_mbps":
                    continue
                value = _sample(rng, metric, overlay, active, t - onset, duration - onset, uptimes[device], t)
                values[metric] = value
                if metric == "link_utilization_pct":
                    util = value
            if "throughput_mbps" in metrics_for and "throughput_mbps" not in dropped and util is not None:
                values["throughput_mbps"] = round(util / 100.0 * node.uplink_mbps, 2)
            status = overlay.status if active and overlay else DeviceStatus.UP
            records.append(_render(device, ts, status, values, fmt))

    if spec.mutations:
        records = mutate(records, spec.mutations, seed=seed)
    return records, truth_for(spec, seed, [d for d in device_ids if d in spec.overlays])


def _render(device: str, ts: str, status: DeviceStatus, values: dict[str, float], fmt: str) -> dict[str, Any]:
    if fmt == "organizer":
        metrics = {m: (int(v) if m in COUNT_METRICS else v) for m, v in values.items()}
        out: dict[str, Any] = {"timestamp": ts, "device_id": device, "metrics": metrics}
        if status is not DeviceStatus.UNKNOWN:
            out["status"] = ORGANIZER_STATUS[status]
        return out
    return {
        "schema_version": "2.0",
        "device_id": device,
        "timestamp": ts,
        "status": status.value,
        "metrics": {
            m: {"value": int(v) if m in COUNT_METRICS else v, "unit": METRICS[m].unit.value}
            for m, v in values.items()
        },
    }


def truth_for(spec: ScenarioSpec, seed: int, faulty_devices: list[str]) -> dict[str, Any]:
    return {
        "scenario": spec.name,
        "seed": seed,
        "description": spec.description,
        "expected_status": spec.expected_status.value,
        "root_cause": spec.root_cause.value if spec.root_cause else None,
        "acceptable_root_causes": [h.value for h in spec.acceptable],
        "abstain_ok": spec.abstain_ok,
        "root_device": spec.root_device,
        "affected_devices": sorted(faulty_devices),
        "symptomatic_devices": sorted(spec.symptomatic),
        "secondary_root_causes": [h.value for h in spec.secondary],
        "mutations": spec.mutations,
    }


def scenario_names() -> list[str]:
    return list(SCENARIOS)
