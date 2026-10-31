"""Figures and tables for the dashboard (pure functions: testable without Streamlit).

Colors follow a validated reference palette: categorical slots in fixed order for identity,
reserved status colors (always paired with a text label) for device state.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import pandas as pd
import plotly.graph_objects as go

from netcoop.config import Settings
from netcoop.protocol.envelope import Envelope
from netcoop.protocol.payloads import DiagnosticReport, IngestionReport, TelemetryRecord
from netcoop.protocol.vocabulary import METRICS, MessageType
from netcoop.topology import Topology

SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
STATUS = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b"}
MUTED, GRID, NO_DATA = "#898781", "#e1e0d9", "#c3c2b7"

MESSAGE_GROUPS: dict[str, tuple[str, str]] = {  # message type -> (group label, color)
    "TELEMETRY_RAW": ("data", SERIES[0]), "TELEMETRY_NORMALIZED": ("data", SERIES[0]),
    "TASK_ASSIGNMENT": ("task / finding", SERIES[1]), "FINDING": ("task / finding", SERIES[1]),
    "QUERY": ("challenge round", SERIES[2]), "RESPONSE": ("challenge round", SERIES[2]),
    "DIAGNOSIS": ("decision / report", SERIES[3]), "REPORT": ("decision / report", SERIES[3]),
    "REMEDIATION_PROPOSAL": ("approval (HITL)", SERIES[4]), "APPROVAL_REQUEST": ("approval (HITL)", SERIES[4]),
    "APPROVAL_DECISION": ("approval (HITL)", SERIES[4]), "ACK": ("approval (HITL)", SERIES[4]),
    "ERROR": ("error", STATUS["critical"]),
}
LANES = ["operator", "correlation_agent", "ingestion_agent", "performance_agent", "infrastructure_agent",
         "connectivity_agent", "services_agent", "reporting_agent", "remediation_agent", "broadcast"]


def _layout(fig: go.Figure, title: str, height: int = 420) -> go.Figure:
    fig.update_layout(title={"text": title, "x": 0, "y": 0.985, "yanchor": "top"}, height=height,
                      margin={"l": 10, "r": 10, "t": 92, "b": 10},
                      legend={"orientation": "h", "yanchor": "bottom", "y": 1.01, "x": 0},
                      hoverlabel={"namelength": -1})
    fig.update_xaxes(gridcolor=GRID, zeroline=False)
    fig.update_yaxes(gridcolor=GRID, zeroline=False)
    return fig


# -- conversation -------------------------------------------------------------------------------


def summarize(env: Envelope) -> str:
    p, t = env.payload, env.type
    try:
        if t is MessageType.FINDING:
            return f"top={p.top_hypothesis.value if p.top_hypothesis else 'none'}, {len(p.evidence)} evidence items"
        if t is MessageType.QUERY:
            return p.question
        if t is MessageType.RESPONSE:
            return f"answer={p.answer}: {p.explanation}"
        if t is MessageType.ERROR:
            return f"{p.code.value}: {p.message}"
        if t is MessageType.TASK_ASSIGNMENT:
            return f"{p.kind}: {len(p.records)} records"
        if t is MessageType.TELEMETRY_NORMALIZED:
            return f"{p.report.records_accepted}/{p.report.records_in} records, coverage {p.report.data_coverage:.2f}"
        if t is MessageType.DIAGNOSIS:
            return f"{p.status.value} {p.root_cause_code.value if p.root_cause_code else ''} conf={p.confidence:.2f}"
        if t is MessageType.REPORT:
            return f"report: {p.report.status.value}"
        if t is MessageType.APPROVAL_REQUEST:
            return f"approve? {p.action_id} (risk {p.risk})"
        if t is MessageType.ACK:
            return p.note
        if t is MessageType.TELEMETRY_RAW:
            return f"{len(p.content)} chars from {p.source}"
    except AttributeError:
        pass
    return ""


def conversation_table(messages: list[Envelope]) -> pd.DataFrame:
    start = messages[0].timestamp if messages else None
    rows = [{
        "#": i, "t (ms)": round((m.timestamp - start).total_seconds() * 1000, 1) if start else 0,
        "type": m.type.value, "from": m.from_agent, "to": m.to_agent,
        "group": MESSAGE_GROUPS.get(m.type.value, ("other", MUTED))[0], "summary": summarize(m),
        "msg_id": m.msg_id, "in reply to": m.causation_id or "",
    } for i, m in enumerate(messages)]
    return pd.DataFrame(rows)


def conversation_figure(messages: list[Envelope]) -> go.Figure:
    """Sequence diagram: one lane per agent, one arrow per message, colored by message group."""
    lanes = [lane for lane in LANES if any(lane in (m.from_agent, m.to_agent) for m in messages)]
    lanes += sorted({a for m in messages for a in (m.from_agent, m.to_agent)} - set(lanes))
    y = {lane: i for i, lane in enumerate(reversed(lanes))}
    fig = go.Figure()
    seen: set[str] = set()
    for i, m in enumerate(messages):
        group, color = MESSAGE_GROUPS.get(m.type.value, ("other", MUTED))
        y0, y1 = y[m.from_agent], y.get(m.to_agent, y.get("broadcast", 0))
        fig.add_trace(go.Scatter(
            x=[i, i], y=[y0, y1], mode="lines+markers", line={"color": color, "width": 2},
            marker={"size": [8, 11], "symbol": ["circle", "triangle-down" if y1 < y0 else "triangle-up"],
                    "color": color},
            name=group, legendgroup=group, showlegend=group not in seen,
            hovertemplate=f"<b>#{i} {m.type.value}</b><br>{m.from_agent} → {m.to_agent}<br>"
                          f"{summarize(m)[:140]}<extra></extra>"))
        seen.add(group)
    challenge = [i for i, m in enumerate(messages) if m.type in (MessageType.QUERY, MessageType.RESPONSE)]
    if challenge:
        fig.add_vrect(x0=min(challenge) - 0.5, x1=max(challenge) + 0.5, fillcolor=SERIES[2], opacity=0.08,
                      line_width=0, annotation_text="challenge round", annotation_position="top left")
    fig.update_yaxes(tickvals=list(y.values()), ticktext=list(y.keys()))
    fig.update_xaxes(title="message order")
    return _layout(fig, "Agent conversation (sequence diagram)", height=120 + 42 * len(lanes))


# -- topology -----------------------------------------------------------------------------------


def device_states(report: DiagnosticReport | None, records: list[TelemetryRecord]) -> dict[str, str]:
    with_data = {r.device_id for r in records}
    states = {d: "healthy" for d in with_data}
    if report is None:
        return states
    for ev in [*report.supporting_evidence, *report.contradicting_evidence]:
        if ev.device_id and ev.severity > 0 and ev.basis == "baseline":
            states[ev.device_id] = "early warning"
    for d in report.symptomatic_devices:
        states[d] = "symptomatic"
    for s in report.secondary_incidents:
        for d in s.devices[:1]:
            states[d] = "secondary root"
    if report.root_device and report.status.value != "normal":
        states[report.root_device] = "root cause" if report.status.value == "diagnosed" else "suspect"
    return states


STATE_STYLE = {
    "root cause": STATUS["critical"], "suspect": STATUS["serious"], "secondary root": STATUS["critical"],
    "symptomatic": STATUS["serious"], "early warning": STATUS["warning"], "healthy": STATUS["good"],
    "no data": NO_DATA,
}


def _tree_positions(topo: Topology) -> dict[str, tuple[float, float]]:
    roots = [n for n, node in topo.nodes.items() if not node.parent]
    pos: dict[str, tuple[float, float]] = {}
    cursor = [0.0]

    def place(node: str, depth: int) -> float:
        kids = topo.children(node)
        if not kids:
            x = cursor[0]
            cursor[0] += 1.0
        else:
            xs = [place(k, depth + 1) for k in kids]
            x = sum(xs) / len(xs)
        pos[node] = (x, -depth)
        return x

    for r in roots:
        place(r, 0)
    return pos


def _wrap_label(node_id: str) -> str:
    """HOSTEL_A_AP_01 -> 'HOSTEL A<br>AP 01', so neighbouring leaf labels do not collide."""
    parts = node_id.split("_")
    if len(parts) <= 2:
        return " ".join(parts)
    half = (len(parts) + 1) // 2
    return " ".join(parts[:half]) + "<br>" + " ".join(parts[half:])


def topology_figure(topo: Topology, states: dict[str, str]) -> go.Figure:
    pos = _tree_positions(topo)
    fig = go.Figure()
    ex, ey = [], []
    for child, node in topo.nodes.items():
        if node.parent:
            (x0, y0), (x1, y1) = pos[node.parent], pos[child]
            ex += [x0, x1, None]
            ey += [y0, y1, None]
    fig.add_trace(go.Scatter(x=ex, y=ey, mode="lines", line={"color": NO_DATA, "width": 1.5},
                             hoverinfo="skip", showlegend=False))
    by_state: dict[str, list[str]] = defaultdict(list)
    for n in topo.nodes:
        by_state[states.get(n, "no data")].append(n)
    for state in STATE_STYLE:
        nodes = by_state.get(state)
        if not nodes:
            continue
        fig.add_trace(go.Scatter(
            x=[pos[n][0] for n in nodes], y=[pos[n][1] for n in nodes], mode="markers+text",
            marker={"size": 18 if state in ("root cause", "secondary root", "suspect") else 13,
                    "color": STATE_STYLE[state], "line": {"color": "#ffffff", "width": 2}},
            text=[_wrap_label(n) for n in nodes], textposition="bottom center", textfont={"size": 9},
            name=state, customdata=[[topo.nodes[n].type, topo.nodes[n].zone] for n in nodes],
            hovertemplate="<b>%{text}</b><br>%{customdata[0]} · %{customdata[1]}<br>" + state + "<extra></extra>"))
    fig.update_xaxes(visible=False)
    deepest = min(y for _, y in pos.values())
    fig.update_yaxes(visible=False, range=[deepest - 0.7, 0.4])  # room for the bottom row's labels
    return _layout(fig, "Campus topology (state from the diagnosis)", height=540)


# -- telemetry ----------------------------------------------------------------------------------


def telemetry_frame(records: list[TelemetryRecord]) -> pd.DataFrame:
    rows = [{"device": r.device_id, "timestamp": r.timestamp, "metric": m, "value": v.value, "quality": v.quality.value}
            for r in records for m, v in r.metrics.items() if v.value is not None]
    return pd.DataFrame(rows, columns=["device", "timestamp", "metric", "value", "quality"])


def telemetry_figure(frame: pd.DataFrame, metric: str, devices: list[str], settings: Settings) -> go.Figure:
    fig = go.Figure()
    data = frame[(frame["metric"] == metric) & (frame["device"].isin(devices))]
    for i, device in enumerate(devices[:8]):
        d = data[data["device"] == device].sort_values("timestamp")
        if d.empty:
            continue
        fig.add_trace(go.Scatter(x=d["timestamp"], y=d["value"], mode="lines+markers", name=device,
                                 line={"color": SERIES[i % len(SERIES)], "width": 2}, marker={"size": 6},
                                 hovertemplate=f"{device}<br>%{{x|%H:%M}}: %{{y:.4g}} {METRICS[metric].unit.value}"
                                               "<extra></extra>"))
    t = settings.thresholds.get(metric)
    if t is not None:
        fig.add_hline(y=t.warn, line_dash="dash", line_color=STATUS["warning"], annotation_text=f"warn {t.warn:g}",
                      annotation_position="top left")
        fig.add_hline(y=t.crit, line_dash="dash", line_color=STATUS["critical"], annotation_text=f"crit {t.crit:g}",
                      annotation_position="top left")
    fig.update_xaxes(title="time (UTC)")
    fig.update_yaxes(title=f"{metric} ({METRICS[metric].unit.value})")
    return _layout(fig, f"{metric} per device, with alert thresholds")


# -- report & data quality ----------------------------------------------------------------------


def confidence_gauge(report: DiagnosticReport, settings: Settings) -> go.Figure:
    cats = settings.scoring["confidence_categories"]
    fig = go.Figure(go.Indicator(
        mode="gauge+number", value=report.confidence, number={"valueformat": ".2f"},
        gauge={"axis": {"range": [0, 1]}, "bar": {"color": SERIES[0]},
               "steps": [{"range": [0, cats["medium"]], "color": "#f0efec"},
                         {"range": [cats["medium"], cats["high"]], "color": "#cde2fb"},
                         {"range": [cats["high"], 1], "color": "#9ec5f4"}],
               "threshold": {"line": {"color": MUTED, "width": 2},
                             "value": float(settings.scoring["decision"]["decide_threshold"])}},
        title={"text": f"confidence ({report.confidence_category.value})"}))
    fig.update_layout(height=240, margin={"l": 20, "r": 20, "t": 50, "b": 0})
    return fig


def evidence_frame(items: list[Any]) -> pd.DataFrame:
    return pd.DataFrame([{
        "device": e.device_id, "metric": e.metric, "value": e.value, "unit": e.unit.value if e.unit else "",
        "threshold": e.threshold, "severity": round(e.severity, 2), "basis": e.basis,
        "interpretation": e.interpretation, "agent": e.source_agent, "quality": e.quality.value,
    } for e in items])


def ingestion_frames(rep: IngestionReport) -> tuple[pd.DataFrame, pd.DataFrame]:
    conv = pd.DataFrame([{
        "field": c.original_field, "canonical": c.canonical_field, "method": c.method.value,
        "from unit": c.from_unit or "", "to unit": c.to_unit or "", "count": c.count, "note": c.note or "",
    } for c in rep.conversions])
    rej = pd.DataFrame([{
        "code": r.code.value, "reason": r.reason, "device": r.device_id or "", "field": r.field or "",
        "record dropped": r.record_rejected,
    } for r in rep.rejections])
    return conv, rej
