"""Turn canonical telemetry into per-device metric summaries and evidence items.

Per device and metric we take the most recent `recent_window` usable samples and summarize
them with the median (robust to one noisy sample). Earlier samples, when there are enough,
form a baseline for a z-score (NetMedic-style "abnormal relative to history").
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from netcoop.config import Settings
from netcoop.protocol.payloads import Direction, EvidenceItem, TelemetryRecord
from netcoop.protocol.vocabulary import METRICS, DeviceStatus, Quality, Unit

# Derived signals that behave like metrics in the weight tables.
DEVICE_STATUS = "device_status"
UPTIME_RESET = "uptime_reset"
SHARED_UPSTREAM = "shared_upstream_symptoms"

# metric -> (phrase when abnormal, phrase when healthy)
PHRASES: dict[str, tuple[str, str]] = {
    "latency_ms": ("Elevated network latency", "Latency within normal range"),
    "jitter_ms": ("High jitter (delay variation)", "Jitter within normal range"),
    "packet_loss_pct": ("Packet loss above acceptable level", "No significant packet loss"),
    "link_utilization_pct": ("High link utilization", "Link utilization normal"),
    "throughput_mbps": ("Throughput reading", "Throughput reading"),
    "retransmission_pct": ("High retransmission rate", "Retransmissions normal"),
    "wifi_rssi_dbm": ("Weak Wi-Fi signal (low RSSI)", "Wi-Fi signal strength adequate"),
    "wifi_snr_db": ("Low signal-to-noise ratio", "SNR adequate"),
    "cpu_utilization_pct": ("High device CPU utilization", "CPU utilization normal"),
    "memory_utilization_pct": ("High device memory utilization", "Memory utilization normal"),
    "queue_drops": ("Elevated egress queue drops", "Queue drops negligible"),
    "interface_errors": ("Interface/CRC errors detected", "No interface errors"),
    "interface_flaps": ("Link instability (interface flaps)", "Link stable"),
    "dns_success_pct": ("DNS resolution failures", "DNS resolving normally"),
    "dns_latency_ms": ("Slow DNS resolution", "DNS latency normal"),
    "dhcp_success_pct": ("DHCP lease failures", "DHCP leases succeeding"),
    "dhcp_ack_ms": ("Slow DHCP offer/ack", "DHCP timing normal"),
    "dhcp_pool_utilization_pct": ("DHCP pool nearly exhausted", "DHCP pool has headroom"),
    "apipa_clients": ("Clients self-assigning APIPA addresses", "No APIPA clients"),
    DEVICE_STATUS: ("Device reports degraded/critical status", "Device status up"),
    UPTIME_RESET: ("Device rebooted (uptime reset)", "No reboot observed"),
    SHARED_UPSTREAM: ("Neighbouring devices under the same upstream are also degraded",
                      "Neighbouring devices under the same upstream are healthy"),
}


@dataclass
class MetricSummary:
    device_id: str
    metric: str
    value: float
    unit: Unit | None
    quality: Quality
    quality_factor: float
    severity: float
    samples: int
    timestamp: datetime | None = None
    z_score: float | None = None
    threshold: float | None = None
    direction: Direction = Direction.ABOVE
    baseline_only: bool = False  # abnormal only relative to its own history, below the fixed threshold


def robust_z(value: float, baseline: list[float], metric: str, settings: Settings) -> float:
    """(value - median) / robust spread. MAD ignores the degraded samples that a step change leaves
    in the baseline window; the floors stop quiet, very steady devices producing huge z from noise."""
    med = statistics.median(baseline)
    mad = 1.4826 * statistics.median(abs(x - med) for x in baseline)
    floor = float(settings.scoring["series"].get("baseline_min_sd", {}).get(metric, 0.0))
    scale = max(mad, 0.05 * abs(med), floor, 1e-6)
    return round((value - med) / scale, 2)


def baseline_severity(z: float, direction: str, settings: Settings) -> float:
    """Capped severity for a deviation in the harmful direction (see scoring.yaml: series)."""
    series = settings.scoring["series"]
    z_thr, z_full = float(series["z_threshold"]), float(series.get("z_full", 6.0))
    cap = float(series.get("baseline_severity_cap", 0.4))
    harmful = z if direction == "above" else -z
    if harmful < z_thr:
        return 0.0
    return round(cap * min(1.0, (harmful - z_thr + 1.0) / (z_full - z_thr + 1.0)), 4)


def quality_factor(settings: Settings, quality: Quality | str) -> float:
    return float(settings.scoring["quality_factor"].get(str(quality), 0.0))


def _worst_quality(settings: Settings, qualities: list[Quality]) -> Quality:
    return min(qualities, key=lambda q: quality_factor(settings, q))


def series_by_device(records: list[TelemetryRecord]) -> dict[str, list[TelemetryRecord]]:
    out: dict[str, list[TelemetryRecord]] = defaultdict(list)
    for rec in records:
        out[rec.device_id].append(rec)
    for series in out.values():
        series.sort(key=lambda r: r.timestamp)
    return dict(out)


def summarize_device(
    device_id: str, series: list[TelemetryRecord], metrics: set[str], settings: Settings
) -> dict[str, MetricSummary]:
    """Summaries for the metrics this caller owns (plus derived status/uptime signals if asked)."""
    window = int(settings.scoring["series"]["recent_window"])
    min_baseline = int(settings.scoring["series"]["min_baseline"])
    out: dict[str, MetricSummary] = {}

    for metric in metrics:
        if metric not in METRICS:
            continue
        points = [
            (rec.timestamp, mv.value, mv.quality)
            for rec in series
            if (mv := rec.metrics.get(metric)) is not None
            and mv.value is not None
            and quality_factor(settings, mv.quality) > 0
        ]
        if not points:
            continue
        recent, earlier = points[-window:], points[:-window]
        threshold = settings.thresholds.get(metric)
        value = float(statistics.median(p[1] for p in recent))
        persist = int(settings.scoring["series"].get("persistence_samples", 2))
        tail = recent[-persist:]
        if threshold and len(recent) > persist and all(threshold.severity(p[1]) > 0 for p in tail):
            # the newest samples all breach the threshold: the condition is current, even if the
            # window median (still holding pre-onset samples) looks healthy. Use the milder of the
            # newest samples, but never let this rule soften what the median already shows.
            current = float(min((p[1] for p in tail), key=threshold.severity))
            if threshold.severity(current) > threshold.severity(value):
                value = current
                recent = tail
        quality = _worst_quality(settings, [p[2] for p in recent])
        # Guard band (as in CFAR change detection): skip one window right before the recent one, so a
        # change that began a little earlier does not contaminate its own reference.
        reference = earlier[:max(min_baseline, len(earlier) - window)]
        z = robust_z(value, [p[1] for p in reference], metric, settings) if len(earlier) >= min_baseline else None
        severity = threshold.severity(value) if threshold else 0.0
        baseline_only = False
        if threshold and severity == 0 and z is not None:
            baseline_sev = baseline_severity(z, threshold.direction, settings)
            # materiality: statistically unusual is not enough, the value must also have moved a real
            # share of the way from its baseline towards the alert threshold
            med = statistics.median(p[1] for p in reference)
            room = threshold.warn - med
            progress = (value - med) / room if room and (room > 0) == (threshold.direction == "above") else 0.0
            min_progress = float(settings.scoring["series"].get("baseline_min_progress", 0.25))
            if baseline_sev > 0 and progress >= min_progress:
                severity, baseline_only = baseline_sev, True
        out[metric] = MetricSummary(
            device_id=device_id,
            metric=metric,
            value=round(value, 4),
            unit=METRICS[metric].unit,
            quality=quality,
            quality_factor=quality_factor(settings, quality),
            severity=round(severity, 4),
            samples=len(recent),
            timestamp=recent[-1][0],
            z_score=z,
            threshold=(threshold.crit if severity >= 1 else threshold.warn) if threshold else None,
            direction=Direction(threshold.direction) if threshold else Direction.ABOVE,
            baseline_only=baseline_only,
        )

    if DEVICE_STATUS in metrics:
        statuses = [r.status for r in series[-window:] if r.status is not DeviceStatus.UNKNOWN]
        if statuses:
            sev_map = settings.scoring["status_severity"]
            worst = max(statuses, key=lambda s: sev_map[s.value])
            latest = series[-1]
            out[DEVICE_STATUS] = MetricSummary(
                device_id, DEVICE_STATUS, sev_map[worst.value], None, latest.record_quality,
                quality_factor(settings, latest.record_quality), float(sev_map[worst.value]), len(statuses),
                latest.timestamp,
            )

    if UPTIME_RESET in metrics:
        uptimes = [(r.timestamp, mv.value) for r in series
                   if (mv := r.metrics.get("uptime_s")) is not None and mv.value is not None]
        if len(uptimes) >= 2:
            recent_pairs = uptimes[-(window + 1):]
            # a reboot restarts uptime near zero; small decreases are collector noise, not resets
            min_drop = float(settings.scoring["series"].get("reboot_min_drop_s", 600))
            reset = any(b[1] < a[1] * 0.5 and a[1] - b[1] >= min_drop
                        for a, b in zip(recent_pairs, recent_pairs[1:], strict=False))
            out[UPTIME_RESET] = MetricSummary(
                device_id, UPTIME_RESET, 1.0 if reset else 0.0, None, Quality.OK, 1.0,
                1.0 if reset else 0.0, len(recent_pairs), uptimes[-1][0],
            )
    return out


def to_evidence(summary: MetricSummary, source_agent: str) -> EvidenceItem:
    abnormal, healthy = PHRASES.get(summary.metric, (summary.metric, summary.metric))
    if summary.metric == DEVICE_STATUS:
        status = {0.0: "up", 0.5: "degraded", 1.0: "down/critical"}.get(summary.value, str(summary.value))
        text = f"{abnormal}: {status}" if summary.severity > 0 else healthy
    elif summary.baseline_only:
        text = (f"Early warning: {abnormal[0].lower() + abnormal[1:]} relative to this device's own baseline "
                f"({_fmt(summary.value)} {summary.unit or ''}, {abs(summary.z_score or 0):.1f} sigma from its history; "
                f"still below the warning threshold {_fmt(summary.threshold)})")
    elif summary.severity > 0:
        level = "critical" if summary.severity >= 1 else "warning"
        text = f"{abnormal} ({_fmt(summary.value)} {summary.unit or ''} vs {level} threshold {_fmt(summary.threshold)})"
    else:
        text = healthy
    if not summary.baseline_only and summary.z_score is not None and abs(summary.z_score) >= 3:
        text += f"; {abs(summary.z_score):.1f} sigma {'above' if summary.z_score > 0 else 'below'} its baseline"
    return EvidenceItem(
        metric=summary.metric,
        value=summary.value,
        unit=summary.unit,
        device_id=summary.device_id,
        threshold=summary.threshold,
        severity=summary.severity,
        direction=summary.direction,
        interpretation=text.replace("  ", " ").strip(),
        quality=summary.quality,
        source_agent=source_agent,
        timestamp=summary.timestamp,
        z_score=summary.z_score,
        basis="baseline" if summary.baseline_only else "threshold",
    )


def _fmt(value: float | None) -> str:
    if value is None:
        return "?"
    return f"{value:.0f}" if abs(value) >= 100 else f"{value:.2f}".rstrip("0").rstrip(".")
