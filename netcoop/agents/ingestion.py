"""Ingestion & Schema Adapter agent: TELEMETRY_RAW -> TELEMETRY_NORMALIZED (+ ERRORs)."""

from __future__ import annotations

from collections import defaultdict

from netcoop.agents.base import BaseAgent
from netcoop.agents.registry import register_agent
from netcoop.config import Settings
from netcoop.ingest.adapter import FieldMapper, TelemetryAdapter
from netcoop.protocol.envelope import Envelope
from netcoop.protocol.payloads import Skill, TelemetryNormalized, TelemetryRaw
from netcoop.protocol.vocabulary import METRICS, ErrorCode, MessageType
from netcoop.topology import Topology, default_topology


@register_agent
class IngestionAgent(BaseAgent):
    name = "ingestion_agent"
    description = ("Schema adapter: parses JSON/JSONL/CSV, maps field aliases, converts units, migrates schema "
                   "versions, validates and quality-tags every value.")
    consumes = frozenset({MessageType.TELEMETRY_RAW})
    produces = frozenset({MessageType.TELEMETRY_NORMALIZED, MessageType.ERROR})
    metrics = tuple(METRICS)
    skills = (
        Skill(id="schema-adapter", name="Telemetry schema adapter",
              description="Organizer v1, legacy v1.x and canonical v2 telemetry to canonical v2",
              tags=["ingestion", "interoperability", "units"]),
    )

    def __init__(self, bus, settings: Settings | None = None, topology: Topology | None = None,  # type: ignore[no-untyped-def]
                 field_mapper: FieldMapper | None = None) -> None:
        super().__init__(bus, settings)
        try:
            topo = topology or default_topology()
        except (OSError, ValueError, KeyError):
            topo = None
        self.adapter = TelemetryAdapter(self.settings, topo, field_mapper)

    async def handle(self, envelope: Envelope) -> None:
        if envelope.type is not MessageType.TELEMETRY_RAW:
            return
        raw: TelemetryRaw = envelope.payload
        result = self.adapter.ingest(raw.content, raw.content_type, raw.source)
        # one ERROR per error code (aggregated), so the log stays readable on large batches
        grouped: dict[ErrorCode, list[str]] = defaultdict(list)
        for note in result.report.rejections:
            grouped[note.code].append(note.reason)
        for code, reasons in grouped.items():
            await self.send_error(code, reasons[0] + (f" (+{len(reasons) - 1} more)" if len(reasons) > 1 else ""),
                                  original=envelope, detail={"occurrences": len(reasons)},
                                  recoverable=not result.report.fatal)
        await self.reply(envelope, MessageType.TELEMETRY_NORMALIZED,
                         TelemetryNormalized(records=result.records, report=result.report))
