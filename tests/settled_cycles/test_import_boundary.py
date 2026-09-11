"""Import and side-effect boundary tests for research.settled_cycles.

The clean subprocess uses cwd-based import resolution to avoid stale bytecode
side effects and to mirror the required offline discovery command.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import unittest
from pathlib import Path


class ImportBoundaryTests(unittest.TestCase):
    def test_clean_import_loads_no_business_modules(self) -> None:
        program = """
import json
import sys
import research.settled_cycles
forbidden = {"arbitrage", "execution", "monitors", "backtest", "chains", "core"}
assert not forbidden.intersection(sys.modules), forbidden.intersection(sys.modules)
print(json.dumps({"ok": True, "loaded": sorted(sys.modules)}))
"""
        completed = subprocess.run(
            [sys.executable, "-B", "-c", program],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn('"ok": true', completed.stdout)

    def test_init_has_no_network_subprocess_or_write_apis(self) -> None:
        path = Path(__file__).resolve().parents[2] / "research/settled_cycles/__init__.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        forbidden_names = {
            "open",
            "socket",
            "requests",
            "urllib",
            "httpx",
            "subprocess",
            "Popen",
            "write_text",
            "write_bytes",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                self.assertNotIn(node.id, forbidden_names)
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, forbidden_names)
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                modules = {alias.name for alias in node.names}
                if isinstance(node, ast.ImportFrom) and node.module:
                    modules.add(node.module)
                for module in modules:
                    self.assertFalse(
                        module.split(".")[0] in {"socket", "subprocess", "urllib", "http"},
                        module,
                    )

    def test_runtime_module_state_after_import(self) -> None:
        initial_modules = set(sys.modules.keys())
        import research.settled_cycles

        newly_loaded = set(sys.modules.keys()) - initial_modules
        forbidden = {"arbitrage", "execution", "monitors", "backtest", "chains", "core"}
        leaked = [mod for mod in newly_loaded if mod.split(".")[0] in forbidden]
        self.assertFalse(leaked, f"Leaked modules: {leaked}")
        self.assertTrue(hasattr(research.settled_cycles, "SettledCycleRecord"))


if __name__ == "__main__":
    unittest.main()
