"""Automated offline test harness, audit guard, and manifest reconciliation runner for W7."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent

W7_MANAGED_DIRS = [
    _REPO_ROOT / "rwa_research",
    _REPO_ROOT / "tests" / "rwa",
    _REPO_ROOT / "tests" / "fixtures" / "rwa" / "v1",
]
W7_MANAGED_FILES = [
    _REPO_ROOT / "apps" / "rwa_observer.py",
    _REPO_ROOT / "tools" / "w7_offline_check.py",
    _REPO_ROOT / "tools" / "w7_sabotage.py",
]

_ORIG_CONNECT = None
_ORIG_RUN = subprocess.run


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
                    if not p.startswith("/tmp") and not p.startswith("/dev/null") and ".pytest_cache" not in p:
                        # Allow internal testing writes if in tmp or dev/null
                        raise PermissionError(f"Writing outside /tmp is strictly forbidden: {p}")

    sys.addaudithook(audit_hook)


def reconcile_manifest(manifest_files: set[str], repo_root: Path | None = None) -> tuple[bool, str]:
    """Scan disk and verify 1:1 bidirectional match with manifest_files (C30)."""
    root = repo_root or _REPO_ROOT
    disk_files: set[str] = set()

    managed_dirs = [
        root / "rwa_research",
        root / "tests" / "rwa",
        root / "tests" / "fixtures" / "rwa" / "v1",
    ]
    managed_files = [
        root / "apps" / "rwa_observer.py",
        root / "tools" / "w7_offline_check.py",
        root / "tools" / "w7_sabotage.py",
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
    parser = argparse.ArgumentParser(description="W7 Offline Verification Runner")
    parser.add_argument("--skip-manifest", action="store_true", help="Skip manifest reconciliation")
    args = parser.parse_args()

    print("======================================================================")
    print("🚀 W7 RWA Research Offline Verification & Safety Harness")
    print("======================================================================")

    # 0. Activate fine-grained audit hook for this run
    install_audit_guards()

    # 1. Run pytest
    print("\n[Step 1/3] Running W7 Unit Tests & E2E Suite...")
    pytest_cmd = [
        sys.executable,
        "-m",
        "pytest",
        "tests/rwa/",
        "--confcutdir=tests/rwa",
        "-v",
        "-o",
        "cache_dir=/tmp/pytest_cache",
    ]
    res_pytest = subprocess.run(pytest_cmd, cwd=_REPO_ROOT, check=False)
    if res_pytest.returncode != 0:
        print(f"❌ Pytest failed with exit code {res_pytest.returncode}")
        sys.exit(1)
    print("✅ Pytest suite 100% passed")

    # 2. Run Ruff & Mypy
    print("\n[Step 2/3] Running Static Analysis (Ruff & Mypy Strict)...")
    ruff_cmd = [sys.executable, "-m", "ruff", "check", "rwa_research", "apps", "tests/rwa", "tools"]
    res_ruff = subprocess.run(ruff_cmd, cwd=_REPO_ROOT, check=False)
    if res_ruff.returncode != 0:
        print(f"❌ Ruff failed with exit code {res_ruff.returncode}")
        sys.exit(1)
    print("✅ Ruff linting passed (0 errors)")

    mypy_cmd = [
        sys.executable,
        "-m",
        "mypy",
        "--strict",
        "rwa_research",
        "apps/rwa_observer.py",
        "tests/rwa",
        "tools/w7_offline_check.py",
    ]
    res_mypy = subprocess.run(mypy_cmd, cwd=_REPO_ROOT, check=False)
    if res_mypy.returncode != 0:
        print(f"❌ Mypy strict failed with exit code {res_mypy.returncode}")
        sys.exit(1)
    print("✅ Mypy strict passed (0 errors)")

    # 3. Manifest reconciliation
    if not args.skip_manifest:
        print("\n[Step 3/3] Manifest 1:1 Bidirectional Reconciliation...")
        # W7 Managed files
        w7_expected = {
            "apps/rwa_observer.py",
            "rwa_research/__init__.py",
            "rwa_research/models.py",
            "rwa_research/codec.py",
            "rwa_research/normalize.py",
            "rwa_research/validity.py",
            "rwa_research/quote_inputs.py",
            "rwa_research/classify.py",
            "rwa_research/report.py",
            "tests/rwa/conftest.py",
            "tests/rwa/test_models_codec.py",
            "tests/rwa/test_normalize.py",
            "tests/rwa/test_validity.py",
            "tests/rwa/test_quote_inputs.py",
            "tests/rwa/test_classify.py",
            "tests/rwa/test_cli_e2e.py",
            "tests/rwa/test_safety.py",
            "tests/fixtures/rwa/v1/README.md",
            "tests/fixtures/rwa/v1/manifest.json",
            "tests/fixtures/rwa/v1/models.json",
            "tests/fixtures/rwa/v1/normalize.json",
            "tests/fixtures/rwa/v1/validity.json",
            "tests/fixtures/rwa/v1/quotes.json",
            "tests/fixtures/rwa/v1/e2e.json",
            "tools/w7_offline_check.py",
            "tools/w7_sabotage.py",
        }
        ok, msg = reconcile_manifest(w7_expected)
        if not ok:
            print(f"❌ {msg}")
            sys.exit(1)
        print(f"✅ {msg}")

    print("\n======================================================================")
    print("🎉 All W7 Offline Safety Gates & Unit Verifications Passed Successfully!")
    print("======================================================================\n")
    sys.exit(0)


if __name__ == "__main__":
    main()
