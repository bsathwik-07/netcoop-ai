"""Persistent workflow state: pause after a phase, resume later (new bus, new agents, same store)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from netcoop.bus.store import StateStore
from netcoop.pipeline import resume_incident, run_incident_async, scenario_input
from netcoop.protocol.vocabulary import DiagnosisStatus, MessageType


def _start(store: StateStore, incident: str, pause_after: str, out_dir: Path | None = None):  # type: ignore[no-untyped-def]
    records, _ = scenario_input("hardware_failure", seed=3)
    return asyncio.run(run_incident_async(json.dumps(records), incident_id=incident, store=store,
                                          pause_after=pause_after, out_dir=out_dir))


def test_pause_after_ingestion_then_resume_in_a_new_process(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite"
    paused = _start(StateStore(db), "INC-PAUSE1", "ingestion", tmp_path)
    assert paused.report is None and paused.paused == "paused after ingestion"
    assert not any(m.type is MessageType.TASK_ASSIGNMENT for m in paused.messages)  # stopped before analysis
    info = StateStore(db).incident("INC-PAUSE1")
    assert info["phase"] == "paused" and info["paused_after"] == "ingestion"

    resumed = resume_incident("INC-PAUSE1", StateStore(db), out_dir=tmp_path)  # fresh store connection + agents
    r = resumed.report
    assert r is not None and r.status is DiagnosisStatus.DIAGNOSED and r.root_cause_code.value == "hardware_failure"
    assert any("resumed from checkpoint" in lim for lim in r.limitations)
    assert StateStore(db).incident("INC-PAUSE1")["phase"] == "reported"
    log = (tmp_path / "INC-PAUSE1" / "messages.jsonl").read_text().splitlines()
    types = [json.loads(line)["type"] for line in log]
    assert types.count("TELEMETRY_RAW") == 2 and "TASK_ASSIGNMENT" in types and types[-1] in ("REPORT", "APPROVAL_REQUEST",
                                                                                               "REMEDIATION_PROPOSAL")


def test_pause_after_correlation_resumes_with_the_same_diagnosis(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "s.sqlite")
    paused = _start(store, "INC-PAUSE2", "correlation")
    assert paused.paused == "paused after correlation"
    saved = json.loads(store.load("INC-PAUSE2", "diagnosis"))
    resumed = resume_incident("INC-PAUSE2", store)
    assert resumed.report is not None
    assert resumed.report.root_cause_code.value == saved["root_cause_code"]
    assert resumed.report.confidence == saved["confidence"]


def test_resume_without_checkpoint_is_a_clear_failure() -> None:
    result = resume_incident("INC-NOPE", StateStore(":memory:"))
    assert result.report is not None and result.report.status is DiagnosisStatus.FAILED
    assert any("no checkpoint" in lim for lim in result.report.limitations)
