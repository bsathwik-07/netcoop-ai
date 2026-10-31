"""Remediation simulator.

###########################################################################################
#  SIMULATION ONLY. Nothing in this module (or anywhere in NetCoop AI) connects to, logs   #
#  into or sends commands to a real network device. "Applying" an action means editing a  #
#  copy of the incident's synthetic telemetry and re-running the diagnosis on it.          #
###########################################################################################

An approved action's `simulate` effect (config/remediation_playbook.yaml) restores the metrics it
would plausibly fix on the target device (and, for upstream fixes, on the devices below it). The
simulator appends a new window of samples after the incident, so the re-diagnosis sees the
state "after the fix" while everything else keeps its last observed value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from netcoop.protocol.payloads import DiagnosticReport, RecommendedAction, TelemetryRecord
from netcoop.protocol.vocabulary import METRICS
from netcoop.topology import Topology

SIMULATION_BANNER = "SIMULATION ONLY - no real device was touched"

# effect -> (metrics restored on the target, metrics restored on devices below it, restore status)
# Values are mid-points of the healthy ranges used by the scenario generator.
HEALTHY_VALUE: dict[str, float] = {
    "latency_ms": 8.0, "jitter_ms": 2.0, "packet_loss_pct": 0.01, "link_utilization_pct": 40.0,
    "cpu_utilization_pct": 30.0, "memory_utilization_pct": 50.0, "queue_drops": 5.0, "interface_errors": 0.0,
    "interface_flaps": 0.0, "wifi_rssi_dbm": -55.0, "wifi_snr_db": 38.0, "retransmission_pct": 3.0,
    "dns_success_pct": 99.9, "dns_latency_ms": 20.0, "dhcp_success_pct": 99.8, "dhcp_ack_ms": 200.0,
    "dhcp_pool_utilization_pct": 65.0, "apipa_clients": 0.0,
}
PATH = ("latency_ms", "jitter_ms", "packet_loss_pct")
EFFECTS: dict[str, tuple[tuple[str, ...], tuple[str, ...], bool]] = {
    "reduce_utilization": (("link_utilization_pct", "queue_drops", *PATH), PATH, False),
    "improve_rssi": (("wifi_rssi_dbm", "wifi_snr_db", "retransmission_pct", *PATH), (), False),
    "clear_interface_errors": (("interface_errors", "interface_flaps", *PATH), PATH, True),
    "failover_device": (("cpu_utilization_pct", "link_utilization_pct", "queue_drops", "interface_errors",
                         "interface_flaps", *PATH), PATH, True),
    "reduce_cpu": (("cpu_utilization_pct", "memory_utilization_pct", "queue_drops", *PATH), PATH, True),
    "restore_dns": (("dns_success_pct", "dns_latency_ms"), (), True),
    "restore_dhcp": (("dhcp_success_pct", "dhcp_ack_ms", "dhcp_pool_utilization_pct", "apipa_clients"), (), True),
}


@dataclass
class SimulationResult:
    action_id: str
    effect: str
    simulated: bool
    devices: list[str] = field(default_factory=list)
    before: dict[str, dict[str, float]] = field(default_factory=dict)
    after: dict[str, dict[str, float]] = field(default_factory=dict)
    records: list[dict[str, Any]] = field(default_factory=list)
    report: DiagnosticReport | None = None
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "banner": SIMULATION_BANNER, "action_id": self.action_id, "effect": self.effect,
            "simulated": self.simulated, "devices": self.devices, "before": self.before, "after": self.after,
            "summary": self.summary,
            "re_diagnosis": None if self.report is None else {
                "incident_id": self.report.incident_id, "status": self.report.status.value,
                "root_cause_code": self.report.root_cause_code.value if self.report.root_cause_code else None,
                "confidence": self.report.confidence, "suspected_root_cause": self.report.suspected_root_cause},
        }


def effect_of(action: RecommendedAction, playbook: dict[str, Any]) -> str:
    if action.hypothesis is None:
        return "none"
    for entry in playbook.get(action.hypothesis.value, []):
        if action.action_id.split("@")[0].endswith(entry["id"]):
            return str(entry.get("simulate", "none"))
    return "none"


def _to_raw(rec: TelemetryRecord) -> dict[str, Any]:
    return {
        "schema_version": "2.0", "device_id": rec.device_id,
        "timestamp": rec.timestamp.isoformat().replace("+00:00", "Z"), "status": rec.status.value,
        "metrics": {m: {"value": v.value, "unit": v.unit.value} for m, v in rec.metrics.items() if v.value is not None},
    }


def apply_action(
    records: list[TelemetryRecord], action: RecommendedAction, effect: str, topology: Topology | None,
    samples: int = 6, interval_s: int = 60,
) -> SimulationResult:
    """Return the incident telemetry plus `samples` new post-fix samples (raw canonical v2 dicts)."""
    result = SimulationResult(action.action_id, effect, simulated=False)
    if effect not in EFFECTS or not records:
        result.summary = f"{SIMULATION_BANNER}: '{action.action}' has no simulated effect (inspection or manual work)"
        return result
    target = action.target_device
    if not target or not any(r.device_id == target for r in records):
        result.summary = f"{SIMULATION_BANNER}: target device {target!r} has no telemetry to simulate on"
        return result
    on_target, downstream, restore_status = EFFECTS[effect]
    below = topology.descendants(target) if topology and target in topology else set()

    last: dict[str, TelemetryRecord] = {}
    for rec in sorted(records, key=lambda r: r.timestamp):
        last[rec.device_id] = rec
    end = max(r.timestamp for r in records)
    raw = [_to_raw(r) for r in records]

    touched: list[str] = []
    for device, rec in sorted(last.items()):
        fixed = on_target if device == target else downstream if device in below else ()
        before = {m: v.value for m, v in rec.metrics.items() if m in fixed and v.value is not None}
        after = {m: HEALTHY_VALUE[m] for m in before if m in HEALTHY_VALUE}
        if after:
            touched.append(device)
            result.before[device] = {m: round(float(v), 3) for m, v in before.items()}
            result.after[device] = after
        status = "up" if (device == target and restore_status) or (after and rec.status.value == "degraded") else rec.status.value
        for i in range(1, samples + 1):
            sample = _to_raw(rec)
            sample["timestamp"] = (end + timedelta(seconds=i * interval_s)).isoformat().replace("+00:00", "Z")
            sample["status"] = status
            for m, value in after.items():
                sample["metrics"][m] = {"value": value, "unit": METRICS[m].unit.value}
            if "uptime_s" in sample["metrics"]:
                sample["metrics"]["uptime_s"]["value"] = float(sample["metrics"]["uptime_s"]["value"]) + i * interval_s
            raw.append(sample)
    result.simulated = True
    result.devices = touched
    result.records = raw
    result.summary = f"{SIMULATION_BANNER}: applied '{effect}' to {target}" + (
        f" and {len(touched) - 1} downstream device(s)" if len(touched) > 1 else "")
    return result


async def simulate_and_rediagnose(
    records: list[TelemetryRecord], action: RecommendedAction, playbook: dict[str, Any],
    topology: Topology | None, incident_id: str, **pipeline_kwargs: Any,
) -> SimulationResult:
    """Apply the action to a copy of the telemetry and run the whole multi-agent diagnosis again."""
    from netcoop.pipeline import run_incident_async

    result = apply_action(records, action, effect_of(action, playbook), topology)
    if not result.simulated:
        return result
    run = await run_incident_async(result.records, source=f"simulation:{action.action_id}",
                                   incident_id=incident_id, topology=topology, **pipeline_kwargs)
    result.report = run.report
    if run.report is not None:
        result.summary += (f"; re-diagnosis: {run.report.status.value}"
                           f" ({run.report.root_cause_code.value if run.report.root_cause_code else '-'},"
                           f" confidence {run.report.confidence:.2f})")
    else:
        result.summary += f"; re-diagnosis failed: {run.error}"
    return result
