"""Controlled offline test runner collecting all tests under tests/arc_readiness."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "tests/arc_readiness",
        "-o",
        "cache_dir=/tmp/pytest_cache",
        "-v",
    ]
    proc = subprocess.run(cmd, cwd=repo_root, check=False)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
