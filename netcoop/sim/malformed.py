"""Malformed input fixtures. Each must yield a clear error and a non-crashing report.

`EXPECTED` maps file name -> error codes that must appear in the ingestion report.
"""

from __future__ import annotations

import json
from pathlib import Path

from netcoop.protocol.vocabulary import ErrorCode

_GOOD = {
    "timestamp": "2026-10-14T07:05:49Z",
    "device_id": "SW_CAMPUS_4",
    "metrics": {
        "cpu_utilization_pct": 99.9,
        "link_utilization_pct": 100.0,
        "packet_loss_pct": 28.51,
        "latency_ms": 926.46,
        "queue_drops": 13416,
        "interface_errors": 382,
    },
    "status": "Critical",
}


def _with(**metric_overrides: object) -> dict:
    rec = json.loads(json.dumps(_GOOD))
    rec["metrics"].update(metric_overrides)
    return rec


def _text(obj: object) -> str:
    return json.dumps(obj, indent=2)


FIXTURES: dict[str, str | bytes] = {
    "truncated.json": _text(_GOOD)[:140],
    "wrong_types.json": _text(_with(latency_ms="high", cpu_utilization_pct="ninety", queue_drops=[1, 2])),
    "negative_values.json": _text(_with(latency_ms=-5.0, queue_drops=-10)),
    "percent_over_100.json": _text(_with(packet_loss_pct=140.0, link_utilization_pct=250.0)),
    "nan_values.json": json.dumps(_with(latency_ms=float("nan"), packet_loss_pct=None, cpu_utilization_pct="N/A")),
    "empty.json": "",
    "whitespace.json": "   \n\t  ",
    "unknown_device.json": _text({**_GOOD, "device_id": "ROUTER_ON_MARS_9"}),
    "future_timestamp.json": _text([_GOOD, {**_GOOD, "timestamp": "2099-01-01T00:00:00Z"}]),
    "duplicate_record.json": _text([_GOOD, _GOOD]),
    "missing_device_id.json": _text({k: v for k, v in _GOOD.items() if k != "device_id"}),
    "bad_timestamp.json": _text({**_GOOD, "timestamp": "yesterday at noon"}),
    "not_telemetry.json": _text({"hello": "world", "answer": 42}),
    "unsupported_schema.json": _text({**_GOOD, "schema_version": "9.0"}),
    "binary_garbage.json": bytes(range(256)) * 4,
    "malformed.csv": "device_id,timestamp,latency_ms\nSW_CAMPUS_1,2026-10-14T07:05:49Z,12\nSW_CAMPUS_2,not-a-time\n",
}

EXPECTED: dict[str, list[ErrorCode]] = {
    "truncated.json": [ErrorCode.E_SCHEMA_INVALID],
    "wrong_types.json": [ErrorCode.E_SCHEMA_INVALID],
    "negative_values.json": [ErrorCode.E_OUT_OF_RANGE],
    "percent_over_100.json": [ErrorCode.E_OUT_OF_RANGE],
    "nan_values.json": [ErrorCode.E_SCHEMA_INVALID],
    "empty.json": [ErrorCode.E_SCHEMA_INVALID],
    "whitespace.json": [ErrorCode.E_SCHEMA_INVALID],
    "unknown_device.json": [ErrorCode.E_UNKNOWN_DEVICE],
    "future_timestamp.json": [ErrorCode.E_OUT_OF_RANGE],
    "duplicate_record.json": [ErrorCode.E_SCHEMA_INVALID],
    "missing_device_id.json": [ErrorCode.E_MISSING_REQUIRED],
    "bad_timestamp.json": [ErrorCode.E_SCHEMA_INVALID],
    "not_telemetry.json": [ErrorCode.E_MISSING_REQUIRED],
    "unsupported_schema.json": [ErrorCode.E_UNSUPPORTED_VERSION],
    "binary_garbage.json": [ErrorCode.E_SCHEMA_INVALID],
    "malformed.csv": [ErrorCode.E_SCHEMA_INVALID],
}


def write_fixtures(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, content in FIXTURES.items():
        path = out_dir / name
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        paths.append(path)
    manifest = {name: [c.value for c in codes] for name, codes in EXPECTED.items()}
    (out_dir / "expected.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return paths
