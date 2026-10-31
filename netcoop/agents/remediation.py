"""Remediation agent (human-in-the-loop): REMEDIATION_PROPOSAL -> APPROVAL_REQUEST,
APPROVAL_DECISION -> simulated fix + re-diagnosis (only for approved actions) -> ACK.

SIMULATION ONLY: see netcoop/remediation/simulator.py. Nothing here touches a real device.
"""

from __future__ import annotations

from pathlib import Path

from netcoop.agents.base import BaseAgent
from netcoop.agents.registry import register_agent
from netcoop.bus.store import StateStore
from netcoop.config import Settings
from netcoop.protocol.envelope import BROADCAST, Envelope
from netcoop.protocol.payloads import (
    Ack,
    ApprovalDecision,
    ApprovalRequest,
    RecommendedAction,
    RemediationProposal,
    Skill,
    TelemetryNormalized,
)
from netcoop.protocol.vocabulary import ErrorCode, MessageType
from netcoop.remediation.simulator import SIMULATION_BANNER, simulate_and_rediagnose
from netcoop.topology import Topology, default_topology


@register_agent
class RemediationAgent(BaseAgent):
    name = "remediation_agent"
    description = "Turns proposed actions into approval requests and simulates only human-approved ones (dry run)."
    consumes = frozenset({MessageType.REMEDIATION_PROPOSAL, MessageType.APPROVAL_DECISION})
    produces = frozenset({MessageType.APPROVAL_REQUEST, MessageType.ACK, MessageType.ERROR})
    skills = (Skill(id="hitl-remediation", name="Human-approved simulated remediation",
                    description="Approval workflow; dry-run simulation and re-diagnosis", tags=["hitl", "simulation"]),)

    def __init__(self, bus, settings: Settings | None = None, topology: Topology | None = None,  # type: ignore[no-untyped-def]
                 store: StateStore | None = None, out_dir: Path | None = None) -> None:
        super().__init__(bus, settings)
        self.store = store or StateStore(":memory:")
        self.out_dir = out_dir
        try:
            self.topology: Topology | None = topology or default_topology()
        except (OSError, ValueError, KeyError):
            self.topology = None

    async def handle(self, envelope: Envelope) -> None:
        if envelope.type is MessageType.REMEDIATION_PROPOSAL:
            proposal: RemediationProposal = envelope.payload
            for action in proposal.actions:
                self.store.add_pending(proposal.incident_id, action.action_id, action.model_dump_json())
                await self.send(MessageType.APPROVAL_REQUEST,
                                ApprovalRequest(incident_id=proposal.incident_id, action_id=action.action_id,
                                                action=action.action, risk=action.risk),
                                correlation_id=envelope.correlation_id, to=BROADCAST, causation_id=envelope.msg_id)
        elif envelope.type is MessageType.APPROVAL_DECISION:
            await self._decide(envelope)

    async def _decide(self, envelope: Envelope) -> None:
        decision: ApprovalDecision = envelope.payload
        pending = {a["action_id"]: a for a in self.store.approvals(decision.incident_id)}
        row = pending.get(decision.action_id)
        if row is None:
            await self.send_error(ErrorCode.E_MISSING_REQUIRED,
                                  f"no proposed action {decision.action_id!r} for incident {decision.incident_id}",
                                  original=envelope)
            return
        if row["state"] != "pending":
            await self.reply(envelope, MessageType.ACK, Ack(acked_msg_id=envelope.msg_id,
                                                            note=f"already {row['state']}; nothing done"))
            return
        if not decision.approved:
            self.store.decide(decision.incident_id, decision.action_id, "rejected", decision.decided_by, decision.reason)
            await self.reply(envelope, MessageType.ACK, Ack(acked_msg_id=envelope.msg_id,
                                                            note="rejected by human: nothing simulated"))
            return
        self.store.decide(decision.incident_id, decision.action_id, "approved", decision.decided_by, decision.reason)
        self.spawn(self._simulate(envelope, decision, RecommendedAction.model_validate(row["action"])),
                   name=f"simulate:{decision.action_id}")

    async def _simulate(self, envelope: Envelope, decision: ApprovalDecision, action: RecommendedAction) -> None:
        raw = self.store.load(decision.incident_id, "normalized")
        result: dict
        if raw is None:
            note = "approved, but the incident telemetry is not in the state store: cannot simulate"
            result = {"banner": SIMULATION_BANNER, "summary": note}
        else:
            try:
                records = TelemetryNormalized.model_validate_json(raw).records
                n = sum(1 for a in self.store.approvals(decision.incident_id) if a["state"] == "simulated") + 1
                sim = await simulate_and_rediagnose(records, action, self.settings.playbook, self.topology,
                                                    incident_id=f"{decision.incident_id}-SIM{n}",
                                                    settings=self.settings, out_dir=self.out_dir)
                result, note = sim.to_dict(), sim.summary
            except Exception as exc:  # the human must always get an answer
                self.log.exception("simulation of %s failed", decision.action_id)
                note = f"approved, but the simulation failed: {type(exc).__name__}: {exc}"
                result = {"banner": SIMULATION_BANNER, "summary": note}
                await self.send_error(ErrorCode.E_INTERNAL, note, original=envelope)
        self.store.decide(decision.incident_id, decision.action_id, "simulated", decision.decided_by,
                          decision.reason, result)
        await self.reply(envelope, MessageType.ACK, Ack(acked_msg_id=envelope.msg_id, note=note))
