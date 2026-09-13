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

    def test_shadow_cli_live_rejection_has_zero_filesystem_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_shadow_nomutate_") as tmpdir:
            ledger_dir = Path(tmpdir) / "should-not-exist"
            cmd = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--ledger-dir",
                str(ledger_dir),
                "--rpc-endpoint",
                "https://rpc.arc.network",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            assert res.returncode == 2
            assert "Live shadow evaluation requires verified RPC" in res.stderr
            assert not ledger_dir.exists()

    def test_shadow_cli_fixture_refuses_to_clobber_existing_ledger(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_shadow_noclobber_") as tmpdir:
            ledger_dir = Path(tmpdir)
            ledger_file = ledger_dir / "opportunities.jsonl"
            ledger_file.write_text("ORIGINAL\n", encoding="utf-8")
            cmd = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--ledger-dir",
                str(ledger_dir),
                "--fixture-mode",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            assert res.returncode == 2
            assert "Refusing to overwrite existing shadow evidence" in res.stderr
            assert ledger_file.read_text(encoding="utf-8") == "ORIGINAL\n"

    def test_shadow_cli_fixture_refuses_to_clobber_existing_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_shadow_ckpt_") as tmpdir:
            ledger_dir = Path(tmpdir)
            ckpt_file = ledger_dir / "opportunities.jsonl.checkpoint"
            ckpt_file.write_text("ORIGINAL_CKPT\n", encoding="utf-8")
            cmd = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--ledger-dir",
                str(ledger_dir),
                "--fixture-mode",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            assert res.returncode == 2
            assert "Refusing to overwrite existing shadow evidence" in res.stderr
            assert ckpt_file.read_text(encoding="utf-8") == "ORIGINAL_CKPT\n"

    def test_shadow_cli_fixture_rejects_dangling_symlink(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_shadow_symlink_") as tmpdir:
            ledger_dir = Path(tmpdir) / "ledgers"
            ledger_dir.mkdir(parents=True, exist_ok=True)
            outside_target = Path(tmpdir) / "victim_ledger.jsonl"
            sentinel = ledger_dir / "opportunities.jsonl"
            sentinel.symlink_to(outside_target)
            assert sentinel.is_symlink()
            assert not outside_target.exists()

            cmd = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--ledger-dir",
                str(ledger_dir),
                "--fixture-mode",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            assert res.returncode == 2
            assert "Refusing to overwrite existing shadow evidence" in res.stderr
            assert not outside_target.exists()

    def test_shadow_cli_fixture_rejects_checkpoint_dangling_symlink(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_shadow_symckpt_") as tmpdir:
            ledger_dir = Path(tmpdir) / "ledgers"
            ledger_dir.mkdir(parents=True, exist_ok=True)
            outside_target = Path(tmpdir) / "victim_checkpoint.json"
            sentinel = ledger_dir / "opportunities.jsonl.checkpoint"
            sentinel.symlink_to(outside_target)
            assert sentinel.is_symlink()
            assert not outside_target.exists()

            cmd = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--ledger-dir",
                str(ledger_dir),
                "--fixture-mode",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            assert res.returncode == 2
            assert "Refusing to overwrite existing shadow evidence" in res.stderr
            assert not outside_target.exists()

    def test_shadow_cli_concurrent_race_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_shadow_race_") as tmpdir:
            ledger_dir = Path(tmpdir) / "ledgers"
            cmd = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--ledger-dir",
                str(ledger_dir),
                "--fixture-mode",
            ]
            p1 = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            p2 = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            out1, err1 = p1.communicate()
            out2, err2 = p2.communicate()
            rcs = sorted([p1.returncode, p2.returncode])
            assert rcs == [0, 2], f"Expected [0, 2], got {rcs}"
            err = err1 if p1.returncode == 2 else err2
            assert "Refusing to overwrite existing shadow evidence" in err

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

    def test_shadow_cli_rejects_symlink_ledger_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_shadow_symdir_") as tmpdir:
            victim_dir = Path(tmpdir) / "victim_store"
            victim_dir.mkdir()
            sym_dir = Path(tmpdir) / "sym_store"
            sym_dir.symlink_to(victim_dir)

            cmd = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--ledger-dir",
                str(sym_dir),
                "--fixture-mode",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            assert res.returncode == 2
            assert "Symlink detected in path component" in res.stderr
            assert list(victim_dir.iterdir()) == []

    def test_shadow_cli_rejects_symlink_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_shadow_symparent_") as tmpdir:
            real_parent = Path(tmpdir) / "real_parent"
            real_parent.mkdir()
            sym_parent = Path(tmpdir) / "sym_parent"
            sym_parent.symlink_to(real_parent)
            target = sym_parent / "ledgers"

            cmd = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--ledger-dir",
                str(target),
                "--fixture-mode",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            assert res.returncode == 2
            assert "Symlink detected in path component" in res.stderr
            assert list(real_parent.iterdir()) == []

    def test_shadow_cli_no_zero_byte_placeholder_on_early_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_shadow_nofake_") as tmpdir:
            ledger_dir = Path(tmpdir) / "ledgers"
            cmd = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--ledger-dir",
                str(ledger_dir),
                "--max-amount-usd",
                "999.0",  # invalid: exceeds 500 cap
                "--fixture-mode",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            assert res.returncode == 2
            assert "max_amount_usd must be in" in res.stderr
            # Ensure no 0-byte fake placeholder exists on disk
            assert not (ledger_dir / "opportunities.jsonl").exists()
