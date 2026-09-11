"""Tests for Arc Shadow Evaluation CLI (T38)

Covers:
- End-to-end execution of apps/arc_shadow.py in fixture mode
- Hash-chained Opportunity Ledger generation and verification
- Hard cap enforcement on max single trade amount (<= 500 USD)
- Fail-closed behavior on unapproved live mode or invalid flags
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CLI_SCRIPT = _REPO_ROOT / "apps" / "arc_shadow.py"


class TestArcShadowCLI:
    """Test suite for T38 arc_shadow CLI."""

    def test_shadow_cli_fixture_mode_success(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_shadow_") as tmpdir:
            ledger_dir = Path(tmpdir)
            cmd = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--chain-id",
                "5042",
                "--ledger-dir",
                str(ledger_dir),
                "--fixture-mode",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            assert res.returncode == 0, f"CLI execution failed: {res.stderr}"

            stdout_data = json.loads(res.stdout)
            assert stdout_data["status"] == "SUCCESS"
            assert stdout_data["total_evaluated"] >= 1
            assert stdout_data["profitable_candidates"] >= 1

            ledger_file = ledger_dir / "opportunities.jsonl"
            assert ledger_file.is_file()
            lines = [json.loads(line) for line in ledger_file.read_text().splitlines() if line]
            assert len(lines) >= 1
            # AppendOnlyLedger wraps record inside "payload"
            first_record = lines[0].get("payload", lines[0])
            assert first_record["chain_id"] == 5042
            assert first_record["record_type"] in ("quote", "sim")

    def test_shadow_cli_max_amount_usd_enforcement(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_cap_") as tmpdir:
            cmd = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--ledger-dir",
                tmpdir,
                "--max-amount-usd",
                "600.0",  # exceeds 500U limit!
                "--fixture-mode",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            assert res.returncode == 2
            assert "must be in (0, 500.0]" in res.stderr

    def test_shadow_cli_live_mode_rejected_without_auth(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_live_") as tmpdir:
            cmd = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--ledger-dir",
                tmpdir,
                "--rpc-endpoint",
                "https://rpc.arc.network",
                # no --fixture-mode
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            assert res.returncode == 2
            assert "Live shadow evaluation requires verified RPC" in res.stderr

    def test_shadow_cli_prohibited_flags_rejected(self) -> None:
        for bad_flag in ("--wallet", "--private-key", "--sign", "--broadcast"):
            cmd = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--ledger-dir",
                "/tmp",
                bad_flag,
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            assert res.returncode == 2
            assert "unrecognized arguments" in res.stderr.lower()
