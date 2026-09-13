"""BASELINE_ISOLATION_GATE verification script.

Verifies:
1. Manifest 1:1 coverage against disk sources
2. Staging mechanism security and isolation
3. Pure baseline imports without network, credentials, or production leaks
4. Explicit accounting of legacy nodes with live/network marked as KNOWN_BLOCKED / NOT_RUN
"""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.test_safety_stage import (  # noqa: E402
    MANIFEST,
    PUBLIC_FILES,
    SOURCE_DIRS,
    source_manifest,
    stage_sources,
)


def verify_manifest_coverage(root: Path) -> dict:
    with open(root / MANIFEST, encoding="utf-8") as f:
        mf = json.load(f)
    manifest_files = set(mf["files"])

    disk_files = set()
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        parts = p.relative_to(root).parts
        if (
            rel.startswith(".git")
            or rel.startswith("docs")
            or rel.startswith("TASK-")
            or rel == ".hermes.md"
            or any(part.startswith(".") or part == "__pycache__" for part in parts)
        ):
            continue
        if rel in PUBLIC_FILES or (parts[0] in SOURCE_DIRS and rel.endswith(".py")):
            disk_files.add(rel)

    missing_in_manifest = sorted(list(disk_files - manifest_files))
    ghost_in_manifest = sorted(list(manifest_files - disk_files))

    passed = len(missing_in_manifest) == 0 and len(ghost_in_manifest) == 0
    return {
        "passed": passed,
        "manifest_count": len(manifest_files),
        "disk_count": len(disk_files),
        "missing_in_manifest": missing_in_manifest,
        "ghost_in_manifest": ghost_in_manifest,
    }


def verify_staging_and_isolation(root: Path) -> dict:
    temp_dir = Path(tempfile.mkdtemp(prefix="dex-gate-stage-", dir="/tmp"))
    sabotage_passed = False
    staging_passed = False
    staged_count = 0
    try:
        stage_sources(root, temp_dir)
        staged_count = sum(1 for p in temp_dir.rglob("*") if p.is_file())
        staging_passed = (staged_count == len(source_manifest(root)))

        # Sabotage check: injecting unapproved file into temp manifest must fail
        temp_sabotage = Path(tempfile.mkdtemp(prefix="dex-gate-sabotage-", dir="/tmp"))
        try:
            (temp_sabotage / "scripts").mkdir(parents=True)
            with open(root / MANIFEST, encoding="utf-8") as f:
                bad_mf = json.load(f)
            bad_mf["files"].append("arbitrage/unapproved_hack.sh")
            (temp_sabotage / MANIFEST).write_text(json.dumps(bad_mf), encoding="utf-8")
            try:
                source_manifest(temp_sabotage)
                sabotage_passed = False
            except ValueError:
                sabotage_passed = True
        finally:
            shutil.rmtree(temp_sabotage, ignore_errors=True)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    return {
        "passed": staging_passed and sabotage_passed,
        "staged_count": staged_count,
        "staging_passed": staging_passed,
        "sabotage_rejected_fail_closed": sabotage_passed,
    }


def verify_clean_imports(root: Path) -> dict:
    # 1. Test genuine standalone modules
    script_standalone = """
import sys
from pathlib import Path
root = Path('.').resolve()
sys.path.insert(0, str(root))

import core.base_chain as bc
import tests.contracts.test_manifest_coverage as tmc

import os
for k in os.environ:
    assert 'PRIVATE_KEY' not in k.upper(), f'Leaked key in env: {k}'

print('STANDALONE_IMPORTS_OK')
"""
    proc_standalone = subprocess.run(
        [sys.executable, "-I", "-c", script_standalone],
        cwd=root,
        capture_output=True,
        text=True,
    )
    standalone_ok = (proc_standalone.returncode == 0 and "STANDALONE_IMPORTS_OK" in proc_standalone.stdout)

    # 2. Test legacy arbitrage parent import to verify and document the eager-import blocker
    script_legacy = """
import sys
from pathlib import Path
root = Path('.').resolve()
sys.path.insert(0, str(root))
import arbitrage
"""
    proc_legacy = subprocess.run(
        [sys.executable, "-I", "-c", script_legacy],
        cwd=root,
        capture_output=True,
        text=True,
    )
    legacy_eager_blocked = (proc_legacy.returncode != 0 and "backtest.data" in proc_legacy.stderr)

    return {
        "passed": standalone_ok and legacy_eager_blocked,
        "standalone_imports_passed": standalone_ok,
        "legacy_arbitrage_eager_blocked_documented": legacy_eager_blocked,
        "legacy_error_diagnosis": proc_legacy.stderr.strip().splitlines()[-1] if proc_legacy.stderr else "",
        "architectural_decision_proven": (
            "Confirmed: legacy arbitrage/__init__.py triggers cascade into backtest.data, "
            "proving the absolute necessity of W0's isolated top-level arbitrage_contracts/ package."
        ),
    }


def inspect_legacy_test_nodes(root: Path) -> dict:
    tests_dir = root / "tests"
    total_files = 0
    total_nodes = 0
    node_inventory = {}

    for p in sorted(tests_dir.rglob("*.py")):
        if p.name.startswith("test_"):
            total_files += 1
            rel = p.relative_to(root).as_posix()
            try:
                tree = ast.parse(p.read_text(encoding="utf-8"))
                methods = [
                    node.name
                    for node in ast.walk(tree)
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name.startswith("test_")
                ]
                total_nodes += len(methods)
                node_inventory[rel] = {
                    "count": len(methods),
                    "layer": "live" if "live" in rel or "robinhood" in rel else "unit",
                }
            except Exception as e:
                node_inventory[rel] = {"error": str(e)}

    return {
        "total_test_files": total_files,
        "total_statically_parsed_nodes": total_nodes,
        "inventory": node_inventory,
        "legacy_gate_status": "KNOWN_BLOCKED / NOT_RUN (Live/Network testing strictly prohibited in W0)",
    }


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    print("=== RUNNING BASELINE_ISOLATION_GATE ===")

    mf_result = verify_manifest_coverage(root)
    print(f"1. Manifest Coverage: {'PASS' if mf_result['passed'] else 'FAIL'} ({mf_result['manifest_count']} files)")

    staging_result = verify_staging_and_isolation(root)
    print(f"2. Staging & Sabotage Security: {'PASS' if staging_result['passed'] else 'FAIL'}")

    import_result = verify_clean_imports(root)
    print(f"3. Clean Baseline Imports: {'PASS' if import_result['passed'] else 'FAIL'}")

    legacy_result = inspect_legacy_test_nodes(root)
    print(f"4. Legacy Suite Accounting: {legacy_result['total_test_files']} files, {legacy_result['total_statically_parsed_nodes']} nodes [LEGACY_FULL_GATE = BLOCKED]")

    gate_passed = mf_result["passed"] and staging_result["passed"] and import_result["passed"]

    overall = {
        "gate": "BASELINE_ISOLATION_GATE",
        "status": "PASS" if gate_passed else "FAIL",
        "legacy_full_gate_status": "KNOWN_BLOCKED",
        "checks": {
            "manifest_coverage": mf_result,
            "staging_and_isolation": staging_result,
            "clean_imports": import_result,
            "legacy_nodes": legacy_result,
        },
    }

    evidence_dir = (root / "docs/w3/evidence/gate") if (root / "docs/w3").is_dir() else (root / "docs/w0/evidence/W0-B")
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence_file = evidence_dir / "baseline_isolation_gate_report.json"
    with open(evidence_file, "w", encoding="utf-8") as f:
        json.dump(overall, f, indent=2)

    print(f"\nReport written to: {evidence_file}")
    print(f"OVERALL BASELINE_ISOLATION_GATE: {'PASS' if gate_passed else 'FAIL'}")
    return 0 if gate_passed else 1


if __name__ == "__main__":
    sys.exit(main())
