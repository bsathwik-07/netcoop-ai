"""Multi-seed evaluation: accuracy, confusion matrix, abstention, calibration (ECE, Brier).

Calibration follows Guo et al. (ICML 2017): confidences are binned, and ECE is the
sample-weighted mean |accuracy - mean confidence| over bins. Here "accuracy" asks whether the
system's top hypothesis is acceptable under the ground truth, so abstained runs (low
confidence) count too: a well-calibrated system is rarely right when it says 0.2.
"""

from __future__ import annotations

import dataclasses
import json
import os
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from netcoop.config import Settings, get_settings
from netcoop.evaluation import grade
from netcoop.protocol.payloads import DiagnosticReport
from netcoop.protocol.vocabulary import DiagnosisStatus
from netcoop.sim.chaos import MUTATIONS
from netcoop.sim.generator import scenario_names
from netcoop.sim.organizer import ORGANIZER_FAULT_TYPES, ORGANIZER_TRUTH, generate_batch

MUTATION_NAMES = list(MUTATIONS)


@dataclass(frozen=True)
class Job:
    kind: str  # scenario | chaos | organizer | organizer_batch
    name: str
    seed: int
    chaos: tuple[str, ...] = ()


@dataclass
class EvalResult:
    rows: list[dict[str, Any]]
    seconds: float
    settings_overrides: dict[str, Any] = field(default_factory=dict)

    # -- aggregates ---------------------------------------------------------------------------

    def by_group(self) -> dict[str, dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in self.rows:
            groups[row["group"]].append(row)
        out = {}
        for name, rows in sorted(groups.items()):
            counts = Counter(r["outcome"] for r in rows)
            n = len(rows)
            out[name] = {
                "runs": n,
                "correct": counts["correct"],
                "abstain": counts["abstain"],
                "wrong": counts["wrong"],
                "correct_or_abstain": round((counts["correct"] + counts["abstain"]) / n, 4),
                "accuracy": round(counts["correct"] / n, 4),
                "high_confidence_wrong": sum(r["high_confidence_wrong"] for r in rows),
                "mean_confidence": round(sum(r["confidence"] for r in rows) / n, 4),
            }
        return out

    def confusion(self) -> tuple[list[str], list[str], dict[tuple[str, str], int]]:
        cells: Counter[tuple[str, str]] = Counter((r["expected_label"], r["predicted_label"]) for r in self.rows)
        rows = sorted({k[0] for k in cells})
        cols = sorted({k[1] for k in cells}, key=lambda c: (c in ("abstain", "failed"), c))
        return rows, cols, dict(cells)

    def calibration(self, bins: int = 10) -> dict[str, Any]:
        """Over runs with a verifiable answer (see `calibration_eligible`); ambiguous ones are counted."""
        usable = [r for r in self.rows if r["status"] not in ("failed", "crash") and r["calibration_eligible"]]
        excluded = sum(1 for r in self.rows if not r["calibration_eligible"])
        edges = [i / bins for i in range(bins + 1)]
        table = []
        ece = 0.0
        for lo, hi in zip(edges, edges[1:], strict=False):
            members = [r for r in usable if lo <= r["confidence"] < hi or (hi == 1.0 and r["confidence"] == 1.0)]
            if not members:
                continue
            acc = sum(r["top_correct"] for r in members) / len(members)
            conf = sum(r["confidence"] for r in members) / len(members)
            ece += len(members) / len(usable) * abs(acc - conf)
            table.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": len(members), "accuracy": round(acc, 4),
                          "confidence": round(conf, 4)})
        brier = sum((r["confidence"] - r["top_correct"]) ** 2 for r in usable) / max(len(usable), 1)
        decided = [r for r in usable if r["status"] in ("diagnosed", "normal")]
        return {
            "ece": round(ece, 4),
            "brier": round(brier, 4),
            "runs": len(usable),
            "excluded_ambiguous": excluded,
            "bins": table,
            "decided_runs": len(decided),
            "decided_accuracy": round(sum(r["top_correct"] for r in decided) / max(len(decided), 1), 4),
        }

    def headline(self) -> dict[str, Any]:
        n = len(self.rows)
        counts = Counter(r["outcome"] for r in self.rows)
        return {
            "runs": n,
            "seconds": round(self.seconds, 1),
            "correct_or_abstain": round((counts["correct"] + counts["abstain"]) / n, 4) if n else 0,
            "accuracy": round(counts["correct"] / n, 4) if n else 0,
            "abstention_rate": round(sum(r["status"] == "insufficient_evidence" for r in self.rows) / n, 4) if n else 0,
            "wrong": counts["wrong"],
            "high_confidence_wrong": sum(r["high_confidence_wrong"] for r in self.rows),
            "normal_false_alarms": sum(1 for r in self.rows if r["expected_label"] == "normal"
                                       and r["status"] == "diagnosed"),
            "crashes": sum(1 for r in self.rows if r["status"] == "crash"),
            **{k: v for k, v in self.calibration().items() if k in ("ece", "brier")},
        }


# ---------------------------------------------------------------------------------------------
# running
# ---------------------------------------------------------------------------------------------


def jobs_for(seeds: int, chaos_seeds: int | None = None, organizer: bool = True) -> list[Job]:
    jobs = [Job("scenario", name, seed) for name in scenario_names() for seed in range(seeds)]
    for name in scenario_names():
        for seed in range(chaos_seeds if chaos_seeds is not None else min(seeds, 10)):
            jobs.append(Job("chaos", name, seed, (MUTATION_NAMES[seed % len(MUTATION_NAMES)],)))
    if organizer:
        jobs += [Job("organizer", fault, seed) for fault in ORGANIZER_FAULT_TYPES for seed in range(seeds)]
        jobs += [Job("organizer_batch", fault, seed) for fault in ORGANIZER_FAULT_TYPES
                 for seed in range(min(seeds, 10))]
    return jobs


def _settings_with(overrides: dict[str, Any]) -> Settings:
    base = get_settings()
    if not overrides:
        return base
    scoring = json.loads(json.dumps(base.scoring))
    for dotted, value in overrides.items():
        section, _, key = dotted.partition(".")
        assert section == "scoring", "only scoring.* overrides are supported"
        target = scoring
        parts = key.split(".")
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]] = value
    return dataclasses.replace(base, scoring=scoring)


def _labels(report: DiagnosticReport | None, truth: dict[str, Any]) -> tuple[str, str]:
    """Row label says what the ground truth accepts, so acceptable answers never look like errors."""
    expected = truth.get("expected_status")
    acceptable = truth.get("acceptable_root_causes", [])
    if truth.get("secondary_root_causes"):
        expected_label = "multi_fault: " + " | ".join(acceptable)
    elif expected == "insufficient_evidence":
        expected_label = ("ambiguous: abstain | " + " | ".join(acceptable)) if acceptable else "insufficient_evidence"
    else:
        expected_label = truth.get("root_cause") or "normal"
    if report is None:
        return expected_label, "crash"
    predicted = {
        DiagnosisStatus.DIAGNOSED: report.root_cause_code.value if report.root_cause_code else "?",
        DiagnosisStatus.NORMAL: "normal",
        DiagnosisStatus.INSUFFICIENT_EVIDENCE: "abstain",
        DiagnosisStatus.FAILED: "failed",
    }[report.status]
    return expected_label, predicted


def run_job(job: Job, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one evaluation job in this process. Top-level so worker processes can import it."""
    from netcoop.pipeline import diagnose, run_scenario

    settings = _settings_with(overrides or {})
    if job.kind in ("scenario", "chaos"):
        result = run_scenario(job.name, seed=job.seed, chaos=list(job.chaos) or None, settings=settings)
        truth = result.truth or {}
    else:
        count = 1 if job.kind == "organizer" else 10
        result = diagnose(generate_batch(job.name, count, seed=job.seed), settings=settings,
                          source=f"organizer:{job.name}")
        truth = {"scenario": job.name, **ORGANIZER_TRUTH[job.name]}
    report = result.report
    g = grade(report, truth)
    expected_label, predicted_label = _labels(report, truth)
    acceptable = set(truth.get("acceptable_root_causes", []))
    top = report.root_cause_code.value if report and report.root_cause_code else None
    return {
        "group": f"{job.kind}:{job.name}" if job.kind != "scenario" else job.name,
        "kind": job.kind,
        "name": job.name,
        "seed": job.seed,
        "chaos": "+".join(job.chaos),
        "status": report.status.value if report else "crash",
        "top": top,
        "confidence": report.confidence if report else 0.0,
        "category": report.confidence_category.value if report else "low",
        "outcome": g.outcome,
        "reason": g.reason,
        "high_confidence_wrong": int(g.high_confidence_wrong),
        "top_correct": int(top in acceptable) if report else 0,
        # calibration needs a verifiable answer: skip runs whose truth names no cause but accepts several
        "calibration_eligible": int(truth.get("root_cause") is not None or not acceptable),
        "expected_label": expected_label,
        "predicted_label": predicted_label,
        "conflict": int(bool(report and report.conflict_resolution.conflict_detected)),
    }


def _run_job_tuple(args: tuple[Job, dict[str, Any]]) -> dict[str, Any]:
    return run_job(*args)


def run_eval(seeds: int = 50, workers: int | None = None, overrides: dict[str, Any] | None = None,
             chaos_seeds: int | None = None, organizer: bool = True) -> EvalResult:
    jobs = jobs_for(seeds, chaos_seeds, organizer)
    overrides = overrides or {}
    started = time.perf_counter()
    workers = workers if workers is not None else max(1, (os.cpu_count() or 2) - 1)
    if workers <= 1:
        rows = [run_job(j, overrides) for j in jobs]
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            rows = list(pool.map(_run_job_tuple, [(j, overrides) for j in jobs], chunksize=8))
    return EvalResult(rows, time.perf_counter() - started, overrides)


def tune_temperature(candidates: list[float], seeds: int = 10, workers: int | None = None
                     ) -> list[dict[str, Any]]:
    """Grid search for the softmax temperature (Guo et al.): minimize ECE subject to zero
    high-confidence wrong answers and >= 95 % correct-or-abstain."""
    results = []
    for t in candidates:
        res = run_eval(seeds=seeds, workers=workers, overrides={"scoring.softmax_temperature": t},
                       chaos_seeds=min(seeds, 10))
        h = res.headline()
        results.append({"temperature": t, **h, "admissible": h["high_confidence_wrong"] == 0
                        and h["correct_or_abstain"] >= 0.95})
    return results


# ---------------------------------------------------------------------------------------------
# outputs
# ---------------------------------------------------------------------------------------------


def write_outputs(result: EvalResult, out_dir: Path, tuning: list[dict[str, Any]] | None = None) -> list[Path]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    headline, groups, calib = result.headline(), result.by_group(), result.calibration()
    rows_l, cols_l, cells = result.confusion()

    summary = {"headline": headline, "groups": groups, "calibration": calib,
               "confusion": {"rows": rows_l, "cols": cols_l,
                             "cells": [{"expected": r, "predicted": c, "n": n} for (r, c), n in sorted(cells.items())]},
               "tuning": tuning or [], "overrides": result.settings_overrides}
    path = out_dir / "summary.json"
    path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    written.append(path)

    # confusion matrix
    fig, ax = plt.subplots(figsize=(3.0 + 0.85 * len(cols_l), 1.2 + 0.5 * len(rows_l)))
    grid = [[cells.get((r, c), 0) for c in cols_l] for r in rows_l]
    ax.imshow([[min(v, 60) for v in row] for row in grid], cmap="Blues")
    ax.set_xticks(range(len(cols_l)), cols_l, rotation=40, ha="right", fontsize=8)
    ax.set_yticks(range(len(rows_l)), rows_l, fontsize=8)
    for i, row in enumerate(grid):
        for j, v in enumerate(row):
            if v:
                ax.text(j, i, str(v), ha="center", va="center", fontsize=8,
                        color="white" if v > 40 else "black")
    ax.set_xlabel("predicted (abstain = insufficient_evidence)")
    ax.set_ylabel("expected")
    ax.set_title(f"Confusion matrix ({headline['runs']} runs)")
    fig.tight_layout()
    path = out_dir / "confusion_matrix.png"
    fig.savefig(path, dpi=140)
    plt.close(fig)
    written.append(path)

    # reliability diagram
    fig, ax = plt.subplots(figsize=(5.2, 4.6))
    ax.plot([0, 1], [0, 1], linestyle="--", color="grey", linewidth=1, label="perfect calibration")
    xs = [b["confidence"] for b in calib["bins"]]
    ys = [b["accuracy"] for b in calib["bins"]]
    sizes = [max(20, min(400, b["n"] * 2)) for b in calib["bins"]]
    ax.scatter(xs, ys, s=sizes, color="#2563eb", zorder=3, label="bins (size = runs)")
    ax.plot(xs, ys, color="#2563eb", linewidth=1)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1.05)
    ax.set_xlabel("reported confidence")
    ax.set_ylabel("share of runs whose top hypothesis is acceptable")
    ax.set_title(f"Reliability diagram  ECE={calib['ece']:.3f}  Brier={calib['brier']:.3f}")
    ax.legend(loc="upper left", fontsize=8)
    fig.tight_layout()
    path = out_dir / "reliability.png"
    fig.savefig(path, dpi=140)
    plt.close(fig)
    written.append(path)
    return written
