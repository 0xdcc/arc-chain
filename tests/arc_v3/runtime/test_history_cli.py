"""Tests for Arc Historical Replay CLI (T38)

Covers:
- End-to-end execution of apps/arc_history.py in fixture mode
- Verification of PRE_EXECUTION_HYPOTHESIS marking and non-guaranteed profit disclaimer
- Fail-closed error handling on invalid ranges and range caps
- Rejection of live RPC requests without authorization
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CLI_SCRIPT = _REPO_ROOT / "apps" / "arc_history.py"


class TestArcHistoryCLI:
    """Test suite for T38 arc_history CLI."""

    def test_history_cli_fixture_mode_success(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_history_") as tmpdir:
            out_dir = Path(tmpdir)
            cmd = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--chain-id",
                "5042",
                "--from-block",
                "1000",
                "--to-block",
                "1005",
                "--output-dir",
                str(out_dir),
                "--fixture-mode",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            assert res.returncode == 0, f"CLI execution failed: {res.stderr}"

            stdout_data = json.loads(res.stdout)
            assert stdout_data["status"] == "SUCCESS"
            assert stdout_data["scanned_blocks"] == 6
            assert "post-block state hypotheses" in stdout_data["disclaimer"].lower()

            hypotheses_file = out_dir / "history_hypotheses.jsonl"
            assert hypotheses_file.is_file()
            lines = [json.loads(line) for line in hypotheses_file.read_text().splitlines() if line]
            assert len(lines) >= 1
            assert lines[0]["hypothesis_type"] == "PRE_EXECUTION_HYPOTHESIS"
            assert lines[0]["claimed_profit_guaranteed"] is False
            assert lines[0]["block_timestamp"] != lines[0]["observed_at_timestamp"]

    def test_history_cli_invalid_block_range(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_range_") as tmpdir:
            cmd = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--from-block",
                "2000",
                "--to-block",
                "1000",
                "--output-dir",
                tmpdir,
                "--fixture-mode",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            assert res.returncode == 2
            assert "cannot exceed" in res.stderr

    def test_history_cli_batch_limit_exceeded(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_cap_") as tmpdir:
            cmd = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--from-block",
                "1",
                "--to-block",
                "600",  # > 500 blocks limit!
                "--output-dir",
                tmpdir,
                "--fixture-mode",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            assert res.returncode == 2
            assert "capped at 500 blocks" in res.stderr

    def test_history_cli_live_mode_rejected_without_auth(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_live_") as tmpdir:
            cmd = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--from-block",
                "100",
                "--to-block",
                "105",
                "--output-dir",
                tmpdir,
                "--rpc-endpoint",
                "https://rpc.arc.network",
                # no --fixture-mode
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            assert res.returncode == 2
            assert "Live historical scan requires verified RPC" in res.stderr
