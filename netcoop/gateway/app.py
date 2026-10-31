"""REST gateway: the bus over HTTP, so agents in another process or language can join.

External agents only need the JSON Schema (GET /schemas/message):
  1. POST /agents/register   with a CAPABILITY_ANNOUNCE message   -> they are on the bus
  2. GET  /agents/{name}/inbox?wait=2                             -> messages addressed to them
  3. POST /messages          with any message (FINDING, RESPONSE, ...)
Agent Cards (A2A-style) are served at /.well-known/agent-card.json (A2A's canonical path; agent.json is an
alias) and /agents/{name}/.well-known/agent-card.json.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from netcoop import __version__
from netcoop.agents.correlation import new_incident_id
from netcoop.bus import MessageBus, MessageLog
from netcoop.bus.store import StateStore, default_store_path
from netcoop.config import get_settings, reports_dir
from netcoop.pipeline import OperatorClient, build_agents
from netcoop.protocol.envelope import Envelope, ProtocolError, decode_message
from netcoop.protocol.payloads import CapabilityAnnounce, Report, TelemetryRaw
from netcoop.protocol.schema_export import message_schema
from netcoop.protocol.vocabulary import PROTOCOL, MessageType
from netcoop.topology import default_topology


class Runtime:
    """A long-lived bus with every registered agent, plus HTTP inboxes for external agents."""

    def __init__(self, store: StateStore) -> None:
        self.store = store
        self.settings = get_settings()
        self.bus = MessageBus(MessageLog(reports_dir()))
        self.agents = build_agents(self.bus, self.settings, default_topology(), reports_dir(),
                                   extra={"store": store})
        self.operator = OperatorClient(self.bus, self.settings)
        self.inboxes: dict[str, asyncio.Queue[str]] = {}
        self.cards: dict[str, CapabilityAnnounce] = {}
        self.incidents: dict[str, asyncio.Future[Report | str]] = {}

    async def start(self) -> None:
        for agent in [*self.agents, self.operator]:
            await agent.start()
            self.cards[agent.name] = agent.capability()
        await self.bus.drain(5)

    async def stop(self) -> None:
        await self.bus.close()

    async def register_external(self, envelope: Envelope) -> str:
        cap: CapabilityAnnounce = envelope.payload
        if cap.agent in self.cards and cap.agent not in self.inboxes:
            raise HTTPException(409, f"agent name {cap.agent!r} is taken by a built-in agent")
        if cap.agent not in self.inboxes:
            inbox: asyncio.Queue[str] = asyncio.Queue()

            async def deliver(env: Envelope) -> None:
                await inbox.put(env.to_wire())

            self.bus.subscribe(cap.agent, deliver, frozenset(cap.consumes))
            self.inboxes[cap.agent] = inbox
        self.cards[cap.agent] = cap
        await self.bus.publish(envelope)  # the correlator learns about it like any other agent
        return cap.agent


def agent_card(cap: CapabilityAnnounce, base_url: str) -> dict[str, Any]:
    """A2A-style Agent Card derived from the agent's CAPABILITY_ANNOUNCE."""
    return {
        "name": cap.agent,
        "description": cap.description,
        "version": cap.version,
        "url": f"{base_url}agents/{cap.agent}",
        "protocolVersions": cap.supported_protocols,
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "capabilities": {"consumes": [str(t) for t in cap.consumes], "produces": [str(t) for t in cap.produces],
                         "metrics": cap.metrics, "inputs": cap.inputs,
                         "queryPredicates": [str(q) for q in cap.query_predicates]},
        "skills": [s.model_dump() for s in cap.skills],
    }


def create_app(store: StateStore | None = None) -> FastAPI:
    runtime_holder: dict[str, Runtime] = {}

    @asynccontextmanager
    async def lifespan(_: FastAPI):  # type: ignore[no-untyped-def]
        runtime = Runtime(store or StateStore(default_store_path()))
        await runtime.start()
        runtime_holder["rt"] = runtime
        yield
        await runtime.stop()

    app = FastAPI(title="NetCoop AI gateway", version=__version__, lifespan=lifespan,
                  description="Multi-agent network fault diagnosis. Synthetic data; remediation is simulated only.")

    def rt() -> Runtime:
        return runtime_holder["rt"]

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "protocol": PROTOCOL, "agents": sorted(rt().cards)}

    # A2A's canonical discovery path is /.well-known/agent-card.json; agent.json is kept as an alias.
    @app.get("/.well-known/agent-card.json")
    @app.get("/.well-known/agent.json", include_in_schema=False)
    async def system_card(request: Request) -> dict[str, Any]:
        base = str(request.base_url)
        return {
            "name": "netcoop-ai", "description": "Cooperating agents that diagnose campus network faults",
            "version": __version__, "url": base, "protocolVersions": [PROTOCOL],
            "defaultInputModes": ["application/json", "text/csv"], "defaultOutputModes": ["application/json"],
            "skills": [s.model_dump() for cap in rt().cards.values() for s in cap.skills],
            "agents": [f"{base}agents/{name}/.well-known/agent-card.json" for name in sorted(rt().cards)],
        }

    @app.get("/agents")
    async def agents(request: Request) -> list[dict[str, Any]]:
        return [agent_card(cap, str(request.base_url)) for _, cap in sorted(rt().cards.items())]

    @app.get("/agents/{name}/.well-known/agent-card.json")
    @app.get("/agents/{name}/.well-known/agent.json", include_in_schema=False)
    async def one_card(name: str, request: Request) -> dict[str, Any]:
        cap = rt().cards.get(name)
        if cap is None:
            raise HTTPException(404, f"unknown agent {name}")
        return agent_card(cap, str(request.base_url))

    @app.get("/schemas/message")
    async def schema() -> dict[str, Any]:
        return message_schema()

    @app.post("/agents/register")
    async def register(request: Request) -> dict[str, Any]:
        envelope = await _decode(request)
        if envelope.type is not MessageType.CAPABILITY_ANNOUNCE:
            raise HTTPException(422, "registration needs a CAPABILITY_ANNOUNCE message")
        return {"registered": await rt().register_external(envelope)}

    @app.get("/agents/{name}/inbox")
    async def inbox(name: str, wait: float = 0.0, limit: int = 50) -> list[dict[str, Any]]:
        queue = rt().inboxes.get(name)
        if queue is None:
            raise HTTPException(404, f"{name} is not a registered external agent")
        out: list[dict[str, Any]] = []
        if queue.empty() and wait > 0:
            try:
                out.append(json.loads(await asyncio.wait_for(queue.get(), timeout=min(wait, 30.0))))
            except TimeoutError:
                return out
        while not queue.empty() and len(out) < limit:
            out.append(json.loads(queue.get_nowait()))
        return out

    @app.post("/messages")
    async def post_message(request: Request) -> JSONResponse:
        body = await request.body()
        try:
            envelope = decode_message(body)
        except ProtocolError as exc:
            return JSONResponse({"accepted": False, "code": exc.code.value, "error": exc.message}, status_code=422)
        if envelope.from_agent not in rt().inboxes:
            return JSONResponse({"accepted": False, "code": "E_SCHEMA_INVALID",
                                 "error": f"{envelope.from_agent!r} must register first (POST /agents/register)"},
                                status_code=403)
        await rt().bus.publish_wire(body)
        return JSONResponse({"accepted": True, "msg_id": envelope.msg_id})

    @app.post("/incidents")
    async def new_incident(body: dict[str, Any]) -> dict[str, Any]:
        content = body.get("content")
        if not isinstance(content, str) or not content.strip():
            raise HTTPException(422, "body must contain 'content' (telemetry as JSON/JSONL/CSV text)")
        incident_id = new_incident_id()
        r = rt()
        r.incidents[incident_id] = r.operator.expect(incident_id)
        await r.operator.send(MessageType.TELEMETRY_RAW,
                              TelemetryRaw(source=str(body.get("source", "gateway")), content=content,
                                           content_type=body.get("content_type", "auto")),
                              correlation_id=incident_id, to="correlation_agent")
        return {"incident_id": incident_id, "status": "running"}

    @app.get("/incidents/{incident_id}")
    async def get_incident(incident_id: str) -> dict[str, Any]:
        fut = rt().incidents.get(incident_id)
        if fut is None:
            stored = rt().store.load(incident_id, "report")
            if stored:
                return {"incident_id": incident_id, "status": "done", "report": json.loads(stored)}
            raise HTTPException(404, f"unknown incident {incident_id}")
        if not fut.done():
            return {"incident_id": incident_id, "status": "running"}
        outcome = fut.result()
        if isinstance(outcome, str):
            return {"incident_id": incident_id, "status": outcome}
        return {"incident_id": incident_id, "status": "done", "report": json.loads(outcome.report.model_dump_json())}

    async def _decode(request: Request) -> Envelope:
        try:
            return decode_message(await request.body())
        except ProtocolError as exc:
            raise HTTPException(422, {"code": exc.code.value, "error": exc.message}) from None

    return app
