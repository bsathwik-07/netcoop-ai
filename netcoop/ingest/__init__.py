"""Telemetry ingestion: the schema adapter used by the ingestion agent."""

from netcoop.ingest.adapter import AdapterResult, TelemetryAdapter, norm_key

__all__ = ["AdapterResult", "TelemetryAdapter", "norm_key"]
