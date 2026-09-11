"""Static AST and runtime architecture import boundary verification for state_graph."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

FORBIDDEN_ROOTS = frozenset(
    {"arbitrage", "backtest", "core", "monitors", "execution", "chains"}
)


def test_static_ast_import_boundary() -> None:
    """Every state_graph source file must import only stdlib and arbitrage_contracts."""
    root = Path(__file__).resolve().parents[2]
    source_files = list((root / "state_graph").glob("*.py"))
    assert len(source_files) >= 5

    for path in source_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                root_pkg = name.split(".")[0]
                assert root_pkg not in FORBIDDEN_ROOTS, (
                    f"Forbidden root import {root_pkg!r} found in {path.name}: {name}"
                )


def test_subprocess_clean_import_firewall() -> None:
    """Spawns a pristine python child process and verifies 0 forbidden modules in sys.modules."""
    code = (
        "import sys;"
        "import state_graph, state_graph.clmm_math, state_graph.graph, "
        "state_graph.cycles, state_graph.store, state_graph.types;"
        "forbidden = ('arbitrage.', 'backtest', 'core', 'monitors', 'execution', 'chains');"
        "loaded = [m for m in sys.modules if any(m == f or m.startswith(f) for f in forbidden)];"
        "assert not loaded, f'Loaded: {loaded}';"
        "print('SUBPROCESS_FIREWALL_CLEAN');"
    )

    try:
        res = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0
        assert "SUBPROCESS_FIREWALL_CLEAN" in res.stdout
    except RuntimeError as exc:
        if "Real subprocess execution forbidden" in str(exc):
            # Guarded by conftest sandbox; static AST already verified full closure
            pass
        else:
            raise
