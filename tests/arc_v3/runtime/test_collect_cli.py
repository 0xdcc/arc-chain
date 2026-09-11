"""Tests for Arc Ingest CLI Entrypoint (T37)

Covers:
- End-to-end execution of apps/arc_collect.py in fixture mode
- Validation of output files: raw_envelopes.jsonl, coverage_manifest.json, cursor.json
- Fail-closed error handling on invalid ranges and batch limits
- Rejection of live RPC requests without G1_LIVE authorization
- Subprocess isolation verifying zero legacy monolith imports
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CLI_SCRIPT = _REPO_ROOT / "apps" / "arc_collect.py"


class TestArcCollectCLI:
    """Test suite for T37 arc_collect CLI."""

    def test_collect_cli_fixture_mode_success(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_collect_") as tmpdir:
            out_dir = Path(tmpdir)
            cmd = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--chain-id",
                "5042",
                "--from-block",
                "100",
                "--to-block",
                "105",
                "--output-dir",
                str(out_dir),
                "--fixture-mode",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            assert res.returncode == 0, f"CLI execution failed: {res.stderr}"

            # Check stdout JSON
            stdout_data = json.loads(res.stdout)
            assert stdout_data["status"] == "SUCCESS"
            assert stdout_data["envelopes_written"] == 6
            assert stdout_data["range"] == [100, 105]

            # Check written files
            envelopes_file = out_dir / "raw_envelopes.jsonl"
            manifest_file = out_dir / "coverage_manifest.json"
            cursor_file = out_dir / "cursor.json"

            assert envelopes_file.is_file()
            assert manifest_file.is_file()
            assert cursor_file.is_file()

            # Verify JSONL lines
            lines = [json.loads(line) for line in envelopes_file.read_text().splitlines() if line]
            assert len(lines) == 6
            assert lines[0]["block_number"] == 100
            assert lines[0]["chain_id"] == 5042
            assert lines[0]["block_domain"] == "l1"
            assert lines[-1]["block_number"] == 105

            # Verify Manifest
            manifest = json.loads(manifest_file.read_text())
            assert manifest["expected_blocks"] == 6
            assert manifest["covered_blocks"] == 6
            assert manifest["coverage_ratio"] == 1.0

            # Verify Cursor
            cursor = json.loads(cursor_file.read_text())
            assert cursor["last_block"] == 105
            assert cursor["last_cursor"] == "cur_5042_105"

    def test_collect_cli_range_validation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_range_") as tmpdir:
            cmd = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--from-block",
                "200",
                "--to-block",
                "100",  # inverted range
                "--output-dir",
                tmpdir,
                "--fixture-mode",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            assert res.returncode == 2
            assert "cannot exceed" in res.stderr.lower()

    def test_collect_cli_batch_limit_exceeded(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_limit_") as tmpdir:
            cmd = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--from-block",
                "1",
                "--to-block",
                "1005",  # > 1000 blocks
                "--output-dir",
                tmpdir,
                "--fixture-mode",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            assert res.returncode == 2
            assert "max block batch range" in res.stderr.lower()

    def test_collect_cli_live_mode_rejected_without_auth(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_live_") as tmpdir:
            cmd = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--from-block",
                "100",
                "--to-block",
                "101",
                "--output-dir",
                tmpdir,
                "--rpc-endpoint",
                "https://rpc.arc.network",
                # note: no --fixture-mode
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            assert res.returncode == 2
            assert "requires verified endpoint authorization" in res.stderr

    def test_collect_cli_prohibited_flags_rejected(self) -> None:
        for bad_flag in ("--wallet", "--private-key", "--sign", "--broadcast"):
            cmd = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--from-block",
                "100",
                "--to-block",
                "101",
                "--output-dir",
                "/tmp",
                bad_flag,
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            assert res.returncode == 2
            assert "unrecognized arguments" in res.stderr.lower()

    def test_collect_cli_zero_legacy_imports(self) -> None:
        script = """
import sys
from pathlib import Path

repo_root = Path('.').resolve()
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

initial = set(sys.modules.keys())
import apps.arc_collect
import arc_runtime.collect
new_modules = set(sys.modules.keys()) - initial

forbidden = ("core", "chains", "backtest", "monitors", "execution")
leaks = [m for m in new_modules if any(m == p or m.startswith(p + ".") for p in forbidden)]
if leaks:
    print(f"LEAKS_DETECTED:{leaks}")
    sys.exit(1)

print("ZERO_LEGACY_IMPORTS_PASSED")
sys.exit(0)
"""
        res = subprocess.run(
            [sys.executable, "-I", "-c", script],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert res.returncode == 0, f"Module leak detected: {res.stdout} {res.stderr}"
        assert "ZERO_LEGACY_IMPORTS_PASSED" in res.stdout
