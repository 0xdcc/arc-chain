"""CLI entrypoint for Arc network readiness inspection and reporting."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from arc_readiness.models import (  # noqa: E402
    ArcBalanceObservation,
    ArcFeeObservation,
    ArcNetworkIdentity,
)
from arc_readiness.reporting import build_network_readiness_report  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Arc Network Readiness CLI")
    parser.add_argument("--offline", action="store_true", help="Run in strict offline mode")
    parser.add_argument("--input", type=str, required=True, help="Path to input stream JSON")
    parser.add_argument(
        "--output", type=str, required=True, help="Path to write output readiness JSON"
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
        raw_identity = raw_data.get("network_identity")
        if not raw_identity or not isinstance(raw_identity, dict):
            sys.stderr.write("Error: Missing or invalid 'network_identity' section\n")
            return 2
        identity = ArcNetworkIdentity.from_dict(raw_identity)

        balances = [
            ArcBalanceObservation.from_dict(b)
            for b in raw_data.get("balance_observations", [])
            if isinstance(b, dict)
        ]
        fees = [
            ArcFeeObservation.from_dict(f)
            for f in raw_data.get("fee_observations", [])
            if isinstance(f, dict)
        ]

        report = build_network_readiness_report(
            identity=identity,
            balances=balances,
            fees=fees,
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        sys.stdout.write(f"Readiness report written successfully to {output_path}\n")
        return 0
    except Exception as exc:
        sys.stderr.write(f"Error processing readiness stream: {exc}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
