from __future__ import annotations

from datetime import UTC, datetime

import pytest

from netcoop.protocol.payloads import (
    Ack,
    AlternativeCause,
    ApprovalDecision,
    ApprovalRequest,
    CapabilityAnnounce,
    Diagnosis,
    DiagnosticReport,
    ErrorPayload,
    EvidenceItem,
    Finding,
    Heartbeat,
    HypothesisScore,
    IngestionReport,
    MetricValue,
    Payload,
    Query,
    QueryPredicate,
    RecommendedAction,
    RemediationProposal,
    Report,
    Response,
    TaskAssignment,
    TelemetryNormalized,
    TelemetryRaw,
    TelemetryRecord,
)
from netcoop.protocol.vocabulary import (
    ConfidenceCategory,
    DeviceStatus,
    DiagnosisStatus,
    ErrorCode,
    Hypothesis,
    MessageType,
    Unit,
)


def _record() -> TelemetryRecord:
    return TelemetryRecord(
        device_id="SW_CAMPUS_4",
        timestamp=datetime(2026, 10, 14, 7, 5, 49, tzinfo=UTC),
        status=DeviceStatus.DOWN,
        status_original="Critical",
        metrics={"latency_ms": MetricValue(value=926.46, unit=Unit.MS)},
        source_format="organizer_v1",
    )


def _evidence() -> EvidenceItem:
    return EvidenceItem(
        metric="interface_errors",
        value=382,
        unit=Unit.COUNT,
        device_id="SW_CAMPUS_4",
        threshold=100,
        severity=1.0,
        interpretation="Interface errors far above threshold",
        source_agent="infrastructure_agent",
    )


def sample_payloads() -> dict[MessageType, Payload]:
    report = DiagnosticReport(
        incident_id="INC-0001",
        status=DiagnosisStatus.DIAGNOSED,
        suspected_root_cause="Device hardware failure",
        root_cause_code=Hypothesis.HARDWARE_FAILURE,
        confidence=0.7,
        confidence_category=ConfidenceCategory.MEDIUM,
        supporting_evidence=[_evidence()],
        alternative_causes=[AlternativeCause(cause=Hypothesis.LINK_CONGESTION, probability=0.1)],
    )
    action = RecommendedAction(action_id="failover", action="Fail over", hypothesis=Hypothesis.HARDWARE_FAILURE)
    return {
        MessageType.TASK_ASSIGNMENT: TaskAssignment(task_id="T1", deadline_s=3.0, records=[_record()]),
        MessageType.TELEMETRY_RAW: TelemetryRaw(content='{"device_id": "SW_CAMPUS_1"}'),
        MessageType.TELEMETRY_NORMALIZED: TelemetryNormalized(records=[_record()], report=IngestionReport(records_in=1)),
        MessageType.FINDING: Finding(
            task_id="T1",
            agent="infrastructure_agent",
            hypothesis_scores=[HypothesisScore(hypothesis=Hypothesis.HARDWARE_FAILURE, device_id="SW_CAMPUS_4", raw=3.2, score=0.8)],
            top_hypothesis=Hypothesis.HARDWARE_FAILURE,
            evidence=[_evidence()],
        ),
        MessageType.QUERY: Query(
            query_id="Q1",
            question="Are interface_errors on SW_CAMPUS_4 above threshold?",
            predicate=QueryPredicate.METRIC_ABOVE_THRESHOLD,
            params={"device_id": "SW_CAMPUS_4", "metric": "interface_errors"},
        ),
        MessageType.RESPONSE: Response(query_id="Q1", answer=True, value=382, unit=Unit.COUNT),
        MessageType.DIAGNOSIS: Diagnosis(status=DiagnosisStatus.DIAGNOSED, root_cause_code=Hypothesis.HARDWARE_FAILURE),
        MessageType.REPORT: Report(report=report),
        MessageType.ERROR: ErrorPayload(code=ErrorCode.E_OUT_OF_RANGE, message="packet_loss_pct=140 > 100"),
        MessageType.ACK: Ack(acked_msg_id="abc"),
        MessageType.HEARTBEAT: Heartbeat(agent="x", version="1.0.0"),
        MessageType.CAPABILITY_ANNOUNCE: CapabilityAnnounce(agent="x", version="1.0.0", metrics=["latency_ms"]),
        MessageType.REMEDIATION_PROPOSAL: RemediationProposal(incident_id="INC-0001", actions=[action]),
        MessageType.APPROVAL_REQUEST: ApprovalRequest(incident_id="INC-0001", action_id="failover", action="Fail over"),
        MessageType.APPROVAL_DECISION: ApprovalDecision(incident_id="INC-0001", action_id="failover", approved=True),
    }


@pytest.fixture
def payloads() -> dict[MessageType, Payload]:
    return sample_payloads()
