# Demo script (3 to 5 minutes)

Rehearse with `netcoop demo --pause` (waits for Enter between steps; without `--pause` the whole sequence runs in a
few seconds). Record the dashboard (`netcoop dashboard`) for steps 2 to 6 and a terminal for steps 5 and 7.
Before recording: `pip install -e ".[dev]"`, then run `netcoop eval --seeds 50` once so step 7 has numbers.

| # | Time | Show | Say |
|---|---|---|---|
| 1 | 0:00-0:20 | README architecture diagram | "Six specialised agents never call each other: every interaction is a versioned, validated JSON message with explicit units. The correlator orchestrates and judges; it can also say *I don't know*." |
| 2 | 0:20-1:20 | Dashboard → **Link congestion**. Tabs: Agent conversation, Topology, Report | "Raw telemetry goes to the ingestion agent, tasks go only to agents that declared those metrics, findings come back. The connectivity agent maps symptoms onto the topology: the hostel uplink is the root (red), eight downstream devices are symptoms (orange), not causes. Confidence 1.00 = P(top) × coverage × agreement, each factor shown." |
| 3 | 1:20-2:05 | Dashboard → **Organizer HW failure**. Agent conversation (green challenge-round band), Report → challenge-round box | "This is the organizer's own generator. Performance says congestion, infrastructure says hardware: a conflict. The correlator asks the owners targeted questions: are the interface errors critical, is the loss real? Confirmed. Interface errors come from the physical layer, not from load, so hardware failure wins; congestion and interface fault are listed as alternatives. Medium confidence, because this format has no uptime or flap data." |
| 4 | 2:05-2:35 | Dashboard → **Latency spike**. Report: yellow banner, Next data to collect | "High latency and jitter, moderate load, no neighbours reporting. Upstream is the leading idea, but nothing corroborates it, so the system refuses to claim a root cause and asks for telemetry from the devices sharing the upstream." |
| 5 | 2:35-3:20 | Dashboard → **Chaos: interop** → Data quality tab; then terminal `netcoop diagnose --input data/malformed/truncated.json` | "Same incident, but fields renamed, latency in seconds, utilization as fractions, a legacy schema. Every conversion is listed, the answer is unchanged, confidence drops slightly. Truncated JSON gives a clean failure with an error code, never a stack trace." |
| 6 | 3:20-3:50 | Dashboard → Link congestion again → Remediation tab → **Approve** "Shift part of the traffic..." | "Actions need a human. Approving runs a simulation only: it edits a copy of the synthetic telemetry, never a device, and the agents diagnose again: normal. Before and after values are shown, and everything is logged." |
| 7 | 3:50-4:20 | Evaluation tab (confusion matrix, reliability diagram); terminal `netcoop demo` last lines or `claude mcp list` | "Over 960 seeded runs: no high-confidence wrong answer, no false alarm, calibration error under 0.1. The same tools are available to Claude through MCP, external agents join over HTTP with agent cards, and a new agent is about 20 lines." |

Backup if the dashboard misbehaves: `netcoop demo --pause` shows every step in the terminal.
