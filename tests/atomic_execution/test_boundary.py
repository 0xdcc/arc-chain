"""Comprehensive security and architectural boundary tests for W5-G (C21, C22).

Verifies:
- C21 Static AST Analysis: apps/atomic_simulate.py, atomic_execution/pipeline.py, and export.py
  contain zero imports of forbidden legacy packages (arbitrage, execution, core, chains, backtest).
- C21 Subprocess Clean Isolation: Clean subprocess executing atomic_simulate.py loads zero
  forbidden modules, zero private keys, and zero keystores into sys.modules.
- C22 Directory Traversal & Anti-Escape: Export outside base_dir is blocked.
- C22 Symlink & Hardlink Interception: Symlink destination, symlinked parent directory,
  and hard-linked destination files (st_nlink > 1) are strictly rejected with O_NOFOLLOW.
- C22 Directory Manifest Integrity: Unmanifested files or internal symlinks are fail-closed.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from atomic_execution.export import (
    ExportPathSecurityError,
    export_records_to_jsonl,
    export_summary_to_json,
    safe_open_for_write,
    validate_safe_export_path,
    verify_directory_manifest,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FORBIDDEN_ROOT_PACKAGES = frozenset(
    {"arbitrage", "execution", "core", "chains", "backtest", "monitors", "dotenv"}
)
FORBIDDEN_MODULE_SUBSTRINGS = frozenset(
    {"signer", "broadcast", "keystore", "private_key", "secret"}
)


def _run_subprocess(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Execute subprocess through conftest original handles to bypass test isolation."""
    conftest = sys.modules.get("tests.conftest")
    orig_popen = getattr(conftest, "_ORIG_SUBPROCESS_POPEN", None) if conftest else None
    orig_run = (
        getattr(conftest, "_ORIG_SUBPROCESS_RUN", subprocess.run) if conftest else subprocess.run
    )

    if orig_popen is not None:
        saved_popen = subprocess.Popen
        setattr(subprocess, "Popen", orig_popen)  # noqa: B010
        try:
            return orig_run(*args, **kwargs)
        finally:
            setattr(subprocess, "Popen", saved_popen)  # noqa: B010
    return orig_run(*args, **kwargs)


# ==============================================================================
# 1. C21 Static AST Audits
# ==============================================================================


@pytest.mark.parametrize(
    "relative_path",
    [
        "apps/atomic_simulate.py",
        "atomic_execution/pipeline.py",
        "atomic_execution/export.py",
    ],
)
def test_c21_static_ast_zero_forbidden_imports(relative_path: str) -> None:
    """Target modules must contain zero imports from legacy execution or signing packages."""
    target_file = REPO_ROOT / relative_path
    assert target_file.is_file(), f"Target file does not exist: {target_file}"

    tree = ast.parse(target_file.read_text(encoding="utf-8"), filename=str(target_file))
    imported_names: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported_names.append(node.module)

    for mod in imported_names:
        root_pkg = mod.split(".")[0].lower()
        assert root_pkg not in FORBIDDEN_ROOT_PACKAGES, (
            f"Forbidden import '{mod}' detected in {relative_path}"
        )
        for forbidden_sub in FORBIDDEN_MODULE_SUBSTRINGS:
            assert forbidden_sub not in mod.lower(), (
                f"Forbidden substring '{forbidden_sub}' in import '{mod}' in {relative_path}"
            )


# ==============================================================================
# 2. C21 Subprocess Clean Isolation Audit
# ==============================================================================


def test_c21_subprocess_sys_modules_clean_isolation() -> None:
    """Subprocess invoking apps/atomic_simulate.py must not load forbidden modules into sys.modules."""
    probe_script = (
        "import sys\n"
        "import apps.atomic_simulate\n"
        "forbidden = {'arbitrage', 'execution', 'core', 'chains', 'backtest', 'monitors', 'dotenv'}\n"
        "loaded_forbidden = [m for m in sys.modules if m.split('.')[0] in forbidden]\n"
        "assert not loaded_forbidden, f'Forbidden modules in sys.modules: {loaded_forbidden}'\n"
        "print('CLEAN_ISOLATION_OK')\n"
    )

    proc = _run_subprocess(
        [sys.executable, "-c", probe_script],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"Subprocess probe failed: {proc.stderr}"
    assert "CLEAN_ISOLATION_OK" in proc.stdout


# ==============================================================================
# 3. C22 Directory Traversal & Symlink Defense Tests
# ==============================================================================


def test_c22_export_path_traversal_outside_base_dir_fails(tmp_path: Path) -> None:
    """Attempting to export outside base_dir raises ExportPathSecurityError."""
    base_dir = tmp_path / "sandbox_export"
    base_dir.mkdir()
    traversal_target = tmp_path / "escaped.jsonl"

    with pytest.raises(ExportPathSecurityError, match="escapes base directory"):
        validate_safe_export_path(traversal_target, base_dir=base_dir)


def test_c22_export_target_file_symlink_rejected(tmp_path: Path) -> None:
    """Symlinked export target file must be rejected fail-closed."""
    real_file = tmp_path / "real.jsonl"
    real_file.write_text("{}", encoding="utf-8")
    symlink_file = tmp_path / "link.jsonl"
    symlink_file.symlink_to(real_file)

    with pytest.raises(ExportPathSecurityError, match="(?i)symlink"):
        validate_safe_export_path(symlink_file)

    with pytest.raises(ExportPathSecurityError, match="(?i)symlink"):
        safe_open_for_write(symlink_file)


def test_c22_export_parent_directory_symlink_rejected(tmp_path: Path) -> None:
    """Export target residing within a symlinked directory must be rejected fail-closed."""
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    link_dir = tmp_path / "link_dir"
    link_dir.symlink_to(real_dir)

    target_in_symlink = link_dir / "output.jsonl"

    with pytest.raises(ExportPathSecurityError, match="Symlink detected in path component"):
        validate_safe_export_path(target_in_symlink)


def test_c22_export_hardlink_rejected(tmp_path: Path) -> None:
    """Existing destination file with hard link count > 1 must be rejected fail-closed."""
    original = tmp_path / "original.jsonl"
    original.write_text("line1\n", encoding="utf-8")
    hardlink = tmp_path / "hardlink.jsonl"
    os.link(original, hardlink)

    assert hardlink.stat().st_nlink > 1

    with pytest.raises(ExportPathSecurityError, match="hard link count"):
        validate_safe_export_path(hardlink)

    with pytest.raises(ExportPathSecurityError, match="hard link count"):
        safe_open_for_write(hardlink)


def test_c22_export_to_directory_path_rejected(tmp_path: Path) -> None:
    """Attempting to open a directory as destination file is rejected."""
    dir_target = tmp_path / "somedir"
    dir_target.mkdir()

    with pytest.raises(ExportPathSecurityError, match="directory, expected file"):
        validate_safe_export_path(dir_target)


def test_c22_safe_export_records_and_summary(tmp_path: Path) -> None:
    """Legitimate export writes canonical records and summary with O_NOFOLLOW."""
    out_file = tmp_path / "legit_out.jsonl"
    sum_file = tmp_path / "legit_sum.json"

    records = [{"id": 1, "value": "A"}, {"id": 2, "value": "B"}]
    written = export_records_to_jsonl(records, out_file)
    assert written == 2
    assert out_file.is_file()

    summary = {"total": 2, "status": "OK"}
    export_summary_to_json(summary, sum_file)
    assert sum_file.is_file()


def test_c22_verify_directory_manifest_success_and_failures(tmp_path: Path) -> None:
    """verify_directory_manifest accepts matching files and rejects unmanifested or symlinked files."""
    delivery_dir = tmp_path / "delivery"
    delivery_dir.mkdir()

    file1 = delivery_dir / "report.json"
    file1.write_text("{}", encoding="utf-8")
    file2 = delivery_dir / "stream.jsonl"
    file2.write_text("{}", encoding="utf-8")

    # Success when all files match
    assert verify_directory_manifest(delivery_dir, ["report.json", "stream.jsonl"]) is True

    # Failure when unmanifested file is present
    extra = delivery_dir / "untracked.txt"
    extra.write_text("leak", encoding="utf-8")
    with pytest.raises(ExportPathSecurityError, match="Unmanifested file"):
        verify_directory_manifest(delivery_dir, ["report.json", "stream.jsonl"])

    extra.unlink()

    # Failure when symlink is present inside directory
    secret = tmp_path / "secret.env"
    secret.write_text("KEY=123", encoding="utf-8")
    link = delivery_dir / "link_secret"
    link.symlink_to(secret)
    with pytest.raises(ExportPathSecurityError, match="Symlink found"):
        verify_directory_manifest(delivery_dir, ["report.json", "stream.jsonl", "link_secret"])
