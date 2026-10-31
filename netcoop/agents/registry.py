"""Plugin registry. Agents join via the @register_agent decorator or the `netcoop.agents` entry point.

Adding an agent (see README "Add a new agent"): subclass AnalysisAgent, declare name/inputs/
metrics/hypotheses, and either decorate it or list it under [project.entry-points."netcoop.agents"].
The correlator discovers it from its CAPABILITY_ANNOUNCE; no orchestrator change is needed.
"""

from __future__ import annotations

import logging
from importlib import import_module
from importlib.metadata import entry_points
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from netcoop.agents.base import BaseAgent

logger = logging.getLogger("netcoop.registry")
_REGISTRY: dict[str, type[BaseAgent]] = {}
ENTRY_POINT_GROUP = "netcoop.agents"
CORE_MODULES = (
    "netcoop.agents.ingestion",
    "netcoop.agents.performance",
    "netcoop.agents.infrastructure",
    "netcoop.agents.connectivity",
    "netcoop.agents.correlation",
    "netcoop.agents.reporting",
    "netcoop.agents.remediation",
)


def register_agent(cls: type[BaseAgent]) -> type[BaseAgent]:
    _REGISTRY[cls.name] = cls
    return cls


def discover() -> dict[str, type[BaseAgent]]:
    """Core agents plus every agent published under the `netcoop.agents` entry point group."""
    for module in CORE_MODULES:
        import_module(module)
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            cls = ep.load()
        except Exception as exc:  # a broken plugin must not take the system down
            logger.warning("could not load agent plugin %s: %s", ep.name, exc)
            continue
        _REGISTRY.setdefault(cls.name, cls)
    return dict(_REGISTRY)
