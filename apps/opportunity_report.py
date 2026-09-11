"""Thin local CLI for W2 shadow ledger reporting."""

from __future__ import annotations

import argparse
from pathlib import Path

from opportunities.report import write_report
from opportunities.store import LedgerError


def main() -> int:
    """Generate local report files and return a business-safe exit code."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        write_report(args.ledger, args.output_root)
    except (LedgerError, OSError, ValueError) as error:
        print(str(error))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
