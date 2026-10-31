"""The organizer's Colab generator, copied verbatim (Mock_data_builder_netScoopAI.ipynb).

`generate_telemetry` below is an exact copy, including the rounding to 2 decimals and the
deprecated `datetime.utcnow()`. Only the seeded wrappers underneath are ours.
"""

import json
import random
import warnings
from datetime import datetime


def generate_telemetry(fault_type="normal"):
    data = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "device_id": f"SW_CAMPUS_{random.randint(1, 10)}",
        "metrics": {}
    }

    if fault_type == "latency_spike":
        data["metrics"] = {
            "cpu_utilization_pct": round(random.uniform(40, 70), 2),
            "link_utilization_pct": round(random.uniform(60, 85), 2),
            "packet_loss_pct": round(random.uniform(0.1, 1.5), 2),
            "latency_ms": round(random.uniform(150, 400), 2), # Spike
            "jitter_ms": round(random.uniform(30, 80), 2),
            "queue_drops": random.randint(0, 50)
        }
        data["status"] = "Warning"
    elif fault_type == "hardware_failure":
        data["metrics"] = {
            "cpu_utilization_pct": 99.9,
            "link_utilization_pct": 100.0,
            "packet_loss_pct": round(random.uniform(15, 30), 2),
            "latency_ms": round(random.uniform(500, 1000), 2),
            "queue_drops": random.randint(5000, 15000),
            "interface_errors": random.randint(100, 500)
        }
        data["status"] = "Critical"
    else:
        data["metrics"] = {
            "cpu_utilization_pct": round(random.uniform(10, 35), 2),
            "link_utilization_pct": round(random.uniform(20, 50), 2),
            "packet_loss_pct": 0.0,
            "latency_ms": round(random.uniform(5, 20), 2),
            "jitter_ms": round(random.uniform(1, 5), 2),
            "queue_drops": 0
        }
        data["status"] = "Healthy"

    return data


# ---------------------------------------------------------------------------------------------
# Seeded wrappers (ours). The metric values are reproducible for a given seed; timestamps come
# from the organizer's utcnow() call, exactly as in their notebook.
# ---------------------------------------------------------------------------------------------

ORGANIZER_FAULT_TYPES = ("normal", "latency_spike", "hardware_failure")

# Ground truth for organizer output (see README): hardware_failure may also be reported as an
# interface fault; latency_spike has no single clear cause, so abstaining is the expected answer.
ORGANIZER_TRUTH: dict[str, dict[str, object]] = {
    "normal": {"expected_status": "normal", "root_cause": "normal",
               "acceptable_root_causes": ["normal"], "abstain_ok": False},
    "latency_spike": {"expected_status": "insufficient_evidence", "root_cause": None,
                      "acceptable_root_causes": ["link_congestion", "upstream_issue"], "abstain_ok": True},
    "hardware_failure": {"expected_status": "diagnosed", "root_cause": "hardware_failure",
                         "acceptable_root_causes": ["hardware_failure", "interface_fault"], "abstain_ok": False},
}


def generate_batch(fault_type: str, count: int = 1, seed: int | None = 42) -> list[dict]:
    if seed is not None:
        random.seed(seed)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return [generate_telemetry(fault_type) for _ in range(count)]


def generate_mixed(counts: dict[str, int], seed: int | None = 42) -> list[dict]:
    """A judge-style batch mixing fault types, e.g. {'normal': 8, 'hardware_failure': 1}."""
    if seed is not None:
        random.seed(seed)
    records: list[dict] = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        for fault_type, n in counts.items():
            records.extend(generate_telemetry(fault_type) for _ in range(n))
    return records


if __name__ == "__main__":
    print(json.dumps(generate_telemetry("hardware_failure"), indent=2))
