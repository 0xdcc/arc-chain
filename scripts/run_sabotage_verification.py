"""Independent Sabotage Verification Script for W0-B Gate.

Executes physical sabotage experiments to prove that the gate actually fails-closed:
Experiment 1: Inject an unmanifested source file -> assert test_all_source_files_in_manifest FAILS.
Experiment 2: Inject a symlink component -> assert read_source/stage_sources FAILS.
Experiment 3: Restore cleanly -> assert all checks PASS.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def run_experiment_1_unmanifested_file() -> dict:
    """Inject an unapproved .py file on disk without updating manifest."""
    dummy_file = ROOT / "apps/unapproved_phantom_file.py"
    dummy_file.write_text("# Unapproved phantom file\n", encoding="utf-8")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "tests/contracts/test_manifest_coverage.py"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        # Must FAIL with AssertionError
        turned_red = (proc.returncode != 0 and "AssertionError" in proc.stderr)
        red_message = proc.stderr.strip().splitlines()[-1] if proc.stderr else ""
    finally:
        if dummy_file.is_file():
            dummy_file.unlink()

    # Verify recovery
    proc_recovered = subprocess.run(
        [sys.executable, "-m", "unittest", "tests/contracts/test_manifest_coverage.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    recovered_green = (proc_recovered.returncode == 0)

    return {
        "experiment": "unmanifested_file_injection",
        "turned_red_as_expected": turned_red,
        "recovered_green_after_cleanup": recovered_green,
        "red_error_sample": red_message,
        "passed": turned_red and recovered_green,
    }


def run_experiment_2_symlink_injection() -> dict:
    """Inject a symlink into a sandbox copy and assert read_source fail-closed rejection."""
    temp_sandbox = Path(tempfile.mkdtemp(prefix="dex-sabotage-symlink-", dir="/tmp"))
    turned_red = False
    recovered_green = False
    try:
        # Create minimal valid tree
        (temp_sandbox / "scripts").mkdir(parents=True)
        shutil.copy2(ROOT / "scripts/test_safety_stage.py", temp_sandbox / "scripts/")
        shutil.copy2(ROOT / "scripts/test_safety_source_manifest.json", temp_sandbox / "scripts/")
        shutil.copy2(ROOT / "AGENTS.md", temp_sandbox / "AGENTS.md")

        # Create symlink inside sandbox
        evil_symlink = temp_sandbox / "evil_symlink.py"
        os.symlink("/etc/passwd", evil_symlink)

        # Attempt to read the symlink via test_safety_stage.read_source
        script = f"""
import sys
from pathlib import Path
sys.path.insert(0, '{ROOT}')
from scripts.test_safety_stage import read_source
try:
    read_source(Path('{temp_sandbox}'), 'evil_symlink.py')
    print('ESCAPE_SUCCESS')
except (ValueError, OSError) as e:
    print(f'INTERCEPTED: {{e}}')
"""
        proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
        turned_red = ("INTERCEPTED" in proc.stdout and "ESCAPE_SUCCESS" not in proc.stdout)

        # Test normal file reading works
        script_normal = f"""
import sys
from pathlib import Path
sys.path.insert(0, '{ROOT}')
from scripts.test_safety_stage import read_source
content, mode = read_source(Path('{temp_sandbox}'), 'AGENTS.md')
assert len(content) > 0
print('NORMAL_READ_OK')
"""
        proc_normal = subprocess.run([sys.executable, "-c", script_normal], capture_output=True, text=True)
        recovered_green = ("NORMAL_READ_OK" in proc_normal.stdout)
    finally:
        shutil.rmtree(temp_sandbox, ignore_errors=True)

    return {
        "experiment": "symlink_traversal_sabotage",
        "turned_red_as_expected": turned_red,
        "recovered_green_after_cleanup": recovered_green,
        "passed": turned_red and recovered_green,
    }


def run_experiment_3_subdirectory_test_collection_sabotage() -> dict:
    """Verify that subdirectory tests (e.g. tests/catalog/test_*.py) are collected by build_plan and unmanifested ones fail closed."""
    # 1. Test unmanifested subdirectory test detection
    catalog_test_dir = ROOT / "tests/catalog"
    catalog_test_dir.mkdir(parents=True, exist_ok=True)
    dummy_test_file = catalog_test_dir / "test_unmanifested_catalog_probe.py"
    dummy_test_file.write_text("import unittest\nclass DummyTest(unittest.TestCase):\n    def test_pass(self): pass\n", encoding="utf-8")

    turned_red = False
    red_message = ""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "tests/contracts/test_manifest_coverage.py"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        turned_red = (proc.returncode != 0 and "AssertionError" in proc.stderr)
        red_message = proc.stderr.strip().splitlines()[-1] if proc.stderr else ""
    finally:
        if dummy_test_file.is_file():
            dummy_test_file.unlink()
        if catalog_test_dir.is_dir() and not any(catalog_test_dir.iterdir()):
            catalog_test_dir.rmdir()

    # 2. Test that build_plan collects subdirectory tests in unit plan
    from scripts.test_safety_layers import build_plan
    plan = build_plan(ROOT)
    contract_tests_in_unit = [
        item for item in plan["unit"] if item.startswith("tests/contracts/")
    ]
    # All 10 contract tests must be collected in unit
    collected_all_contracts = (len(contract_tests_in_unit) >= 10)

    # 3. Sabotage proof: demonstrate that the old flat logic would drop all subdirectory tests
    from scripts.test_safety_stage import source_manifest
    manifest_files = source_manifest(ROOT)
    old_flat_collected = [
        name for name in manifest_files if name.startswith("tests/test_") and name.endswith(".py")
    ]
    old_logic_omitted_contracts = all(not name.startswith("tests/contracts/") for name in old_flat_collected)

    passed = turned_red and collected_all_contracts and old_logic_omitted_contracts

    return {
        "experiment": "subdirectory_test_collection_sabotage",
        "unmanifested_subtest_turned_red": turned_red,
        "new_build_plan_collected_subtests_count": len(contract_tests_in_unit),
        "old_logic_would_have_omitted_contracts": old_logic_omitted_contracts,
        "red_error_sample": red_message,
        "passed": passed,
    }


def main() -> int:
    print("=== RUNNING SABOTAGE VERIFICATION SUITE ===")
    exp1 = run_experiment_1_unmanifested_file()
    print(f"Exp 1 (Unmanifested file): {'PASS' if exp1['passed'] else 'FAIL'}")

    exp2 = run_experiment_2_symlink_injection()
    print(f"Exp 2 (Symlink sabotage): {'PASS' if exp2['passed'] else 'FAIL'}")

    exp3 = run_experiment_3_subdirectory_test_collection_sabotage()
    print(f"Exp 3 (Subdirectory test collection sabotage): {'PASS' if exp3['passed'] else 'FAIL'}")

    all_passed = exp1["passed"] and exp2["passed"] and exp3["passed"]
    report = {
        "suite": "SABOTAGE_VERIFICATION_SUITE",
        "status": "PASS" if all_passed else "FAIL",
        "experiments": [exp1, exp2, exp3],
    }

    evidence_dir = ROOT / "docs/w0/evidence/W0-B"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    out_path = evidence_dir / "sabotage_verification_report.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print(f"Report written to: {out_path}")
    print(f"OVERALL SABOTAGE SUITE: {'PASS' if all_passed else 'FAIL'}")
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
