#!/usr/bin/env python3
import sys, subprocess, json
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
    
    # Layer 1: Contracts & Pure Models
    run_cmd([str(py), "-m", "pytest", "tests/test_arbitrage_contracts.py", "-q"])
    
    # Layer 2: State Graph & Math
    if (root / "tests/test_state_graph.py").exists():
        run_cmd([str(py), "-m", "pytest", "tests/test_state_graph.py", "-q"])
    
    # Layer 3: Catalog & Opportunities
    if (root / "tests/test_market_catalog.py").exists():
        run_cmd([str(py), "-m", "pytest", "tests/test_market_catalog.py", "-q"])
    if (root / "tests/test_opportunities.py").exists():
        run_cmd([str(py), "-m", "pytest", "tests/test_opportunities.py", "-q"])

    print("All core verification layers passed.")

if __name__ == "__main__":
    main()
