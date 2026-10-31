"""Typed payloads, one per message type. Each declares its `SCHEMA` name/version.

Versioning policy (see PROTOCOL.md): payload minor versions only ADD optional fields,
and every receiver ignores fields it does not know (must-ignore rule), so minors are
fully compatible in both directions. A breaking change bumps the major version and
ships a migration in `migrations.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, PlainSerializer

from netcoop.protocol.vocabulary import (
    PROTOCOL,
    ConfidenceCategory,
    DeviceStatus,
    DiagnosisStatus,
    ErrorCode,
    Hypothesis,
    MessageType,
    Quality,
    Unit,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _iso_z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


Timestamp = Annotated[AwareDatetime, PlainSerializer(_iso_z, return_type=str, when_used="json")]
Fraction = Annotated[float, Field(ge=0.0, le=1.0)]
Scalar = str | float | int | bool | None


class NCModel(BaseModel):
    """Base for every protocol model: unknown fields are ignored (forward compatibility)."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class Payload(NCModel):
    SCHEMA: ClassVar[str] = "payload/1.0"


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


class MappingMethod(StrEnum):
    DIRECT = "direct"
    ALIAS = "alias"
    FUZZY = "fuzzy"
    LLM_SUGGESTED = "llm_suggested"
    UNIT_CONVERSION = "unit_conversion"
    HEURISTIC = "heuristic"
    MIGRATION = "migration"
    IMPUTED = "imputed"


class Provenance(NCModel):
    original_field: str | None = Field(None, description="Field name as it appeared in the input")
    original_value: Scalar = Field(None, description="Value as it appeared in the input")
    original_unit: str | None = Field(None, description="Unit of the original value, if known")
    method: MappingMethod = Field(MappingMethod.DIRECT, description="How the canonical value was obtained")
    note: str | None = None


class MetricValue(NCModel):
    value: float | None = Field(None, description="Canonical value; null when missing or rejected")
    unit: Unit = Field(description="Canonical unit (see vocabulary)")
    quality: Quality = Quality.OK
    provenance: list[Provenance] = Field(default_factory=list)


class TelemetryRecord(NCModel):
    device_id: str
    timestamp: Timestamp
    status: DeviceStatus = DeviceStatus.UNKNOWN
    status_original: str | None = Field(None, description="Status string as supplied (e.g. 'Critical')")
    metrics: dict[str, MetricValue] = Field(default_factory=dict, description="Keyed by canonical metric name")
    unmapped: dict[str, Scalar] = Field(default_factory=dict, description="Fields we could not map")
    source_format: str = Field("canonical_v2", description="organizer_v1 | canonical_v2 | legacy_v1_1 | csv")
    record_quality: Quality = Quality.OK


class ConversionNote(NCModel):
    record_index: int | None = None
    device_id: str | None = None
    original_field: str
    canonical_field: str
    method: MappingMethod
    from_value: Scalar = None
    to_value: Scalar = None
    from_unit: str | None = None
    to_unit: str | None = None
    note: str | None = None
    count: int = Field(1, ge=1, description="Identical conversions are aggregated; this is how many")


class RejectionNote(NCModel):
    record_index: int | None = None
    device_id: str | None = None
    field: str | None = None
    code: ErrorCode
    reason: str
    record_rejected: bool = Field(False, description="True if the whole record was dropped")


class IngestionReport(NCModel):
    records_in: int = 0
    records_accepted: int = 0
    records_rejected: int = 0
    formats_detected: list[str] = Field(default_factory=list)
    schema_versions: list[str] = Field(default_factory=list)
    conversions: list[ConversionNote] = Field(default_factory=list)
    rejections: list[RejectionNote] = Field(default_factory=list)
    unmapped_fields: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    quality_counts: dict[str, int] = Field(default_factory=dict)
    data_coverage: Fraction = Field(0.0, description="Share of usable metric values among those expected")
    fatal: bool = Field(False, description="True if no usable record survived ingestion")


class TelemetryRaw(Payload):
    SCHEMA: ClassVar[str] = "telemetry_raw/1.0"
    source: str = Field("inline", description="File name, 'inline' or 'gateway'")
    content_type: Literal["auto", "json", "jsonl", "csv"] = "auto"
    content: str = Field(description="Raw text exactly as received")


class TelemetryNormalized(Payload):
    SCHEMA: ClassVar[str] = "telemetry_normalized/1.0"
    records: list[TelemetryRecord]
    report: IngestionReport


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


class Direction(StrEnum):
    ABOVE = "above"
    BELOW = "below"


class EvidenceItem(NCModel):
    metric: str = Field(description="Canonical metric name, or a derived signal such as 'topology'")
    value: float | None = None
    unit: Unit | None = None
    device_id: str | None = None
    threshold: float | None = Field(None, description="Threshold the value was compared against (same unit)")
    severity: Fraction = Field(0.0, description="0 = at/below warn, 1 = at/beyond crit (piecewise linear)")
    direction: Direction = Direction.ABOVE
    interpretation: str
    quality: Quality = Quality.OK
    source_agent: str = ""
    timestamp: Timestamp | None = None
    z_score: float | None = Field(None, description="Robust deviation from the device's own baseline (series only)")
    basis: Literal["threshold", "baseline"] = Field(
        "threshold", description="'baseline' = below the fixed threshold but abnormal versus the device's history")


class HypothesisScore(NCModel):
    """One agent's contribution to one hypothesis on one device.

    `raw` is this agent's share of raw(h, d) (README#confidence); the correlator sums the
    raw contributions of all agents and normalizes by Z(h). `score` is the agent's own
    normalized view (raw divided by the part of Z(h) the agent can observe).
    """

    hypothesis: Hypothesis
    device_id: str | None = Field(None, description="Device the score refers to; null = incident-wide")
    raw: float = Field(0.0, description="Unnormalized weighted evidence sum from this agent")
    score: float = Field(0.0, description="Agent-local normalized score")
    rationale: str = ""


class Claim(NCModel):
    """A machine-checkable assertion, used by the correlator to detect contradictions."""

    subject: str = Field(description="Usually a device_id")
    predicate: str = Field(description="e.g. 'interface_errors_above_threshold'")
    value: bool
    source_agent: str = ""


class Localization(NCModel):
    root_device: str | None = None
    symptomatic_devices: list[str] = Field(default_factory=list)
    affected_devices: list[str] = Field(default_factory=list)
    affected_segments: list[str] = Field(default_factory=list)
    partitioned_devices: list[str] = Field(default_factory=list)
    method: str = ""


class TaskAssignment(Payload):
    SCHEMA: ClassVar[str] = "task_assignment/1.0"
    task_id: str
    kind: Literal["analyze_telemetry", "resume_incident"] = "analyze_telemetry"
    deadline_s: float = Field(description="Seconds the agent has to reply with a FINDING")
    records: list[TelemetryRecord] = Field(description="Only the metrics this agent declared it consumes")
    metrics_of_interest: list[str] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)


class Finding(Payload):
    SCHEMA: ClassVar[str] = "finding/1.0"
    task_id: str
    agent: str
    agent_version: str = "1.0.0"
    hypothesis_scores: list[HypothesisScore] = Field(default_factory=list)
    top_hypothesis: Hypothesis | None = None
    evidence: list[EvidenceItem] = Field(default_factory=list)
    devices_flagged: list[str] = Field(default_factory=list)
    metrics_analyzed: list[str] = Field(default_factory=list)
    metrics_missing: list[str] = Field(default_factory=list)
    data_coverage: Fraction = 1.0
    claims: list[Claim] = Field(default_factory=list)
    localization: Localization | None = None
    limitations: list[str] = Field(default_factory=list)


class QueryPredicate(StrEnum):
    METRIC_ABOVE_THRESHOLD = "metric_above_threshold"
    FAULT_LOCALIZATION = "fault_localization"
    SERVICE_HEALTH = "service_health"
    PATH_HEALTHY = "path_healthy"
    DEVICE_STATUS = "device_status"


class Query(Payload):
    SCHEMA: ClassVar[str] = "query/1.0"
    query_id: str
    round: int = Field(1, ge=1)
    question: str = Field(description="Human-readable form of the question")
    predicate: QueryPredicate
    params: dict[str, Scalar] = Field(default_factory=dict)
    hypotheses_at_stake: list[Hypothesis] = Field(default_factory=list)


class Response(Payload):
    SCHEMA: ClassVar[str] = "response/1.0"
    query_id: str
    answer: bool | None = Field(None, description="null = cannot tell from available data")
    value: float | None = None
    unit: Unit | None = None
    explanation: str = ""
    evidence: list[EvidenceItem] = Field(default_factory=list)
    supports: list[Hypothesis] = Field(default_factory=list)
    refutes: list[Hypothesis] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Decision and report
# ---------------------------------------------------------------------------


class AlternativeCause(NCModel):
    cause: Hypothesis
    description: str = ""
    probability: Fraction
    why_less_likely: str = ""


class RecommendedAction(NCModel):
    action_id: str
    action: str
    hypothesis: Hypothesis | None = None
    priority: int = Field(1, ge=1)
    risk: Literal["low", "medium", "high"] = "low"
    safe_to_simulate: bool = True
    requires_approval: bool = True
    target_device: str | None = None
    simulated_result: str | None = None


class AgentFindingSummary(NCModel):
    agent: str
    top_hypothesis: Hypothesis | None = None
    score: float = 0.0
    evidence_count: int = 0
    responded: bool = True


class ConflictResolution(NCModel):
    conflict_detected: bool = False
    reasons: list[str] = Field(default_factory=list)
    rounds: int = 0
    queries: list[str] = Field(default_factory=list)
    resolution: str = ""


class DataQuality(NCModel):
    coverage: Fraction = 0.0
    records_in: int = 0
    conversions: int = 0
    rejected_records: int = 0
    unmapped_fields: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ConfidenceBreakdown(NCModel):
    p_top: Fraction = 0.0
    p_second: Fraction = 0.0
    margin: float = 0.0
    data_coverage: Fraction = 0.0
    agreement_factor: Fraction = 0.0
    combiner: str = "weighted_softmax"
    unknown_mass: Fraction | None = Field(None, description="Dempster-Shafer combiner only")
    method: str = "confidence = p_top * data_coverage * agreement_factor (see README#confidence)"


class SecondaryIncident(NCModel):
    root_cause_code: Hypothesis
    devices: list[str] = Field(default_factory=list)
    probability: Fraction = 0.0
    explanation: str = ""


class Diagnosis(Payload):
    SCHEMA: ClassVar[str] = "diagnosis/1.0"
    status: DiagnosisStatus
    root_cause_code: Hypothesis | None = None
    confidence: Fraction = 0.0
    confidence_category: ConfidenceCategory = ConfidenceCategory.LOW
    probabilities: dict[str, Fraction] = Field(default_factory=dict)
    root_device: str | None = None
    affected_devices: list[str] = Field(default_factory=list)
    symptomatic_devices: list[str] = Field(default_factory=list)
    affected_segments: list[str] = Field(default_factory=list)
    supporting_evidence: list[EvidenceItem] = Field(default_factory=list)
    contradicting_evidence: list[EvidenceItem] = Field(default_factory=list)
    alternative_causes: list[AlternativeCause] = Field(default_factory=list)
    agent_findings_summary: list[AgentFindingSummary] = Field(default_factory=list)
    conflict_resolution: ConflictResolution = Field(default_factory=ConflictResolution)
    data_quality: DataQuality = Field(default_factory=DataQuality)
    confidence_breakdown: ConfidenceBreakdown = Field(default_factory=ConfidenceBreakdown)
    secondary_incidents: list[SecondaryIncident] = Field(default_factory=list)
    next_data_to_collect: list[str] = Field(default_factory=list)
    evidence_sources: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class DiagnosticReport(NCModel):
    """Final report. A strict superset of the organizer's example (same field names)."""

    incident_id: str
    status: DiagnosisStatus
    suspected_root_cause: str
    root_cause_code: Hypothesis | None = None
    confidence: Fraction
    confidence_category: ConfidenceCategory
    affected_devices: list[str] = Field(default_factory=list)
    root_device: str | None = None
    symptomatic_devices: list[str] = Field(default_factory=list)
    affected_segments: list[str] = Field(default_factory=list)
    supporting_evidence: list[EvidenceItem] = Field(default_factory=list)
    contradicting_evidence: list[EvidenceItem] = Field(default_factory=list)
    alternative_causes: list[AlternativeCause] = Field(default_factory=list)
    recommended_actions: list[RecommendedAction] = Field(default_factory=list)
    evidence_sources: list[str] = Field(default_factory=list)
    agent_findings_summary: list[AgentFindingSummary] = Field(default_factory=list)
    conflict_resolution: ConflictResolution = Field(default_factory=ConflictResolution)
    data_quality: DataQuality = Field(default_factory=DataQuality)
    confidence_breakdown: ConfidenceBreakdown = Field(default_factory=ConfidenceBreakdown)
    secondary_incidents: list[SecondaryIncident] = Field(default_factory=list)
    next_data_to_collect: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    narrative: str = ""
    protocol_version: str = PROTOCOL
    generated_at: Timestamp = Field(default_factory=utcnow)
    message_log: str | None = None


class Report(Payload):
    SCHEMA: ClassVar[str] = "report/1.0"
    report: DiagnosticReport
    json_path: str | None = None
    markdown_path: str | None = None


# ---------------------------------------------------------------------------
# Control messages
# ---------------------------------------------------------------------------


class ErrorPayload(Payload):
    SCHEMA: ClassVar[str] = "error/1.0"
    code: ErrorCode
    message: str
    detail: dict[str, Scalar] = Field(default_factory=dict)
    offending_msg_id: str | None = None
    recoverable: bool = True


class Ack(Payload):
    SCHEMA: ClassVar[str] = "ack/1.0"
    acked_msg_id: str
    note: str = ""


class Heartbeat(Payload):
    SCHEMA: ClassVar[str] = "heartbeat/1.0"
    agent: str
    version: str
    state: Literal["starting", "ready", "busy", "stopping"] = "ready"
    uptime_s: float = 0.0


class Skill(NCModel):
    """A2A-style skill description, so each agent can be published as an Agent Card."""

    id: str
    name: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)


class CapabilityAnnounce(Payload):
    SCHEMA: ClassVar[str] = "capability_announce/1.0"
    agent: str
    version: str
    description: str = ""
    supported_protocols: list[str] = Field(default_factory=lambda: [PROTOCOL])
    consumes: list[MessageType] = Field(default_factory=list)
    produces: list[MessageType] = Field(default_factory=list)
    inputs: list[str] = Field(default_factory=list, description="Canonical metrics it needs in TASK_ASSIGNMENTs")
    metrics: list[str] = Field(default_factory=list, description="Metrics/signals it judges and answers QUERYs on")
    hypotheses: list[Hypothesis] = Field(default_factory=list, description="Hypotheses it can score")
    query_predicates: list[QueryPredicate] = Field(default_factory=list, description="QUERY types it answers")
    skills: list[Skill] = Field(default_factory=list)


class RemediationProposal(Payload):
    SCHEMA: ClassVar[str] = "remediation_proposal/1.0"
    incident_id: str
    actions: list[RecommendedAction]


class ApprovalRequest(Payload):
    SCHEMA: ClassVar[str] = "approval_request/1.0"
    incident_id: str
    action_id: str
    action: str
    risk: Literal["low", "medium", "high"] = "low"
    requested_from: str = "human_operator"


class ApprovalDecision(Payload):
    SCHEMA: ClassVar[str] = "approval_decision/1.0"
    incident_id: str
    action_id: str
    approved: bool
    decided_by: str = "human_operator"
    reason: str = ""
    simulated_result: str | None = None


PAYLOAD_MODELS: dict[MessageType, type[Payload]] = {
    MessageType.TASK_ASSIGNMENT: TaskAssignment,
    MessageType.TELEMETRY_RAW: TelemetryRaw,
    MessageType.TELEMETRY_NORMALIZED: TelemetryNormalized,
    MessageType.FINDING: Finding,
    MessageType.QUERY: Query,
    MessageType.RESPONSE: Response,
    MessageType.DIAGNOSIS: Diagnosis,
    MessageType.REPORT: Report,
    MessageType.ERROR: ErrorPayload,
    MessageType.ACK: Ack,
    MessageType.HEARTBEAT: Heartbeat,
    MessageType.CAPABILITY_ANNOUNCE: CapabilityAnnounce,
    MessageType.REMEDIATION_PROPOSAL: RemediationProposal,
    MessageType.APPROVAL_REQUEST: ApprovalRequest,
    MessageType.APPROVAL_DECISION: ApprovalDecision,
}
