"""Unit tests verifying contracts_check CLI end-to-end, cross-path reproducibility, and boundaries (A25, A26, A27)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class TestContractCLI(unittest.TestCase):
    """A25 ~ A27: File-based contracts CLI verification, cross-path invariance, and isolation."""

    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parent.parent.parent
        self.fixtures_dir = self.root / "tests" / "fixtures" / "contracts" / "v1"
        self.legacy_fixture = self.fixtures_dir / "legacy.jsonl"

        # Extract positive-only legacy records
        self.valid_legacy_records: list[dict] = []
        with open(self.legacy_fixture, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                data = json.loads(line)
                if data.get("expected_status") == "complete":
                    self.valid_legacy_records.append(data)

    def _run_cli(self, *cli_args: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(self.root)
        cmd = [sys.executable, "-m", "apps.contracts_check", *cli_args]
        return subprocess.run(
            cmd,
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_cli_adapt_legacy_and_validate_roundtrip(self) -> None:
        """A25: Verify adapt-legacy produces canonical JSON and validate mode reads it back successfully."""
        with tempfile.TemporaryDirectory(prefix="dex-cli-a25-") as tmpdir:
            work_dir = Path(tmpdir)
            input_file = work_dir / "valid_legacy.jsonl"
            input_file.write_text(
                "\n".join(json.dumps(item) for item in self.valid_legacy_records) + "\n",
                encoding="utf-8",
            )
            output_canonical = work_dir / "canonical.jsonl"

            # 1. Run adapt-legacy mode
            proc_adapt = self._run_cli(
                "--input",
                str(input_file),
                "--output",
                str(output_canonical),
                "--mode",
                "adapt-legacy",
            )
            self.assertEqual(proc_adapt.returncode, 0, f"Adapt failed: {proc_adapt.stderr}")
            self.assertIn("[SUCCESS]", proc_adapt.stdout)
            self.assertTrue(output_canonical.is_file())

            # Count output lines
            canonical_lines = [
                line.strip()
                for line in output_canonical.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(canonical_lines), len(self.valid_legacy_records))

            # 2. Run validate mode on canonical output
            output_readback = work_dir / "readback.jsonl"
            proc_val = self._run_cli(
                "--input",
                str(output_canonical),
                "--output",
                str(output_readback),
                "--mode",
                "validate",
            )
            self.assertEqual(proc_val.returncode, 0, f"Validate failed: {proc_val.stderr}")
            self.assertIn("[SUCCESS]", proc_val.stdout)
            self.assertTrue(output_readback.is_file())

            # Verify exact byte-level invariance after roundtrip
            self.assertEqual(
                output_canonical.read_text(encoding="utf-8"),
                output_readback.read_text(encoding="utf-8"),
            )

    def test_cli_rejection_of_empty_input_file(self) -> None:
        """A25: Verify empty input file exits non-zero fail-closed."""
        with tempfile.TemporaryDirectory(prefix="dex-cli-empty-") as tmpdir:
            empty_file = Path(tmpdir) / "empty.jsonl"
            empty_file.write_text("", encoding="utf-8")
            out_file = Path(tmpdir) / "out.jsonl"

            proc = self._run_cli(
                "--input",
                str(empty_file),
                "--output",
                str(out_file),
                "--mode",
                "validate",
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("empty", proc.stderr.lower())

    def test_cli_rejection_of_nonexistent_input_file(self) -> None:
        """A25: Verify missing input file exits non-zero fail-closed."""
        with tempfile.TemporaryDirectory(prefix="dex-cli-missing-") as tmpdir:
            non_existent = Path(tmpdir) / "non_existent.jsonl"
            out_file = Path(tmpdir) / "out.jsonl"

            proc = self._run_cli(
                "--input",
                str(non_existent),
                "--output",
                str(out_file),
                "--mode",
                "validate",
            )
            self.assertNotEqual(proc.returncode, 0)

    def test_cli_rejection_of_corrupt_json_lines(self) -> None:
        """A25: Verify corrupt syntax in input file exits non-zero fail-closed."""
        with tempfile.TemporaryDirectory(prefix="dex-cli-corrupt-") as tmpdir:
            corrupt_file = Path(tmpdir) / "corrupt.jsonl"
            corrupt_file.write_text('{"valid": 1}\n{not valid json\n', encoding="utf-8")
            out_file = Path(tmpdir) / "out.jsonl"

            proc = self._run_cli(
                "--input",
                str(corrupt_file),
                "--output",
                str(out_file),
                "--mode",
                "validate",
            )
            self.assertNotEqual(proc.returncode, 0)

    def test_cli_rejection_of_negative_fixture(self) -> None:
        """A25: Verify adapt-legacy on full fixture containing negative counter-examples exits non-zero."""
        with tempfile.TemporaryDirectory(prefix="dex-cli-neg-") as tmpdir:
            out_file = Path(tmpdir) / "out.jsonl"
            proc = self._run_cli(
                "--input",
                str(self.legacy_fixture),
                "--output",
                str(out_file),
                "--mode",
                "adapt-legacy",
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("[FAIL]", proc.stderr)
            self.assertIn("missing", proc.stderr.lower())

    def test_cli_rejection_of_prohibited_network_or_dryrun_flags(self) -> None:
        """A25: Verify prohibited flags (--rpc, --dry-run, --wallet) are rejected by CLI."""
        with tempfile.TemporaryDirectory(prefix="dex-cli-flags-") as tmpdir:
            in_file = Path(tmpdir) / "in.jsonl"
            in_file.write_text("{}", encoding="utf-8")
            out_file = Path(tmpdir) / "out.jsonl"

            for bad_flag in (
                "--dry-run",
                "--rpc",
                "http://localhost:8545",
                "--wallet",
                "--private-key",
            ):
                proc = self._run_cli(
                    "--input",
                    str(in_file),
                    "--output",
                    str(out_file),
                    "--mode",
                    "validate",
                    bad_flag,
                )
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("unrecognized arguments", proc.stderr.lower())

    def test_cli_cross_path_reproducibility(self) -> None:
        """A26: Verify executing on the same input from two separate paths yields identical SHA256 hashes."""
        with (
            tempfile.TemporaryDirectory(prefix="dex-path-1-") as dir1,
            tempfile.TemporaryDirectory(prefix="dex-path-2-") as dir2,
        ):
            path1 = Path(dir1)
            path2 = Path(dir2)

            in1 = path1 / "input.jsonl"
            in2 = path2 / "input.jsonl"
            payload = "\n".join(json.dumps(item) for item in self.valid_legacy_records) + "\n"
            in1.write_text(payload, encoding="utf-8")
            in2.write_text(payload, encoding="utf-8")

            out1 = path1 / "output.jsonl"
            out2 = path2 / "output.jsonl"

            proc1 = self._run_cli(
                "--input", str(in1), "--output", str(out1), "--mode", "adapt-legacy"
            )
            proc2 = self._run_cli(
                "--input", str(in2), "--output", str(out2), "--mode", "adapt-legacy"
            )

            self.assertEqual(proc1.returncode, 0)
            self.assertEqual(proc2.returncode, 0)

            hash1 = hashlib.sha256(out1.read_bytes()).hexdigest()
            hash2 = hashlib.sha256(out2.read_bytes()).hexdigest()
            self.assertEqual(hash1, hash2, "Cross-path output hashes must be 100% identical")

    def test_cli_subprocess_isolation_no_legacy_imports(self) -> None:
        """A27: Verify CLI execution loads zero legacy modules."""
        script_code = """
import sys
from pathlib import Path

project_root = Path('.').resolve()
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import apps.contracts_check as cc

forbidden_prefixes = (
    "arbitrage",
    "backtest",
    "chains",
    "core",
    "execution",
    "monitors",
)

leaks = [
    name for name in sorted(sys.modules.keys())
    if any(name == p or name.startswith(p + ".") for p in forbidden_prefixes)
]

if leaks:
    print(f"CLI_LEAKS:{leaks}")
    sys.exit(1)

print("CLI_ISOLATION_OK")
sys.exit(0)
"""
        proc = subprocess.run(
            [sys.executable, "-I", "-c", script_code],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, f"CLI isolation failed: {proc.stderr}")
        self.assertIn("CLI_ISOLATION_OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
