"""W5 Offline Master Check Runner (TASK-W5-G-v1, C21~C24).

Executes comprehensive verification:
1. Ruff linter and code style formatting check.
2. Mypy strict static type checking.
3. Full pytest regression suite for tests/atomic_execution/.
4. Architectural & security boundary checks (AST analysis, sys.modules clean audit, CLI fail-closed).
"""

from __future__ import annotations

import argparse
import ast
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

_DEFAULT_REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON_BIN = sys.executable

FORBIDDEN_PACKAGES = frozenset(
    {"arbitrage", "execution", "core", "chains", "backtest", "monitors", "dotenv"}
)
FORBIDDEN_SUBSTRINGS = frozenset({"signer", "broadcast", "keystore", "private_key", "secret"})


def _run_cmd(cmd: list[str], description: str, repo_root: Path) -> bool:
    """Execute command and print status."""
    start = time.perf_counter()
    sub_env = dict(os.environ)
    sub_env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        cmd,
        cwd=str(repo_root),
        env=sub_env,
        capture_output=True,
        text=True,
        check=False,
    )
    elapsed = time.perf_counter() - start
    if proc.returncode == 0:
        print(f"  [PASS] {description} ({elapsed:.2f}s)")
        return True

    print(f"  [FAIL] {description} ({elapsed:.2f}s) - Exit code {proc.returncode}")
    if proc.stdout:
        print("--- STDOUT ---")
        print(proc.stdout.strip())
    if proc.stderr:
        print("--- STDERR ---")
        print(proc.stderr.strip())
    return False


def run_ruff_checks(repo_root: Path) -> bool:
    """Run ruff linter and formatting checks."""
    print("[1/4] Running Ruff Linter and Style Checks...")
    lint_targets = [
        "atomic_execution",
        "apps/atomic_simulate.py",
        "tools/w5_offline_check.py",
        "tools/w5_sabotage.py",
        "tests/atomic_execution",
    ]
    format_targets = [
        "apps/atomic_simulate.py",
        "atomic_execution/pipeline.py",
        "atomic_execution/export.py",
        "tests/atomic_execution/test_pipeline.py",
        "tests/atomic_execution/test_cli.py",
        "tests/atomic_execution/test_boundary.py",
        "tools/w5_offline_check.py",
        "tools/w5_sabotage.py",
    ]
    check_ok = _run_cmd(
        [PYTHON_BIN, "-m", "ruff", "check"] + lint_targets,
        "Ruff Lint Check",
        repo_root,
    )
    format_ok = _run_cmd(
        [PYTHON_BIN, "-m", "ruff", "format", "--check"] + format_targets,
        "Ruff Format Consistency Check",
        repo_root,
    )
    return check_ok and format_ok


def run_mypy_checks(repo_root: Path) -> bool:
    """Run mypy strict type checker."""
    print("[2/4] Running Mypy Strict Type Analysis...")
    targets = [
        "atomic_execution",
        "apps/atomic_simulate.py",
        "tools",
        "tests/atomic_execution",
    ]
    return _run_cmd(
        [PYTHON_BIN, "-m", "mypy", "--follow-imports=silent"] + targets,
        "Mypy Type Checking",
        repo_root,
    )


def run_pytest_checks(repo_root: Path) -> bool:
    """Run pytest suite on tests/atomic_execution/."""
    print("[3/4] Running Atomic Execution Test Suite...")
    return _run_cmd(
        [
            PYTHON_BIN,
            "-m",
            "pytest",
            "tests/atomic_execution/",
            "-o",
            "cache_dir=/tmp/pytest_cache",
            "-v",
        ],
        "Pytest Atomic Execution Suite",
        repo_root,
    )


def run_boundary_checks(repo_root: Path) -> bool:
    """Run architectural boundary checks, subprocess sys.modules audit, and CLI guardrail probe."""
    print("[4/4] Running Architectural & Security Boundary Checks...")
    all_ok = True

    # 4.1 AST Audit
    ast_targets = [
        repo_root / "apps" / "atomic_simulate.py",
        repo_root / "atomic_execution" / "pipeline.py",
        repo_root / "atomic_execution" / "export.py",
        repo_root / "tools" / "w5_offline_check.py",
        repo_root / "tools" / "w5_sabotage.py",
    ]
    ast_failures = []
    for file_path in ast_targets:
        if not file_path.is_file():
            continue
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        for node in ast.walk(tree):
            mod_names: list[str] = []
            if isinstance(node, ast.Import):
                mod_names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mod_names.append(node.module)

            for mod in mod_names:
                root_pkg = mod.split(".")[0].lower()
                if root_pkg in FORBIDDEN_PACKAGES:
                    ast_failures.append(f"{file_path.name}: forbidden import {mod}")
                for forbidden_sub in FORBIDDEN_SUBSTRINGS:
                    if forbidden_sub in mod.lower():
                        ast_failures.append(f"{file_path.name}: forbidden substring {mod}")

    if not ast_failures:
        print("  [PASS] Static AST Check (0 forbidden imports across all target files)")
    else:
        print(f"  [FAIL] Static AST Check: {ast_failures}")
        all_ok = False

    # 4.2 Subprocess sys.modules Audit
    probe_script = (
        "import sys\n"
        "import apps.atomic_simulate\n"
        "forbidden = {'arbitrage', 'execution', 'core', 'chains', 'backtest', 'monitors', 'dotenv'}\n"
        "loaded = [m for m in sys.modules if m.split('.')[0] in forbidden]\n"
        "assert not loaded, f'Forbidden modules in sys.modules: {loaded}'\n"
        "print('ISOLATION_OK')\n"
    )
    sub_env = dict(os.environ)
    sub_env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc_sys = subprocess.run(
        [PYTHON_BIN, "-c", probe_script],
        cwd=str(repo_root),
        env=sub_env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc_sys.returncode == 0 and "ISOLATION_OK" in proc_sys.stdout:
        print("  [PASS] Subprocess sys.modules Isolation (0 forbidden modules loaded)")
    else:
        print(f"  [FAIL] Subprocess sys.modules Isolation: {proc_sys.stderr}")
        all_ok = False

    # 4.3 CLI Guardrail Fail-Closed Probe
    cli_script = str(repo_root / "apps" / "atomic_simulate.py")
    stream_file = str(
        repo_root / "tests" / "fixtures" / "atomic_execution" / "v1" / "e2e-stream.jsonl"
    )
    for flag in ("--live", "--send", "--broadcast", "--approve"):
        p_flag = subprocess.run(
            [PYTHON_BIN, cli_script, flag, "--input", stream_file],
            cwd=str(repo_root),
            env=sub_env,
            capture_output=True,
            text=True,
            check=False,
        )
        if p_flag.returncode != 0:
            print(
                f"  [PASS] CLI Guardrail Probe: {flag} rejected fail-closed (code {p_flag.returncode})"
            )
        else:
            print(f"  [FAIL] CLI Guardrail Probe: {flag} was erroneously accepted (code 0)")
            all_ok = False

    return all_ok


def main() -> int:
    """Execute all offline checks and return unified exit code."""
    sys.dont_write_bytecode = True
    parser = argparse.ArgumentParser(description="W5 Offline Master Check Runner")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=_DEFAULT_REPO_ROOT,
        help="Path to repository root (defaults to parent of tools directory)",
    )
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()

    print("================================================================================")
    print("W5 OFFLINE MASTER CHECK RUNNER (TASK-W5-G-v1)")
    print("================================================================================")

    steps: list[tuple[str, Callable[[], bool]]] = [
        ("Ruff Checks", lambda: run_ruff_checks(repo_root)),
        ("Mypy Checks", lambda: run_mypy_checks(repo_root)),
        ("Pytest Checks", lambda: run_pytest_checks(repo_root)),
        ("Boundary Checks", lambda: run_boundary_checks(repo_root)),
    ]

    all_passed = True
    for step_name, step_fn in steps:
        success = step_fn()
        if not success:
            all_passed = False
            print(f"\nFATAL: Step '{step_name}' failed.")
            break
        print()

    print("================================================================================")
    if all_passed:
        print("RESULT: ALL W5 OFFLINE CHECKS PASSED SUCCESSFULLY (EXIT 0)")
        print("================================================================================")
        return 0

    print("RESULT: W5 OFFLINE CHECKS FAILED (EXIT 1)")
    print("================================================================================")
    return 1


if __name__ == "__main__":
    sys.exit(main())
