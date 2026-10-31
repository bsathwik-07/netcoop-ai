# Incident INC-EXAMPLE-ORG-SPIKE: insufficient_evidence

**Insufficient evidence for a definitive root cause; leading candidate: problem upstream of the observed device (WAN/ISP/core path) at SW_CAMPUS_4 (P=0.63)**

- Root cause code: `upstream_issue`
- Confidence: **0.13** (low) = P(top) 0.63 x coverage 0.68 x agreement 1.00
- Root device: SW_CAMPUS_4
- Affected devices: SW_CAMPUS_4
- Symptomatic (not causal): none
- Segments: hostel_zone

## Summary

Insufficient evidence for a definitive root cause; leading candidate: problem upstream of the observed device (WAN/ISP/core path) at SW_CAMPUS_4 (P=0.63). Key evidence: Elevated network latency (173 ms vs critical threshold 150) on SW_CAMPUS_4; Packet loss above acceptable level (0.77 % vs warning threshold 0.1) on SW_CAMPUS_4; High jitter (delay variation) (30.99 ms vs warning threshold 20) on SW_CAMPUS_4. The agents initially disagreed; left unresolved; leading candidate upstream_issue after 1 challenge round(s). Main alternative: link_congestion (P=0.20) - score 0.41 vs 0.53 for upstream_issue; expected signals are healthy on SW_CAMPUS_4: queue_drops. Confidence 0.13 (low) = P(top) 0.63 x coverage 0.68 x agreement 1.00. To resolve the remaining uncertainty, collect: telemetry from devices sharing the upstream of SW_CAMPUS_4, to tell a local fault from an upstream one; telemetry from devices sharing the upstream of SW_CAMPUS_4 (confirms/rules out upstream_issue); interface_errors on SW_CAMPUS_4 (would confirm or rule out interface_fault).

## Supporting evidence

| Device | Metric | Value | Threshold | Interpretation | Source | Quality |
|---|---|---|---|---|---|---|
| SW_CAMPUS_4 | latency_ms | 172.53 ms | 150 | Elevated network latency (173 ms vs critical threshold 150) | performance_agent | ok |
| SW_CAMPUS_4 | packet_loss_pct | 0.77 % | 0.1 | Packet loss above acceptable level (0.77 % vs warning threshold 0.1) | performance_agent | ok |
| SW_CAMPUS_4 | jitter_ms | 30.99 ms | 20 | High jitter (delay variation) (30.99 ms vs warning threshold 20) | performance_agent | ok |

## Alternative causes

| Cause | P | Why less likely |
|---|---|---|
| link_congestion | 0.202 | score 0.41 vs 0.53 for upstream_issue; expected signals are healthy on SW_CAMPUS_4: queue_drops |
| interface_fault | 0.071 | score 0.31 vs 0.53 for upstream_issue; key signals not observed: interface_errors, interface_flaps |
| hardware_failure | 0.044 | score 0.26 vs 0.53 for upstream_issue; expected signals are healthy on SW_CAMPUS_4: cpu_utilization_pct; key signals not observed: interface_errors, uptime_reset |
| device_overload | 0.024 | score 0.20 vs 0.53 for upstream_issue; expected signals are healthy on SW_CAMPUS_4: cpu_utilization_pct; key signals not observed: memory_utilization_pct |

## Recommended actions (human approval required; simulation only)

| # | Action | Risk | Action id |
|---|---|---|---|
| 1 | Collect the additional telemetry listed in next_data_to_collect before acting | low | `gather.collect_more_data` |
| 2 | Keep SW_CAMPUS_4 under observation and re-run the diagnosis on the next telemetry window | low | `gather.monitor` |
| 3 | Check the upstream path from SW_CAMPUS_4 to the campus core / ISP (traceroute, core counters) | low | `upstream_issue.check_upstream` |
| 4 | Inspect upstream and egress link utilization on SW_CAMPUS_4 | low | `link_congestion.inspect_utilization` |

## Agent collaboration

- Conflict detected: True; rounds: 1
- Resolution: left unresolved; leading candidate upstream_issue after 1 challenge round(s)
- Query: connectivity_agent: Is the fault localized above or below SW_CAMPUS_4?
- connectivity_agent: top=none score=0.00 evidence=0
- infrastructure_agent: top=normal score=0.00 evidence=3
- performance_agent: top=upstream_issue score=0.71 evidence=4

## Data quality

- Coverage 1.00; records 1; conversions 1; rejected records 0

## Next data to collect

- telemetry from devices sharing the upstream of SW_CAMPUS_4, to tell a local fault from an upstream one
- telemetry from devices sharing the upstream of SW_CAMPUS_4 (confirms/rules out upstream_issue)
- interface_errors on SW_CAMPUS_4 (would confirm or rule out interface_fault)
- interface_flaps on SW_CAMPUS_4 (would confirm or rule out interface_fault)

## Limitations

- no neighbouring devices reported data: cannot tell whether the fault is local or upstream
- abstained: confidence 0.43 < decide_threshold 0.55
- abstained: 'upstream_issue' cannot be confirmed yet: shared_upstream_symptoms

_Protocol netcoop/2.0; generated 2026-10-31T11:50:31.231967+00:00; message log: `reports\examples\INC-EXAMPLE-ORG-SPIKE\messages.jsonl`_
