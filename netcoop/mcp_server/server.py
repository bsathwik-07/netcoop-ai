"""MCP server exposing NetCoop's telemetry, topology and diagnosis tools (stdio transport).

Connect from Claude Code:     claude mcp add netcoop -- <path-to-venv>/python -m netcoop.mcp_server.server
Claude Desktop (config JSON): {"mcpServers": {"netcoop": {"command": "<venv python>",
                                                          "args": ["-m", "netcoop.mcp_server.server"]}}}

SIMULATION ONLY: `simulate_remediation` edits a copy of synthetic telemetry; it never touches a
device. The MCP client's own tool-approval prompt is the human approval for that call, and the
decision is recorded as decided_by="mcp_client".
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import MCPServer

from netcoop.bus.store import StateStore, default_store_path
from netcoop.config import REPO_ROOT, config_dir, reports_dir
from netcoop.pipeline import decide_action_async, run_incident_async, scenario_input
from netcoop.protocol.payloads import TelemetryNormalized
from netcoop.protocol.schema_export import vocabulary_document
from netcoop.sim.generator import SCENARIOS
from netcoop.topology import default_topology

INSTRUCTIONS = (
    "NetCoop AI diagnoses campus network faults with cooperating agents. Use list_devices/get_topology for "
    "context, get_telemetry to inspect metrics, run_diagnosis on a built-in scenario or a telemetry payload, "
    "get_report for a past incident, list_actions and simulate_remediation for dry-run fixes. All data is "
    "synthetic; remediation is simulated only."
)


def _store() -> StateStore:
    return StateStore(default_store_path())


def _compact(report: Any) -> dict[str, Any]:
    r = report
    return {
        "incident_id": r.incident_id, "status": r.status.value, "suspected_root_cause": r.suspected_root_cause,
        "root_cause_code": r.root_cause_code.value if r.root_cause_code else None,
        "confidence": r.confidence, "confidence_category": r.confidence_category.value,
        "root_device": r.root_device, "affected_devices": r.affected_devices,
        "supporting_evidence": [{"device": e.device_id, "metric": e.metric, "value": e.value,
                                 "interpretation": e.interpretation} for e in r.supporting_evidence[:6]],
        "alternative_causes": [{"cause": a.cause.value, "probability": a.probability} for a in r.alternative_causes],
        "recommended_actions": [{"action_id": a.action_id, "action": a.action, "risk": a.risk}
                                for a in r.recommended_actions],
        "next_data_to_collect": r.next_data_to_collect, "challenge_round": r.conflict_resolution.resolution,
        "narrative": r.narrative,
    }


def build_server() -> MCPServer:
    server = MCPServer(name="netcoop", instructions=INSTRUCTIONS, version="0.1.0")

    @server.tool(description="List the devices in the campus topology (id, type, zone, parent).")
    def list_devices() -> list[dict[str, Any]]:
        return default_topology().to_dict()["nodes"]

    @server.tool(description="Return the campus topology tree (nodes with parent links and uplink capacity).")
    def get_topology() -> dict[str, Any]:
        return default_topology().to_dict()

    @server.tool(description="Time series of canonical telemetry for one device. Source: an incident's normalized "
                             "telemetry (incident_id) or a built-in scenario (default link_congestion).")
    def get_telemetry(device_id: str, metric: str | None = None, incident_id: str | None = None,
                      scenario: str = "link_congestion", seed: int = 42, window: int = 20) -> dict[str, Any]:
        if incident_id:
            raw = _store().load(incident_id, "normalized")
            if raw is None:
                return {"error": f"no telemetry stored for incident {incident_id}"}
            records = TelemetryNormalized.model_validate_json(raw).records
        else:
            if scenario not in SCENARIOS:
                return {"error": f"unknown scenario {scenario}; choose from {sorted(SCENARIOS)}"}
            from netcoop.config import get_settings
            from netcoop.ingest import TelemetryAdapter

            raw_records, _ = scenario_input(scenario, seed=seed)
            records = TelemetryAdapter(get_settings(), default_topology()).ingest(raw_records).records
        samples = [r for r in records if r.device_id == device_id][-window:]
        if not samples:
            return {"error": f"no telemetry for device {device_id}"}
        return {"device_id": device_id, "samples": [
            {"timestamp": r.timestamp.isoformat(), "status": r.status.value,
             "metrics": {m: {"value": v.value, "unit": v.unit.value, "quality": v.quality.value}
                         for m, v in r.metrics.items() if metric is None or m == metric}}
            for r in samples]}

    @server.tool(description="Run the full multi-agent diagnosis on a built-in scenario (optionally with chaos "
                             "mutations, comma-separated) or on a raw telemetry payload (JSON/JSONL/CSV text).")
    async def run_diagnosis(scenario: str | None = None, payload: str | None = None, seed: int = 42,
                            chaos: str | None = None) -> dict[str, Any]:
        if (scenario is None) == (payload is None):
            return {"error": "give exactly one of scenario or payload"}
        if scenario is not None:
            if scenario not in SCENARIOS:
                return {"error": f"unknown scenario {scenario}; choose from {sorted(SCENARIOS)}"}
            records, _ = scenario_input(scenario, seed=seed, chaos=chaos)
            raw: Any = json.dumps(records)
            source = f"mcp:scenario:{scenario}"
        else:
            raw, source = payload, "mcp:payload"
        result = await run_incident_async(raw, source=source, store=_store(), out_dir=reports_dir())
        if result.report is None:
            return {"error": result.error}
        return _compact(result.report)

    @server.tool(description="Fetch the full JSON report of a past incident.")
    def get_report(incident_id: str) -> dict[str, Any]:
        raw = _store().load(incident_id, "report")
        return json.loads(raw) if raw else {"error": f"no report for incident {incident_id}"}

    @server.tool(description="List the proposed remediation actions of an incident and their approval state.")
    def list_actions(incident_id: str) -> list[dict[str, Any]]:
        return [{"action_id": a["action_id"], "action": a["action"]["action"], "risk": a["action"]["risk"],
                 "state": a["state"]} for a in _store().approvals(incident_id)]

    @server.tool(description="Approve one proposed action and SIMULATE it (dry run on synthetic telemetry, never a "
                             "real device), then re-diagnose. Returns before/after metrics and the new status.")
    async def simulate_remediation(incident_id: str, action_id: str) -> dict[str, Any]:
        out = await decide_action_async(incident_id, action_id, True, _store(), decided_by="mcp_client",
                                        reason="approved via MCP tool call", out_dir=reports_dir())
        row = out["approval"]
        return {"note": out["note"], "state": row["state"] if row else None,
                "result": row.get("result") if row else None}

    @server.resource("netcoop://vocabulary", description="Protocol vocabulary: message types, metrics with units, "
                                                         "hypotheses, error codes.", mime_type="application/json")
    def vocabulary() -> str:
        return json.dumps(vocabulary_document(), indent=2)

    @server.resource("netcoop://thresholds", description="Metric thresholds with their sources.",
                     mime_type="text/yaml")
    def thresholds() -> str:
        return (config_dir() / "thresholds.yaml").read_text(encoding="utf-8")

    @server.resource("netcoop://protocol", description="The NetCoop protocol specification (PROTOCOL.md).",
                     mime_type="text/markdown")
    def protocol() -> str:
        path = REPO_ROOT / "PROTOCOL.md"
        return path.read_text(encoding="utf-8") if path.exists() else json.dumps(vocabulary_document(), indent=2)

    return server


def main() -> None:
    build_server().run("stdio")


if __name__ == "__main__":
    main()
