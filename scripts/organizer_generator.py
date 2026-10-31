"""The organizer's Colab generator (exact copy lives in netcoop/sim/organizer.py) + sample writer.

    python scripts/organizer_generator.py                       # print one hardware_failure record
    python scripts/organizer_generator.py --fault latency_spike --count 5 --seed 7
    python scripts/organizer_generator.py --write               # regenerate data/organizer_samples/
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from netcoop.config import data_dir  # noqa: E402
from netcoop.sim.organizer import (  # noqa: E402
    ORGANIZER_FAULT_TYPES,
    ORGANIZER_TRUTH,
    generate_batch,
    generate_mixed,
)

TRUTH = ORGANIZER_TRUTH


def write_samples(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def dump(name: str, obj: object, truth: dict) -> None:
        path = out_dir / f"{name}.json"
        path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")
        (out_dir / f"{name}.truth.json").write_text(json.dumps({"scenario": name, **truth}, indent=2) + "\n", encoding="utf-8")
        written.append(path)

    for fault in ORGANIZER_FAULT_TYPES:
        dump(f"single_{fault}", generate_batch(fault, 1, seed=42)[0], TRUTH[fault])
        dump(f"batch_{fault}", generate_batch(fault, 10, seed=7), TRUTH[fault])
    dump("mixed_normal_plus_hardware", generate_mixed({"normal": 8, "hardware_failure": 1}, seed=11), TRUTH["hardware_failure"])
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fault", choices=ORGANIZER_FAULT_TYPES, default="hardware_failure")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--write", action="store_true", help="write data/organizer_samples/")
    args = parser.parse_args()
    if args.write:
        paths = write_samples(data_dir() / "organizer_samples")
        print(f"wrote {len(paths)} organizer sample files")
        return
    batch = generate_batch(args.fault, args.count, args.seed)
    print(json.dumps(batch[0] if args.count == 1 else batch, indent=2))


if __name__ == "__main__":
    main()
