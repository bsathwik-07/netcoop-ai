"""Composable input mutations for the chaos harness (AIOpsLab-style fault injection on inputs).

Every mutation takes a list of raw telemetry records (canonical v2 or organizer v1 shape),
a seeded RNG and returns a NEW list; inputs are never modified in place.
"""

from __future__ import annotations

import copy
import math
import random
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

Record = dict[str, Any]
Mutation = Callable[[list[Record], random.Random], list[Record]]

METRIC_CONTAINER = "metrics"

# Deterministic alias choices, deliberately mixing styles (camelCase, kebab-case, RFC 8343 names).
RENAMES: dict[str, str] = {
    "latency_ms": "rtt",
    "jitter_ms": "ipdv",
    "packet_loss_pct": "pktLoss",
    "link_utilization_pct": "link-util",
    "cpu_utilization_pct": "cpuLoad",
    "memory_utilization_pct": "mem",
    "queue_drops": "out-discards",
    "interface_errors": "in-errors",
    "interface_flaps": "linkFlaps",
    "wifi_rssi_dbm": "rssi",
    "wifi_snr_db": "snr",
    "retransmission_pct": "retry_rate",
    "throughput_mbps": "goodput",
    "dns_success_pct": "dnsSuccessRate",
    "dns_latency_ms": "dns_rtt",
    "dhcp_success_pct": "lease_success",
    "dhcp_ack_ms": "dora_ms",
    "dhcp_pool_utilization_pct": "pool_utilization",
    "apipa_clients": "apipa",
}
IDENTITY_RENAMES = {"device_id": "deviceId", "timestamp": "ts", "status": "state"}
REVERSE_RENAMES = {v: k for k, v in RENAMES.items()}


# -- helpers --------------------------------------------------------------------------------------


def _container(rec: Record) -> dict[str, Any]:
    """The dict that holds metrics: nested `metrics`, another nesting key, or the record itself."""
    for key in (METRIC_CONTAINER, "measurements"):
        if isinstance(rec.get(key), dict):
            return rec[key]
    return rec


_NON_METRIC = {"device_id", "deviceId", "timestamp", "ts", "status", "state", "schema_version",
               METRIC_CONTAINER, "measurements"}


def _metric_keys(rec: Record) -> list[str]:
    box = _container(rec)
    return [k for k in box if k not in _NON_METRIC]


def _read(raw: Any) -> tuple[Any, str | None]:
    if isinstance(raw, dict):
        return raw.get("value"), raw.get("unit")
    return raw, None


def _write(box: dict[str, Any], key: str, value: Any, unit: str | None, template: Any) -> None:
    if isinstance(template, dict):
        box[key] = {**template, "value": value, "unit": unit}
    elif unit is None:
        box[key] = value
    else:
        box[key] = {"value": value, "unit": unit}


def _canonical_name(key: str) -> str:
    return REVERSE_RENAMES.get(key, key)


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), tz=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _ts_key(rec: Record) -> str:
    return "ts" if "ts" in rec else "timestamp"


# -- mutations ------------------------------------------------------------------------------------


def rename_fields(records: list[Record], rng: random.Random) -> list[Record]:
    out = []
    for rec in copy.deepcopy(records):
        for old, new in IDENTITY_RENAMES.items():
            if old in rec:
                rec[new] = rec.pop(old)
        box = _container(rec)
        for key in list(box):
            if key in RENAMES:
                box[RENAMES[key]] = box.pop(key)
        out.append(rec)
    return out


def swap_units(records: list[Record], rng: random.Random) -> list[Record]:
    """Latency in seconds, utilization/loss as fractions, RSSI as positive magnitude."""
    out = []
    for rec in copy.deepcopy(records):
        box = _container(rec)
        for key in list(box):
            name = _canonical_name(key)
            value, _unit = _read(box[key])
            if not isinstance(value, int | float) or isinstance(value, bool) or not math.isfinite(value):
                continue
            template = box[key]
            if name in ("latency_ms", "dns_latency_ms"):
                _write(box, key, round(value / 1000.0, 6), "s", template)
            elif name in ("link_utilization_pct", "packet_loss_pct"):
                _write(box, key, round(value / 100.0, 6), "fraction", template)
            elif name == "cpu_utilization_pct":
                # no unit given on purpose: the adapter must infer "fraction" from plausibility
                del box[key]
                box[key] = round(value / 100.0, 6)
            elif name == "wifi_rssi_dbm":
                _write(box, key, abs(value), None, template if isinstance(template, dict) else None)
                if isinstance(box[key], dict):
                    box[key].pop("unit", None)
            elif name == "throughput_mbps":
                _write(box, key, round(value * 1000.0, 3), "kbps", template)
        out.append(rec)
    return out


def drop_metrics(records: list[Record], rng: random.Random, p: float = 0.3) -> list[Record]:
    out = []
    for rec in copy.deepcopy(records):
        box = _container(rec)
        for key in _metric_keys(rec):
            if rng.random() < p:
                del box[key]
        out.append(rec)
    return out


def inject_nan(records: list[Record], rng: random.Random, p: float = 0.1) -> list[Record]:
    out = []
    for rec in copy.deepcopy(records):
        box = _container(rec)
        for key in _metric_keys(rec):
            if rng.random() < p:
                bad = rng.choice([float("nan"), None, "N/A", "", "inf"])
                _write(box, key, bad, _read(box[key])[1], box[key])
        out.append(rec)
    return out


def downgrade_schema(records: list[Record], rng: random.Random) -> list[Record]:
    """Rewrite into the legacy v1.1 flat format: deviceId, epoch `ts`, `state`, flat metrics."""
    status_names = {"up": "Healthy", "degraded": "Warning", "down": "Critical"}
    out = []
    for rec in copy.deepcopy(records):
        legacy: Record = {"schema_version": "1.1"}
        legacy["deviceId"] = rec.get("device_id", rec.get("deviceId"))
        ts = _parse_ts(rec.get("timestamp", rec.get("ts")))
        legacy["ts"] = int(ts.timestamp()) if ts else rec.get("timestamp")
        status = rec.get("status", rec.get("state"))
        legacy["state"] = status_names.get(str(status).lower(), status)
        for key in _metric_keys(rec):
            value, unit = _read(_container(rec)[key])
            # v1.1 had no unit objects; keep explicit units as a sibling `<name>_unit` field
            legacy[key] = value
            if unit and unit not in ("ms", "%", "count", "dBm", "dB", "Mbps", "s"):
                legacy[f"{key}_unit"] = unit
            elif unit == "s" and not key.endswith("_s"):
                legacy[f"{key}_unit"] = unit
        out.append(legacy)
    return out


def shuffle_timestamps(records: list[Record], rng: random.Random) -> list[Record]:
    out = copy.deepcopy(records)
    rng.shuffle(out)
    return out


def add_noise(records: list[Record], rng: random.Random, sigma: float = 0.05) -> list[Record]:
    """Multiplicative measurement noise. For success-rate style metrics (near 100 %) the noise is
    applied to the failure share instead, so 99.8 % becomes ~99.79 %, not a fabricated 94.8 %."""
    out = []
    for rec in copy.deepcopy(records):
        box = _container(rec)
        for key in _metric_keys(rec):
            value, unit = _read(box[key])
            if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value):
                name = _canonical_name(key)
                if name in ("dns_success_pct", "dhcp_success_pct") and value > 1.0:
                    noisy = 100.0 - (100.0 - value) * (1.0 + rng.gauss(0.0, sigma))
                else:
                    noisy = value * (1.0 + rng.gauss(0.0, sigma))
                if name.endswith("_pct"):
                    noisy = min(max(noisy, 0.0), 100.0 if abs(value) > 1.0 else 1.0)
                elif name != "wifi_rssi_dbm":
                    noisy = max(noisy, 0.0)
                noisy = round(noisy) if isinstance(value, int) else round(noisy, 4)
                _write(box, key, noisy, unit, box[key])
        out.append(rec)
    return out


def duplicate_records(records: list[Record], rng: random.Random, p: float = 0.2) -> list[Record]:
    out: list[Record] = []
    for rec in records:
        out.append(copy.deepcopy(rec))
        if rng.random() < p:
            out.append(copy.deepcopy(rec))
    if len(out) == len(records) and records:
        out.append(copy.deepcopy(records[-1]))
    return out


def stale_timestamps(records: list[Record], rng: random.Random, p: float = 0.3, hours: float = 3.0) -> list[Record]:
    out = []
    for rec in copy.deepcopy(records):
        if rng.random() < p:
            key = _ts_key(rec)
            ts = _parse_ts(rec.get(key))
            if ts is not None:
                shifted = ts - timedelta(hours=hours)
                rec[key] = shifted.isoformat().replace("+00:00", "Z") if isinstance(rec[key], str) else int(shifted.timestamp())
        out.append(rec)
    return out


def flatten_or_nest(records: list[Record], rng: random.Random) -> list[Record]:
    out = []
    for rec in copy.deepcopy(records):
        if isinstance(rec.get(METRIC_CONTAINER), dict):
            metrics = rec.pop(METRIC_CONTAINER)
            rec.update(metrics)
        else:
            metrics = {k: rec.pop(k) for k in _metric_keys(rec)}
            rec["measurements"] = metrics
        out.append(rec)
    return out


MUTATIONS: dict[str, Mutation] = {
    "rename_fields": rename_fields,
    "swap_units": swap_units,
    "drop_metrics": drop_metrics,
    "inject_nan": inject_nan,
    "downgrade_schema": downgrade_schema,
    "shuffle_timestamps": shuffle_timestamps,
    "add_noise": add_noise,
    "duplicate_records": duplicate_records,
    "stale_timestamps": stale_timestamps,
    "flatten_or_nest": flatten_or_nest,
}


def mutate(records: list[Record], names: list[str] | str, seed: int = 0) -> list[Record]:
    """Apply mutations in order. Unknown names raise ValueError (a CLI/user error, not data)."""
    if isinstance(names, str):
        names = [n.strip() for n in names.split(",") if n.strip()]
    rng = random.Random(seed)
    current = records
    for name in names:
        if name not in MUTATIONS:
            raise ValueError(f"unknown mutation {name!r}; choose from {', '.join(MUTATIONS)}")
        current = MUTATIONS[name](current, rng)
    return current
