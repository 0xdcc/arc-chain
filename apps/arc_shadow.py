"""Arc Shadow Evaluation CLI Entrypoint (T38)

Usage:
  python apps/arc_shadow.py --chain-id 5042 --ledger-dir runtime-data/ledgers --fixture-mode
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

from arc_runtime.shadow import ShadowPipelineConfig, run_shadow_pipeline  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Readonly shadow evaluation pipeline CLI for Arc chain.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--chain-id", type=int, default=5042, help="Target Arc chain ID (5042=mainnet, 5042002=testnet)")
    parser.add_argument("--ledger-dir", type=Path, required=True, help="Directory to store hash-chained opportunity ledgers")
    parser.add_argument("--max-amount-usd", type=float, default=500.0, help="Maximum single trade amount cap in USD (<= 500)")
    parser.add_argument("--fixture-mode", action="store_true", help="Run in deterministic offline fixture mode")
    parser.add_argument("--rpc-endpoint", type=str, default=None, help="Readonly RPC URL for live mode")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        config = ShadowPipelineConfig(
            chain_id=args.chain_id,
            ledger_dir=args.ledger_dir,
            max_amount_usd=args.max_amount_usd,
            fixture_mode=args.fixture_mode,
            rpc_endpoint=args.rpc_endpoint,
        )
        res = run_shadow_pipeline(config)
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
