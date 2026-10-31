"""`netcoop` command-line interface (works without the dashboard)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from netcoop.bus.store import StateStore, default_store_path
from netcoop.config import reports_dir
from netcoop.evaluation import grade
from netcoop.pipeline import RunResult, decide_action, diagnose, resume_incident, run_scenario
from netcoop.protocol.envelope import Envelope
from netcoop.protocol.payloads import DiagnosticReport
from netcoop.protocol.vocabulary import DiagnosisStatus, MessageType
from netcoop.sim.chaos import MUTATIONS
from netcoop.sim.generator import SCENARIOS, scenario_names

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="NetCoop AI: multi-agent network fault diagnosis (synthetic data only, simulation only).")
console = Console()

STATUS_STYLE = {
    DiagnosisStatus.DIAGNOSED: "bold red",
    DiagnosisStatus.INSUFFICIENT_EVIDENCE: "bold yellow",
    DiagnosisStatus.NORMAL: "bold green",
    DiagnosisStatus.FAILED: "bold magenta",
}
TYPE_STYLE = {
    MessageType.QUERY: "bold cyan", MessageType.RESPONSE: "cyan", MessageType.FINDING: "green",
    MessageType.ERROR: "red", MessageType.DIAGNOSIS: "bold magenta", MessageType.REPORT: "bold magenta",
}


def summarize_message(env: Envelope) -> str:
    p = env.payload
    t = env.type
    if t is MessageType.FINDING:
        return f"top={p.top_hypothesis.value if p.top_hypothesis else 'none'} evidence={len(p.evidence)}"
    if t is MessageType.QUERY:
        return p.question
    if t is MessageType.RESPONSE:
        return f"answer={p.answer}: {p.explanation}"
    if t is MessageType.ERROR:
        return f"{p.code.value}: {p.message}"
    if t is MessageType.TASK_ASSIGNMENT:
        return f"{len(p.records)} records, metrics={','.join(p.metrics_of_interest[:4])}"
    if t is MessageType.TELEMETRY_NORMALIZED:
        r = p.report
        return f"{r.records_accepted}/{r.records_in} records, coverage={r.data_coverage:.2f}, conversions={sum(c.count for c in r.conversions)}"
    if t is MessageType.TELEMETRY_RAW:
        return f"{len(p.content)} chars from {p.source}"
    if t is MessageType.DIAGNOSIS:
        return f"{p.status.value} {p.root_cause_code.value if p.root_cause_code else ''} conf={p.confidence:.2f}"
    if t is MessageType.REPORT:
        return f"report {p.report.status.value}"
    if t is MessageType.REMEDIATION_PROPOSAL:
        return f"{len(p.actions)} action(s) proposed"
    return ""


def live_feed(env: Envelope) -> None:
    if env.correlation_id == "system":
        return
    style = TYPE_STYLE.get(env.type, "white")
    console.print(f"[dim]{env.timestamp.strftime('%H:%M:%S.%f')[:-3]}[/] [{style}]{env.type.value:<22}[/] "
                  f"{env.from_agent} -> {env.to_agent}  [dim]{summarize_message(env)[:110]}[/]")


def print_report(r: DiagnosticReport) -> None:
    style = STATUS_STYLE[r.status]
    cb = r.confidence_breakdown
    head = (f"[{style}]{r.status.value.upper()}[/]  {r.suspected_root_cause}\n"
            f"confidence [bold]{r.confidence:.2f}[/] ({r.confidence_category.value}) = P(top) {cb.p_top:.2f} x "
            f"coverage {cb.data_coverage:.2f} x agreement {cb.agreement_factor:.2f}\n"
            f"root device: {r.root_device or '-'}   affected: {', '.join(r.affected_devices[:8]) or '-'}")
    console.print(Panel(head, title=f"Incident {r.incident_id}", border_style=style))
    if r.supporting_evidence:
        t = Table("device", "metric", "value", "threshold", "interpretation", "agent", title="Supporting evidence")
        for e in r.supporting_evidence[:8]:
            t.add_row(str(e.device_id), e.metric, f"{e.value:g} {e.unit or ''}" if e.value is not None else "-",
                      f"{e.threshold:g}" if e.threshold is not None else "-", e.interpretation, e.source_agent)
        console.print(t)
    if r.alternative_causes:
        t = Table("alternative", "P", "why less likely", title="Alternatives")
        for alt in r.alternative_causes[:4]:
            t.add_row(alt.cause.value, f"{alt.probability:.3f}", alt.why_less_likely)
        console.print(t)
    cr = r.conflict_resolution
    if cr.conflict_detected:
        console.print(Panel("\n".join([*(f"- {x}" for x in cr.reasons), *(f"? {q}" for q in cr.queries),
                                       f"=> {cr.resolution}"]),
                            title=f"Conflict & challenge round ({cr.rounds} round(s))", border_style="cyan"))
    if r.secondary_incidents:
        for s in r.secondary_incidents:
            console.print(f"[yellow]secondary incident:[/] {s.root_cause_code.value} on {', '.join(s.devices[:4])}")
    if r.recommended_actions:
        t = Table("#", "action", "risk", "id", title="Recommended actions (human approval required)")
        for a in r.recommended_actions[:6]:
            t.add_row(str(a.priority), a.action, a.risk, a.action_id)
        console.print(t)
    if r.next_data_to_collect:
        console.print("[bold]Next data to collect:[/] " + "; ".join(r.next_data_to_collect[:4]))
    dq = r.data_quality
    console.print(f"[dim]data quality: coverage {dq.coverage:.2f}, records {dq.records_in}, conversions "
                  f"{dq.conversions}, rejected {dq.rejected_records}[/]")
    for w in dq.warnings[:6]:
        console.print(f"[dim]  ! {w}[/]")


def _finish(result: RunResult, as_json: bool) -> None:
    if result.report is None:
        console.print(f"[bold red]System failure:[/] {result.error}")
        raise typer.Exit(code=2)
    if as_json:
        sys.stdout.write(result.report.model_dump_json(indent=2) + "\n")
        return
    print_report(result.report)
    if result.report_path:
        console.print(f"[dim]report: {result.report_path}[/]")


@app.command()
def scenarios() -> None:
    """List the built-in scenarios."""
    t = Table("scenario", "expected", "root cause", "description")
    for name, spec in SCENARIOS.items():
        t.add_row(name, spec.expected_status.value, spec.root_cause.value if spec.root_cause else "-", spec.description)
    console.print(t)


@app.command("diagnose")
def diagnose_cmd(
    scenario: Annotated[str | None, typer.Option(help="Built-in scenario name")] = None,
    input: Annotated[Path | None, typer.Option("--input", "-i", help="Telemetry file (JSON, JSONL or CSV)")] = None,
    seed: Annotated[int, typer.Option(help="Generator seed")] = 42,
    format: Annotated[str, typer.Option(help="canonical | organizer (scenario input format)")] = "canonical",
    chaos: Annotated[str | None, typer.Option(help=f"Comma-separated mutations: {', '.join(MUTATIONS)}")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the JSON report only")] = False,
    feed: Annotated[bool, typer.Option(help="Show the live agent message feed")] = True,
    out: Annotated[Path | None, typer.Option(help="Reports directory")] = None,
    pause_after: Annotated[str | None, typer.Option(help="Pause after 'ingestion' or 'correlation'")] = None,
    state: Annotated[Path | None, typer.Option(help="SQLite state store (default state/netcoop.sqlite)")] = None,
) -> None:
    """Diagnose a scenario or an input file."""
    if (scenario is None) == (input is None):
        console.print("[red]Give exactly one of --scenario or --input.[/]")
        raise typer.Exit(code=2)
    if pause_after not in (None, "ingestion", "correlation"):
        console.print("[red]--pause-after must be 'ingestion' or 'correlation'.[/]")
        raise typer.Exit(code=2)
    out_dir = out or reports_dir()
    tap = live_feed if feed and not as_json else None
    common = {"out_dir": out_dir, "on_message": tap, "store": StateStore(state or default_store_path()),
              "pause_after": pause_after}
    if scenario:
        if scenario not in SCENARIOS:
            console.print(f"[red]Unknown scenario {scenario!r}.[/] Choose from: {', '.join(scenario_names())}")
            raise typer.Exit(code=2)
        try:
            result = run_scenario(scenario, seed=seed, fmt=format, chaos=chaos, **common)
        except ValueError as exc:  # e.g. unknown mutation name
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(code=2) from None
    else:
        assert input is not None
        if not input.exists():
            console.print(f"[red]No such file: {input}[/]")
            raise typer.Exit(code=2)
        content_type = {".csv": "csv", ".jsonl": "jsonl"}.get(input.suffix.lower(), "auto")
        result = diagnose(input.read_bytes(), source=input.name, content_type=content_type, **common)
    if result.paused:
        console.print(f"[yellow]Incident {result.incident_id} {result.paused}.[/] Continue with: "
                      f"netcoop resume {result.incident_id}")
        return
    _finish(result, as_json)
    if result.report and result.report.recommended_actions and not as_json:
        console.print(f"[dim]approve an action (simulation only): netcoop approve {result.incident_id} <action id>[/]")


@app.command("run")
def run_cmd(
    scenario: Annotated[str, typer.Option(help="Built-in scenario name")] = "link_congestion",
    pause_after: Annotated[str | None, typer.Option(help="Pause after 'ingestion' or 'correlation'")] = None,
    seed: Annotated[int, typer.Option(help="Generator seed")] = 42,
) -> None:
    """Alias of `diagnose --scenario`, mainly for `netcoop run --pause-after correlation`."""
    diagnose_cmd(scenario=scenario, input=None, seed=seed, format="canonical", chaos=None, as_json=False, feed=True,
                 out=None, pause_after=pause_after, state=None)


@app.command()
def resume(
    incident_id: Annotated[str, typer.Argument(help="Incident to continue")],
    state: Annotated[Path | None, typer.Option(help="SQLite state store")] = None,
) -> None:
    """Continue a paused (or crashed) incident from its last checkpoint."""
    store = StateStore(state or default_store_path())
    if store.incident(incident_id) is None:
        console.print(f"[red]Unknown incident {incident_id}[/] (see `netcoop incidents`)")
        raise typer.Exit(code=2)
    result = resume_incident(incident_id, store, out_dir=reports_dir(), on_message=live_feed)
    _finish(result, as_json=False)


@app.command()
def incidents(state: Annotated[Path | None, typer.Option(help="SQLite state store")] = None) -> None:
    """List incidents in the state store with their phase."""
    t = Table("incident", "phase", "paused after", "status", "root cause", "conf", "updated")
    for row in StateStore(state or default_store_path()).incidents():
        t.add_row(row["incident_id"], row["phase"], row["paused_after"] or "-", row["status"] or "-",
                  row["root_cause"] or "-", f"{row['confidence']:.2f}" if row["confidence"] is not None else "-",
                  row["updated_at"])
    console.print(t)


@app.command()
def actions(
    incident_id: Annotated[str, typer.Argument(help="Incident")],
    state: Annotated[Path | None, typer.Option(help="SQLite state store")] = None,
) -> None:
    """Show the proposed actions of an incident and their approval state."""
    t = Table("action id", "action", "risk", "state", "result")
    for a in StateStore(state or default_store_path()).approvals(incident_id):
        summary = (a["result"] or {}).get("summary", "") if a["result"] else ""
        t.add_row(a["action_id"], a["action"]["action"], a["action"]["risk"], a["state"], summary)
    console.print(t)


@app.command()
def approve(
    incident_id: Annotated[str, typer.Argument(help="Incident")],
    action_id: Annotated[str, typer.Argument(help="Action id (see `netcoop actions <incident>`)")],
    reject: Annotated[bool, typer.Option("--reject", help="Reject instead of approve")] = False,
    reason: Annotated[str, typer.Option(help="Reason recorded with the decision")] = "",
    by: Annotated[str, typer.Option(help="Who decides")] = "human_operator",
    state: Annotated[Path | None, typer.Option(help="SQLite state store")] = None,
) -> None:
    """Approve (or --reject) a proposed action. Approved actions are SIMULATED only, then re-diagnosed."""
    store = StateStore(state or default_store_path())
    out = decide_action(incident_id, action_id, not reject, store, reason=reason, decided_by=by, out_dir=reports_dir())
    row = out["approval"]
    if row is None:
        console.print(f"[red]{out['note']}[/]")
        raise typer.Exit(code=2)
    style = {"simulated": "green", "rejected": "yellow"}.get(row["state"], "white")
    console.print(Panel(out["note"], title=f"{action_id}: {row['state']}", border_style=style))
    result = row.get("result") or {}
    for device, before in (result.get("before") or {}).items():
        after = result["after"].get(device, {})
        console.print(f"  {device}: " + ", ".join(f"{m} {before[m]:g} -> {after.get(m, before[m]):g}" for m in before))


@app.command("run-all")
def run_all(
    seed: Annotated[int, typer.Option(help="Generator seed")] = 42,
    out: Annotated[Path | None, typer.Option(help="Reports directory")] = None,
) -> None:
    """Run every scenario and print a summary table (reports are written per incident)."""
    t = Table("scenario", "status", "root cause", "conf", "root device", "challenge", "grade")
    failures = 0
    for name in scenario_names():
        result = run_scenario(name, seed=seed, out_dir=out or reports_dir())
        r = result.report
        if r is None:
            failures += 1
            t.add_row(name, "[red]SYSTEM FAILURE[/]", "-", "-", "-", "-", result.error or "")
            continue
        g = grade(r, result.truth or {})
        colour = {"correct": "green", "abstain": "yellow", "wrong": "red"}[g.outcome]
        t.add_row(name, f"[{STATUS_STYLE[r.status]}]{r.status.value}[/]",
                  r.root_cause_code.value if r.root_cause_code else "-", f"{r.confidence:.2f}", r.root_device or "-",
                  f"{r.conflict_resolution.rounds} round(s)" if r.conflict_resolution.conflict_detected else "-",
                  f"[{colour}]{g.outcome}[/] {g.reason}")
    console.print(t)
    if failures:
        raise typer.Exit(code=1)


@app.command()
def chaos(
    scenario: Annotated[str, typer.Option(help="Scenario to mutate")] = "hardware_failure",
    seed: Annotated[int, typer.Option(help="Generator seed")] = 42,
) -> None:
    """Apply each chaos mutation on its own (plus a combined one) and show the diagnosis."""
    t = Table("mutation", "status", "root cause", "conf", "coverage", "conversions", "rejected", "grade")
    combos = [[m] for m in MUTATIONS] + [["rename_fields", "swap_units", "downgrade_schema"]]
    for combo in combos:
        result = run_scenario(scenario, seed=seed, chaos=combo)
        r = result.report
        if r is None:
            t.add_row("+".join(combo), "[red]SYSTEM FAILURE[/]", "", "", "", "", "", result.error or "")
            continue
        g = grade(r, result.truth or {})
        colour = {"correct": "green", "abstain": "yellow", "wrong": "red"}[g.outcome]
        t.add_row("+".join(combo), r.status.value, r.root_cause_code.value if r.root_cause_code else "-",
                  f"{r.confidence:.2f}", f"{r.data_quality.coverage:.2f}", str(r.data_quality.conversions),
                  str(r.data_quality.rejected_records), f"[{colour}]{g.outcome}[/]")
    console.print(t)


@app.command("eval")
def eval_cmd(
    seeds: Annotated[int, typer.Option(help="Seeds per scenario")] = 50,
    workers: Annotated[int | None, typer.Option(help="Worker processes (default: CPUs - 1)")] = None,
    tune: Annotated[str | None, typer.Option(help="Comma-separated softmax temperatures to search first")] = None,
    out: Annotated[Path | None, typer.Option(help="Output directory")] = None,
) -> None:
    """Multi-seed evaluation: accuracy, confusion matrix, abstention, calibration (ECE/Brier)."""
    from netcoop.benchmark import run_eval, tune_temperature, write_outputs

    tuning = None
    if tune:
        with console.status("searching softmax temperature..."):
            tuning = tune_temperature([float(t) for t in tune.split(",")], seeds=min(seeds, 10), workers=workers)
    with console.status(f"running {seeds} seeds per scenario..."):
        result = run_eval(seeds=seeds, workers=workers)
    out_dir = out or reports_dir() / "eval"
    write_outputs(result, out_dir, tuning)
    h = result.headline()
    t = Table("group", "runs", "correct", "abstain", "wrong", "correct-or-abstain", "high-conf wrong", "mean conf",
              title=f"Evaluation: {h['runs']} runs in {h['seconds']} s")
    for name, g in result.by_group().items():
        t.add_row(name, str(g["runs"]), str(g["correct"]), str(g["abstain"]), str(g["wrong"]),
                  f"{g['correct_or_abstain']:.1%}", str(g["high_confidence_wrong"]), f"{g['mean_confidence']:.2f}")
    console.print(t)
    console.print(f"correct-or-abstain [bold]{h['correct_or_abstain']:.1%}[/], accuracy {h['accuracy']:.1%}, "
                  f"abstention {h['abstention_rate']:.1%}, wrong {h['wrong']}, high-confidence wrong "
                  f"[bold]{h['high_confidence_wrong']}[/], normal false alarms {h['normal_false_alarms']}, "
                  f"ECE [bold]{h['ece']:.3f}[/], Brier {h['brier']:.3f}")
    console.print(f"[dim]written to {out_dir}[/]")
    if h["crashes"] or h["high_confidence_wrong"]:
        raise typer.Exit(code=1)


@app.command()
def demo(
    pause: Annotated[bool, typer.Option(help="Wait for Enter between steps (for recording)")] = False,
) -> None:
    """Scripted demo: congestion, organizer HW failure, latency spike, chaos, malformed input,
    simulated remediation, evaluation numbers. Runs end to end in well under two minutes."""
    import json
    import time

    from netcoop.config import REPO_ROOT, data_dir
    from netcoop.mcp_server.server import build_server
    from netcoop.sim.organizer import generate_batch

    store = StateStore(":memory:")
    started = time.perf_counter()

    def step(n: int, title: str, say: str) -> None:
        if pause and n > 1:
            input("\n[Enter] next step ")
        console.rule(f"[bold]{n}. {title}")
        console.print(f"[italic]{say}[/]")

    step(1, "Problem and architecture",
         "Six specialised agents (ingestion, performance, infrastructure, connectivity, services, correlation) plus "
         "reporting and remediation talk only through a versioned JSON message bus (netcoop/2.0).")
    step(2, "Link congestion: localization and the report",
         "The connectivity agent localizes the fault to the hostel uplink; 8 downstream devices are symptoms.")
    congestion = run_scenario("link_congestion", seed=42, store=store, on_message=live_feed)
    _finish(congestion, as_json=False)

    step(3, "Organizer hardware_failure: agents disagree, the challenge round decides",
         "Performance says congestion, infrastructure says hardware; targeted QUERYs confirm the interface errors.")
    _finish(diagnose(generate_batch("hardware_failure", 1, seed=4)[0], source="organizer", store=store,
                     on_message=live_feed), as_json=False)

    step(4, "Organizer latency_spike: honest uncertainty",
         "No neighbour data and no local cause: the system abstains and says what to collect next.")
    _finish(diagnose(generate_batch("latency_spike", 1, seed=4)[0], source="organizer", store=store), as_json=False)

    step(5, "Chaos: renamed fields + swapped units + legacy schema, then malformed input",
         "Still the right answer, with lower confidence and every conversion reported; broken JSON fails cleanly.")
    _finish(run_scenario("hardware_failure", seed=7, chaos=["rename_fields", "swap_units", "downgrade_schema"],
                         store=store), as_json=False)
    _finish(diagnose((data_dir() / "malformed" / "truncated.json").read_bytes(), source="truncated.json",
                     store=store), as_json=False)

    step(6, "Remediation: a human approves, the fix is simulated, the network is normal again",
         "SIMULATION ONLY: the action edits a copy of the synthetic telemetry, then the agents re-diagnose.")
    if congestion.report is not None:
        out = decide_action(congestion.incident_id, "link_congestion.reroute_traffic", True, store, reason="demo")
        console.print(Panel(out["note"], title="approved: link_congestion.reroute_traffic", border_style="green"))

    step(7, "Evaluation, MCP and extensibility",
         "Measured over many seeds; the same tools are exposed to Claude through MCP; new agents plug in by entry point.")
    summary = REPO_ROOT / "reports" / "eval" / "summary.json"
    if summary.exists():
        h = json.loads(summary.read_text())["headline"]
        console.print(f"{h['runs']} runs: correct-or-abstain {h['correct_or_abstain']:.1%}, high-confidence wrong "
                      f"{h['high_confidence_wrong']}, false alarms {h['normal_false_alarms']}, ECE {h['ece']:.3f}")
    import asyncio

    tools = asyncio.run(build_server().list_tools())
    console.print("MCP tools: " + ", ".join(sorted(t.name for t in tools)))
    console.print("Plug-in agent: [project.entry-points.\"netcoop.agents\"] in pyproject.toml (see README).")
    console.rule(f"[dim]demo finished in {time.perf_counter() - started:.1f} s")


@app.command()
def mcp() -> None:
    """Start the MCP server on stdio (see README for `claude mcp add`)."""
    from netcoop.mcp_server.server import main as mcp_main

    mcp_main()


@app.command()
def gateway(host: Annotated[str, typer.Option(help="Bind address")] = "127.0.0.1",
            port: Annotated[int, typer.Option(help="Port")] = 8000) -> None:
    """Start the REST gateway (external agents join over HTTP; A2A-style agent cards)."""
    import uvicorn

    from netcoop.gateway.app import create_app

    uvicorn.run(create_app(), host=host, port=port)


@app.command()
def dashboard(port: Annotated[int, typer.Option(help="Port")] = 8501) -> None:
    """Launch the Streamlit dashboard."""
    import subprocess

    from netcoop.config import REPO_ROOT

    app_path = REPO_ROOT / "dashboard" / "app.py"
    raise typer.Exit(subprocess.call([sys.executable, "-m", "streamlit", "run", str(app_path),
                                      "--server.port", str(port)]))


@app.command()
def schemas(out: Annotated[Path | None, typer.Option(help="Output directory")] = None) -> None:
    """Export the protocol JSON Schemas and regenerate PROTOCOL.md."""
    from netcoop.config import REPO_ROOT
    from netcoop.protocol import protocol_doc
    from netcoop.protocol.schema_export import export_all

    paths = export_all(out or REPO_ROOT / "schemas")
    console.print(f"wrote {len(paths)} schema files and {protocol_doc.write()}")


if __name__ == "__main__":  # pragma: no cover
    app()
