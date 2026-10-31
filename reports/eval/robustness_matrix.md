| scenario | rename fields | swap units | drop metrics | inject nan | downgrade schema | shuffle timestamps | add noise | duplicate records | stale timestamps | flatten or nest |
|---|---|---|---|---|---|---|---|---|---|---|
| normal | C | C | C | C | C | C | C | C | C | C |
| link_congestion | C | C | C | C | C | C | C | C | C | C |
| weak_wifi | C | C | C | C | C | C | C | C | C | C |
| interface_fault | C | C | C | C | C | C | C | C | C | C |
| dns_failure | C | C | C | C | C | C | C | C | C | C |
| dhcp_failure | C | C | C | C | C | C | C | C | C | C |
| device_overload | C | C | C | C | C | C | C | C | C | C |
| hardware_failure | C | C | C | C | C | C | C | C | C | C |
| latency_spike | C | C | C | C | C | C | C | C | C | C |
| interoperability_failure | C | A | C | C | C | C | C | C | C | C |
| multi_fault | C | C | C | C | C | C | C | C | C | C |
| subtle_degradation | C | C | C | C | C | C | C | C | C | C |
| insufficient_data | C | C | C | C | C | C | C | C | C | C |

C = correct 129, A = abstained 1, X = wrong 0, ! = crash 0 (seed 0, one mutation at a time).
