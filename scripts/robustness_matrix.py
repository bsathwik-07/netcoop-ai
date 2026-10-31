"""Scenario x chaos-mutation matrix for the README: C = correct, A = abstained, X = wrong, ! = crash.

    python scripts/robustness_matrix.py > reports/eval/robustness_matrix.md
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from netcoop.evaluation import grade  # noqa: E402
from netcoop.pipeline import run_scenario  # noqa: E402
from netcoop.sim.chaos import MUTATIONS  # noqa: E402
from netcoop.sim.generator import scenario_names  # noqa: E402

SYMBOL = {"correct": "C", "abstain": "A", "wrong": "X"}


def main() -> None:
    mutations = list(MUTATIONS)
    print("| scenario | " + " | ".join(m.replace("_", " ") for m in mutations) + " |")
    print("|---|" + "---|" * len(mutations))
    totals = {"C": 0, "A": 0, "X": 0, "!": 0}
    for name in scenario_names():
        cells = []
        for m in mutations:
            result = run_scenario(name, seed=0, chaos=[m])
            symbol = "!" if result.report is None else SYMBOL[grade(result.report, result.truth or {}).outcome]
            totals[symbol] += 1
            cells.append(symbol)
        print(f"| {name} | " + " | ".join(cells) + " |")
    print(f"\nC = correct {totals['C']}, A = abstained {totals['A']}, X = wrong {totals['X']}, ! = crash {totals['!']} "
          f"(seed 0, one mutation at a time).")


if __name__ == "__main__":
    main()
