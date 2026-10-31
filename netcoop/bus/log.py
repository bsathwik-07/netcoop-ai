"""Append-only JSONL message log, one file per incident (correlation_id)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from netcoop.protocol.envelope import Envelope

_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def safe_name(value: str) -> str:
    return _SAFE.sub("_", value)[:120] or "unknown"


class MessageLog:
    """Keeps every delivered message in memory and, if `base_dir` is set, on disk.

    Files: <base_dir>/<correlation_id>/messages.jsonl (system traffic goes to 'system').
    Each line is the exact wire JSON, so a log can be replayed through `decode_message`.
    """

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = base_dir
        self.entries: list[Envelope] = []

    def path_for(self, correlation_id: str) -> Path | None:
        if self.base_dir is None:
            return None
        return self.base_dir / safe_name(correlation_id) / "messages.jsonl"

    def append(self, envelope: Envelope, wire: str) -> None:
        self.entries.append(envelope)
        path = self.path_for(envelope.correlation_id)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(wire.replace("\n", " ") + "\n")

    def for_incident(self, correlation_id: str) -> list[Envelope]:
        return [e for e in self.entries if e.correlation_id == correlation_id]

    @staticmethod
    def read_file(path: Path) -> list[dict[str, Any]]:
        lines: list[dict[str, Any]] = []
        if not path.exists():
            return lines
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    lines.append(json.loads(line))
        return lines
