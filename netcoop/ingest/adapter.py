"""Schema adapter: any supported raw telemetry -> canonical v2 records + an IngestionReport.

Accepts a single object, a list, a {"records": [...]} wrapper, JSONL or CSV; the organizer
flat format, our canonical v2 format, or legacy variants. It never raises on bad data:
every problem becomes a RejectionNote with a protocol error code, every value change a
ConversionNote, and every value carries a quality tag and provenance.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from rapidfuzz import fuzz, process

from netcoop.config import Settings
from netcoop.protocol.migrations import detect_telemetry_schema
from netcoop.protocol.payloads import (
    ConversionNote,
    IngestionReport,
    MappingMethod,
    MetricValue,
    Provenance,
    RejectionNote,
    TelemetryRecord,
)
from netcoop.protocol.vocabulary import METRICS, STATUS_MAP, DeviceStatus, ErrorCode, Quality
from netcoop.topology import Topology

FieldMapper = Callable[[str, list[str]], str | None]
MAX_NOTES = 300
NA_STRINGS = {"", "n/a", "na", "nan", "null", "none", "-", "--", "?", "unknown", "nil"}
WRAPPER_KEYS = ("records", "data", "telemetry", "items", "samples", "measurements")

_CAMEL_A = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_B = re.compile(r"([a-z0-9])([A-Z])")
_NON_WORD = re.compile(r"[^0-9a-zA-Z%]+")


def norm_key(key: str) -> str:
    """camelCase / kebab-case / dotted / spaced -> snake_case."""
    s = _CAMEL_A.sub(r"\1_\2", str(key))
    s = _CAMEL_B.sub(r"\1_\2", s)
    s = _NON_WORD.sub("_", s).strip("_").lower()
    return re.sub(r"_+", "_", s)


class _UnknownUnit(Exception):
    pass


@dataclass
class _RawMetric:
    original_key: str
    canonical: str
    method: MappingMethod
    value: Any
    unit: str | None
    unit_source: str | None  # "explicit" | "suffix" | None


@dataclass
class _Pending:
    index: int
    device_id: str
    timestamp: datetime | None
    ts_problem: str | None
    ts_note: str | None
    status: DeviceStatus
    status_original: str | None
    schema: str
    metrics: list[_RawMetric]
    unmapped: dict[str, Any]
    source_order: int


@dataclass
class AdapterResult:
    records: list[TelemetryRecord]
    report: IngestionReport


@dataclass
class _Ctx:
    report: IngestionReport = field(default_factory=IngestionReport)
    dropped_notes: int = 0
    _agg: dict[tuple[Any, ...], ConversionNote] = field(default_factory=dict)

    def reject(self, code: ErrorCode, reason: str, *, index: int | None = None, device: str | None = None,
               fld: str | None = None, whole: bool = False) -> None:
        if len(self.report.rejections) >= MAX_NOTES:
            self.dropped_notes += 1
            return
        self.report.rejections.append(RejectionNote(record_index=index, device_id=device, field=fld, code=code,
                                                    reason=reason, record_rejected=whole))

    def convert(self, note: ConversionNote) -> None:
        """Identical conversions (same field, method and units) are aggregated with a count."""
        key = (note.original_field, note.canonical_field, note.method, note.from_unit, note.to_unit, note.note)
        if key in self._agg:
            self._agg[key].count += 1
            return
        if len(self.report.conversions) >= MAX_NOTES:
            self.dropped_notes += 1
            return
        self._agg[key] = note
        self.report.conversions.append(note)


class TelemetryAdapter:
    def __init__(self, settings: Settings, topology: Topology | None = None,
                 field_mapper: FieldMapper | None = None) -> None:
        self.settings = settings
        self.topology = topology
        self.field_mapper = field_mapper
        aliases = settings.field_aliases
        self.identity: dict[str, str] = {}
        for canonical, names in aliases["identity"].items():
            for name in names:
                self.identity[norm_key(name)] = canonical
        self.metric_alias: dict[str, str] = {m: m for m in METRICS}
        for canonical, names in aliases["metrics"].items():
            for name in names:
                self.metric_alias.setdefault(norm_key(name), canonical)
        self.containers = {norm_key(c) for c in aliases.get("metric_containers", [])}
        units = settings.units
        self.conversions: dict[str, dict[str, float]] = {
            canon: {str(k): float(v) for k, v in table.items()} for canon, table in units["conversions"].items()
        }
        self.suffixes = sorted(units["suffixes"].items(), key=lambda kv: len(kv[0]), reverse=True)
        heur = units.get("heuristics", {})
        self.fraction_candidates = set(heur.get("fraction_candidates", [])) if heur.get("percent_as_fraction") else set()
        self.positive_rssi = bool(heur.get("positive_rssi_is_magnitude", True))
        self.epoch_ms_threshold = float(heur.get("epoch_ms_threshold", 1e11))
        ing = settings.orchestration["ingestion"]
        self.fuzzy_cutoff = float(ing.get("fuzzy_match_score", 88))
        self.max_bytes = int(ing.get("max_input_bytes", 20_000_000))
        self.stale_reference = str(ing.get("stale_reference", "batch"))
        self.stale_window = timedelta(seconds=float(ing.get("stale_window_s", 900)))
        self.future_tolerance = timedelta(seconds=float(ing.get("future_tolerance_s", 300)))
        self._resolve_cache: dict[str, tuple[str, MappingMethod, str | None] | None] = {}

    # ------------------------------------------------------------------------------------------
    # entry points
    # ------------------------------------------------------------------------------------------

    def ingest(self, raw: str | bytes | dict | list, content_type: str = "auto", source: str = "inline",
               now: datetime | None = None) -> AdapterResult:
        ctx = _Ctx()
        try:
            objects = self._parse(raw, content_type, ctx)
            records = self._build(objects, ctx, now or datetime.now(UTC)) if objects is not None else []
        except Exception as exc:  # last line of defence: the adapter never raises
            ctx.reject(ErrorCode.E_INTERNAL, f"ingestion failed unexpectedly: {type(exc).__name__}: {exc}")
            records = []
        rep = ctx.report
        rep.records_accepted = len(records)
        rep.fatal = not records
        if ctx.dropped_notes:
            rep.warnings.append(f"{ctx.dropped_notes} further notes omitted")
        if rep.fatal and not any(r.record_rejected or r.record_index is None for r in rep.rejections):
            ctx.reject(ErrorCode.E_MISSING_REQUIRED, "no usable telemetry record in the input")
        if rep.fatal:
            rep.warnings.insert(0, f"no usable telemetry in the input from {source}")
        return AdapterResult(records, rep)

    # ------------------------------------------------------------------------------------------
    # parsing
    # ------------------------------------------------------------------------------------------

    def _parse(self, raw: str | bytes | dict | list, content_type: str, ctx: _Ctx) -> list[Any] | None:
        if isinstance(raw, dict | list):
            return self._unwrap(raw)
        if isinstance(raw, bytes):
            if len(raw) > self.max_bytes:
                ctx.reject(ErrorCode.E_SCHEMA_INVALID, f"input is {len(raw)} bytes, above the {self.max_bytes} limit")
                return None
            try:
                raw = raw.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                ctx.reject(ErrorCode.E_SCHEMA_INVALID,
                           f"input is not UTF-8 text (binary data?): invalid byte at position {exc.start}")
                return None
        text = raw
        if len(text) > self.max_bytes:
            ctx.reject(ErrorCode.E_SCHEMA_INVALID, f"input is {len(text)} characters, above the {self.max_bytes} limit")
            return None
        control = sum(1 for ch in text[:4096] if ord(ch) < 32 and ch not in "\r\n\t")
        if control > 8:
            ctx.reject(ErrorCode.E_SCHEMA_INVALID, "input contains binary/control characters; it is not telemetry text")
            return None
        stripped = text.strip()
        if not stripped:
            ctx.reject(ErrorCode.E_SCHEMA_INVALID, "input is empty")
            return None
        kind = content_type
        if kind == "auto":
            kind = "json" if stripped[0] in "[{" else "csv"
        if kind == "csv":
            ctx.report.formats_detected.append("csv")
            return self._parse_csv(stripped, ctx)
        try:
            return self._unwrap(json.loads(stripped))
        except json.JSONDecodeError as exc:
            lines = [ln for ln in stripped.splitlines() if ln.strip()]
            if kind != "json" or (len(lines) > 1 and all(ln.lstrip().startswith("{") for ln in lines)):
                parsed = self._parse_jsonl(lines, ctx)
                if parsed:
                    return parsed
            hint = " (the input looks truncated)" if exc.pos >= len(stripped) - 2 else ""
            ctx.reject(ErrorCode.E_SCHEMA_INVALID,
                       f"input is not valid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}{hint}")
            return None

    def _parse_jsonl(self, lines: list[str], ctx: _Ctx) -> list[Any]:
        out: list[Any] = []
        for i, line in enumerate(lines):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as exc:
                ctx.reject(ErrorCode.E_SCHEMA_INVALID, f"JSONL line {i + 1} is not valid JSON: {exc.msg}",
                           index=i, whole=True)
                out.append(None)
        if any(o is not None for o in out):
            ctx.report.formats_detected.append("jsonl")
            return out
        return []

    def _parse_csv(self, text: str, ctx: _Ctx) -> list[Any] | None:
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames or len(reader.fieldnames) < 2:
            ctx.reject(ErrorCode.E_SCHEMA_INVALID, "input is neither JSON nor CSV with a header row")
            return None
        rows: list[Any] = []
        for i, row in enumerate(reader):
            if None in row or any(v is None for v in row.values()):
                ctx.reject(ErrorCode.E_SCHEMA_INVALID,
                           f"CSV row {i + 2} has a different number of fields than the header", index=i, whole=True)
                rows.append(None)
                continue
            rows.append({k: v for k, v in row.items() if v != ""})
        return rows

    @staticmethod
    def _unwrap(obj: Any) -> list[Any]:
        if isinstance(obj, list):
            return obj
        if isinstance(obj, dict):
            for key in WRAPPER_KEYS:
                inner = obj.get(key)
                if isinstance(inner, list) and inner and all(isinstance(x, dict) for x in inner):
                    return inner
            return [obj]
        return [obj]

    # ------------------------------------------------------------------------------------------
    # field mapping
    # ------------------------------------------------------------------------------------------

    def resolve_metric(self, key: str) -> tuple[str, MappingMethod, str | None] | None:
        """Canonical metric for a field name: (name, method, unit implied by a suffix)."""
        if key in self._resolve_cache:
            return self._resolve_cache[key]
        result = self._resolve(norm_key(key))
        if result is None and self.field_mapper is not None:
            try:
                suggestion = self.field_mapper(key, sorted(METRICS))
            except Exception:
                suggestion = None
            if suggestion in METRICS:
                result = (suggestion, MappingMethod.LLM_SUGGESTED, None)
        self._resolve_cache[key] = result
        return result

    def _resolve(self, k: str) -> tuple[str, MappingMethod, str | None] | None:
        if k in METRICS:
            return k, MappingMethod.DIRECT, None
        if k in self.metric_alias:
            return self.metric_alias[k], MappingMethod.ALIAS, None
        for container in self.containers:
            if k.startswith(container + "_"):
                inner = self._resolve(k[len(container) + 1:])
                if inner:
                    return inner
        for suffix, unit in self.suffixes:
            if k.endswith(suffix) and len(k) > len(suffix):
                base = k[: -len(suffix)]
                if base in self.metric_alias:
                    return self.metric_alias[base], MappingMethod.UNIT_CONVERSION, str(unit)
        if len(k) >= 5:
            match = process.extractOne(k, list(self.metric_alias), scorer=fuzz.ratio, score_cutoff=self.fuzzy_cutoff)
            if match:
                return self.metric_alias[match[0]], MappingMethod.FUZZY, None
        return None

    def _split(self, rec: dict[str, Any]) -> tuple[dict[str, tuple[str, Any]], list[tuple[str, Any, str | None]],
                                                   dict[str, Any], bool]:
        """identity fields, candidate metric (key, value, explicit unit), units by key, nested?"""
        identity: dict[str, tuple[str, Any]] = {}
        candidates: list[tuple[str, Any, str | None]] = []
        units: dict[str, Any] = {}
        nested = False

        def visit(key: str, value: Any, prefix: str = "", depth: int = 0) -> None:
            nonlocal nested
            nk = norm_key(key)
            full = f"{prefix}{key}"
            if depth == 0 and nk in self.identity and not isinstance(value, dict):
                identity.setdefault(self.identity[nk], (key, value))
                return
            if nk.endswith("_unit") or nk == "unit":
                units[norm_key(full)[: -len("_unit")] if nk != "unit" else norm_key(prefix.rstrip("."))] = value
                return
            if isinstance(value, dict):
                if "value" in value:
                    unit = value.get("unit")
                    candidates.append((full, value.get("value"), str(unit) if unit not in (None, "") else None))
                elif depth == 0 and nk in self.containers:
                    nested = True
                    for k2, v2 in value.items():
                        visit(k2, v2, "", depth + 1)
                elif depth < 2:
                    for k2, v2 in value.items():
                        visit(k2, v2, f"{full}_", depth + 1)
                return
            candidates.append((full, value, None))

        for key, value in rec.items():
            visit(str(key), value)
        return identity, candidates, units, nested

    # ------------------------------------------------------------------------------------------
    # value parsing
    # ------------------------------------------------------------------------------------------

    @staticmethod
    def parse_number(value: Any) -> tuple[float | None, str | None, bool]:
        """(number, problem, is_placeholder)."""
        if isinstance(value, bool):
            return None, f"boolean {value} is not a number", False
        if isinstance(value, int | float):
            if not math.isfinite(value):
                return None, f"non-finite number ({value})", False
            return float(value), None, False
        if value is None:
            return None, "null value", True
        if isinstance(value, str):
            s = value.strip()
            if s.lower() in NA_STRINGS:
                return None, f"placeholder {value!r} instead of a number", True
            try:
                number = float(s.rstrip("%").strip())
            except ValueError:
                return None, f"not a number: {value!r}", False
            if not math.isfinite(number):
                return None, f"non-finite number ({value})", False
            return number, None, False
        return None, f"expected a number, got {type(value).__name__}", False

    def _factor(self, canonical: str, unit: str) -> float:
        table = self.conversions.get(METRICS[canonical].unit.value, {})
        if unit in table:
            return table[unit]
        lowered = {k.lower(): v for k, v in table.items()}
        if unit.lower() in lowered:
            return lowered[unit.lower()]
        raise _UnknownUnit(unit)

    def _parse_ts(self, value: Any) -> tuple[datetime | None, str | None, str | None]:
        """(timestamp, conversion note, problem)."""
        if value is None:
            return None, None, "missing timestamp"
        number, _problem, _ = self.parse_number(value) if not isinstance(value, str) or _looks_numeric(value) else (None, None, False)
        if number is not None:
            seconds = number / 1000.0 if number > self.epoch_ms_threshold else number
            if not 0 < seconds < 4_102_444_800:
                return None, None, f"epoch timestamp {value!r} out of range"
            unit = "epoch ms" if number > self.epoch_ms_threshold else "epoch s"
            return datetime.fromtimestamp(seconds, tz=UTC), f"{unit} -> ISO-8601 UTC", None
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00").replace(" ", "T", 1))
            except ValueError:
                return None, None, f"unparseable timestamp {value!r}"
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=UTC), "naive timestamp assumed UTC", None
            return parsed.astimezone(UTC), None, None
        return None, None, f"timestamp has unsupported type {type(value).__name__}"

    # ------------------------------------------------------------------------------------------
    # record building
    # ------------------------------------------------------------------------------------------

    def _build(self, objects: list[Any], ctx: _Ctx, now: datetime) -> list[TelemetryRecord]:
        rep = ctx.report
        rep.records_in = len(objects)
        pending: list[_Pending] = []
        schemas: set[str] = set()
        formats: list[str] = rep.formats_detected
        unmapped_all: set[str] = set()

        for index, obj in enumerate(objects):
            if obj is None:
                continue  # already rejected while parsing
            if not isinstance(obj, dict):
                ctx.reject(ErrorCode.E_SCHEMA_INVALID, f"record is a {type(obj).__name__}, expected an object",
                           index=index, whole=True)
                continue
            identity, candidates, units, nested = self._split(obj)
            version_raw = identity.get("schema_version", (None, None))[1]
            schema, major = detect_telemetry_schema(version_raw, nested)
            if major is None or major > 2:
                ctx.reject(ErrorCode.E_UNSUPPORTED_VERSION,
                           f"telemetry schema_version {version_raw!r} is not supported (supported: 1.x, 2.x)",
                           index=index, whole=True)
                continue
            schemas.add(schema)
            if schema not in formats:
                formats.append(schema)

            device_raw = identity.get("device_id", (None, None))[1]
            if device_raw is None or (isinstance(device_raw, str) and not device_raw.strip()) or isinstance(device_raw, dict | list):
                ctx.reject(ErrorCode.E_MISSING_REQUIRED, "record has no device_id (or alias such as deviceId/host)",
                           index=index, whole=True)
                continue
            device = str(device_raw).strip()

            ts_key, ts_raw = identity.get("timestamp", ("timestamp", None))
            ts, ts_note, ts_problem = self._parse_ts(ts_raw)

            status_raw = identity.get("status", (None, None))[1]
            status = DeviceStatus.UNKNOWN
            if status_raw is not None:
                status = STATUS_MAP.get(str(status_raw).strip().lower(), DeviceStatus.UNKNOWN)
                if status is DeviceStatus.UNKNOWN and str(status_raw).strip().lower() != "unknown":
                    rep.warnings.append(f"record {index} ({device}): unrecognised status {status_raw!r}")

            metrics: list[_RawMetric] = []
            unmapped: dict[str, Any] = {}
            for key, value, explicit_unit in candidates:
                resolved = self.resolve_metric(key)
                if resolved is None:
                    unmapped[key] = value if isinstance(value, str | int | float | bool) or value is None else str(value)
                    if key not in unmapped_all:
                        unmapped_all.add(key)
                        ctx.reject(ErrorCode.E_FIELD_UNMAPPED,
                                   f"field {key!r} does not map to any canonical metric; kept under 'unmapped'",
                                   index=index, device=device, fld=key)
                    continue
                canonical, method, suffix_unit = resolved
                unit = explicit_unit or units.get(norm_key(key))
                unit_source = "explicit" if unit else None
                if unit is None and suffix_unit:
                    unit, unit_source = suffix_unit, "suffix"
                metrics.append(_RawMetric(key, canonical, method, value, str(unit) if unit else None, unit_source))

            if not metrics and status_raw is None:
                ctx.reject(ErrorCode.E_MISSING_REQUIRED,
                           f"record for {device} has no recognisable metrics or status", index=index, device=device,
                           whole=True)
                continue
            if self.topology is not None and device not in self.topology:
                ctx.reject(ErrorCode.E_UNKNOWN_DEVICE,
                           f"device {device!r} is not in the topology; analysed without topology context",
                           index=index, device=device)
            pending.append(_Pending(index, device, ts, ts_problem, ts_note, status,
                                    str(status_raw) if status_raw is not None else None, schema, metrics, unmapped,
                                    index))
            if ts_problem:
                ctx.reject(ErrorCode.E_SCHEMA_INVALID, f"{ts_problem}; timestamp imputed from the batch",
                           index=index, device=device, fld=ts_key)

        rep.schema_versions = sorted(schemas)
        rep.unmapped_fields = sorted(unmapped_all)
        if not pending:
            return []
        migrated = [p for p in pending if p.schema != "canonical_v2"]
        if migrated:
            kinds = sorted({p.schema for p in migrated})
            ctx.convert(ConversionNote(original_field="schema_version", canonical_field="schema_version",
                                       method=MappingMethod.MIGRATION, from_value=",".join(kinds), to_value="2.0",
                                       note=f"{len(migrated)} record(s) migrated to canonical telemetry v2"))
        fraction_keys = self._fraction_keys(pending)
        return self._finalize(pending, fraction_keys, ctx, now)

    def _fraction_keys(self, pending: list[_Pending]) -> set[tuple[str, str]]:
        """(original key, canonical) pairs whose unit-less values are all in [0, 1] -> fractions."""
        values: dict[tuple[str, str], list[float]] = defaultdict(list)
        for p in pending:
            for m in p.metrics:
                if m.canonical in self.fraction_candidates and m.unit is None:
                    number, _, _ = self.parse_number(m.value)
                    if number is not None:
                        values[(m.original_key, m.canonical)].append(number)
        return {
            key for key, vals in values.items()
            if vals and all(0.0 <= v <= 1.0 for v in vals) and any(0.0 < v < 1.0 for v in vals)
        }

    def _finalize(self, pending: list[_Pending], fraction_keys: set[tuple[str, str]], ctx: _Ctx,
                  now: datetime) -> list[TelemetryRecord]:
        rep = ctx.report
        future_limit = now + self.future_tolerance
        valid_ts = [p.timestamp for p in pending if p.timestamp is not None and p.timestamp <= future_limit]
        if self.stale_reference == "now" or not valid_ts:
            reference = now
        else:
            reference = max(valid_ts)

        records: list[TelemetryRecord] = []
        seen: set[str] = set()
        last_ts: dict[str, datetime] = {}
        out_of_order: set[str] = set()
        stale_counts: dict[str, int] = defaultdict(int)
        total_slots = 0
        usable = 0.0
        qf = self.settings.scoring["quality_factor"]

        for p in pending:
            record_quality = Quality.OK
            ts = p.timestamp
            if ts is None:
                ts = reference
                record_quality = Quality.IMPUTED
            elif ts > future_limit:
                record_quality = Quality.OUT_OF_RANGE
                ctx.reject(ErrorCode.E_OUT_OF_RANGE,
                           f"timestamp {ts.isoformat()} is in the future; record excluded from scoring",
                           index=p.index, device=p.device_id, fld="timestamp")
            elif ts < reference - self.stale_window:
                record_quality = Quality.STALE
                stale_counts[p.device_id] += 1
            if p.ts_note:
                ctx.convert(ConversionNote(record_index=p.index, device_id=p.device_id, original_field="timestamp",
                                           canonical_field="timestamp", method=MappingMethod.UNIT_CONVERSION,
                                           to_value=ts.isoformat(), note=p.ts_note))
            if p.device_id in last_ts and ts < last_ts[p.device_id]:
                out_of_order.add(p.device_id)
            last_ts[p.device_id] = max(ts, last_ts.get(p.device_id, ts))

            metrics: dict[str, MetricValue] = {}
            for m in p.metrics:
                mv = self._metric_value(m, p, fraction_keys, ctx)
                if record_quality in (Quality.STALE, Quality.OUT_OF_RANGE) and mv.quality in (Quality.OK, Quality.CONVERTED):
                    mv.quality = record_quality
                if m.canonical in metrics:
                    existing = metrics[m.canonical]
                    if qf.get(str(existing.quality), 0) >= qf.get(str(mv.quality), 0):
                        rep.warnings.append(f"record {p.index} ({p.device_id}): duplicate field for "
                                            f"{m.canonical} ({m.original_key!r}) ignored")
                        continue
                metrics[m.canonical] = mv

            fingerprint = json.dumps([p.device_id, ts.isoformat(), {k: v.value for k, v in metrics.items()},
                                      p.status_original], sort_keys=True, default=str)
            if fingerprint in seen:
                ctx.reject(ErrorCode.E_SCHEMA_INVALID, "duplicate record (same device, timestamp and values) dropped",
                           index=p.index, device=p.device_id, whole=True)
                continue
            seen.add(fingerprint)

            for mv in metrics.values():
                total_slots += 1
                usable += float(qf.get(str(mv.quality), 0.0))
            records.append(TelemetryRecord(
                device_id=p.device_id, timestamp=ts, status=p.status, status_original=p.status_original,
                metrics=metrics, unmapped=p.unmapped,
                source_format=p.schema if "csv" not in rep.formats_detected else "csv",
                record_quality=record_quality,
            ))

        for device, count in sorted(stale_counts.items()):
            ctx.reject(ErrorCode.E_STALE_DATA,
                       f"{count} record(s) of {device} are older than {int(self.stale_window.total_seconds())} s "
                       f"before the batch reference time; down-weighted as stale", device=device)
        if out_of_order:
            names = sorted(out_of_order)
            more = f" (+{len(names) - 5} more)" if len(names) > 5 else ""
            rep.warnings.append(f"records of {len(names)} device(s) arrived out of order and were sorted by "
                                f"timestamp: {', '.join(names[:5])}{more}")

        # Gaps count as missing: a metric a device reports in some samples but not in others is a hole
        # in its series (expected slots = metrics the device ever reported x its number of samples).
        per_device: dict[str, list[TelemetryRecord]] = defaultdict(list)
        for r in records:
            per_device[r.device_id].append(r)
        expected = sum(len({m for r in recs for m in r.metrics}) * len(recs) for recs in per_device.values())
        total_slots = max(total_slots, expected)
        accepted_share = len(records) / max(rep.records_in, 1)
        rep.data_coverage = round((usable / total_slots) * accepted_share, 4) if total_slots else 0.0
        counts: dict[str, int] = defaultdict(int)
        for r in records:
            for mv in r.metrics.values():
                counts[str(mv.quality)] += 1
        rep.quality_counts = dict(sorted(counts.items()))
        rep.records_rejected = rep.records_in - len(records)
        records.sort(key=lambda r: (r.device_id, r.timestamp))
        return records

    def _metric_value(self, m: _RawMetric, p: _Pending, fraction_keys: set[tuple[str, str]], ctx: _Ctx) -> MetricValue:
        spec = METRICS[m.canonical]
        prov = Provenance(original_field=m.original_key,
                          original_value=m.value if isinstance(m.value, str | int | float | bool) or m.value is None else str(m.value),
                          original_unit=m.unit, method=m.method)
        number, problem, placeholder = self.parse_number(m.value)
        if number is None:
            ctx.reject(ErrorCode.E_SCHEMA_INVALID, f"{m.original_key}: {problem}", index=p.index, device=p.device_id,
                       fld=m.original_key)
            return MetricValue(value=None, unit=spec.unit, quality=Quality.MISSING if placeholder else Quality.REJECTED,
                               provenance=[prov])

        quality = Quality.OK
        method = m.method
        unit = m.unit
        if method in (MappingMethod.FUZZY,):
            quality = Quality.CONVERTED
        if method is MappingMethod.LLM_SUGGESTED:
            quality = Quality.IMPUTED
        if method in (MappingMethod.ALIAS, MappingMethod.FUZZY, MappingMethod.LLM_SUGGESTED):
            ctx.convert(ConversionNote(record_index=p.index, device_id=p.device_id, original_field=m.original_key,
                                       canonical_field=m.canonical, method=method, from_value=number,
                                       to_value=number))
        value = number
        if unit is None and (m.original_key, m.canonical) in fraction_keys:
            unit, method = "fraction", MappingMethod.HEURISTIC
        if unit is not None:
            try:
                factor = self._factor(m.canonical, unit)
            except _UnknownUnit:
                ctx.reject(ErrorCode.E_UNIT_UNKNOWN,
                           f"{m.original_key}: unit {unit!r} cannot be converted to {spec.unit.value}",
                           index=p.index, device=p.device_id, fld=m.original_key)
                return MetricValue(value=None, unit=spec.unit, quality=Quality.REJECTED, provenance=[prov])
            if factor != 1.0:
                value = number * factor
                quality = Quality.CONVERTED if quality is Quality.OK else quality
                note = MappingMethod.HEURISTIC if method is MappingMethod.HEURISTIC else MappingMethod.UNIT_CONVERSION
                prov = prov.model_copy(update={"method": note})
                ctx.convert(ConversionNote(record_index=p.index, device_id=p.device_id, original_field=m.original_key,
                                           canonical_field=m.canonical, method=note, from_value=number,
                                           to_value=round(value, 6), from_unit=unit, to_unit=spec.unit.value,
                                           note="plausibility heuristic: all values in [0, 1]" if note is MappingMethod.HEURISTIC else None))
        if m.canonical == "wifi_rssi_dbm" and value > 0 and self.positive_rssi:
            value = -value
            quality = Quality.CONVERTED if quality is Quality.OK else quality
            ctx.convert(ConversionNote(record_index=p.index, device_id=p.device_id, original_field=m.original_key,
                                       canonical_field=m.canonical, method=MappingMethod.HEURISTIC, from_value=number,
                                       to_value=value, to_unit="dBm", note="positive RSSI treated as magnitude"))

        if (spec.min_value is not None and value < spec.min_value) or (spec.max_value is not None and value > spec.max_value):
            bound = f"[{spec.min_value}, {spec.max_value if spec.max_value is not None else 'inf'}]"
            ctx.reject(ErrorCode.E_OUT_OF_RANGE,
                       f"{m.original_key}={value:g} {spec.unit.value} is outside the valid range {bound}",
                       index=p.index, device=p.device_id, fld=m.original_key)
            return MetricValue(value=round(value, 6), unit=spec.unit, quality=Quality.OUT_OF_RANGE, provenance=[prov])
        return MetricValue(value=round(value, 6), unit=spec.unit, quality=quality, provenance=[prov])


def _looks_numeric(value: str) -> bool:
    try:
        float(value.strip())
        return True
    except ValueError:
        return False
