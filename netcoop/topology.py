"""Campus topology as a NetworkX tree (edges point from parent to child)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import networkx as nx

from netcoop.config import data_dir


@dataclass(frozen=True)
class Node:
    id: str
    type: str
    zone: str
    parent: str | None
    uplink_mbps: float


class Topology:
    def __init__(self, nodes: list[Node], name: str = "") -> None:
        self.name = name
        self.nodes = {n.id: n for n in nodes}
        self.graph = nx.DiGraph()
        for n in nodes:
            self.graph.add_node(n.id, type=n.type, zone=n.zone, uplink_mbps=n.uplink_mbps)
        for n in nodes:
            if n.parent:
                if n.parent not in self.nodes:
                    raise ValueError(f"{n.id} has unknown parent {n.parent}")
                self.graph.add_edge(n.parent, n.id, capacity_mbps=n.uplink_mbps)
        if not nx.is_directed_acyclic_graph(self.graph):
            raise ValueError("topology must be a tree/DAG")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Topology:
        nodes = [
            Node(
                id=str(n["id"]),
                type=str(n.get("type", "device")),
                zone=str(n.get("zone", "unknown_zone")),
                parent=n.get("parent"),
                uplink_mbps=float(n.get("uplink_mbps", 1000)),
            )
            for n in data["nodes"]
        ]
        return cls(nodes, str(data.get("name", "")))

    @classmethod
    def load(cls, path: Path | None = None) -> Topology:
        path = path or data_dir() / "topology" / "campus.json"
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def __contains__(self, device_id: object) -> bool:
        return device_id in self.nodes

    def parent(self, device_id: str) -> str | None:
        node = self.nodes.get(device_id)
        return node.parent if node else None

    def ancestors(self, device_id: str) -> list[str]:
        """Path from the device's parent up to the root, nearest first."""
        path: list[str] = []
        current = self.parent(device_id)
        while current:
            path.append(current)
            current = self.parent(current)
        return path

    def descendants(self, device_id: str) -> set[str]:
        return set(nx.descendants(self.graph, device_id)) if device_id in self.graph else set()

    def children(self, device_id: str) -> list[str]:
        return sorted(self.graph.successors(device_id)) if device_id in self.graph else []

    def siblings(self, device_id: str) -> list[str]:
        parent = self.parent(device_id)
        return [c for c in self.children(parent) if c != device_id] if parent else []

    def zone(self, device_id: str) -> str | None:
        node = self.nodes.get(device_id)
        return node.zone if node else None

    def lowest_common_ancestor(self, devices: list[str]) -> str | None:
        """Deepest node that is the device itself or an ancestor of every device in the list."""
        known = [d for d in devices if d in self.nodes]
        if not known:
            return None
        chains = [[d, *self.ancestors(d)] for d in known]
        common = set(chains[0]).intersection(*map(set, chains[1:]))
        for candidate in chains[0]:  # nearest-first, so the first common one is the deepest
            if candidate in common:
                return candidate
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "nodes": [
                {"id": n.id, "type": n.type, "zone": n.zone, "parent": n.parent, "uplink_mbps": n.uplink_mbps}
                for n in self.nodes.values()
            ],
        }


@lru_cache(maxsize=1)
def default_topology() -> Topology:
    return Topology.load()
