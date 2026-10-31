"""Dashboard smoke tests (Streamlit AppTest) and the figure helpers."""

from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

from netcoop import viz
from netcoop.config import REPO_ROOT, get_settings
from netcoop.pipeline import run_scenario
from netcoop.protocol.vocabulary import MessageType
from netcoop.topology import default_topology

APP = str(Path(REPO_ROOT) / "dashboard" / "app.py")


def test_dashboard_renders_empty_state() -> None:
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    assert any("Pick a demo shortcut" in i.value for i in at.info)


def test_dashboard_runs_a_demo_incident_without_errors(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("NETCOOP_REPORTS_DIR", str(tmp_path))
    at = AppTest.from_file(APP, default_timeout=120).run()
    at.button(key="demo_hw").click().run()
    assert not at.exception, at.exception
    assert any("DIAGNOSED" in e.value for e in at.error)  # status banner


def test_figures_build_from_a_real_run() -> None:
    result = run_scenario("link_congestion", seed=1)
    msgs = [m for m in result.messages if m.type is not MessageType.HEARTBEAT]
    assert len(viz.conversation_figure(msgs).data) == len(msgs)
    records = [m.payload.records for m in result.messages if m.type is MessageType.TELEMETRY_NORMALIZED][0]
    states = viz.device_states(result.report, records)
    assert states["HOSTEL_CORE_SW_01"] == "root cause" and states["SW_CAMPUS_1"] == "symptomatic"
    assert viz.topology_figure(default_topology(), states).data
    frame = viz.telemetry_frame(records)
    fig = viz.telemetry_figure(frame, "link_utilization_pct", ["HOSTEL_CORE_SW_01"], get_settings())
    assert fig.data and fig.layout.shapes  # series plus threshold lines
    assert not viz.conversation_table(msgs).empty
