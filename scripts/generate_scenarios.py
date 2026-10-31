"""Generate scenario telemetry + ground truth.

    python scripts/generate_scenarios.py --all
    python scripts/generate_scenarios.py --scenario link_congestion --seed 7 --duration 30 --format organizer
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from netcoop.config import data_dir  # noqa: E402
from netcoop.sim.generator import generate, scenario_names  # noqa: E402


def write(name: str, seed: int, duration: int, fmt: str, devices: int | None, out: Path) -> Path:
    records, truth = generate(name, seed=seed, duration=duration, fmt=fmt, devices=devices)
    out.mkdir(parents=True, exist_ok=True)
    suffix = "" if fmt == "canonical" else f".{fmt}"
    path = out / f"{name}{suffix}.json"
    path.write_text(json.dumps(records, indent=1) + "\n", encoding="utf-8")
    (out / f"{name}.truth.json").write_text(json.dumps(truth, indent=2) + "\n", encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", choices=scenario_names())
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--duration", type=int, default=20, help="samples per device (1 minute apart)")
    parser.add_argument("--devices", type=int, default=None, help="limit the number of devices")
    parser.add_argument("--format", choices=["canonical", "organizer"], default="canonical")
    parser.add_argument("--out", type=Path, default=data_dir() / "scenarios")
    args = parser.parse_args()
    names = scenario_names() if args.all or not args.scenario else [args.scenario]
    for name in names:
        path = write(name, args.seed, args.duration, args.format, args.devices, args.out)
        print(f"{name:26s} -> {path}")


if __name__ == "__main__":
    main()
