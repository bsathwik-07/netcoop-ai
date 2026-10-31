# Incident INC-EXAMPLE-MALFORMED: failed

**Diagnosis failed: no usable telemetry (see data_quality and limitations)**

- Root cause code: `n/a`
- Confidence: **0.00** (low) = P(top) 0.00 x coverage 0.00 x agreement 0.00
- Root device: n/a
- Affected devices: none identified
- Symptomatic (not causal): none
- Segments: n/a

## Summary

Diagnosis failed: no usable telemetry (see data_quality and limitations). Confidence 0.00 (low) = P(top) 0.00 x coverage 0.00 x agreement 0.00. To resolve the remaining uncertainty, collect: Re-send the telemetry as valid JSON/JSONL/CSV with device_id, timestamp and metrics.

## Agent collaboration

- Conflict detected: False; rounds: 0
- Resolution: 

## Data quality

- Coverage 0.00; records 0; conversions 0; rejected records 0
- E_SCHEMA_INVALID: input is not valid JSON: Unterminated string starting at at line 6 column 5
- no usable telemetry in the input from truncated.json

## Next data to collect

- Re-send the telemetry as valid JSON/JSONL/CSV with device_id, timestamp and metrics

## Limitations

- input rejected: input is not valid JSON: Unterminated string starting at at line 6 column 5

_Protocol netcoop/2.0; generated 2026-10-31T11:50:31.445011+00:00; message log: `reports\examples\INC-EXAMPLE-MALFORMED\messages.jsonl`_
