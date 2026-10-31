"""Human-in-the-loop remediation: approval requests, rejection, simulated fix, re-diagnosis."""

from __future__ import annotations

import asyncio
import json

import pytest

from netcoop.bus.store import StateStore
from netcoop.pipeline import decide_action, run_incident_async, scenario_input
from netcoop.protocol.vocabulary import DiagnosisStatus


def _diagnose(scenario: str, store: StateStore, incident: str):  # type: ignore[no-untyped-def]
    records, _ = scenario_input(scenario, seed=2)
    return asyncio.run(run_incident_async(json.dumps(records), incident_id=incident, store=store))


@pytest.mark.parametrize(("scenario", "action_suffix"), [
    ("link_congestion", "reroute_traffic"),
    ("interface_fault", "bounce_port"),
    ("dns_failure", "dns_failover"),
    ("hardware_failure", "failover"),
    ("device_overload", "apply_copp"),
])
def test_approved_fix_is_simulated_and_rediagnosed_as_normal(scenario: str, action_suffix: str) -> None:
    store = StateStore(":memory:")
    result = _diagnose(scenario, store, f"INC-HITL-{scenario}")
    assert result.approval_requests, "every proposed action must become an APPROVAL_REQUEST"
    pending = store.approvals(result.incident_id)
    assert pending and all(a["state"] == "pending" for a in pending)
    action = next(a for a in pending if a["action_id"].split(".")[1].startswith(action_suffix))
    outcome = decide_action(result.incident_id, action["action_id"], True, store, reason="test")
    row = outcome["approval"]
    assert row["state"] == "simulated"
    assert "SIMULATION ONLY" in row["result"]["banner"]
    assert row["result"]["re_diagnosis"]["status"] == DiagnosisStatus.NORMAL.value, row["result"]["summary"]
    assert row["result"]["before"] and row["result"]["after"]


def test_rejected_action_is_never_simulated_and_decisions_are_final() -> None:
    store = StateStore(":memory:")
    result = _diagnose("link_congestion", store, "INC-HITL-REJ")
    action_id = store.approvals(result.incident_id)[0]["action_id"]
    out = decide_action(result.incident_id, action_id, False, store, reason="change freeze")
    assert out["approval"]["state"] == "rejected" and out["approval"]["result"] is None
    assert "nothing simulated" in out["note"]
    again = decide_action(result.incident_id, action_id, True, store)
    assert again["approval"]["state"] == "rejected" and "already" in again["note"]


def test_inspection_only_action_reports_no_simulated_effect() -> None:
    store = StateStore(":memory:")
    result = _diagnose("link_congestion", store, "INC-HITL-INSPECT")
    inspect_id = next(a["action_id"] for a in store.approvals(result.incident_id) if "inspect" in a["action_id"])
    row = decide_action(result.incident_id, inspect_id, True, store)["approval"]
    assert row["state"] == "simulated" and row["result"]["simulated"] is False


def test_unknown_action_gets_a_structured_error() -> None:
    store = StateStore(":memory:")
    result = _diagnose("dns_failure", store, "INC-HITL-UNKNOWN")
    out = decide_action(result.incident_id, "no.such_action", True, store, timeout_s=10)
    assert out["approval"] is None and out["note"].startswith("error")
