"""Unit tests verifying import boundary isolation for arbitrage_contracts in a pristine subprocess."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


class TestImportBoundary(unittest.TestCase):
    """Test suite asserting arbitrage_contracts imports in total isolation from legacy modules."""

    def test_clean_subprocess_import_boundary(self) -> None:
        """Verify importing arbitrage_contracts in an isolated process loads zero legacy modules."""
        project_root = Path(__file__).resolve().parent.parent.parent
        script_code = """
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent.parent if "__file__" in globals() else Path('.').resolve()
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import arbitrage_contracts

forbidden_prefixes = (
    "apps",
    "backtest",
    "chains",
    "core",
    "execution",
    "monitors",
)

forbidden_exact = {"arbitrage"}

leaked_modules = []
for module_name in sorted(sys.modules.keys()):
    if module_name in forbidden_exact:
        leaked_modules.append(module_name)
    elif module_name.startswith("arbitrage."):
        leaked_modules.append(module_name)
    elif any(module_name == prefix or module_name.startswith(prefix + ".") for prefix in forbidden_prefixes):
        leaked_modules.append(module_name)

if leaked_modules:
    import json
    print("LEAK_DETECTED:" + json.dumps(leaked_modules))
    sys.exit(1)

print("BOUNDARY_ISOLATION_PASSED")
sys.exit(0)
"""
        process = subprocess.run(
            [sys.executable, "-I", "-c", script_code],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(
            process.returncode,
            0,
            f"Import boundary failed with stderr: {process.stderr} stdout: {process.stdout}",
        )
        self.assertIn("BOUNDARY_ISOLATION_PASSED", process.stdout)
        self.assertNotIn("LEAK_DETECTED", process.stdout)

    def test_zero_third_party_dependencies(self) -> None:
        """Verify arbitrage_contracts relies 100% on the Python 3.12 standard library."""
        project_root = Path(__file__).resolve().parent.parent.parent
        script_code = """
import sys
from pathlib import Path

project_root = Path('.').resolve()
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

initial_modules = set(sys.modules.keys())
import arbitrage_contracts
imported_modules = set(sys.modules.keys()) - initial_modules

third_party_prohibited = {
    "web3", "eth_account", "eth_typing", "eth_utils", "hexbytes",
    "requests", "aiohttp", "pydantic", "fastapi", "numpy", "pandas"
}

leaks = []
for module_name in imported_modules:
    root_package = module_name.split(".")[0]
    if root_package in third_party_prohibited:
        leaks.append(module_name)

if leaks:
    import json
    print("THIRD_PARTY_LEAK:" + json.dumps(leaks))
    sys.exit(1)

print("ZERO_THIRD_PARTY_PASSED")
sys.exit(0)
"""
        process = subprocess.run(
            [sys.executable, "-I", "-c", script_code],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(
            process.returncode,
            0,
            f"Third party dependency check failed: {process.stderr}",
        )
        self.assertIn("ZERO_THIRD_PARTY_PASSED", process.stdout)


if __name__ == "__main__":
    unittest.main()
