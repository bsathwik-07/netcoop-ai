"""Canonical vocabulary shared by every agent: hypotheses, metrics, units, enums.

Anything that crosses the bus uses these names. External agents can read the same
vocabulary from `schemas/vocabulary.json` (exported by `schema_export.py`).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

PROTOCOL_FAMILY = "netcoop"
PROTOCOL_VERSION = "2.0"
PROTOCOL = f"{PROTOCOL_FAMILY}/{PROTOCOL_VERSION}"
# Majors we can read. 1.x is migrated up to 2.0 by `migrations.py`.
SUPPORTED_MAJORS = (1, 2)
CANONICAL_TELEMETRY_SCHEMA = "2.0"


class MessageType(StrEnum):
    TASK_ASSIGNMENT = "TASK_ASSIGNMENT"
    TELEMETRY_RAW = "TELEMETRY_RAW"
    TELEMETRY_NORMALIZED = "TELEMETRY_NORMALIZED"
    FINDING = "FINDING"
    QUERY = "QUERY"
    RESPONSE = "RESPONSE"
    DIAGNOSIS = "DIAGNOSIS"
    REPORT = "REPORT"
    ERROR = "ERROR"
    ACK = "ACK"
    HEARTBEAT = "HEARTBEAT"
    CAPABILITY_ANNOUNCE = "CAPABILITY_ANNOUNCE"
    REMEDIATION_PROPOSAL = "REMEDIATION_PROPOSAL"
    APPROVAL_REQUEST = "APPROVAL_REQUEST"
    APPROVAL_DECISION = "APPROVAL_DECISION"


class ErrorCode(StrEnum):
    E_SCHEMA_INVALID = "E_SCHEMA_INVALID"
    E_UNIT_UNKNOWN = "E_UNIT_UNKNOWN"
    E_FIELD_UNMAPPED = "E_FIELD_UNMAPPED"
    E_MISSING_REQUIRED = "E_MISSING_REQUIRED"
    E_OUT_OF_RANGE = "E_OUT_OF_RANGE"
    E_STALE_DATA = "E_STALE_DATA"
    E_AGENT_TIMEOUT = "E_AGENT_TIMEOUT"
    E_UNSUPPORTED_VERSION = "E_UNSUPPORTED_VERSION"
    E_UNKNOWN_DEVICE = "E_UNKNOWN_DEVICE"
    E_INTERNAL = "E_INTERNAL"


ERROR_DESCRIPTIONS: dict[ErrorCode, str] = {
    ErrorCode.E_SCHEMA_INVALID: "Message or record does not match its JSON Schema (bad type, bad JSON, truncated).",
    ErrorCode.E_UNIT_UNKNOWN: "A unit was given that cannot be converted to the canonical unit.",
    ErrorCode.E_FIELD_UNMAPPED: "A field could not be mapped to the canonical vocabulary; it is kept under `unmapped`.",
    ErrorCode.E_MISSING_REQUIRED: "A required field (e.g. device_id) is absent; the record is rejected.",
    ErrorCode.E_OUT_OF_RANGE: "A value is physically impossible (e.g. percent > 100, negative latency).",
    ErrorCode.E_STALE_DATA: "A record is older than the staleness window relative to the batch.",
    ErrorCode.E_AGENT_TIMEOUT: "An agent did not answer a task or query before its deadline.",
    ErrorCode.E_UNSUPPORTED_VERSION: "The message uses a protocol major version this receiver cannot migrate.",
    ErrorCode.E_UNKNOWN_DEVICE: "The device_id is not present in the topology.",
    ErrorCode.E_INTERNAL: "An agent raised an unexpected exception; it was contained and reported.",
}


class Hypothesis(StrEnum):
    NORMAL = "normal"
    LINK_CONGESTION = "link_congestion"
    WEAK_WIFI = "weak_wifi"
    INTERFACE_FAULT = "interface_fault"
    HARDWARE_FAILURE = "hardware_failure"
    DEVICE_OVERLOAD = "device_overload"
    DNS_FAILURE = "dns_failure"
    DHCP_FAILURE = "dhcp_failure"
    UPSTREAM_ISSUE = "upstream_issue"


HYPOTHESIS_DESCRIPTIONS: dict[Hypothesis, str] = {
    Hypothesis.NORMAL: "Network operating within expected ranges",
    Hypothesis.LINK_CONGESTION: "Link congestion causing egress queue drops and added latency",
    Hypothesis.WEAK_WIFI: "Weak Wi-Fi signal causing retransmissions and loss for wireless clients",
    Hypothesis.INTERFACE_FAULT: "Ethernet interface/cabling fault causing errors and packet loss",
    Hypothesis.HARDWARE_FAILURE: "Device hardware failure (interface errors, pegged CPU, severe loss)",
    Hypothesis.DEVICE_OVERLOAD: "Device CPU/memory overload delaying forwarding and control plane",
    Hypothesis.DNS_FAILURE: "DNS service failure while the IP path is healthy",
    Hypothesis.DHCP_FAILURE: "DHCP service failure preventing clients from obtaining addresses",
    Hypothesis.UPSTREAM_ISSUE: "Problem upstream of the observed device (WAN/ISP/core path)",
}


class Unit(StrEnum):
    MS = "ms"
    PERCENT = "%"
    COUNT = "count"
    DBM = "dBm"
    DB = "dB"
    MBPS = "Mbps"
    SECONDS = "s"


# UCUM codes for each canonical unit, as used by OpenTelemetry semantic conventions.
UNIT_UCUM: dict[Unit, str] = {
    Unit.MS: "ms",
    Unit.PERCENT: "%",
    Unit.COUNT: "{count}",
    Unit.DBM: "dB[mW]",
    Unit.DB: "dB",
    Unit.MBPS: "Mbit/s",
    Unit.SECONDS: "s",
}


class Quality(StrEnum):
    OK = "ok"
    CONVERTED = "converted"
    IMPUTED = "imputed"
    MISSING = "missing"
    STALE = "stale"
    OUT_OF_RANGE = "out_of_range"
    REJECTED = "rejected"


class DeviceStatus(StrEnum):
    UP = "up"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"


class DiagnosisStatus(StrEnum):
    DIAGNOSED = "diagnosed"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    NORMAL = "normal"
    FAILED = "failed"


class ConfidenceCategory(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class MetricDomain(StrEnum):
    PERFORMANCE = "performance"
    INFRASTRUCTURE = "infrastructure"
    WIRELESS = "wireless"
    SERVICE = "service"


@dataclass(frozen=True)
class MetricSpec:
    name: str
    unit: Unit
    domain: MetricDomain
    description: str
    min_value: float | None = None
    max_value: float | None = None


def _m(
    name: str,
    unit: Unit,
    domain: MetricDomain,
    description: str,
    lo: float | None = None,
    hi: float | None = None,
) -> MetricSpec:
    return MetricSpec(name, unit, domain, description, lo, hi)


_P, _I, _W, _S = MetricDomain.PERFORMANCE, MetricDomain.INFRASTRUCTURE, MetricDomain.WIRELESS, MetricDomain.SERVICE

METRICS: dict[str, MetricSpec] = {
    m.name: m
    for m in [
        _m("latency_ms", Unit.MS, _P, "Round-trip latency (ICMP) to the gateway/core", 0, None),
        _m("jitter_ms", Unit.MS, _P, "Variation in round-trip latency", 0, None),
        _m("packet_loss_pct", Unit.PERCENT, _P, "Share of probe packets lost", 0, 100),
        _m("throughput_mbps", Unit.MBPS, _P, "Measured goodput on the uplink", 0, None),
        _m("bandwidth_mbps", Unit.MBPS, _P, "Provisioned link capacity", 0, None),
        _m("link_utilization_pct", Unit.PERCENT, _P, "Uplink utilization relative to capacity", 0, 100),
        _m("retransmission_pct", Unit.PERCENT, _W, "Share of frames retransmitted (Wi-Fi/TCP)", 0, 100),
        _m("wifi_rssi_dbm", Unit.DBM, _W, "Received signal strength at the client/AP", -120, 0),
        _m("wifi_snr_db", Unit.DB, _W, "Signal-to-noise ratio", -10, 90),
        _m("cpu_utilization_pct", Unit.PERCENT, _I, "Device CPU utilization", 0, 100),
        _m("memory_utilization_pct", Unit.PERCENT, _I, "Device memory utilization", 0, 100),
        _m("queue_drops", Unit.COUNT, _I, "Egress queue drops in the sample interval", 0, None),
        _m("interface_errors", Unit.COUNT, _I, "Interface input/CRC errors in the sample interval", 0, None),
        _m("interface_flaps", Unit.COUNT, _I, "Link up/down transitions in the sample interval", 0, None),
        _m("uptime_s", Unit.SECONDS, _I, "Device uptime (a drop indicates a reboot)", 0, None),
        _m("dns_success_pct", Unit.PERCENT, _S, "Share of DNS probe queries resolved", 0, 100),
        _m("dns_latency_ms", Unit.MS, _S, "DNS resolution time", 0, None),
        _m("dhcp_success_pct", Unit.PERCENT, _S, "Share of DHCP lease attempts that succeeded", 0, 100),
        _m("dhcp_ack_ms", Unit.MS, _S, "Time from DHCPDISCOVER to DHCPACK", 0, None),
        _m("dhcp_pool_utilization_pct", Unit.PERCENT, _S, "Share of the DHCP pool leased", 0, 100),
        _m("apipa_clients", Unit.COUNT, _S, "Clients holding a 169.254/16 self-assigned address", 0, None),
    ]
}

# Organizer status strings -> canonical status. Original value is always preserved.
STATUS_MAP: dict[str, DeviceStatus] = {
    "healthy": DeviceStatus.UP,
    "ok": DeviceStatus.UP,
    "up": DeviceStatus.UP,
    "normal": DeviceStatus.UP,
    "warning": DeviceStatus.DEGRADED,
    "degraded": DeviceStatus.DEGRADED,
    "warn": DeviceStatus.DEGRADED,
    "critical": DeviceStatus.DOWN,
    "down": DeviceStatus.DOWN,
    "error": DeviceStatus.DOWN,
    "unknown": DeviceStatus.UNKNOWN,
    # RFC 8343 ietf-interfaces oper-status values
    "testing": DeviceStatus.DEGRADED,
    "dormant": DeviceStatus.DEGRADED,
    "lower-layer-down": DeviceStatus.DOWN,
    "not-present": DeviceStatus.DOWN,
}


def parse_protocol(protocol: str) -> tuple[int, int]:
    """'netcoop/2.0' -> (2, 0). Raises ValueError on anything else."""
    family, _, version = protocol.partition("/")
    if family != PROTOCOL_FAMILY or not version:
        raise ValueError(f"not a netcoop protocol string: {protocol!r}")
    major, _, minor = version.partition(".")
    return int(major), int(minor or 0)
