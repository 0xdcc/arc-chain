"""AST boundary checks for W2 opportunity modules."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

FORBIDDEN_ROOTS = {"arbitrage", "backtest", "core", "execution", "chains", "monitors"}


def test_parent_modules_have_no_forbidden_imports() -> None:
    """Every W2 parent module imports only stdlib, contracts, and opportunities."""
    root = Path(__file__).resolve().parents[2]
    files = list((root / "opportunities").glob("*.py"))
    files.append(root / "apps" / "arb_shadow.py")
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                assert name.split(".")[0] not in FORBIDDEN_ROOTS, f"{path}: {name}"


def test_cli_load_has_no_forbidden_runtime_modules() -> None:
    """A clean subprocess must not load execution or production monitoring stacks."""
    root = Path(__file__).resolve().parents[2]
    script = (
        "import sys;"
        "import apps.arb_shadow;"
        "forbidden=('arbitrage','backtest','core','execution','chains','monitors');"
        "loaded=tuple(name for name in sys.modules if name.split('.')[0] in forbidden);"
        "print(loaded);"
        "assert not loaded, loaded"
    )
    process = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    assert process.stdout.strip() == "()"
