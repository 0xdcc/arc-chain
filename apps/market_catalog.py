"""One-shot, explicit-file catalog export: python -m apps.market_catalog."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TextIO

from market_catalog.export import CatalogHeader, build_snapshot, snapshot_files, write_snapshot
from market_catalog.inputs import ReviewTrust


def main(argv: list[str] | None = None, *, stdout: TextIO | None = None) -> int:
    """Export a bootstrap snapshot and emit its public records as JSONL on stdout."""
    parser = argparse.ArgumentParser(prog="apps.market_catalog", add_help=False, allow_abbrev=False)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--review", type=Path)
    parser.add_argument("--trust", type=Path)
    args = parser.parse_args(argv)
    try:
        header = CatalogHeader(**json.loads(args.metadata.read_text()))
        trust = ReviewTrust(**json.loads(args.trust.read_text())) if args.trust else None
        snapshot = build_snapshot(args.input, header, review=args.review, trust=trust)
        files = snapshot_files(snapshot)
        write_snapshot(snapshot, args.output)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.exit(2, f"catalog: {exc}\n")
    output = stdout if stdout is not None else sys.stdout
    for name in ("assets", "eligibility", "pools", "capabilities", "evidence"):
        output.write(files[f"{name}.jsonl"].decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
