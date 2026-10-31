"""Recommendation & Reporting agent: DIAGNOSIS -> REPORT (JSON + Markdown) + REMEDIATION_PROPOSAL."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema

from netcoop.agents.base import BaseAgent
from netcoop.agents.registry import register_agent
from netcoop.bus.log import safe_name
from netcoop.bus.store import StateStore
from netcoop.config import Settings
from netcoop.protocol.envelope import Envelope
from netcoop.protocol.payloads import (
    Diagnosis,
    DiagnosticReport,
    RecommendedAction,
    RemediationProposal,
    Report,
    Skill,
)
from netcoop.protocol.vocabulary import HYPOTHESIS_DESCRIPTIONS, DiagnosisStatus, Hypothesis, MessageType

Narrator = Any  # callable(report) -> str | None, see netcoop.llm.narrator


@lru_cache(maxsize=1)
def report_schema() -> dict[str, Any]:
    return DiagnosticReport.model_json_schema()


def suspected_root_cause(d: Diagnosis) -> str:
    h = d.root_cause_code
    where = f" at {d.root_device}" if d.root_device else ""
    if d.status is DiagnosisStatus.FAILED:
        return "Diagnosis failed: no usable telemetry (see data_quality and limitations)"
    if d.status is DiagnosisStatus.NORMAL:
        return "No fault detected: network operating within expected ranges"
    desc = HYPOTHESIS_DESCRIPTIONS.get(h, "unknown cause") if h else "unknown cause"
    if d.status is DiagnosisStatus.INSUFFICIENT_EVIDENCE:
        drift = [e for e in d.supporting_evidence if e.basis == "baseline" and e.severity > 0]
        if drift and (h is None or h is Hypothesis.NORMAL):
            e = drift[0]
            return (f"Early warning: {e.metric} on {e.device_id} is drifting from its own baseline "
                    f"({e.value:g} {e.unit or ''}, still below alert thresholds); no root cause can be claimed yet")
        if h is None or h is Hypothesis.NORMAL:
            return "Insufficient evidence for a definitive root cause"
        return (f"Insufficient evidence for a definitive root cause; leading candidate: {desc[0].lower() + desc[1:]}"
                f"{where} (P={d.probabilities.get(h.value, 0):.2f})")
    return f"Likely {desc[0].lower() + desc[1:]}{where}"


def template_narrative(r: DiagnosticReport) -> str:
    lines = [f"{r.suspected_root_cause}."]
    if r.status in (DiagnosisStatus.DIAGNOSED, DiagnosisStatus.INSUFFICIENT_EVIDENCE):
        top = r.supporting_evidence[:3]
        if top:
            lines.append("Key evidence: " + "; ".join(f"{e.interpretation} on {e.device_id}" for e in top) + ".")
        if r.symptomatic_devices:
            lines.append(f"{len(r.symptomatic_devices)} downstream device(s) show symptoms but are not the cause: "
                         f"{', '.join(r.symptomatic_devices[:6])}.")
        if r.conflict_resolution.conflict_detected:
            lines.append(f"The agents initially disagreed; {r.conflict_resolution.resolution}.")
        if r.alternative_causes:
            alt = r.alternative_causes[0]
            lines.append(f"Main alternative: {alt.cause.value} (P={alt.probability:.2f}) - {alt.why_less_likely}.")
    lines.append(f"Confidence {r.confidence:.2f} ({r.confidence_category.value}) = P(top) "
                 f"{r.confidence_breakdown.p_top:.2f} x coverage {r.confidence_breakdown.data_coverage:.2f} x "
                 f"agreement {r.confidence_breakdown.agreement_factor:.2f}.")
    if r.next_data_to_collect:
        lines.append("To resolve the remaining uncertainty, collect: " + "; ".join(r.next_data_to_collect[:3]) + ".")
    return " ".join(lines)


def render_markdown(r: DiagnosticReport) -> str:
    out = [f"# Incident {r.incident_id}: {r.status.value}", "", f"**{r.suspected_root_cause}**", "",
           f"- Root cause code: `{r.root_cause_code.value if r.root_cause_code else 'n/a'}`",
           f"- Confidence: **{r.confidence:.2f}** ({r.confidence_category.value}) = P(top) {r.confidence_breakdown.p_top:.2f}"
           f" x coverage {r.confidence_breakdown.data_coverage:.2f} x agreement {r.confidence_breakdown.agreement_factor:.2f}",
           f"- Root device: {r.root_device or 'n/a'}",
           f"- Affected devices: {', '.join(r.affected_devices) or 'none identified'}",
           f"- Symptomatic (not causal): {', '.join(r.symptomatic_devices) or 'none'}",
           f"- Segments: {', '.join(r.affected_segments) or 'n/a'}", "", "## Summary", "", r.narrative, ""]
    if r.supporting_evidence:
        out += ["## Supporting evidence", "", "| Device | Metric | Value | Threshold | Interpretation | Source | Quality |",
                "|---|---|---|---|---|---|---|"]
        out += [f"| {e.device_id} | {e.metric} | {_v(e.value)} {e.unit or ''} | {_v(e.threshold)} | {e.interpretation} "
                f"| {e.source_agent} | {e.quality.value} |" for e in r.supporting_evidence]
        out.append("")
    if r.contradicting_evidence:
        out += ["## Contradicting evidence", ""]
        out += [f"- {e.device_id} {e.metric} = {_v(e.value)} {e.unit or ''}: {e.interpretation}" for e in r.contradicting_evidence]
        out.append("")
    if r.alternative_causes:
        out += ["## Alternative causes", "", "| Cause | P | Why less likely |", "|---|---|---|"]
        out += [f"| {a.cause.value} | {a.probability:.3f} | {a.why_less_likely} |" for a in r.alternative_causes]
        out.append("")
    if r.secondary_incidents:
        out += ["## Secondary incidents", ""]
        out += [f"- {s.root_cause_code.value} on {', '.join(s.devices[:4])}: {s.explanation}" for s in r.secondary_incidents]
        out.append("")
    if r.recommended_actions:
        out += ["## Recommended actions (human approval required; simulation only)", "",
                "| # | Action | Risk | Action id |", "|---|---|---|---|"]
        out += [f"| {a.priority} | {a.action} | {a.risk} | `{a.action_id}` |" for a in r.recommended_actions]
        out.append("")
    cr = r.conflict_resolution
    out += ["## Agent collaboration", "", f"- Conflict detected: {cr.conflict_detected}; rounds: {cr.rounds}",
            f"- Resolution: {cr.resolution}"]
    out += [f"- Query: {q}" for q in cr.queries]
    out += [f"- {s.agent}: top={s.top_hypothesis.value if s.top_hypothesis else 'none'} score={s.score:.2f} "
            f"evidence={s.evidence_count}{'' if s.responded else ' (no response)'}" for s in r.agent_findings_summary]
    dq = r.data_quality
    out += ["", "## Data quality", "", f"- Coverage {dq.coverage:.2f}; records {dq.records_in}; conversions "
            f"{dq.conversions}; rejected records {dq.rejected_records}"]
    out += [f"- Unmapped fields: {', '.join(dq.unmapped_fields)}"] if dq.unmapped_fields else []
    out += [f"- {w}" for w in dq.warnings]
    if r.next_data_to_collect:
        out += ["", "## Next data to collect", ""] + [f"- {n}" for n in r.next_data_to_collect]
    if r.limitations:
        out += ["", "## Limitations", ""] + [f"- {lim}" for lim in r.limitations]
    out += ["", f"_Protocol {r.protocol_version}; generated {r.generated_at.isoformat()}; message log: "
                f"`{r.message_log}`_", ""]
    return "\n".join(out)


def _v(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:g}" if abs(value) < 1e6 else f"{value:.3g}"


@register_agent
class ReportingAgent(BaseAgent):
    name = "reporting_agent"
    description = "Builds the final JSON/Markdown report, narrative and ranked remediation actions."
    consumes = frozenset({MessageType.DIAGNOSIS})
    produces = frozenset({MessageType.REPORT, MessageType.REMEDIATION_PROPOSAL})
    skills = (Skill(id="reporting", name="Diagnostic reporting",
                    description="Organizer-compatible JSON report, Markdown, narrative, playbook actions",
                    tags=["report", "remediation"]),)

    def __init__(self, bus, settings: Settings | None = None, out_dir: Path | None = None,  # type: ignore[no-untyped-def]
                 narrator: Narrator | None = None, store: StateStore | None = None) -> None:
        super().__init__(bus, settings)
        self.out_dir = out_dir
        self.narrator = narrator
        self.store = store
        self.reports: dict[str, DiagnosticReport] = {}

    async def handle(self, envelope: Envelope) -> None:
        if envelope.type is not MessageType.DIAGNOSIS:
            return
        report = self.build(envelope.correlation_id, envelope.payload)
        json_path, md_path = self.write(report)
        self.reports[report.incident_id] = report
        if self.store is not None:
            self.store.save(report.incident_id, "report", report.model_dump_json())
            self.store.set_phase(report.incident_id, "reported", status=report.status.value,
                                 root_cause=report.root_cause_code.value if report.root_cause_code else None,
                                 confidence=report.confidence)
        await self.send(MessageType.REPORT, Report(report=report, json_path=json_path, markdown_path=md_path),
                        correlation_id=envelope.correlation_id, causation_id=envelope.msg_id)
        if report.recommended_actions and report.status is not DiagnosisStatus.FAILED:
            await self.send(MessageType.REMEDIATION_PROPOSAL,
                            RemediationProposal(incident_id=report.incident_id, actions=report.recommended_actions),
                            correlation_id=envelope.correlation_id, causation_id=envelope.msg_id)

    def actions_for(self, d: Diagnosis) -> list[RecommendedAction]:
        playbook = self.settings.playbook
        device = d.root_device or "the affected device"
        entries: list[tuple[Hypothesis | None, dict[str, Any]]] = []
        if d.status is DiagnosisStatus.DIAGNOSED and d.root_cause_code:
            entries += [(d.root_cause_code, e) for e in playbook.get(d.root_cause_code.value, [])]
        elif d.status is DiagnosisStatus.INSUFFICIENT_EVIDENCE:
            entries += [(None, e) for e in playbook.get("insufficient_evidence", [])]
            for h in [d.root_cause_code, *(a.cause for a in d.alternative_causes[:1])]:
                if h is not None and h is not Hypothesis.NORMAL:
                    entries += [(h, e) for e in playbook.get(h.value, []) if e.get("risk") == "low"][:1]
        for sec in d.secondary_incidents:
            entries += [(sec.root_cause_code, {**e, "_device": sec.devices[0]})
                        for e in playbook.get(sec.root_cause_code.value, [])[:1]]
        actions: list[RecommendedAction] = []
        for i, (h, entry) in enumerate(entries, start=1):
            target = entry.get("_device", device)
            actions.append(RecommendedAction(
                action_id=f"{h.value if h else 'gather'}.{entry['id']}" + (f"@{target}" if target != device else ""),
                action=str(entry["action"]).format(device=target),
                hypothesis=h,
                priority=i,
                risk=entry.get("risk", "low"),
                safe_to_simulate=entry.get("simulate", "none") != "none",
                requires_approval=True,
                target_device=target if target != "the affected device" else None,
            ))
        return actions

    def build(self, incident_id: str, d: Diagnosis) -> DiagnosticReport:
        log_path = (self.out_dir / safe_name(incident_id) / "messages.jsonl") if self.out_dir else None
        report = DiagnosticReport(
            incident_id=incident_id,
            status=d.status,
            suspected_root_cause=suspected_root_cause(d),
            root_cause_code=d.root_cause_code,
            confidence=d.confidence,
            confidence_category=d.confidence_category,
            affected_devices=d.affected_devices,
            root_device=d.root_device,
            symptomatic_devices=d.symptomatic_devices,
            affected_segments=d.affected_segments,
            supporting_evidence=d.supporting_evidence,
            contradicting_evidence=d.contradicting_evidence,
            alternative_causes=d.alternative_causes,
            recommended_actions=self.actions_for(d),
            evidence_sources=d.evidence_sources,
            agent_findings_summary=d.agent_findings_summary,
            conflict_resolution=d.conflict_resolution,
            data_quality=d.data_quality,
            confidence_breakdown=d.confidence_breakdown,
            secondary_incidents=d.secondary_incidents,
            next_data_to_collect=d.next_data_to_collect,
            limitations=d.limitations,
            message_log=str(log_path) if log_path else None,
        )
        narrative = template_narrative(report)
        if self.narrator is not None:
            try:
                narrative = self.narrator(report) or narrative
            except Exception as exc:  # the LLM is garnish: fall back silently to the template
                self.log.warning("narrator failed, using template: %s", exc)
        report.narrative = narrative
        jsonschema.validate(json.loads(report.model_dump_json()), report_schema())
        return report

    def write(self, report: DiagnosticReport) -> tuple[str | None, str | None]:
        if self.out_dir is None:
            return None, None
        folder = self.out_dir / safe_name(report.incident_id)
        folder.mkdir(parents=True, exist_ok=True)
        json_path, md_path = folder / "report.json", folder / "report.md"
        json_path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
        md_path.write_text(render_markdown(report), encoding="utf-8")
        return str(json_path), str(md_path)
