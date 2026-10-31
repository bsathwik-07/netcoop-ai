"""Loads every YAML file in `config/`. All thresholds and weights live there, never inline."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILES = (
    "thresholds",
    "weights",
    "scoring",
    "orchestration",
    "field_aliases",
    "units",
    "remediation_playbook",
)


def config_dir() -> Path:
    return Path(os.environ.get("NETCOOP_CONFIG_DIR", REPO_ROOT / "config"))


def data_dir() -> Path:
    return Path(os.environ.get("NETCOOP_DATA_DIR", REPO_ROOT / "data"))


def reports_dir() -> Path:
    return Path(os.environ.get("NETCOOP_REPORTS_DIR", REPO_ROOT / "reports"))


@dataclass(frozen=True)
class Threshold:
    metric: str
    warn: float
    crit: float
    direction: str  # "above" or "below"
    source: str = ""

    def severity(self, value: float) -> float:
        """Piecewise-linear: 0 at/better than warn, 1 at/worse than crit."""
        span = self.crit - self.warn
        if span == 0:
            raw = 1.0 if self.breached(value, self.crit) else 0.0
        else:
            raw = (value - self.warn) / span
        return max(0.0, min(1.0, raw))

    def breached(self, value: float, level: float | None = None) -> bool:
        level = self.warn if level is None else level
        return value >= level if self.direction == "above" else value <= level


@dataclass(frozen=True)
class Settings:
    thresholds: dict[str, Threshold]
    weights: dict[str, Any]
    scoring: dict[str, Any]
    orchestration: dict[str, Any]
    field_aliases: dict[str, Any]
    units: dict[str, Any]
    playbook: dict[str, Any]
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping at the top level")
    return data


def load_settings(directory: Path | None = None) -> Settings:
    directory = directory or config_dir()
    raw = {name: _load_yaml(directory / f"{name}.yaml") for name in CONFIG_FILES}
    thresholds = {
        metric: Threshold(
            metric=metric,
            warn=float(spec["warn"]),
            crit=float(spec["crit"]),
            direction=str(spec.get("direction", "above")),
            source=str(spec.get("source", "")),
        )
        for metric, spec in raw["thresholds"]["metrics"].items()
    }
    return Settings(
        thresholds=thresholds,
        weights=raw["weights"],
        scoring=raw["scoring"],
        orchestration=raw["orchestration"],
        field_aliases=raw["field_aliases"],
        units=raw["units"],
        playbook=raw["remediation_playbook"],
        raw=raw,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
