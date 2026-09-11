"""Arc Historical Block Replay CLI Entrypoint (T38)

Usage:
  python apps/arc_history.py --chain-id 5042 --from-block 100 --to-block 110 --output-dir runtime-data/history --fixture-mode
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

from arc_runtime.history import HistoryReplayConfig, run_history_scan  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Historical consecutive block scan and replay hypothesis generator for Arc chain.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--chain-id", type=int, default=5042, help="Target Arc chain ID (5042=mainnet, 5042002=testnet)")
    parser.add_argument("--from-block", type=int, required=True, help="Starting block number")
    parser.add_argument("--to-block", type=int, required=True, help="Ending block number")
    parser.add_argument("--output-dir", type=Path, required=True, help="Directory to store replay reports and hypotheses")
    parser.add_argument("--mode", type=str, default="retrospective_state", choices=["retrospective_state", "causal_replay"], help="Historical evaluation mode")
    parser.add_argument("--fixture-mode", action="store_true", help="Run in deterministic offline fixture mode")
    parser.add_argument("--rpc-endpoint", type=str, default=None, help="Readonly RPC URL for live mode")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        config = HistoryReplayConfig(
            chain_id=args.chain_id,
            from_block=args.from_block,
            to_block=args.to_block,
            output_dir=args.output_dir,
            mode=args.mode,
            fixture_mode=args.fixture_mode,
            rpc_endpoint=args.rpc_endpoint,
        )
        res = run_history_scan(config)
        print(json.dumps(res, indent=2))
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
