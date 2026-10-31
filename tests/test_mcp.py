"""MCP server: tools and resources are listed and callable in-process."""

from __future__ import annotations

import asyncio
import json

from netcoop.mcp_server.server import build_server

EXPECTED_TOOLS = {"list_devices", "get_topology", "get_telemetry", "run_diagnosis", "get_report", "list_actions",
                  "simulate_remediation"}


def _payload(result):  # type: ignore[no-untyped-def]
    """Extract the JSON-able result from call_tool, whatever its exact 2.x return shape."""
    def unwrap(d: dict):  # type: ignore[no-untyped-def,type-arg]
        return d["result"] if set(d) == {"result"} else d  # MCP wraps non-object results as {"result": ...}

    if isinstance(result, tuple):
        result = result[-1] if isinstance(result[-1], dict) else result[0]
    if isinstance(result, dict):
        return unwrap(result)
    content = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if content:
        return unwrap(content)
    items = result if isinstance(result, list) else getattr(result, "content", [])
    return json.loads(items[0].text)


def test_tools_and_resources_are_listed() -> None:
    server = build_server()
    tools = asyncio.run(server.list_tools())
    assert EXPECTED_TOOLS <= {t.name for t in tools}
    resources = asyncio.run(server.list_resources())
    assert {"netcoop://vocabulary", "netcoop://thresholds", "netcoop://protocol"} <= {str(r.uri) for r in resources}


def test_tools_return_useful_data(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("netcoop.mcp_server.server.default_store_path", lambda: tmp_path / "s.sqlite")
    monkeypatch.setenv("NETCOOP_REPORTS_DIR", str(tmp_path))
    server = build_server()
    devices = _payload(asyncio.run(server.call_tool("list_devices", {})))
    assert any(d["id"] == "SW_CAMPUS_4" for d in devices)
    report = _payload(asyncio.run(server.call_tool("run_diagnosis", {"scenario": "hardware_failure"})))
    assert report["status"] == "diagnosed" and report["root_cause_code"] == "hardware_failure"
    full = _payload(asyncio.run(server.call_tool("get_report", {"incident_id": report["incident_id"]})))
    assert full["incident_id"] == report["incident_id"]
    tele = _payload(asyncio.run(server.call_tool("get_telemetry", {"device_id": "SW_CAMPUS_4",
                                                                    "incident_id": report["incident_id"]})))
    assert tele["samples"]
    sim = _payload(asyncio.run(server.call_tool("simulate_remediation", {
        "incident_id": report["incident_id"], "action_id": "hardware_failure.failover"})))
    assert sim["state"] == "simulated" and sim["result"]["re_diagnosis"]["status"] == "normal"
