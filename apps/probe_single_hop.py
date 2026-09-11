"""Offline single-hop lifecycle CLI for validated W1 bootstrap catalogs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from arbitrage_contracts.state import StateVersion
from opportunities.input_gate import InputGateError, load_w1_bootstrap_catalog
from opportunities.single_hop import SingleHopQuoteAdapter, SingleHopQuoteRequest
from opportunities.single_hop_lifecycle import (
    LifecycleInputError,
    run_single_hop_lifecycle,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe independent single-hop quote curves")
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--state-json", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--tiers", default="1000000000000000000")
    return parser.parse_args()


def _load_state(raw: str) -> StateVersion:
    try:
        value: Any = json.loads(Path(raw).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        value = json.loads(raw)
    if not isinstance(value, dict):
        raise LifecycleInputError("state must be a JSON object")
    return StateVersion(
        value["chain_id"],
        value["block_number"],
        value["block_hash"],
        received_at_ms=value["received_at_ms"],
        block_domain=value["block_domain"],
        complete_through_block=value.get("complete_through_block"),
        completeness=value["completeness"],
    )


def main() -> int:
    """Run the isolated lifecycle and print a JSON summary."""
    try:
        args = _parse_args()
        state = _load_state(args.state_json)
        catalog = load_w1_bootstrap_catalog(args.catalog)
        if args.output_root.is_symlink():
            raise LifecycleInputError("output root must not be a symlink")
        args.output_root.mkdir(parents=True, exist_ok=True)
        tiers = tuple(int(value) for value in args.tiers.split(","))
        adapter = SingleHopQuoteAdapter(
            rpc=_UnsupportedRpc(),
            request=SingleHopQuoteRequest(
                quoter_v3="0x0000000000000000000000000000000000000001",
                quoter_v4="0x0000000000000000000000000000000000000002",
                data_mode=catalog.data_mode,
                actor_scope="synthetic" if catalog.data_mode == "synthetic" else "third_party",
            ),
        )
        results = run_single_hop_lifecycle(
            catalog.descriptors,
            tiers,
            state,
            adapter,
            args.output_root,
        )
        print(
            json.dumps(
                {str(key): result.capability.can_quote for key, result in results.items()},
                sort_keys=True,
            )
        )
    except (InputGateError, LifecycleInputError, OSError, KeyError, TypeError, ValueError) as error:
        print(f"[Fail-Closed] single-hop probe rejected: {error}", file=sys.stderr)
        return 1
    return 0


class _UnsupportedRpc:
    """Transport stub that makes explicit support the responsibility of configured adapters."""

    def call(
        self,
        method: str,
        params: list[Any] | tuple[Any, ...] | None = None,
        block_identifier: str | None = None,
    ) -> dict[str, Any]:
        raise RuntimeError("RPC transport is not configured for offline single-hop probing")


if __name__ == "__main__":
    sys.exit(main())
