"""Export JSON Schemas (for external agents) and the field tables used in PROTOCOL.md.

Run: python -m netcoop.protocol.schema_export [--out schemas]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter

from netcoop.config import REPO_ROOT
from netcoop.protocol.envelope import MESSAGE_MODELS, AnyMessage, Envelope
from netcoop.protocol.payloads import PAYLOAD_MODELS, DiagnosticReport, EvidenceItem, TelemetryRecord
from netcoop.protocol.vocabulary import (
    ERROR_DESCRIPTIONS,
    HYPOTHESIS_DESCRIPTIONS,
    METRICS,
    PROTOCOL,
    UNIT_UCUM,
    MessageType,
)


def message_schema() -> dict[str, Any]:
    schema = TypeAdapter(AnyMessage).json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = f"NetCoop message ({PROTOCOL})"
    return schema


def model_schema(model: type[BaseModel], title: str | None = None) -> dict[str, Any]:
    schema = model.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    if title:
        schema["title"] = title
    return schema


def vocabulary_document() -> dict[str, Any]:
    return {
        "protocol": PROTOCOL,
        "message_types": [m.value for m in MessageType],
        "hypotheses": {h.value: d for h, d in HYPOTHESIS_DESCRIPTIONS.items()},
        "error_codes": {c.value: d for c, d in ERROR_DESCRIPTIONS.items()},
        "metrics": {
            name: {
                "unit": spec.unit.value,
                "ucum": UNIT_UCUM[spec.unit],
                "domain": spec.domain.value,
                "description": spec.description,
                "min": spec.min_value,
                "max": spec.max_value,
            }
            for name, spec in METRICS.items()
        },
    }


def export_all(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "payloads").mkdir(exist_ok=True)
    written: list[Path] = []

    def dump(path: Path, data: dict[str, Any]) -> None:
        path.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")
        written.append(path)

    dump(out_dir / "message.schema.json", message_schema())
    dump(out_dir / "envelope.schema.json", model_schema(Envelope, "NetCoop envelope"))
    dump(out_dir / "report.schema.json", model_schema(DiagnosticReport, "NetCoop diagnostic report"))
    dump(out_dir / "telemetry_record.schema.json", model_schema(TelemetryRecord, "Canonical telemetry record v2"))
    dump(out_dir / "vocabulary.json", vocabulary_document())
    for message_type, payload in PAYLOAD_MODELS.items():
        name = payload.SCHEMA.split("/")[0]
        dump(out_dir / "payloads" / f"{name}.schema.json", model_schema(payload, f"{message_type} payload ({payload.SCHEMA})"))
    return written


# -- Markdown field tables for PROTOCOL.md ---------------------------------------------------


def _type_of(prop: dict[str, Any] | bool) -> str:
    if not isinstance(prop, dict):
        return "any"
    if "$ref" in prop:
        return prop["$ref"].split("/")[-1]
    if "anyOf" in prop:
        return " | ".join(_type_of(p) for p in prop["anyOf"])
    if "allOf" in prop:
        return " & ".join(_type_of(p) for p in prop["allOf"])
    if "const" in prop:
        return f"'{prop['const']}'"
    if "enum" in prop:
        return " | ".join(str(v) for v in prop["enum"])
    kind = prop.get("type", "any")
    if kind == "array":
        return f"list[{_type_of(prop.get('items', {}))}]"
    if kind == "object" and "additionalProperties" in prop:
        return f"map[str, {_type_of(prop['additionalProperties'])}]"
    if prop.get("format") == "date-time":
        return "datetime (ISO-8601 UTC)"
    return str(kind)


def _unit_hint(field: str, model: type[BaseModel]) -> str:
    if field in METRICS:
        return METRICS[field].unit.value
    if model in (EvidenceItem,) and field in ("value", "threshold"):
        return "see `unit`"
    if field.endswith("_s") or field in ("deadline_s", "ttl_s", "uptime_s"):
        return "s"
    if field in ("confidence", "probability", "severity", "coverage", "data_coverage", "p_top", "p_second",
                 "agreement_factor", "unknown_mass"):
        return "0..1"
    return ""


# Meanings for fields whose model definition carries no description (keeps the models terse).
FIELD_DOCS: dict[str, str] = {
    "task_id": "Identifier of the task this message belongs to",
    "agent": "Name of the agent", "agent_version": "Semantic version of the agent",
    "version": "Semantic version of the agent",
    "hypothesis_scores": "Per-device contributions to each hypothesis (see HypothesisScore)",
    "top_hypothesis": "The agent's own best explanation; null = no opinion",
    "evidence": "Evidence items (see EvidenceItem)",
    "devices_flagged": "Devices with at least one abnormal signal",
    "metrics_analyzed": "Canonical metrics the agent looked at", "metrics_missing": "Declared inputs absent in the batch",
    "data_coverage": "Share of expected data that was usable",
    "claims": "Machine-checkable assertions used for contradiction detection",
    "localization": "Root vs symptomatic devices (connectivity agent)",
    "limitations": "Caveats the reader must know", "query_id": "Identifier echoed by the RESPONSE",
    "round": "Challenge round number", "question": "Human-readable question",
    "predicate": "Machine-readable question type", "params": "Predicate arguments (device_id, metric, ...)",
    "hypotheses_at_stake": "Hypotheses the answer can separate",
    "answer": "true / false, or null = cannot tell from the available data",
    "value": "Observed value", "unit": "Canonical unit of value", "explanation": "Human-readable justification",
    "supports": "Hypotheses this answer supports", "refutes": "Hypotheses this answer refutes",
    "status": "Outcome status", "root_cause_code": "Hypothesis code of the root cause",
    "confidence": "Calibrated confidence = P(top) x coverage x agreement",
    "confidence_category": "high / medium / low", "probabilities": "Softmax probability per hypothesis",
    "root_device": "Device where the root cause sits", "affected_devices": "Root plus symptomatic devices",
    "symptomatic_devices": "Devices showing symptoms that the root cause explains",
    "affected_segments": "Zones of the affected devices",
    "supporting_evidence": "Evidence for the root cause", "contradicting_evidence": "Evidence against it",
    "alternative_causes": "Ranked alternatives with probabilities and why they are less likely",
    "agent_findings_summary": "Each agent's top hypothesis and whether it responded",
    "conflict_resolution": "Whether agents disagreed and how the challenge round resolved it",
    "data_quality": "Ingestion coverage, conversions, rejections, warnings",
    "confidence_breakdown": "Factors of the confidence formula",
    "secondary_incidents": "Independent faults outside the primary root's subtree",
    "next_data_to_collect": "Data that would confirm or rule out the leading candidates",
    "evidence_sources": "Agents whose evidence contributed",
    "recommended_actions": "Ranked actions (human approval required; simulated only)",
    "incident_id": "Incident identifier", "suspected_root_cause": "Human-readable root cause statement",
    "narrative": "Prose summary (template, or LLM when enabled)", "protocol_version": "Protocol of the report",
    "generated_at": "Report creation time (UTC)", "message_log": "Path of the incident's JSONL message log",
    "report": "The diagnostic report", "json_path": "Where the JSON report was written",
    "markdown_path": "Where the Markdown report was written", "records": "Telemetry records",
    "code": "Error code (see error table)", "message": "Human-readable error", "detail": "Structured details",
    "offending_msg_id": "msg_id of the message that caused the error", "recoverable": "Whether processing continued",
    "acked_msg_id": "msg_id being acknowledged", "note": "Free-text outcome", "state": "Lifecycle state",
    "uptime_s": "Seconds since the agent started", "description": "Human-readable description",
    "supported_protocols": "Protocol versions the agent reads", "consumes": "Message types it consumes",
    "produces": "Message types it produces", "skills": "A2A-style skills (for Agent Cards)",
    "actions": "Proposed actions", "action_id": "Identifier of the action", "action": "What to do",
    "risk": "low / medium / high", "requested_from": "Who must decide", "approved": "Human decision",
    "decided_by": "Who decided", "reason": "Why", "simulated_result": "Outcome of the dry-run simulation",
    "source": "Where the telemetry came from", "content_type": "auto / json / jsonl / csv",
    "content": "Telemetry text exactly as received",
    "metric": "Canonical metric name or derived signal", "device_id": "Device identifier",
    "threshold": "Threshold compared against (same unit as value)", "direction": "above = higher is worse",
    "interpretation": "Human-readable meaning", "quality": "ok / converted / imputed / missing / stale / ...",
    "source_agent": "Agent that produced the item", "timestamp": "Time of the observation (UTC)",
    "hypothesis": "Hypothesis code", "raw": "Unnormalized weighted evidence sum",
    "score": "Agent-local normalized score", "rationale": "Why",
    "cause": "Alternative hypothesis", "probability": "Softmax probability",
    "why_less_likely": "Score gap, contradicting rules, healthy or missing key signals",
    "priority": "1 = do first", "safe_to_simulate": "Has a dry-run effect", "requires_approval": "Always true",
    "target_device": "Device the action applies to", "p_top": "Probability of the top hypothesis",
    "p_second": "Probability of the runner-up", "margin": "p_top - p_second",
    "agreement_factor": "Share of eligible agents agreeing (resolved dissent counts partially)",
    "combiner": "Evidence combination method", "unknown_mass": "Unused (reserved)",
    "method": "How the confidence was computed",
}


def field_table(model: type[BaseModel]) -> str:
    schema = model.model_json_schema()
    required = set(schema.get("required", []))
    rows = ["| Field | Type | Required | Unit | Meaning |", "|---|---|---|---|---|"]
    for name, prop in schema.get("properties", {}).items():
        desc = (prop.get("description") or model.model_fields[name].description
                or FIELD_DOCS.get(name, "")).replace("|", "\\|")
        type_text = _type_of(prop).replace("|", "\\|")
        flag = "yes" if name in required else "no"
        rows.append(f"| `{name}` | {type_text} | {flag} | {_unit_hint(name, model)} | {desc} |")
    return "\n".join(rows)


def protocol_tables() -> str:
    """All payload field tables, used to (re)generate the tables section of PROTOCOL.md."""
    from netcoop.protocol import payloads as p

    parts = ["### Envelope", "", field_table(Envelope), ""]
    for message_type, payload in PAYLOAD_MODELS.items():
        parts += [f"### {message_type} (`{payload.SCHEMA}`)", "", field_table(payload), ""]
    parts += ["### Nested types", ""]
    for model in (p.TelemetryRecord, p.MetricValue, p.Provenance, p.IngestionReport, p.ConversionNote,
                  p.RejectionNote, p.EvidenceItem, p.HypothesisScore, p.Localization, p.AlternativeCause,
                  p.RecommendedAction, p.ConfidenceBreakdown, p.ConflictResolution, p.DataQuality,
                  p.DiagnosticReport, p.Skill):
        parts += [f"#### {model.__name__}", "", field_table(model), ""]
    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "schemas")
    args = parser.parse_args()
    paths = export_all(args.out)
    print(f"wrote {len(paths)} schema files to {args.out} ({len(MESSAGE_MODELS)} message types)")


if __name__ == "__main__":
    main()
