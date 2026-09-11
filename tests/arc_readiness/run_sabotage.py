"""Sabotage regression test harness: proves tests turn RED under physical corruption (C30)."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def run_test_suite(repo_root: Path) -> int:
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "tests/arc_readiness",
        "-o",
        "cache_dir=/tmp/pytest_cache",
        "-q",
    ]
    proc = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True, check=False)
    return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Sabotage Regression Runner for Arc Readiness")
    parser.add_argument(
        "--scratch", type=str, default="/tmp/w6-sabotage", help="Scratch dir for mutations"
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    scratch_dir = Path(args.scratch)

    sys.stdout.write("=== Step 1: Baseline Verification (Must be GREEN) ===\n")
    baseline_code = run_test_suite(repo_root)
    if baseline_code != 0:
        sys.stderr.write("Error: Baseline tests are not green! Cannot proceed with sabotage.\n")
        return 1
    sys.stdout.write("Baseline is 100% GREEN.\n\n")

    # Target files to sabotage
    balances_py = repo_root / "arc_readiness" / "balances.py"
    fees_py = repo_root / "arc_readiness" / "fees.py"

    balances_backup = balances_py.read_text(encoding="utf-8")
    fees_backup = fees_py.read_text(encoding="utf-8")

    try:
        # ----------------------------------------------------------------------
        # Sabotage Mutation 1: Corrupt SCALE_FACTOR in balances.py
        # ----------------------------------------------------------------------
        sys.stdout.write("=== Step 2: Sabotage Mutation 1 - SCALE_FACTOR (Must turn RED) ===\n")
        corrupted_balances = balances_backup.replace(
            "SCALE_FACTOR: int = 1_000_000_000_000",
            "SCALE_FACTOR: int = 1_000_000",
        )
        balances_py.write_text(corrupted_balances, encoding="utf-8")
        sabotage_code_1 = run_test_suite(repo_root)
        if sabotage_code_1 == 0:
            sys.stderr.write("Sabotage failure: Mutation 1 did not turn test suite RED!\n")
            return 1
        sys.stdout.write(f"Confirmed: Mutation 1 turned RED (exit code {sabotage_code_1}).\n\n")

        # ----------------------------------------------------------------------
        # Sabotage Mutation 2: Corrupt fee calculation in fees.py
        # ----------------------------------------------------------------------
        sys.stdout.write(
            "=== Step 3: Sabotage Mutation 2 - Corrupt total_atoms (Must turn RED) ===\n"
        )
        balances_py.write_text(balances_backup, encoding="utf-8")  # restore balances
        corrupted_fees = fees_backup.replace(
            "total_atoms = used * price",
            "total_atoms = price",
        )
        fees_py.write_text(corrupted_fees, encoding="utf-8")
        sabotage_code_2 = run_test_suite(repo_root)
        if sabotage_code_2 == 0:
            sys.stderr.write("Sabotage failure: Mutation 2 did not turn test suite RED!\n")
            return 1
        sys.stdout.write(f"Confirmed: Mutation 2 turned RED (exit code {sabotage_code_2}).\n\n")

        # ----------------------------------------------------------------------
        # Sabotage Mutation 3: Disable circular domain rejection
        # ----------------------------------------------------------------------
        sys.stdout.write(
            "=== Step 4: Sabotage Mutation 3 - Allow circular domain pairs (Must turn RED) ===\n"
        )
        fees_py.write_text(fees_backup, encoding="utf-8")  # restore fees
        corrupted_domain = balances_backup.replace(
            "raise ArcValidationError(",
            "pass # Sabotage bypassed: raise ArcValidationError(",
        )
        balances_py.write_text(corrupted_domain, encoding="utf-8")
        sabotage_code_3 = run_test_suite(repo_root)
        if sabotage_code_3 == 0:
            sys.stderr.write("Sabotage failure: Mutation 3 did not turn test suite RED!\n")
            return 1
        sys.stdout.write(f"Confirmed: Mutation 3 turned RED (exit code {sabotage_code_3}).\n\n")

    finally:
        # Always restore original files cleanly
        balances_py.write_text(balances_backup, encoding="utf-8")
        fees_py.write_text(fees_backup, encoding="utf-8")

    sys.stdout.write("=== Step 5: Post-Restoration Verification (Must be GREEN) ===\n")
    restored_code = run_test_suite(repo_root)
    if restored_code != 0:
        sys.stderr.write("Error: Tests failed after restoring files!\n")
        return 1
    sys.stdout.write("All files restored cleanly; test suite is 100% GREEN.\n")
    sys.stdout.write(
        "SABOTAGE VERIFICATION PASSED: All mutations effectively invalidated assertions.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
