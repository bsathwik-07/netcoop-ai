"""Multi-seed evaluation: accuracy per scenario, confusion matrix, abstention, ECE/Brier.

    python scripts/evaluate.py --seeds 50                  # writes reports/eval/
    python scripts/evaluate.py --seeds 10 --tune 0.05,0.1,0.15,0.2
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from netcoop.benchmark import run_eval, tune_temperature, write_outputs  # noqa: E402
from netcoop.config import reports_dir  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seeds", type=int, default=50)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--tune", type=str, default=None, help="comma-separated temperatures to try first")
    parser.add_argument("--out", type=Path, default=reports_dir() / "eval")
    args = parser.parse_args()
    tuning = None
    if args.tune:
        tuning = tune_temperature([float(t) for t in args.tune.split(",")], seeds=min(args.seeds, 10),
                                  workers=args.workers)
        print(json.dumps(tuning, indent=2))
    result = run_eval(seeds=args.seeds, workers=args.workers)
    paths = write_outputs(result, args.out, tuning)
    print(json.dumps(result.headline(), indent=2))
    print("\n".join(str(p) for p in paths))
    h = result.headline()
    return 0 if h["crashes"] == 0 and h["high_confidence_wrong"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
