# Incident INC-EXAMPLE-CONGESTION: diagnosed

**Likely link congestion causing egress queue drops and added latency at HOSTEL_CORE_SW_01**

- Root cause code: `link_congestion`
- Confidence: **1.00** (high) = P(top) 1.00 x coverage 1.00 x agreement 1.00
- Root device: HOSTEL_CORE_SW_01
- Affected devices: HOSTEL_CORE_SW_01, HOSTEL_A_AP_01, HOSTEL_A_AP_02, HOSTEL_B_AP_01, HOSTEL_B_AP_02, SW_CAMPUS_1, SW_CAMPUS_2, SW_CAMPUS_3, SW_CAMPUS_4
- Symptomatic (not causal): HOSTEL_A_AP_01, HOSTEL_A_AP_02, HOSTEL_B_AP_01, HOSTEL_B_AP_02, SW_CAMPUS_1, SW_CAMPUS_2, SW_CAMPUS_3, SW_CAMPUS_4
- Segments: hostel_zone

## Summary

Likely link congestion causing egress queue drops and added latency at HOSTEL_CORE_SW_01. Key evidence: Elevated egress queue drops (3211 count vs critical threshold 1000); 618.1 sigma above its baseline on HOSTEL_CORE_SW_01; High link utilization (95.73 % vs critical threshold 90); 4.0 sigma above its baseline on HOSTEL_CORE_SW_01; Elevated network latency (171 ms vs critical threshold 150); 45.8 sigma above its baseline on HOSTEL_CORE_SW_01. 8 downstream device(s) show symptoms but are not the cause: HOSTEL_A_AP_01, HOSTEL_A_AP_02, HOSTEL_B_AP_01, HOSTEL_B_AP_02, SW_CAMPUS_1, SW_CAMPUS_2. The agents initially disagreed; resolved in favour of link_congestion after 1 challenge round(s); discriminating rules applied: load_without_errors; 1 hypothesis refutation(s) from topology/path checks. Main alternative: device_overload (P=0.00) - score 0.36 vs 1.05 for link_congestion; expected signals are healthy on SW_CAMPUS_4: cpu_utilization_pct, memory_utilization_pct. Confidence 1.00 (high) = P(top) 1.00 x coverage 1.00 x agreement 1.00.

## Supporting evidence

| Device | Metric | Value | Threshold | Interpretation | Source | Quality |
|---|---|---|---|---|---|---|
| HOSTEL_CORE_SW_01 | queue_drops | 3211 count | 1000 | Elevated egress queue drops (3211 count vs critical threshold 1000); 618.1 sigma above its baseline | infrastructure_agent | ok |
| HOSTEL_CORE_SW_01 | link_utilization_pct | 95.73 % | 90 | High link utilization (95.73 % vs critical threshold 90); 4.0 sigma above its baseline | performance_agent | ok |
| HOSTEL_CORE_SW_01 | latency_ms | 170.84 ms | 150 | Elevated network latency (171 ms vs critical threshold 150); 45.8 sigma above its baseline | performance_agent | ok |
| HOSTEL_CORE_SW_01 | packet_loss_pct | 1.17 % | 1 | Packet loss above acceptable level (1.17 % vs critical threshold 1); 58.0 sigma above its baseline | performance_agent | ok |
| HOSTEL_CORE_SW_01 | jitter_ms | 17.56 ms | 20 | Early warning: high jitter (delay variation) relative to this device's own baseline (17.56 ms, 10.6 sigma from its history; still below the warning threshold 20) | performance_agent | ok |
| HOSTEL_A_AP_01 | shared_upstream_symptoms | 1  | 0.3 | Neighbouring devices under the same upstream are also degraded (1 vs critical threshold 0.3) (2/2 peers degraded) (symptom downstream of HOSTEL_CORE_SW_01) | connectivity_agent | ok |
| HOSTEL_A_AP_02 | shared_upstream_symptoms | 1  | 0.3 | Neighbouring devices under the same upstream are also degraded (1 vs critical threshold 0.3) (2/2 peers degraded) (symptom downstream of HOSTEL_CORE_SW_01) | connectivity_agent | ok |
| HOSTEL_B_AP_01 | shared_upstream_symptoms | 1  | 0.3 | Neighbouring devices under the same upstream are also degraded (1 vs critical threshold 0.3) (2/2 peers degraded) (symptom downstream of HOSTEL_CORE_SW_01) | connectivity_agent | ok |

## Alternative causes

| Cause | P | Why less likely |
|---|---|---|
| device_overload | 0.001 | score 0.36 vs 1.05 for link_congestion; expected signals are healthy on SW_CAMPUS_4: cpu_utilization_pct, memory_utilization_pct |
| interface_fault | 0.001 | score 0.33 vs 1.05 for link_congestion; contradicted by load_without_errors fired on HOSTEL_CORE_SW_01; expected signals are healthy on HOSTEL_A_AP_02: interface_errors, interface_flaps |

## Recommended actions (human approval required; simulation only)

| # | Action | Risk | Action id |
|---|---|---|---|
| 1 | Inspect upstream and egress link utilization on HOSTEL_CORE_SW_01 | low | `link_congestion.inspect_utilization` |
| 2 | Review queue configuration and traffic distribution (QoS policy) on HOSTEL_CORE_SW_01 | low | `link_congestion.review_qos` |
| 3 | Shift part of the traffic from HOSTEL_CORE_SW_01 to a redundant uplink / apply rate limiting | medium | `link_congestion.reroute_traffic` |
| 4 | Plan an uplink capacity upgrade for HOSTEL_CORE_SW_01 if utilization stays above 70 % | low | `link_congestion.upgrade_uplink` |

## Agent collaboration

- Conflict detected: True; rounds: 1
- Resolution: resolved in favour of link_congestion after 1 challenge round(s); discriminating rules applied: load_without_errors; 1 hypothesis refutation(s) from topology/path checks
- Query: infrastructure_agent: Is interface_errors on HOSTEL_CORE_SW_01 critical? (premise of rule interface_errors_beat_congestion)
- Query: performance_agent: Is packet_loss_pct on HOSTEL_CORE_SW_01 abnormal? (premise of rule interface_errors_beat_congestion)
- Query: performance_agent: Is link_utilization_pct on HOSTEL_CORE_SW_01 critical? (premise of rule load_without_errors)
- Query: infrastructure_agent: Is interface_errors on HOSTEL_CORE_SW_01 healthy? (premise of rule load_without_errors)
- Query: connectivity_agent: Is the fault localized above or below HOSTEL_CORE_SW_01?
- connectivity_agent: top=upstream_issue score=1.00 evidence=9
- infrastructure_agent: top=none score=0.00 evidence=160
- performance_agent: top=link_congestion score=0.92 evidence=132
- services_agent: top=normal score=0.00 evidence=6

## Data quality

- Coverage 1.00; records 480; conversions 0; rejected records 0

## Limitations

- infrastructure_agent sees abnormal signals it cannot attribute on its own

_Protocol netcoop/2.0; generated 2026-10-31T11:50:30.777098+00:00; message log: `reports\examples\INC-EXAMPLE-CONGESTION\messages.jsonl`_
