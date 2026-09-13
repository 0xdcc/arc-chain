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

    def test_collect_cli_live_rejection_has_zero_filesystem_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_live_nomutate_") as tmpdir:
            out_dir = Path(tmpdir) / "should-not-exist"
            cmd = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--from-block",
                "100",
                "--to-block",
                "101",
                "--output-dir",
                str(out_dir),
                "--rpc-endpoint",
                "https://rpc.arc.network",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            assert res.returncode == 2
            assert "requires verified endpoint authorization" in res.stderr
            assert not out_dir.exists()

    def test_collect_cli_fixture_refuses_to_clobber_existing_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_collect_noclobber_") as tmpdir:
            out_dir = Path(tmpdir)
            sentinel = out_dir / "raw_envelopes.jsonl"
            sentinel.write_text("ORIGINAL\n", encoding="utf-8")
            cmd = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--from-block",
                "100",
                "--to-block",
                "101",
                "--output-dir",
                str(out_dir),
                "--fixture-mode",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            assert res.returncode == 2
            assert "Refusing to overwrite existing collection evidence" in res.stderr
            assert sentinel.read_text(encoding="utf-8") == "ORIGINAL\n"
            assert not (out_dir / "coverage_manifest.json").exists()
            assert not (out_dir / "cursor.json").exists()

    def test_collect_cli_fixture_refuses_to_clobber_existing_manifest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_collect_manifest_") as tmpdir:
            out_dir = Path(tmpdir)
            sentinel = out_dir / "coverage_manifest.json"
            sentinel.write_text("ORIGINAL_MANIFEST\n", encoding="utf-8")
            cmd = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--from-block",
                "100",
                "--to-block",
                "101",
                "--output-dir",
                str(out_dir),
                "--fixture-mode",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            assert res.returncode == 2
            assert "Refusing to overwrite existing collection evidence" in res.stderr
            assert sentinel.read_text(encoding="utf-8") == "ORIGINAL_MANIFEST\n"
            assert not (out_dir / "raw_envelopes.jsonl").exists()
            assert not (out_dir / "cursor.json").exists()

    def test_collect_cli_fixture_refuses_to_clobber_existing_cursor(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_collect_cursor_") as tmpdir:
            out_dir = Path(tmpdir)
            sentinel = out_dir / "cursor.json"
            sentinel.write_text('{"chain_id": 5042, "last_block": 999}\n', encoding="utf-8")
            cmd = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--from-block",
                "100",
                "--to-block",
                "101",
                "--output-dir",
                str(out_dir),
                "--fixture-mode",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            assert res.returncode == 2
            assert "Refusing to overwrite existing collection evidence" in res.stderr
            assert not (out_dir / "raw_envelopes.jsonl").exists()
            assert not (out_dir / "coverage_manifest.json").exists()

    def test_collect_cli_fixture_rejects_dangling_symlink(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_collect_symlink_") as tmpdir:
            out_dir = Path(tmpdir) / "out"
            out_dir.mkdir(parents=True, exist_ok=True)
            outside_target = Path(tmpdir) / "victim_target.jsonl"
            sentinel = out_dir / "raw_envelopes.jsonl"
            sentinel.symlink_to(outside_target)
            assert sentinel.is_symlink()
            assert not outside_target.exists()

            cmd = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--from-block",
                "100",
                "--to-block",
                "101",
                "--output-dir",
                str(out_dir),
                "--fixture-mode",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            assert res.returncode == 2
            assert "Refusing to overwrite existing collection evidence" in res.stderr
            assert not outside_target.exists()

    def test_collect_cli_fixture_rejects_manifest_dangling_symlink(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_collect_symmanifest_") as tmpdir:
            out_dir = Path(tmpdir) / "out"
            out_dir.mkdir(parents=True, exist_ok=True)
            outside_target = Path(tmpdir) / "victim_manifest.json"
            sentinel = out_dir / "coverage_manifest.json"
            sentinel.symlink_to(outside_target)
            assert sentinel.is_symlink()
            assert not outside_target.exists()

            cmd = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--from-block",
                "100",
                "--to-block",
                "101",
                "--output-dir",
                str(out_dir),
                "--fixture-mode",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            assert res.returncode == 2
            assert "Refusing to overwrite existing collection evidence" in res.stderr
            assert not outside_target.exists()

    def test_collect_cli_concurrent_race_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_collect_race_") as tmpdir:
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
            assert "Refusing to overwrite existing collection evidence" in err

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

    def test_collect_cli_continuation_manifest_accumulation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_collect_accum_") as tmpdir:
            out_dir = Path(tmpdir)
            # Batch 1: 100..105
            cmd1 = [
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
            res1 = subprocess.run(cmd1, capture_output=True, text=True, check=False)
            assert res1.returncode == 0, f"Batch 1 failed: {res1.stderr}"

            # Batch 2: 106..110
            cmd2 = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--chain-id",
                "5042",
                "--from-block",
                "106",
                "--to-block",
                "110",
                "--output-dir",
                str(out_dir),
                "--fixture-mode",
            ]
            res2 = subprocess.run(cmd2, capture_output=True, text=True, check=False)
            assert res2.returncode == 0, f"Batch 2 failed: {res2.stderr}"

            # Verify manifest accumulates complete range
            manifest = json.loads((out_dir / "coverage_manifest.json").read_text())
            assert manifest["from_block"] == 100
            assert manifest["to_block"] == 110
            assert manifest["covered_blocks"] == 11
            assert manifest["expected_blocks"] == 11
            assert manifest["coverage_ratio"] == 1.0

            # Verify cursor points to latest block
            cursor = json.loads((out_dir / "cursor.json").read_text())
            assert cursor["last_block"] == 110
            assert cursor["last_cursor"] == "cur_5042_110"

            # Verify raw envelopes has all 11 records
            lines = [
                json.loads(line)
                for line in (out_dir / "raw_envelopes.jsonl").read_text().splitlines()
                if line
            ]
            assert len(lines) == 11
            assert lines[0]["block_number"] == 100
            assert lines[-1]["block_number"] == 110

    def test_collect_cli_continuation_concurrency_race_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_collect_crace_") as tmpdir:
            out_dir = Path(tmpdir)
            # Seed with batch 100..100
            cmd0 = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--chain-id",
                "5042",
                "--from-block",
                "100",
                "--to-block",
                "100",
                "--output-dir",
                str(out_dir),
                "--fixture-mode",
            ]
            res0 = subprocess.run(cmd0, capture_output=True, text=True, check=False)
            assert res0.returncode == 0

            # Launch two concurrent workers both trying to append 101..105
            cmd = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--chain-id",
                "5042",
                "--from-block",
                "101",
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
            assert "Refusing to overwrite existing collection evidence" in err

            # Exactly 6 lines: 100 from seed, 101..105 from winning worker. Zero duplicates.
            lines = [
                json.loads(line)
                for line in (out_dir / "raw_envelopes.jsonl").read_text().splitlines()
                if line
            ]
            assert len(lines) == 6
            assert [line["block_number"] for line in lines] == [100, 101, 102, 103, 104, 105]

    def test_collect_cli_continuation_sequential_multibatch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_collect_multi_") as tmpdir:
            out_dir = Path(tmpdir)
            batches = [(100, 102), (103, 105), (106, 108)]
            for fb, tb in batches:
                cmd = [
                    sys.executable,
                    str(_CLI_SCRIPT),
                    "--chain-id",
                    "5042",
                    "--from-block",
                    str(fb),
                    "--to-block",
                    str(tb),
                    "--output-dir",
                    str(out_dir),
                    "--fixture-mode",
                ]
                res = subprocess.run(cmd, capture_output=True, text=True, check=False)
                assert res.returncode == 0

            manifest = json.loads((out_dir / "coverage_manifest.json").read_text())
            assert manifest["from_block"] == 100
            assert manifest["to_block"] == 108
            assert manifest["covered_blocks"] == 9
            lines = [
                json.loads(line)
                for line in (out_dir / "raw_envelopes.jsonl").read_text().splitlines()
                if line
            ]
            assert len(lines) == 9

    def test_collect_cli_rejects_symlink_output_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_collect_symdir_") as tmpdir:
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
                "102",
                "--output-dir",
                str(sym_dir),
                "--fixture-mode",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            assert res.returncode == 2
            assert "Symlink detected in path component" in res.stderr
            assert list(victim_dir.iterdir()) == []

    def test_collect_cli_rejects_symlink_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_collect_symparent_") as tmpdir:
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
                "102",
                "--output-dir",
                str(target),
                "--fixture-mode",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            assert res.returncode == 2
            assert "Symlink detected in path component" in res.stderr
            assert list(real_parent.iterdir()) == []

    def test_collect_cli_inconsistent_continuation_manifest_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_collect_corrupt_") as tmpdir:
            out_dir = Path(tmpdir)
            cmd1 = [
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
            res1 = subprocess.run(cmd1, capture_output=True, text=True, check=False)
            assert res1.returncode == 0

            # Corrupt manifest covered_blocks
            manifest_file = out_dir / "coverage_manifest.json"
            m = json.loads(manifest_file.read_text())
            m["covered_blocks"] = 999
            manifest_file.write_text(json.dumps(m))

            cmd2 = [
                sys.executable,
                str(_CLI_SCRIPT),
                "--from-block",
                "106",
                "--to-block",
                "110",
                "--output-dir",
                str(out_dir),
                "--fixture-mode",
            ]
            res2 = subprocess.run(cmd2, capture_output=True, text=True, check=False)
            assert res2.returncode == 2
            assert "Refusing to overwrite existing collection evidence" in res2.stderr
            # Original raw envelopes intact
            lines = (out_dir / "raw_envelopes.jsonl").read_text().splitlines()
            assert len(lines) == 6

    def test_collect_cli_interrupted_partial_write_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_test_collect_partial_") as tmpdir:
            out_dir = Path(tmpdir)
            # Only raw_envelopes exists, manifest and cursor missing (simulating process crash)
            env_file = out_dir / "raw_envelopes.jsonl"
            env_file.write_text('{"block_number": 100}\n')

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
            assert "Refusing to overwrite existing collection evidence" in res.stderr
            assert env_file.read_text() == '{"block_number": 100}\n'
