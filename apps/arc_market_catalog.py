"""CLI entrypoint for Arc market catalog registry and diagnostics."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from arc_readiness.catalog import ArcMarketCatalog  # noqa: E402
from arc_readiness.models import (  # noqa: E402
    ArcAssetEligibilityDraft,
    ArcMarketEligibilityDraft,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Arc Market Catalog CLI")
    parser.add_argument("--offline", action="store_true", help="Run in strict offline mode")
    parser.add_argument("--input", type=str, required=True, help="Path to input stream JSON")
    parser.add_argument(
        "--output", type=str, required=True, help="Path to write catalog report JSON"
    )

    args = parser.parse_args(argv)

    input_path = Path(args.input)
    output_path = Path(args.output)

    # Fail-closed checks on input file
    if not input_path.exists() or not input_path.is_file():
        sys.stderr.write(f"Error: Input file does not exist or is not a file: {input_path}\n")
        return 2

    if input_path.stat().st_size == 0:
        sys.stderr.write(f"Error: Input file is empty: {input_path}\n")
        return 2

    try:
        raw_data = json.loads(input_path.read_text(encoding="utf-8"))
    except Exception as exc:
        sys.stderr.write(f"Error: Failed to parse input JSON: {exc}\n")
        return 2

    if not isinstance(raw_data, dict):
        sys.stderr.write(
            f"Error: Input root must be a JSON object, got {type(raw_data).__name__}\n"
        )
        return 2

    try:
        catalog = ArcMarketCatalog()

        raw_assets = raw_data.get("assets", [])
        if isinstance(raw_assets, list):
            for a in raw_assets:
                if isinstance(a, dict):
                    catalog.register_asset(ArcAssetEligibilityDraft.from_dict(a))

        raw_markets = raw_data.get("markets", [])
        if isinstance(raw_markets, list):
            for m in raw_markets:
                if isinstance(m, dict):
                    catalog.register_market(ArcMarketEligibilityDraft.from_dict(m))

        report = catalog.export_summary()

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        sys.stdout.write(
            f"Catalog report written successfully to {output_path} (status={report['status']})\n"
        )
        return 0
    except Exception as exc:
        sys.stderr.write(f"Error processing market catalog stream: {exc}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
