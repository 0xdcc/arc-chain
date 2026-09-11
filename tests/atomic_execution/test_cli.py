"""Comprehensive CLI test suite for apps/atomic_simulate.py (C21, C23).

Verifies:
- C21 Hard Interceptions: fail-closed rejection of --live, --send, --broadcast, --approve, --mode live.
- End-to-end simulation execution via CLI with valid exit code 0.
- Summary and record exports with conservation assertions.
- Input validation: missing args, empty files, malformed JSON, and duplicate keys return exit 2.
- Both in-process main() calls and clean subprocess execution.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from apps.atomic_simulate import main

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "atomic_execution" / "v1"
E2E_STREAM_PATH = FIXTURES_DIR / "e2e-stream.jsonl"
CLI_SCRIPT_PATH = Path(__file__).resolve().parent.parent.parent / "apps" / "atomic_simulate.py"


def _run_subprocess(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Execute subprocess through conftest original handles to bypass test isolation."""
    conftest = sys.modules.get("tests.conftest")
    orig_popen = getattr(conftest, "_ORIG_SUBPROCESS_POPEN", None) if conftest else None
    orig_run = (
        getattr(conftest, "_ORIG_SUBPROCESS_RUN", subprocess.run) if conftest else subprocess.run
    )

    if orig_popen is not None:
        saved_popen = subprocess.Popen
        setattr(subprocess, "Popen", orig_popen)  # noqa: B010
        try:
            return orig_run(*args, **kwargs)
        finally:
            setattr(subprocess, "Popen", saved_popen)  # noqa: B010
    return orig_run(*args, **kwargs)


# ==============================================================================
# 1. C21 Destructive / Live Flag Interception Tests (In-Process)
# ==============================================================================


@pytest.mark.parametrize(
    "flag",
    [
        "--live",
        "--send",
        "--broadcast",
        "--approve",
        "--transact",
        "--write",
        "--sign",
        "--execute",
    ],
)
def test_c21_forbidden_flags_intercepted_in_process(flag: str) -> None:
    """Any destructive flag passed to CLI main() returns exit code 1 fail-closed."""
    code = main([flag, "--input", str(E2E_STREAM_PATH)])
    assert code == 1


def test_c21_mode_live_intercepted_in_process() -> None:
    """Passing --mode live returns exit code 1 fail-closed."""
    code = main(["--mode", "live", "--input", str(E2E_STREAM_PATH)])
    assert code == 1


def test_c21_mode_equals_live_intercepted_in_process() -> None:
    """Passing --mode=live returns exit code 1 fail-closed."""
    code = main(["--mode=live", "--input", str(E2E_STREAM_PATH)])
    assert code == 1


# ==============================================================================
# 2. C21 Destructive / Live Flag Interception Tests (Subprocess)
# ==============================================================================


@pytest.mark.parametrize(
    "flag",
    [
        "--live",
        "--send",
        "--broadcast",
        "--approve",
    ],
)
def test_c21_forbidden_flags_intercepted_subprocess(flag: str) -> None:
    """Subprocess execution with destructive flags must exit 1 with security warning."""
    proc = _run_subprocess(
        [sys.executable, str(CLI_SCRIPT_PATH), flag, "--input", str(E2E_STREAM_PATH)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "FATAL SECURITY VIOLATION" in proc.stderr
    assert "strictly prohibited" in proc.stderr


def test_c21_mode_live_intercepted_subprocess() -> None:
    """Subprocess execution with --mode live must exit 1."""
    proc = _run_subprocess(
        [sys.executable, str(CLI_SCRIPT_PATH), "--mode", "live", "--input", str(E2E_STREAM_PATH)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "FATAL SECURITY VIOLATION" in proc.stderr


# ==============================================================================
# 3. Successful Offline Simulation Tests (In-Process & Subprocess)
# ==============================================================================


def test_cli_successful_offline_simulation(tmp_path: Path) -> None:
    """CLI processes e2e-stream.jsonl, produces output and summary, and exits 0."""
    out_file = tmp_path / "out.jsonl"
    sum_file = tmp_path / "summary.json"

    code = main(
        [
            "--mode",
            "offline",
            "--input",
            str(E2E_STREAM_PATH),
            "--output",
            str(out_file),
            "--summary-output",
            str(sum_file),
        ]
    )
    assert code == 0
    assert out_file.is_file()
    assert sum_file.is_file()

    summary_data = json.loads(sum_file.read_text(encoding="utf-8"))
    assert summary_data["total_processed"] >= 12
    assert summary_data["passed_count"] == 3
    assert summary_data["simulated_success_count"] == 3
    assert summary_data["is_conserved"] is True

    lines = out_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == summary_data["total_processed"]


def test_cli_successful_subprocess_execution(tmp_path: Path) -> None:
    """Subprocess invocation of CLI processes stream and returns exit 0."""
    out_file = tmp_path / "sub_out.jsonl"
    proc = _run_subprocess(
        [
            sys.executable,
            str(CLI_SCRIPT_PATH),
            "--mode",
            "offline",
            "--input",
            str(E2E_STREAM_PATH),
            "--output",
            str(out_file),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert out_file.is_file()


def test_cli_stdout_fallback_when_no_output_file(capsys: pytest.CaptureFixture[str]) -> None:
    """When --output is omitted, CLI prints summary JSON to stdout and exits 0."""
    code = main(["--mode", "offline", "--input", str(E2E_STREAM_PATH)])
    assert code == 0
    captured = capsys.readouterr()
    summary_obj = json.loads(captured.out)
    assert summary_obj["is_conserved"] is True
    assert summary_obj["total_processed"] >= 12


# ==============================================================================
# 4. Fail-Closed Input & Validation Tests
# ==============================================================================


def test_cli_missing_input_parameter_fails() -> None:
    """Omitting required --input returns exit code 2."""
    code = main(["--mode", "offline"])
    assert code == 2


def test_cli_empty_input_file_fails(tmp_path: Path) -> None:
    """Empty input file returns exit code 2."""
    empty_file = tmp_path / "empty.jsonl"
    empty_file.write_text("", encoding="utf-8")
    code = main(["--mode", "offline", "--input", str(empty_file)])
    assert code == 2


def test_cli_nonexistent_input_file_fails(tmp_path: Path) -> None:
    """Non-existent input file returns exit code 2."""
    missing_file = tmp_path / "nonexistent.jsonl"
    code = main(["--mode", "offline", "--input", str(missing_file)])
    assert code == 2


def test_cli_malformed_json_input_fails(tmp_path: Path) -> None:
    """Malformed JSON returns exit code 2."""
    bad_file = tmp_path / "bad.jsonl"
    bad_file.write_text("not a json\n", encoding="utf-8")
    code = main(["--mode", "offline", "--input", str(bad_file)])
    assert code == 2


def test_cli_duplicate_json_keys_fails(tmp_path: Path) -> None:
    """Duplicate keys in JSON object return exit code 2."""
    dup_file = tmp_path / "dup.jsonl"
    dup_file.write_text('{"a": 1, "a": 2}\n', encoding="utf-8")
    code = main(["--mode", "offline", "--input", str(dup_file)])
    assert code == 2
