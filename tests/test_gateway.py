"""REST gateway: agent cards, validation, and an external agent (JSON only) joining a live incident."""

from __future__ import annotations

import importlib.util
import json
import time

from fastapi.testclient import TestClient

from netcoop.bus.store import StateStore
from netcoop.config import REPO_ROOT
from netcoop.gateway.app import create_app
from netcoop.sim.organizer import generate_batch

spec = importlib.util.spec_from_file_location("external_agent", REPO_ROOT / "examples" / "external_agent_http.py")
external = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
spec.loader.exec_module(external)  # type: ignore[union-attr]


def _client(tmp_path, monkeypatch) -> TestClient:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("NETCOOP_REPORTS_DIR", str(tmp_path))
    return TestClient(create_app(StateStore(":memory:")))


def test_agent_cards_and_schema(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    with _client(tmp_path, monkeypatch) as client:
        card = client.get("/.well-known/agent-card.json").json()  # A2A canonical discovery path
        assert card["protocolVersions"] == ["netcoop/2.0"] and card["skills"]
        assert client.get("/.well-known/agent.json").json() == card  # legacy alias
        names = {a["name"] for a in client.get("/agents").json()}
        assert {"correlation_agent", "performance_agent", "services_agent"} <= names
        one = client.get("/agents/infrastructure_agent/.well-known/agent-card.json").json()
        assert "FINDING" in one["capabilities"]["produces"]
        assert "oneOf" in json.dumps(client.get("/schemas/message").json())


def test_invalid_and_unregistered_messages_are_refused(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    with _client(tmp_path, monkeypatch) as client:
        bad = client.post("/messages", content=json.dumps({"protocol": "netcoop/9.0", "from_agent": "x"}))
        assert bad.status_code == 422 and bad.json()["code"] == "E_UNSUPPORTED_VERSION"
        stranger = external.envelope("ACK", {"acked_msg_id": "1"}, "ack/1.0")
        assert client.post("/messages", content=json.dumps(stranger)).status_code == 403


def test_external_agent_joins_an_incident_over_http(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    with _client(tmp_path, monkeypatch) as client:
        external.register(client)
        record = generate_batch("hardware_failure", 1, seed=2)[0]
        incident = client.post("/incidents", json={"content": json.dumps(record), "source": "test"}).json()
        handled = 0
        deadline = time.time() + 30
        status = {"status": "running"}
        while time.time() < deadline:
            handled += external.serve_once(client, wait=0.2)
            status = client.get(f"/incidents/{incident['incident_id']}").json()
            if status["status"] == "done":
                break
        assert handled >= 1, "the external agent never received its TASK_ASSIGNMENT"
        report = status["report"]
        assert report["status"] == "diagnosed" and report["root_cause_code"] == "hardware_failure"
        assert "wan_probe_agent" in report["evidence_sources"]
        assert any(s["agent"] == "wan_probe_agent" and s["responded"] for s in report["agent_findings_summary"])
