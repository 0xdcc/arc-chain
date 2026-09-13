"""Automated offline test harness, audit guard, and manifest reconciliation runner for W3."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

_DEFAULT_REPO_ROOT = Path(__file__).resolve().parent.parent


def install_audit_guards() -> None:
    """Install fine-grained audit hooks preventing socket connections and unapproved file writes.

    Must only be invoked by standalone runner CLI entrypoints, never at module import time.
    """

    def audit_hook(event: str, args: tuple[Any, ...]) -> None:
        if event == "socket.connect":
            raise RuntimeError(f"Network access is strictly forbidden during offline check: {args}")
        if event in ("open", "os.open"):
            path_arg = args[0] if args else ""
            mode_arg = args[1] if len(args) > 1 else "r"
            if isinstance(path_arg, (str, Path)):
                p = str(path_arg)
                is_write = any(m in str(mode_arg) for m in ("w", "a", "x", "+"))
                if is_write:
                    if (
                        not p.startswith("/tmp")
                        and not p.startswith("/dev/null")
                        and ".pytest_cache" not in p
                    ):
                        raise PermissionError(f"Writing outside /tmp is strictly forbidden: {p}")

    sys.addaudithook(audit_hook)


def reconcile_manifest(manifest_files: set[str], repo_root: Path | None = None) -> tuple[bool, str]:
    """Scan disk and verify 1:1 bidirectional match with manifest_files."""
    root = repo_root or _DEFAULT_REPO_ROOT
    disk_files: set[str] = set()

    managed_dirs = [
        root / "research" / "settled_cycles",
        root / "tests" / "settled_cycles",
    ]
    managed_files = [
        root / "apps" / "settled_cycle_research.py",
        root / "research" / "__init__.py",
        root / "tools" / "w3_offline_check.py",
        root / "tools" / "w3_sabotage.py",
    ]

    for d in managed_dirs:
        if not d.exists():
            continue
        for p in d.rglob("*"):
            if p.is_symlink():
                return False, f"Symlink detected and rejected: {p}"
            if p.is_file() and not p.name.endswith(".pyc") and "__pycache__" not in p.parts:
                rel = str(p.relative_to(root))
                disk_files.add(rel)

    for f in managed_files:
        if f.is_symlink():
            return False, f"Symlink detected and rejected: {f}"
        if f.exists() and f.is_file():
            rel = str(f.relative_to(root))
            disk_files.add(rel)

    missing_on_disk = manifest_files - disk_files
    unregistered_on_disk = disk_files - manifest_files

    if missing_on_disk or unregistered_on_disk:
        err_msg = (
            f"Manifest mismatch: missing_on_disk={sorted(missing_on_disk)}, "
            f"unregistered_on_disk={sorted(unregistered_on_disk)}"
        )
        return False, err_msg

    return True, f"Manifest 1:1 bidirectional match confirmed ({len(disk_files)} files)"


def main() -> None:
    parser = argparse.ArgumentParser(description="W3 Offline Verification Runner")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=_DEFAULT_REPO_ROOT,
        help="Path to repository root (defaults to parent of tools directory)",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Custom path to manifest-submission.json",
    )
    parser.add_argument(
        "--skip-manifest",
        action="store_true",
        help="Skip manifest reconciliation",
    )
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()

    print("======================================================================")
    print("🚀 W3 Settled Cycle Research Offline Verification & Safety Harness")
    print("======================================================================")

    # 0. Disable bytecode writing before activating fine-grained audit hook
    sys.dont_write_bytecode = True
    install_audit_guards()

    # Clean child process environment: explicitly forbid bytecode writing
    child_env = dict(os.environ)
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"

    # 1. Run pytest
    print("\n[Step 1/3] Running W3 Unit Tests & E2E Suite...")
    pytest_cmd = [
        sys.executable,
        "-m",
        "pytest",
        "tests/settled_cycles/",
        "--confcutdir=tests/settled_cycles",
        "-v",
        "-o",
        "cache_dir=/tmp/pytest_cache",
    ]
    res_pytest = subprocess.run(pytest_cmd, cwd=str(repo_root), env=child_env, check=False)
    if res_pytest.returncode != 0:
        print(f"❌ Pytest failed with exit code {res_pytest.returncode}")
        sys.exit(1)
    print("✅ Pytest suite 100% passed")

    # 2. Run Ruff & Mypy
    print("\n[Step 2/3] Running Static Analysis (Ruff & Mypy Strict)...")
    ruff_cmd = [
        sys.executable,
        "-m",
        "ruff",
        "check",
        "research/settled_cycles",
        "apps/settled_cycle_research.py",
        "tests/settled_cycles",
        "tools/w3_offline_check.py",
        "tools/w3_sabotage.py",
    ]
    res_ruff = subprocess.run(ruff_cmd, cwd=str(repo_root), env=child_env, check=False)
    if res_ruff.returncode != 0:
        print(f"❌ Ruff failed with exit code {res_ruff.returncode}")
        sys.exit(1)
    print("✅ Ruff linting passed (0 errors)")

    mypy_cmd = [
        sys.executable,
        "-m",
        "mypy",
        "--strict",
        "research/settled_cycles",
        "apps/settled_cycle_research.py",
        "tools/w3_offline_check.py",
        "tools/w3_sabotage.py",
    ]
    res_mypy = subprocess.run(mypy_cmd, cwd=str(repo_root), env=child_env, check=False)
    if res_mypy.returncode != 0:
        print(f"❌ Mypy strict failed with exit code {res_mypy.returncode}")
        sys.exit(1)
    print("✅ Mypy strict type checking passed (0 errors)")

    # 3. Manifest Reconciliation
    print("\n[Step 3/3] Reconciling Delivery Manifest...")
    if args.skip_manifest:
        print("⚠️ Manifest reconciliation skipped by flag")
    else:
        manifest_p = args.manifest or (repo_root / "docs" / "w3" / "manifest-submission.json")
        if not manifest_p.exists():
            print(f"⚠️ {manifest_p} does not exist; skipping reconciliation")
        else:
            manifest_data = json.loads(manifest_p.read_text(encoding="utf-8"))
            claimed_files = {item["path"] for item in manifest_data["files"]}
            ok, msg = reconcile_manifest(claimed_files, repo_root=repo_root)
            if not ok:
                print(f"❌ Manifest reconciliation failed: {msg}")
                sys.exit(1)
            print(f"✅ {msg}")

    print("\n======================================================================")
    print("🎯 W3 Settled Cycle Research ALL OFFLINE CHECKS PASSED!")
    print("======================================================================")


if __name__ == "__main__":
    main()
