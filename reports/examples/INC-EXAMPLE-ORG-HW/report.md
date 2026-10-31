# Incident INC-EXAMPLE-ORG-HW: diagnosed

**Likely device hardware failure (interface errors, pegged CPU, severe loss) at SW_CAMPUS_4**

- Root cause code: `hardware_failure`
- Confidence: **0.68** (medium) = P(top) 0.99 x coverage 0.78 x agreement 0.88
- Root device: SW_CAMPUS_4
- Affected devices: SW_CAMPUS_4
- Symptomatic (not causal): none
- Segments: hostel_zone

## Summary

Likely device hardware failure (interface errors, pegged CPU, severe loss) at SW_CAMPUS_4. Key evidence: Interface/CRC errors detected (179 count vs critical threshold 100) on SW_CAMPUS_4; Device reports degraded/critical status: down/critical on SW_CAMPUS_4; Packet loss above acceptable level (19.55 % vs critical threshold 1) on SW_CAMPUS_4. The agents initially disagreed; resolved in favour of hardware_failure after 1 challenge round(s); discriminating rules applied: interface_errors_beat_congestion, whole_device_failure. Main alternative: interface_fault (P=0.01) - score 0.52 vs 0.98 for hardware_failure; contradicted by whole_device_failure fired on SW_CAMPUS_4; key signals not observed: interface_flaps. Confidence 0.68 (medium) = P(top) 0.99 x coverage 0.78 x agreement 0.88. To resolve the remaining uncertainty, collect: interface_flaps on SW_CAMPUS_4 (would confirm or rule out interface_fault).

## Supporting evidence

| Device | Metric | Value | Threshold | Interpretation | Source | Quality |
|---|---|---|---|---|---|---|
| SW_CAMPUS_4 | interface_errors | 179 count | 100 | Interface/CRC errors detected (179 count vs critical threshold 100) | infrastructure_agent | ok |
| SW_CAMPUS_4 | device_status | 1  | - | Device reports degraded/critical status: down/critical | infrastructure_agent | ok |
| SW_CAMPUS_4 | packet_loss_pct | 19.55 % | 1 | Packet loss above acceptable level (19.55 % vs critical threshold 1) | performance_agent | ok |
| SW_CAMPUS_4 | cpu_utilization_pct | 99.9 % | 90 | High device CPU utilization (99.9 % vs critical threshold 90) | infrastructure_agent | ok |
| SW_CAMPUS_4 | queue_drops | 12845 count | 1000 | Elevated egress queue drops (12845 count vs critical threshold 1000) | infrastructure_agent | ok |
| SW_CAMPUS_4 | latency_ms | 860.61 ms | 150 | Elevated network latency (861 ms vs critical threshold 150) | performance_agent | ok |

## Alternative causes

| Cause | P | Why less likely |
|---|---|---|
| interface_fault | 0.009 | score 0.52 vs 0.98 for hardware_failure; contradicted by whole_device_failure fired on SW_CAMPUS_4; key signals not observed: interface_flaps |
| link_congestion | 0.001 | score 0.31 vs 0.98 for hardware_failure; contradicted by interface_errors_beat_congestion fired on SW_CAMPUS_4 |

## Recommended actions (human approval required; simulation only)

| # | Action | Risk | Action id |
|---|---|---|---|
| 1 | Fail traffic over from SW_CAMPUS_4 to its redundant peer | medium | `hardware_failure.failover` |
| 2 | Run vendor hardware diagnostics on SW_CAMPUS_4 and inspect logs for ASIC/line-card faults | low | `hardware_failure.hardware_diag` |
| 3 | Open an RMA / schedule replacement of SW_CAMPUS_4 | low | `hardware_failure.rma` |

## Agent collaboration

- Conflict detected: True; rounds: 1
- Resolution: resolved in favour of hardware_failure after 1 challenge round(s); discriminating rules applied: interface_errors_beat_congestion, whole_device_failure
- Query: infrastructure_agent: Is interface_errors on SW_CAMPUS_4 critical? (premise of rule interface_errors_beat_congestion)
- Query: performance_agent: Is packet_loss_pct on SW_CAMPUS_4 abnormal? (premise of rule interface_errors_beat_congestion)
- Query: infrastructure_agent: Is cpu_utilization_pct on SW_CAMPUS_4 critical? (premise of rule whole_device_failure)
- Query: infrastructure_agent: Is device_status on SW_CAMPUS_4 critical? (premise of rule whole_device_failure)
- Query: performance_agent: Is link_utilization_pct on SW_CAMPUS_4 critical? (premise of rule load_without_errors)
- Query: infrastructure_agent: Is interface_errors on SW_CAMPUS_4 healthy? (premise of rule load_without_errors)
- connectivity_agent: top=none score=0.00 evidence=0
- infrastructure_agent: top=hardware_failure score=0.72 evidence=4
- performance_agent: top=link_congestion score=0.87 evidence=3

## Data quality

- Coverage 1.00; records 1; conversions 1; rejected records 0

## Next data to collect

- interface_flaps on SW_CAMPUS_4 (would confirm or rule out interface_fault)

## Limitations

- no neighbouring devices reported data: cannot tell whether the fault is local or upstream
- Root cause requires further verification

_Protocol netcoop/2.0; generated 2026-10-31T11:50:31.032329+00:00; message log: `reports\examples\INC-EXAMPLE-ORG-HW\messages.jsonl`_
