#!/usr/bin/env python3
import subprocess
import sys
from pathlib import Path


def run_cmd(cmd):
    print(f"Running: {' '.join(cmd)}")
    res = subprocess.run(cmd)
    if res.returncode != 0:
        sys.exit(res.returncode)

def main():
    root = Path(__file__).resolve().parent.parent.parent
    py = root / "venv/bin/python"
    if not py.exists():
        py = Path(sys.executable)
    
    # Layer 1: Arbitrage Core Pure Contracts
    if (root / "tests/contracts").exists():
        run_cmd([str(py), "-m", "pytest", "tests/contracts/", "--confcutdir=tests/contracts", "-k", "not test_manifest_coverage", "-o", "cache_dir=/tmp/pytest_cache", "-q"])
    
    # Layer 2: Arc v3 Full Test Suite (Foundation, Runtime, Ingest, Markets, Quotes, Independent)
    if (root / "tests/arc_v3").exists():
        run_cmd([str(py), "-m", "pytest", "tests/arc_v3/", "-o", "cache_dir=/tmp/pytest_cache", "-q"])

    print("All core verification layers passed.")

if __name__ == "__main__":
    main()
