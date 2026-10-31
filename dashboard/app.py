"""NetCoop AI dashboard.  Run:  netcoop dashboard   (or: streamlit run dashboard/app.py)

SIMULATION ONLY: remediation actions are dry runs on synthetic telemetry.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from netcoop import viz  # noqa: E402
from netcoop.bus.store import StateStore, default_store_path  # noqa: E402
from netcoop.config import REPO_ROOT, get_settings, reports_dir  # noqa: E402
from netcoop.pipeline import RunResult, decide_action, diagnose, run_scenario  # noqa: E402
from netcoop.protocol.vocabulary import DiagnosisStatus, MessageType  # noqa: E402
from netcoop.sim.chaos import MUTATIONS  # noqa: E402
from netcoop.sim.generator import SCENARIOS  # noqa: E402
from netcoop.sim.organizer import ORGANIZER_FAULT_TYPES, generate_batch  # noqa: E402
from netcoop.topology import default_topology  # noqa: E402

st.set_page_config(page_title="NetCoop AI", page_icon="🛰️", layout="wide")
settings = get_settings()


@st.cache_resource
def store() -> StateStore:
    return StateStore(default_store_path())


def run(kind: str, **kw) -> None:  # type: ignore[no-untyped-def]
    with st.spinner("Agents are collaborating on the incident..."):
        common = {"store": store(), "out_dir": reports_dir()}
        if kind == "scenario":
            result = run_scenario(kw["name"], seed=kw.get("seed", 42), chaos=kw.get("chaos") or None, **common)
        else:
            result = diagnose(kw["raw"], source=kw.get("source", "dashboard"), **common)
    st.session_state["result"] = result
    st.session_state["sim"] = None


# -- sidebar: run ---------------------------------------------------------------------------------

st.sidebar.title("NetCoop AI")
st.sidebar.caption("Multi-agent network fault diagnosis · synthetic data · simulation only")
st.sidebar.subheader("Demo shortcuts")
demo_cols = st.sidebar.columns(2)
if demo_cols[0].button("Link congestion", key="demo_congestion", use_container_width=True):
    run("scenario", name="link_congestion")
if demo_cols[1].button("Organizer HW failure", key="demo_hw", use_container_width=True):
    run("raw", raw=generate_batch("hardware_failure", 1, seed=4)[0], source="organizer generator")
if demo_cols[0].button("Latency spike", key="demo_spike", use_container_width=True):
    run("raw", raw=generate_batch("latency_spike", 1, seed=4)[0], source="organizer generator")
if demo_cols[1].button("Chaos: interop", key="demo_chaos", use_container_width=True):
    run("scenario", name="hardware_failure", chaos=["rename_fields", "swap_units", "downgrade_schema"])

st.sidebar.subheader("Run an incident")
source = st.sidebar.radio("Input", ["Scenario", "Organizer generator", "Upload file", "Paste JSON"], key="source")
if source == "Scenario":
    name = st.sidebar.selectbox("Scenario", list(SCENARIOS), index=1, key="scenario")
    seed = st.sidebar.number_input("Seed", 0, 10_000, 42, key="seed")
    chaos = st.sidebar.multiselect("Chaos mutations", list(MUTATIONS), key="chaos")
    st.sidebar.caption(SCENARIOS[name].description)
    if st.sidebar.button("Diagnose", type="primary", key="run_scenario"):
        run("scenario", name=name, seed=int(seed), chaos=chaos)
elif source == "Organizer generator":
    fault = st.sidebar.selectbox("Fault type", ORGANIZER_FAULT_TYPES, index=2, key="fault")
    count = st.sidebar.slider("Records", 1, 10, 1, key="count")
    oseed = st.sidebar.number_input("Seed", 0, 10_000, 7, key="oseed")
    if st.sidebar.button("Diagnose", type="primary", key="run_org"):
        run("raw", raw=generate_batch(fault, count, seed=int(oseed)), source=f"organizer:{fault}")
elif source == "Upload file":
    up = st.sidebar.file_uploader("JSON, JSONL or CSV telemetry", type=["json", "jsonl", "csv", "txt"])
    if up is not None and st.sidebar.button("Diagnose", type="primary", key="run_upload"):
        run("raw", raw=up.getvalue(), source=up.name)
else:
    text = st.sidebar.text_area("Paste organizer output or any telemetry", height=180, key="paste")
    if text.strip() and st.sidebar.button("Diagnose", type="primary", key="run_paste"):
        run("raw", raw=text, source="pasted")

result: RunResult | None = st.session_state.get("result")

st.title("NetCoop AI · multi-agent network fault diagnosis")
if result is None:
    st.info("Pick a demo shortcut or run an incident from the sidebar. Six specialised agents analyse the "
            "telemetry, exchange structured messages on a versioned bus, resolve disagreements in a challenge "
            "round and produce one explainable report.")
if result is not None and result.report is None:
    st.error(f"System failure: {result.error}")

tabs = st.tabs(["Report", "Agent conversation", "Topology", "Telemetry", "Data quality", "Remediation (HITL)",
                "Evaluation"])

# -- report ---------------------------------------------------------------------------------------
with tabs[0]:
    if result is not None and result.report is not None:
        r = result.report
        banner = {DiagnosisStatus.DIAGNOSED: st.error, DiagnosisStatus.INSUFFICIENT_EVIDENCE: st.warning,
                  DiagnosisStatus.NORMAL: st.success, DiagnosisStatus.FAILED: st.error}[r.status]
        banner(f"**{r.status.value.upper().replace('_', ' ')}** · {r.suspected_root_cause}")
        left, right = st.columns([1, 2])
        with left:
            st.plotly_chart(viz.confidence_gauge(r, settings), use_container_width=True)
            cb = r.confidence_breakdown
            m1, m2, m3 = st.columns(3)
            m1.metric("P(top)", f"{cb.p_top:.2f}")
            m2.metric("coverage", f"{cb.data_coverage:.2f}")
            m3.metric("agreement", f"{cb.agreement_factor:.2f}")
            st.caption("confidence = P(top) × data coverage × agreement factor")
        with right:
            st.markdown(f"**Root device:** {r.root_device or '—'}  ·  **Incident:** `{r.incident_id}`")
            st.markdown(f"**Affected:** {', '.join(r.affected_devices) or '—'}")
            if r.symptomatic_devices:
                st.markdown(f"**Symptomatic, not causal:** {', '.join(r.symptomatic_devices)}")
            st.markdown(r.narrative)
            if r.conflict_resolution.conflict_detected:
                with st.expander(f"Challenge round ({r.conflict_resolution.rounds} round(s)) — "
                                 f"{r.conflict_resolution.resolution}", expanded=True):
                    for line in r.conflict_resolution.reasons:
                        st.markdown(f"- {line}")
                    for q in r.conflict_resolution.queries:
                        st.markdown(f"- ❓ {q}")
        if r.supporting_evidence:
            st.subheader("Supporting evidence")
            st.dataframe(viz.evidence_frame(r.supporting_evidence), use_container_width=True, hide_index=True)
        if r.contradicting_evidence:
            st.subheader("Contradicting evidence")
            st.dataframe(viz.evidence_frame(r.contradicting_evidence), use_container_width=True, hide_index=True)
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("Alternative causes")
            st.dataframe([{"cause": a.cause.value, "P": round(a.probability, 4), "why less likely": a.why_less_likely}
                          for a in r.alternative_causes], use_container_width=True, hide_index=True)
            if r.secondary_incidents:
                st.subheader("Secondary incidents")
                for s in r.secondary_incidents:
                    st.markdown(f"- **{s.root_cause_code.value}** — {s.explanation}")
        with c2:
            st.subheader("Recommended actions")
            st.dataframe([{"#": a.priority, "action": a.action, "risk": a.risk, "id": a.action_id}
                          for a in r.recommended_actions], use_container_width=True, hide_index=True)
            if r.next_data_to_collect:
                st.subheader("Next data to collect")
                for n in r.next_data_to_collect:
                    st.markdown(f"- {n}")
        if r.limitations:
            with st.expander("Limitations"):
                for lim in r.limitations:
                    st.markdown(f"- {lim}")
        from netcoop.agents.reporting import render_markdown

        d1, d2 = st.columns(2)
        d1.download_button("Download JSON report", r.model_dump_json(indent=2), f"{r.incident_id}.json",
                           "application/json")
        d2.download_button("Download Markdown report", render_markdown(r), f"{r.incident_id}.md", "text/markdown")

# -- conversation ---------------------------------------------------------------------------------
with tabs[1]:
    if result is not None and result.messages:
        msgs = [m for m in result.messages if m.type is not MessageType.HEARTBEAT]
        st.plotly_chart(viz.conversation_figure(msgs), use_container_width=True)
        table = viz.conversation_table(msgs)
        st.dataframe(table.drop(columns=["msg_id"]), use_container_width=True, hide_index=True)
        pick = st.number_input("Show raw message #", 0, max(len(msgs) - 1, 0), 0, key="raw_pick")
        st.json(json.loads(msgs[int(pick)].to_wire()), expanded=False)

# -- topology -------------------------------------------------------------------------------------
records = []
if result is not None:
    for m in result.messages:
        if m.type is MessageType.TELEMETRY_NORMALIZED:
            records = m.payload.records
with tabs[2]:
    if result is not None:
        try:
            topo = default_topology()
            states = viz.device_states(result.report, records)
            st.plotly_chart(viz.topology_figure(topo, states), use_container_width=True)
            st.caption("Node colour and legend label give the device state; grey = no telemetry in this incident.")
            unknown = sorted({r.device_id for r in records} - set(topo.nodes))
            if unknown:
                st.warning(f"Devices not in the topology: {', '.join(unknown)}")
        except (OSError, ValueError) as exc:
            st.warning(f"Topology unavailable: {exc}")

# -- telemetry ------------------------------------------------------------------------------------
with tabs[3]:
    if records:
        frame = viz.telemetry_frame(records)
        metrics = sorted(frame["metric"].unique())
        rep = result.report if result else None
        default_devices = [d for d in [rep.root_device if rep else None, *(rep.symptomatic_devices[:2] if rep else [])]
                           if d] or sorted(frame["device"].unique())[:3]
        evid = [e.metric for e in (rep.supporting_evidence if rep else []) if e.metric in metrics]
        c1, c2 = st.columns([1, 2])
        metric = c1.selectbox("Metric", metrics, index=metrics.index(evid[0]) if evid else 0, key="metric")
        devices = c2.multiselect("Devices", sorted(frame["device"].unique()),
                                 default=[d for d in default_devices if d in set(frame["device"])], key="devices")
        st.plotly_chart(viz.telemetry_figure(frame, metric, devices, settings), use_container_width=True)
        with st.expander("Table view"):
            st.dataframe(frame[(frame["metric"] == metric) & (frame["device"].isin(devices))],
                         use_container_width=True, hide_index=True)

# -- data quality ---------------------------------------------------------------------------------
with tabs[4]:
    if result is not None and result.ingestion is not None:
        rep_i = result.ingestion
        c = st.columns(5)
        c[0].metric("records in", rep_i.records_in)
        c[1].metric("accepted", rep_i.records_accepted)
        c[2].metric("rejected", rep_i.records_rejected)
        c[3].metric("coverage", f"{rep_i.data_coverage:.2f}")
        c[4].metric("conversions", sum(x.count for x in rep_i.conversions))
        st.caption(f"Formats: {', '.join(rep_i.formats_detected) or '-'} · schema versions: "
                   f"{', '.join(rep_i.schema_versions) or '-'} · quality: {rep_i.quality_counts}")
        conv, rej = viz.ingestion_frames(rep_i)
        st.subheader("Conversions (aliases, units, migrations)")
        st.dataframe(conv, use_container_width=True, hide_index=True)
        st.subheader("Rejections and warnings (protocol error codes)")
        st.dataframe(rej, use_container_width=True, hide_index=True)
        if rep_i.unmapped_fields:
            st.warning(f"Unmapped fields kept aside: {', '.join(rep_i.unmapped_fields)}")
        for w in rep_i.warnings:
            st.caption(f"⚠ {w}")

# -- remediation ----------------------------------------------------------------------------------
with tabs[5]:
    st.warning("SIMULATION ONLY — approving an action edits a copy of the synthetic telemetry and re-runs the "
               "diagnosis. Nothing ever touches a real device.")
    if result is not None and result.report is not None:
        approvals = store().approvals(result.incident_id)
        if not approvals:
            st.info("No actions proposed for this incident.")
        for a in approvals:
            act = a["action"]
            with st.container(border=True):
                cols = st.columns([5, 1, 1, 1])
                cols[0].markdown(f"**{act['action']}**  \n`{a['action_id']}` · risk **{act['risk']}** · "
                                 f"state **{a['state']}**")
                if a["state"] == "pending":
                    if cols[1].button("Approve", key=f"ok_{a['action_id']}", type="primary"):
                        out = decide_action(result.incident_id, a["action_id"], True, store(),
                                            reason="approved in dashboard", out_dir=reports_dir())
                        st.session_state["sim"] = out
                        st.rerun()
                    if cols[2].button("Reject", key=f"no_{a['action_id']}"):
                        decide_action(result.incident_id, a["action_id"], False, store(),
                                      reason="rejected in dashboard", out_dir=reports_dir())
                        st.rerun()
                res = a.get("result")
                if res:
                    st.caption(res.get("summary", ""))
                    if res.get("before"):
                        st.dataframe([{"device": d, "metric": m, "before": v, "after": res["after"][d].get(m)}
                                      for d, mv in res["before"].items() for m, v in mv.items()],
                                     use_container_width=True, hide_index=True)
                    rd = res.get("re_diagnosis")
                    if rd:
                        (st.success if rd["status"] == "normal" else st.warning)(
                            f"Re-diagnosis {rd['incident_id']}: **{rd['status']}** — {rd['suspected_root_cause']}")

# -- evaluation -----------------------------------------------------------------------------------
with tabs[6]:
    eval_dir = REPO_ROOT / "reports" / "eval"
    summary_path = eval_dir / "summary.json"
    if summary_path.exists():
        summary = json.loads(summary_path.read_text())
        h = summary["headline"]
        c = st.columns(6)
        c[0].metric("runs", h["runs"])
        c[1].metric("correct or abstain", f"{h['correct_or_abstain']:.1%}")
        c[2].metric("high-conf. wrong", h["high_confidence_wrong"])
        c[3].metric("false alarms", h["normal_false_alarms"])
        c[4].metric("ECE", f"{h['ece']:.3f}")
        c[5].metric("Brier", f"{h['brier']:.3f}")
        i1, i2 = st.columns(2)
        if (eval_dir / "confusion_matrix.png").exists():
            i1.image(str(eval_dir / "confusion_matrix.png"))
        if (eval_dir / "reliability.png").exists():
            i2.image(str(eval_dir / "reliability.png"))
        st.dataframe([{"group": k, **v} for k, v in summary["groups"].items()], use_container_width=True,
                     hide_index=True)
    else:
        st.info("No evaluation yet: run `netcoop eval --seeds 50`.")
