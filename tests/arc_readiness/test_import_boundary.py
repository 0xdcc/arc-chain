"""Boundary and isolation tests for arc_readiness package."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

FORBIDDEN_ROOTS = frozenset(
    {
        "arbitrage",
        "atomic_execution",
        "backtest",
        "chains",
        "core",
        "execution",
        "market_catalog",
        "monitors",
        "opportunities",
        "state_graph",
    }
)


def test_arc_readiness_source_has_no_forbidden_ast_imports() -> None:
    """Verify that no file in arc_readiness imports forbidden internal packages."""
    package_dir = Path(__file__).resolve().parents[2] / "arc_readiness"
    py_files = list(package_dir.glob("*.py"))
    assert len(py_files) >= 3, (
        f"Expected at least 3 python files in arc_readiness, found {len(py_files)}"
    )

    for py_file in py_files:
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            imported_names: list[str] = []
            if isinstance(node, ast.Import):
                imported_names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported_names.append(node.module)

            for name in imported_names:
                root_pkg = name.split(".")[0]
                assert root_pkg not in FORBIDDEN_ROOTS, (
                    f"Forbidden import {name!r} detected in {py_file.name}"
                )


def test_import_arc_readiness_loads_zero_forbidden_modules() -> None:
    """Verify that importing arc_readiness loads zero forbidden execution/wallet packages."""
    initial_modules = set(sys.modules.keys())

    import arc_readiness
    import arc_readiness.errors
    import arc_readiness.models

    newly_loaded = set(sys.modules.keys()) - initial_modules
    leaked = [mod for mod in newly_loaded if mod.split(".")[0] in FORBIDDEN_ROOTS]
    assert not leaked, f"Importing arc_readiness leaked forbidden modules: {leaked}"

    # Assert basic package presence
    assert arc_readiness.ARC_TESTNET_CHAIN_ID == 5042002
    assert issubclass(arc_readiness.ArcValidationError, Exception)
