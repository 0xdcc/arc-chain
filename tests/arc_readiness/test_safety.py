"""Safety sandbox, process isolation, and secret protection tests (C29)."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

FORBIDDEN_EXECUTION_MODULES = frozenset(
    {
        "chains",
        "core.wallet_guard",
        "execution",
        "atomic_execution",
        "monitors",
    }
)


def test_c29_import_isolation_in_process() -> None:
    """C29: Importing arc_readiness and CLI apps loads 0 execution or wallet guard modules."""
    initial_modules = set(sys.modules.keys())

    import apps.arc_market_catalog
    import apps.arc_readiness
    import arc_readiness

    # Assert basic availability
    assert arc_readiness.ARC_TESTNET_CHAIN_ID == 5042002
    assert callable(apps.arc_readiness.main)
    assert callable(apps.arc_market_catalog.main)

    new_modules = set(sys.modules.keys()) - initial_modules
    leaked = [
        mod
        for mod in new_modules
        if any(mod == f or mod.startswith(f + ".") for f in FORBIDDEN_EXECUTION_MODULES)
    ]
    assert not leaked, f"Importing readiness entrypoints leaked forbidden modules: {leaked}"


def test_c29_apps_ast_imports_clean() -> None:
    """C29: Static AST inspection of apps/* ensures no forbidden modules are imported."""
    repo_root = Path(__file__).resolve().parents[2]
    app_files = [
        repo_root / "apps" / "arc_readiness.py",
        repo_root / "apps" / "arc_market_catalog.py",
    ]

    for app_file in app_files:
        tree = ast.parse(app_file.read_text(encoding="utf-8"), filename=str(app_file))
        for node in ast.walk(tree):
            imported: list[str] = []
            if isinstance(node, ast.Import):
                imported.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.append(node.module)

            for name in imported:
                root_pkg = name.split(".")[0]
                assert root_pkg not in FORBIDDEN_EXECUTION_MODULES, (
                    f"Forbidden import {name!r} detected in {app_file.name}"
                )


def test_c29_no_secrets_in_repo_tree() -> None:
    """C29: Assert no private keys, .env, or keystores are checked in under arc_readiness or tests."""
    repo_root = Path(__file__).resolve().parents[2]
    targets = [
        repo_root / "arc_readiness",
        repo_root / "apps" / "arc_readiness.py",
        repo_root / "apps" / "arc_market_catalog.py",
        repo_root / "tests" / "arc_readiness",
    ]

    # Obfuscate search targets to prevent self-matching
    key1 = "PRIVATE" + "_KEY"
    key2 = "SECRET" + "_KEY"
    key3 = "-----BEGIN " + "PRIVATE KEY-----"
    forbidden_patterns = [key1, key2, key3]

    for target in targets:
        if target.is_file():
            if target.name == "test_safety.py":
                continue
            text = target.read_text(encoding="utf-8")
            for pat in forbidden_patterns:
                assert pat not in text, f"Secret pattern {pat!r} found in {target}"
        elif target.is_dir():
            for p in target.rglob("*.py"):
                if p.name == "test_safety.py":
                    continue
                text = p.read_text(encoding="utf-8")
                for pat in forbidden_patterns:
                    assert pat not in text, f"Secret pattern {pat!r} found in {p}"
