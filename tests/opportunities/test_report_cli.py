"""Real subprocess coverage for the local opportunity report CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
ALL_NEGATIVE = (
    REPO_ROOT / "tests" / "fixtures" / "opportunities" / "v1" / "all-negative-ledger.jsonl"
)
E_LEDGER = (
    REPO_ROOT / "tests" / "fixtures" / "opportunities" / "v1" / "w2e-final-ledger.jsonl"
)


def _run_cli(ledger: Path, output_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "apps.opportunity_report",
            "--ledger",
            str(ledger),
            "--output-root",
            str(output_root),
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )


def test_real_cli_reports_all_negative_success(tmp_path: Path) -> None:
    """A complete all-negative ledger is a completed local report, not a failure."""
    process = _run_cli(ALL_NEGATIVE, tmp_path / "all-negative")
    assert process.returncode == 0, process.stderr
    report = json.loads((tmp_path / "all-negative" / "report.json").read_text(encoding="utf-8"))
    assert report["totals"]["all_nonpositive"] is True
    assert report["ledger"]["event_count"] == 3


def test_real_cli_reports_evidence_ledger(tmp_path: Path) -> None:
    """The archived W2-E evidence ledger reconciles with an independent denominator."""
    process = _run_cli(E_LEDGER, tmp_path / "evidence")
    assert process.returncode == 0, process.stderr
    report = json.loads((tmp_path / "evidence" / "report.json").read_text(encoding="utf-8"))
    assert report["totals"]["events"] == 2
    assert report["totals"]["sim_available"] is False
    assert report["denominators"]["attempted_quote"]["count"] == 1
    assert report["denominators"]["quoted"]["count"] == 0
    assert report["denominators"]["rejected"]["count"] == 1
    assert report["consistency"]["event_count_matches_ledger_count"] is True


def test_real_cli_fails_closed_on_missing_ledger(tmp_path: Path) -> None:
    """Missing input gives a non-zero, reason-bearing failure."""
    process = _run_cli(tmp_path / "missing.jsonl", tmp_path / "missing-output")
    assert process.returncode != 0
    assert "No such file or directory" in process.stdout


def test_real_cli_rejects_existing_output_root(tmp_path: Path) -> None:
    """The CLI never overwrites prior report artifacts."""
    output_root = tmp_path / "output"
    output_root.mkdir()
    process = _run_cli(ALL_NEGATIVE, output_root)
    assert process.returncode == 2
    assert "File exists" in process.stdout
