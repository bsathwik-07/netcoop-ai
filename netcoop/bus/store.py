"""SQLite state store: incident phases and checkpoints (pause/resume) and human approvals (HITL).

The JSONL message log stays the record of the conversation; this store keeps what is needed to
continue a workflow in a later process: the normalized telemetry, the findings, the diagnosis,
the report, and every approval decision.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from netcoop.config import REPO_ROOT

PHASES = ("received", "normalized", "analyzed", "diagnosed", "reported", "paused", "failed")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    phase TEXT NOT NULL,
    paused_after TEXT,
    source TEXT,
    status TEXT,
    root_cause TEXT,
    confidence REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
    incident_id TEXT NOT NULL,
    name TEXT NOT NULL,
    payload TEXT NOT NULL,
    saved_at TEXT NOT NULL,
    PRIMARY KEY (incident_id, name)
);
CREATE TABLE IF NOT EXISTS approvals (
    incident_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    action TEXT NOT NULL,
    state TEXT NOT NULL,            -- pending | approved | rejected | simulated
    decided_by TEXT,
    reason TEXT,
    result TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (incident_id, action_id)
);
"""


def default_store_path() -> Path:
    """NETCOOP_STATE_DB if set, else state/netcoop.sqlite in the repo, else the temp dir (read-only hosts)."""
    override = os.environ.get("NETCOOP_STATE_DB")
    if override:
        return Path(override)
    preferred = REPO_ROOT / "state" / "netcoop.sqlite"
    try:
        preferred.parent.mkdir(parents=True, exist_ok=True)
        return preferred
    except OSError:
        return Path(tempfile.gettempdir()) / "netcoop" / "netcoop.sqlite"


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class StateStore:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path not in (None, ":memory:") else None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path) if self.path else ":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- incidents --------------------------------------------------------------------------

    def set_phase(self, incident_id: str, phase: str, **fields: Any) -> None:
        if phase not in PHASES:
            raise ValueError(f"unknown phase {phase}")
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO incidents (incident_id, phase, created_at, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(incident_id) DO UPDATE SET phase=excluded.phase, updated_at=excluded.updated_at",
                (incident_id, phase, now, now),
            )
            for key, value in fields.items():
                if key not in ("paused_after", "source", "status", "root_cause", "confidence"):
                    raise ValueError(f"unknown incident field {key}")
                self._conn.execute(f"UPDATE incidents SET {key}=? WHERE incident_id=?", (value, incident_id))
            self._conn.commit()

    def incident(self, incident_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM incidents WHERE incident_id=?", (incident_id,)).fetchone()
        return dict(row) if row else None

    def incidents(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM incidents ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # -- checkpoints ------------------------------------------------------------------------

    def save(self, incident_id: str, name: str, payload: Any) -> None:
        text = payload if isinstance(payload, str) else json.dumps(payload)
        with self._lock:
            self._conn.execute(
                "INSERT INTO checkpoints (incident_id, name, payload, saved_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(incident_id, name) DO UPDATE SET payload=excluded.payload, saved_at=excluded.saved_at",
                (incident_id, name, text, _now()),
            )
            self._conn.commit()

    def load(self, incident_id: str, name: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT payload FROM checkpoints WHERE incident_id=? AND name=?",
                                     (incident_id, name)).fetchone()
        return row["payload"] if row else None

    # -- approvals --------------------------------------------------------------------------

    def add_pending(self, incident_id: str, action_id: str, action_json: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO approvals (incident_id, action_id, action, state, updated_at) "
                "VALUES (?, ?, ?, 'pending', ?)", (incident_id, action_id, action_json, _now()))
            self._conn.commit()

    def decide(self, incident_id: str, action_id: str, state: str, decided_by: str, reason: str = "",
               result: Any = None) -> None:
        if state not in ("approved", "rejected", "simulated"):
            raise ValueError(f"unknown approval state {state}")
        with self._lock:
            self._conn.execute(
                "UPDATE approvals SET state=?, decided_by=?, reason=?, result=COALESCE(?, result), updated_at=? "
                "WHERE incident_id=? AND action_id=?",
                (state, decided_by, reason, json.dumps(result) if result is not None else None, _now(),
                 incident_id, action_id))
            self._conn.commit()

    def approvals(self, incident_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM approvals WHERE incident_id=? ORDER BY rowid",
                                      (incident_id,)).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["action"] = json.loads(item["action"])
            item["result"] = json.loads(item["result"]) if item["result"] else None
            out.append(item)
        return out
