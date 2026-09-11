"""Unit tests for strict mypy validation gate (scripts/test_safety_mypy_checker.py).

Verifies fail-closed semantics against tool crashes, traceback/internal error tails,
unparseable lines, anomalous return codes, and unknown error diagnostics.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.test_safety_mypy_checker import check_mypy_output, classify_line


def test_classify_line_categories() -> None:
    """classify_line correctly categorizes errors, notes, summaries, crashes, and unknown."""
    assert classify_line("core/config.py:10: error: Incompatible types")[0] == "ERROR"
    assert classify_line("core/config.py:10: note: See details")[0] == "NOTE"
    assert classify_line("Found 4 errors in 1 file (checked 80 source files)")[0] == "SUMMARY"
    assert classify_line("Success: no issues found in 50 source files")[0] == "SUMMARY"

    # Crash markers
    assert classify_line("Traceback (most recent call last):")[0] == "CRASH"
    assert classify_line("mypy: INTERNAL ERROR: something crashed")[0] == "CRASH"
    assert classify_line("usage: mypy [-h] [--config-file]")[0] == "CRASH"
    assert classify_line("mypy: error: unrecognized arguments: --foo")[0] == "CRASH"

    # Unknown
    assert classify_line("random unformatted garbage text")[0] == "UNKNOWN"


def test_fail_closed_on_probe_p2_traceback_in_stderr(tmp_path: Path) -> None:
    """REVIEW_1A_INDEPENDENT.md P2 probe: exit 1 + known error + stderr traceback must be RED."""
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("a.py:1: error: known\n", encoding="utf-8")

    code = check_mypy_output(
        returncode=1,
        stdout="a.py:1: error: known\n",
        stderr="Traceback (most recent call last):\nRuntimeError: crash\n",
        baseline_path=baseline,
    )
    assert code == 1, "Must fail closed when stderr contains Traceback / crash information"


def test_fail_closed_on_internal_error_in_stdout(tmp_path: Path) -> None:
    """Exit 1 + known error + INTERNAL ERROR in stdout must be RED."""
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("a.py:1: error: known\n", encoding="utf-8")

    code = check_mypy_output(
        returncode=1,
        stdout="a.py:1: error: known\nmypy: INTERNAL ERROR: uncaught exception\n",
        stderr="",
        baseline_path=baseline,
    )
    assert code == 1


def test_fail_closed_on_unparseable_output(tmp_path: Path) -> None:
    """Exit 1 + known error + unparseable line must be RED."""
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("a.py:1: error: known\n", encoding="utf-8")

    code = check_mypy_output(
        returncode=1,
        stdout="a.py:1: error: known\nUnexpected parser output\n",
        stderr="",
        baseline_path=baseline,
    )
    assert code == 1


def test_fail_closed_on_exit_0_with_errors(tmp_path: Path) -> None:
    """Exit 0 with error diagnostic emitted must be RED (anomalous exit code)."""
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("a.py:1: error: known\n", encoding="utf-8")

    code = check_mypy_output(
        returncode=0,
        stdout="a.py:1: error: known\n",
        stderr="",
        baseline_path=baseline,
    )
    assert code == 1


def test_fail_closed_on_exit_1_with_zero_errors(tmp_path: Path) -> None:
    """Exit 1 with zero parsed errors must be RED."""
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("a.py:1: error: known\n", encoding="utf-8")

    code = check_mypy_output(
        returncode=1,
        stdout="Success: no issues found\n",
        stderr="",
        baseline_path=baseline,
    )
    assert code == 1


def test_fail_closed_on_exit_2_or_signals(tmp_path: Path) -> None:
    """Non 0/1 exit codes (e.g. exit 2, exit 139) must be RED."""
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("a.py:1: error: known\n", encoding="utf-8")

    for rc in (2, 127, 139, -9):
        code = check_mypy_output(
            returncode=rc,
            stdout="",
            stderr="mypy fatal error",
            baseline_path=baseline,
        )
        assert code == 1


def test_fail_closed_on_new_error(tmp_path: Path) -> None:
    """Exit 1 with an error not present in baseline must be RED."""
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("a.py:1: error: known\n", encoding="utf-8")

    code = check_mypy_output(
        returncode=1,
        stdout="a.py:1: error: known\nb.py:2: error: newly introduced error\n",
        stderr="",
        baseline_path=baseline,
    )
    assert code == 1


def test_clean_pass_on_exact_baseline_with_notes_and_summary(tmp_path: Path) -> None:
    """Exit 1 passes when all errors are in baseline and non-error lines are notes/summary."""
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("a.py:1: error: known error [code]\n", encoding="utf-8")

    stdout = (
        "a.py:1: note: By default bodies are not checked\n"
        "a.py:1: error: known error [code]\n"
        "Found 1 error in 1 file (checked 10 source files)\n"
    )
    code = check_mypy_output(
        returncode=1,
        stdout=stdout,
        stderr="",
        baseline_path=baseline,
    )
    assert code == 0


def test_clean_pass_on_exit_0_without_errors(tmp_path: Path) -> None:
    """Exit 0 passes when no errors are emitted."""
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("", encoding="utf-8")

    stdout = "Success: no issues found in 84 source files\n"
    code = check_mypy_output(
        returncode=0,
        stdout=stdout,
        stderr="",
        baseline_path=baseline,
    )
    assert code == 0


def test_fail_closed_on_missing_baseline_file(tmp_path: Path) -> None:
    """Missing baseline file must return 1."""
    missing = tmp_path / "non_existent_baseline.txt"
    code = check_mypy_output(
        returncode=0,
        stdout="",
        stderr="",
        baseline_path=missing,
    )
    assert code == 1
