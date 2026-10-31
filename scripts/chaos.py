"""Chaos harness: mutate clean scenarios, run the full pipeline, check correct-or-abstain.

    python scripts/chaos.py                                  # every scenario x every single mutation
    python scripts/chaos.py --scenario hardware_failure --pairs --seeds 3
"""

from __future__ import annotations

import argparse
import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

from netcoop.evaluation import grade  # noqa: E402
from netcoop.pipeline import run_scenario  # noqa: E402
from netcoop.sim.chaos import MUTATIONS  # noqa: E402
from netcoop.sim.generator import scenario_names  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", choices=scenario_names(), action="append")
    parser.add_argument("--seeds", type=int, default=1)
    parser.add_argument("--pairs", action="store_true", help="also every pair of mutations")
    args = parser.parse_args()

    combos = [[m] for m in MUTATIONS]
    if args.pairs:
        combos += [list(p) for p in itertools.combinations(MUTATIONS, 2)]
    console = Console()
    table = Table("scenario", "mutations", "seed", "status", "root cause", "conf", "grade")
    totals = {"correct": 0, "abstain": 0, "wrong": 0, "crash": 0, "high_conf_wrong": 0}
    for scenario in args.scenario or scenario_names():
        for combo in combos:
            for seed in range(args.seeds):
                result = run_scenario(scenario, seed=seed, chaos=combo)
                if result.report is None:
                    totals["crash"] += 1
                    table.add_row(scenario, "+".join(combo), str(seed), "[red]CRASH[/]", "", "", result.error or "")
                    continue
                g = grade(result.report, result.truth or {})
                totals[g.outcome] += 1
                totals["high_conf_wrong"] += int(g.high_confidence_wrong)
                if g.outcome != "correct":
                    r = result.report
                    table.add_row(scenario, "+".join(combo), str(seed), r.status.value,
                                  r.root_cause_code.value if r.root_cause_code else "-", f"{r.confidence:.2f}",
                                  f"{g.outcome}: {g.reason}")
    if table.row_count:
        console.print(table)
    console.print(totals)
    return 1 if totals["crash"] or totals["wrong"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
