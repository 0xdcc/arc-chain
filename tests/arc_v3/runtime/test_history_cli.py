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

    def test_history_cli_live_rejection_has_zero_filesystem_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_history_nomutate_") as tmpdir:
            out_dir = Path(tmpdir) / "should-not-exist"
            cmd = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--from-block",
                "100",
                "--to-block",
                "105",
                "--output-dir",
                str(out_dir),
                "--rpc-endpoint",
                "https://rpc.arc.network",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            assert res.returncode == 2
            assert "Live historical scan requires verified RPC" in res.stderr
            assert not out_dir.exists()

    def test_history_cli_fixture_refuses_to_clobber_existing_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_history_noclobber_") as tmpdir:
            out_dir = Path(tmpdir)
            sentinel = out_dir / "history_hypotheses.jsonl"
            sentinel.write_text("ORIGINAL\n", encoding="utf-8")
            cmd = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--from-block",
                "100",
                "--to-block",
                "105",
                "--output-dir",
                str(out_dir),
                "--fixture-mode",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            assert res.returncode == 2
            assert "Refusing to overwrite existing history evidence" in res.stderr
            assert sentinel.read_text(encoding="utf-8") == "ORIGINAL\n"
            assert not (out_dir / "history_replay_report.json").exists()

    def test_history_cli_fixture_refuses_to_clobber_existing_report(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_history_report_") as tmpdir:
            out_dir = Path(tmpdir)
            sentinel = out_dir / "history_replay_report.json"
            sentinel.write_text("ORIGINAL_REPORT\n", encoding="utf-8")
            cmd = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--from-block",
                "100",
                "--to-block",
                "105",
                "--output-dir",
                str(out_dir),
                "--fixture-mode",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            assert res.returncode == 2
            assert "Refusing to overwrite existing history evidence" in res.stderr
            assert sentinel.read_text(encoding="utf-8") == "ORIGINAL_REPORT\n"
            assert not (out_dir / "history_hypotheses.jsonl").exists()

    def test_history_cli_fixture_rejects_dangling_symlink(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_history_symlink_") as tmpdir:
            out_dir = Path(tmpdir) / "out"
            out_dir.mkdir(parents=True, exist_ok=True)
            outside_target = Path(tmpdir) / "victim_history.jsonl"
            sentinel = out_dir / "history_hypotheses.jsonl"
            sentinel.symlink_to(outside_target)
            assert sentinel.is_symlink()
            assert not outside_target.exists()

            cmd = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--from-block",
                "100",
                "--to-block",
                "105",
                "--output-dir",
                str(out_dir),
                "--fixture-mode",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            assert res.returncode == 2
            assert "Refusing to overwrite existing history evidence" in res.stderr
            assert not outside_target.exists()

    def test_history_cli_fixture_rejects_report_dangling_symlink(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_history_symreport_") as tmpdir:
            out_dir = Path(tmpdir) / "out"
            out_dir.mkdir(parents=True, exist_ok=True)
            outside_target = Path(tmpdir) / "victim_report.json"
            sentinel = out_dir / "history_replay_report.json"
            sentinel.symlink_to(outside_target)
            assert sentinel.is_symlink()
            assert not outside_target.exists()

            cmd = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--from-block",
                "100",
                "--to-block",
                "105",
                "--output-dir",
                str(out_dir),
                "--fixture-mode",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            assert res.returncode == 2
            assert "Refusing to overwrite existing history evidence" in res.stderr
            assert not outside_target.exists()

    def test_history_cli_concurrent_race_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_history_race_") as tmpdir:
            out_dir = Path(tmpdir) / "out"
            cmd = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--from-block",
                "100",
                "--to-block",
                "105",
                "--output-dir",
                str(out_dir),
                "--fixture-mode",
            ]
            p1 = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            p2 = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            out1, err1 = p1.communicate()
            out2, err2 = p2.communicate()
            rcs = sorted([p1.returncode, p2.returncode])
            assert rcs == [0, 2], f"Expected [0, 2], got {rcs}"
            err = err1 if p1.returncode == 2 else err2
            assert "Refusing to overwrite existing history evidence" in err

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

    def test_history_cli_rejects_symlink_output_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_hist_symdir_") as tmpdir:
            victim_dir = Path(tmpdir) / "victim_store"
            victim_dir.mkdir()
            sym_dir = Path(tmpdir) / "sym_store"
            sym_dir.symlink_to(victim_dir)

            cmd = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--from-block",
                "100",
                "--to-block",
                "105",
                "--output-dir",
                str(sym_dir),
                "--fixture-mode",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            assert res.returncode == 2
            assert "Symlink detected in path component" in res.stderr
            assert list(victim_dir.iterdir()) == []

    def test_history_cli_rejects_symlink_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_hist_symparent_") as tmpdir:
            real_parent = Path(tmpdir) / "real_parent"
            real_parent.mkdir()
            sym_parent = Path(tmpdir) / "sym_parent"
            sym_parent.symlink_to(real_parent)
            target = sym_parent / "output_data"

            cmd = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--from-block",
                "100",
                "--to-block",
                "105",
                "--output-dir",
                str(target),
                "--fixture-mode",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            assert res.returncode == 2
            assert "Symlink detected in path component" in res.stderr
            assert list(real_parent.iterdir()) == []
