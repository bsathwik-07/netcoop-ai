"""CLI smoke tests."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from netcoop.cli import app
from netcoop.config import data_dir

runner = CliRunner()


def test_scenarios_lists_all() -> None:
    result = runner.invoke(app, ["scenarios"])
    assert result.exit_code == 0 and "latency_spike" in result.output


def test_diagnose_organizer_file_json(tmp_path: Path) -> None:
    path = data_dir() / "organizer_samples" / "single_hardware_failure.json"
    result = runner.invoke(app, ["diagnose", "--input", str(path), "--json", "--out", str(tmp_path)])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["status"] == "diagnosed" and report["root_cause_code"] == "hardware_failure"
    folder = tmp_path / report["incident_id"]
    assert (folder / "report.json").exists() and (folder / "report.md").exists()
    assert (folder / "messages.jsonl").exists()


def test_insufficient_evidence_is_not_an_error_exit(tmp_path: Path) -> None:
    result = runner.invoke(app, ["diagnose", "--scenario", "insufficient_data", "--no-feed", "--out", str(tmp_path)])
    assert result.exit_code == 0 and "INSUFFICIENT_EVIDENCE" in result.output


def test_malformed_input_gives_clean_report_not_a_traceback(tmp_path: Path) -> None:
    path = data_dir() / "malformed" / "truncated.json"
    result = runner.invoke(app, ["diagnose", "--input", str(path), "--no-feed", "--out", str(tmp_path)])
    assert result.exit_code == 0
    assert "FAILED" in result.output and "Traceback" not in result.output


def test_bad_arguments_are_clear(tmp_path: Path) -> None:
    assert runner.invoke(app, ["diagnose", "--scenario", "nope"]).exit_code == 2
    assert runner.invoke(app, ["diagnose", "--scenario", "normal", "--chaos", "melt"]).exit_code == 2
    assert runner.invoke(app, ["diagnose", "--input", str(tmp_path / "missing.json")]).exit_code == 2
