"""Arc Ingest CLI Entrypoint (T37)

Usage:
  python apps/arc_collect.py --chain-id 5042 --from-block 100 --to-block 105 --output-dir runtime-data/ingest --fixture-mode
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Self-heal repo root in sys.path
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from arbitrage_contracts.arc_extensions import BlockDomain  # noqa: E402
from arc_runtime.collect import CollectorConfig, execute_collection  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record-only ingest CLI for Arc chain block data.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--chain-id",
        type=int,
        default=5042,
        help="Target Arc chain ID (5042=mainnet, 5042002=testnet)",
    )
    parser.add_argument(
        "--from-block", type=int, required=True, help="Starting block number (inclusive)"
    )
    parser.add_argument(
        "--to-block", type=int, required=True, help="Ending block number (inclusive)"
    )
    parser.add_argument(
        "--output-dir", type=Path, required=True, help="Directory to store JSONL and manifests"
    )
    parser.add_argument(
        "--fixture-mode", action="store_true", help="Run in deterministic offline fixture mode"
    )
    parser.add_argument(
        "--rpc-endpoint", type=str, default=None, help="Readonly RPC URL for live mode"
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        config = CollectorConfig(
            chain_id=args.chain_id,
            block_domain=BlockDomain.L1,
            from_block=args.from_block,
            to_block=args.to_block,
            output_dir=args.output_dir,
            fixture_mode=args.fixture_mode,
            rpc_endpoint=args.rpc_endpoint,
        )
        summary = execute_collection(config)
        output = {
            "status": "SUCCESS",
            "chain_id": summary.chain_id,
            "range": [summary.from_block, summary.to_block],
            "envelopes_written": summary.envelopes_written,
            "output_jsonl": str(summary.output_jsonl),
            "manifest_json": str(summary.manifest_json),
            "cursor_json": str(summary.cursor_json),
            "elapsed_seconds": round(summary.elapsed_seconds, 4),
            "is_fixture_mode": summary.is_fixture_mode,
        }
        print(json.dumps(output, indent=2))
        sys.exit(0)
    except Exception as err:
        error_output = {
            "status": "ERROR",
            "error_type": type(err).__name__,
            "message": str(err),
        }
        print(json.dumps(error_output, indent=2), file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
