"""An external agent that joins NetCoop over HTTP using only the JSON message schema.

It imports nothing from NetCoop: every message is a plain dict built from the schema served at
GET /schemas/message, so the same agent could be written in Go, Java or JavaScript.

What it does: a "WAN probe" agent. For each TASK_ASSIGNMENT it reports, per device, whether the
round-trip latency it is given looks like a WAN-side problem (informational evidence, no vote), and it
answers QUERY messages it cannot judge with answer=null, as the protocol requires.

    netcoop gateway &                      # start the gateway on http://127.0.0.1:8000
    python examples/external_agent_http.py --url http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

NAME = "wan_probe_agent"
PROTOCOL = "netcoop/2.0"


def envelope(msg_type: str, payload: dict[str, Any], schema: str, *, to: str = "broadcast",
             correlation_id: str = "system", causation_id: str | None = None) -> dict[str, Any]:
    return {
        "protocol": PROTOCOL, "msg_id": str(uuid.uuid4()), "correlation_id": correlation_id,
        "causation_id": causation_id, "from_agent": NAME, "to_agent": to, "type": msg_type,
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"), "ttl_s": 300,
        "payload_schema": schema, "payload": payload,
    }


def capability() -> dict[str, Any]:
    return envelope("CAPABILITY_ANNOUNCE", {
        "agent": NAME, "version": "0.1.0",
        "description": "External WAN probe (joins over HTTP; informational latency evidence)",
        "supported_protocols": [PROTOCOL],
        "consumes": ["TASK_ASSIGNMENT", "QUERY"], "produces": ["FINDING", "RESPONSE"],
        "inputs": ["latency_ms", "packet_loss_pct"], "metrics": [], "hypotheses": [], "query_predicates": [],
        "skills": [{"id": "wan-probe", "name": "WAN probe", "description": "Latency sanity check from outside",
                    "tags": ["external", "http"]}],
    }, "capability_announce/1.0")


def finding_for(task_msg: dict[str, Any]) -> dict[str, Any]:
    task = task_msg["payload"]
    latencies: dict[str, list[float]] = {}
    for rec in task["records"]:
        value = (rec.get("metrics", {}).get("latency_ms") or {}).get("value")
        if isinstance(value, int | float):
            latencies.setdefault(rec["device_id"], []).append(float(value))
    evidence = []
    for device, values in sorted(latencies.items()):
        median = statistics.median(values[-5:])
        evidence.append({
            "metric": "wan_probe_rtt", "value": round(median, 2), "unit": "ms", "device_id": device,
            "severity": 0.0, "interpretation": f"external probe sees median RTT {median:.1f} ms",
            "source_agent": NAME,
        })
    payload = {"task_id": task["task_id"], "agent": NAME, "agent_version": "0.1.0", "hypothesis_scores": [],
               "top_hypothesis": None, "evidence": evidence[:10], "limitations": [
                   "external informational agent: reports observations, does not vote on the root cause"]}
    return envelope("FINDING", payload, "finding/1.0", to=task_msg["from_agent"],
                    correlation_id=task_msg["correlation_id"], causation_id=task_msg["msg_id"])


def response_for(query_msg: dict[str, Any]) -> dict[str, Any]:
    return envelope("RESPONSE", {"query_id": query_msg["payload"]["query_id"], "answer": None,
                                 "explanation": f"{NAME} cannot judge this predicate"}, "response/1.0",
                    to=query_msg["from_agent"], correlation_id=query_msg["correlation_id"],
                    causation_id=query_msg["msg_id"])


def register(client: httpx.Client) -> None:
    schema = client.get("/schemas/message").json()
    try:  # optional: validate our own messages against the published schema
        import jsonschema

        jsonschema.validate(capability(), schema)
    except ImportError:
        pass
    client.post("/agents/register", content=json.dumps(capability())).raise_for_status()


def serve_once(client: httpx.Client, wait: float = 2.0) -> int:
    """Handle whatever is in the inbox; returns how many messages were answered."""
    handled = 0
    for msg in client.get(f"/agents/{NAME}/inbox", params={"wait": wait}).json():
        if msg["type"] == "TASK_ASSIGNMENT":
            reply = finding_for(msg)
        elif msg["type"] == "QUERY":
            reply = response_for(msg)
        else:
            continue
        client.post("/messages", content=json.dumps(reply)).raise_for_status()
        handled += 1
    return handled


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    with httpx.Client(base_url=args.url, timeout=40) as client:
        register(client)
        print(f"{NAME} registered at {args.url}; waiting for tasks (Ctrl+C to stop)")
        while True:
            n = serve_once(client, wait=10)
            if n:
                print(f"answered {n} message(s)")
            time.sleep(0.1)


if __name__ == "__main__":
    main()
