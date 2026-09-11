"""Offline shadow replay and discrete integer route evaluation CLI."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from arbitrage_contracts import ContractRecord
from arbitrage_contracts.identity import (
    Amount,
    AssetRef,
    FeeModel,
    PoolDescriptor,
    PoolKey,
    TokenKey,
)
from arbitrage_contracts.quote import DataMode, QuoteEvidence, QuoteStatus
from arbitrage_contracts.serialization import encode_record_json
from arbitrage_contracts.state import Cursor, StateVersion
from state_graph.cycles import find_cycles
from state_graph.evaluate import evaluate_route_exact_input, resolve_token_decimals
from state_graph.graph import PoolGraph
from state_graph.index import DirtyRouteIndex
from state_graph.store import StateStore
from state_graph.types import PoolStateSnapshot


def _load_non_empty_json(path_str: str) -> dict[str, Any]:
    path = Path(path_str)
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"Input file does not exist or is empty: {path_str}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"Root of {path_str} must be a JSON object")
        return data
    except Exception as exc:
        raise ValueError(f"Failed to parse JSON from {path_str}: {exc}") from exc


def _parse_registry(
    data: dict[str, Any], default_chain_id: int = 4663
) -> tuple[list[PoolDescriptor], list[AssetRef]]:
    chain_id = data.get("chain_id", default_chain_id)
    raw_pools = data.get("pools", [])
    if not isinstance(raw_pools, list) or not raw_pools:
        raise ValueError("Registry must contain a non-empty 'pools' list")

    assets_cache: dict[str, AssetRef] = {}

    def get_asset(address: str) -> AssetRef:
        clean_addr = address.lower()
        if clean_addr not in assets_cache:
            assets_cache[clean_addr] = AssetRef(
                interface_kind="erc20",
                chain_id=chain_id,
                token_key=TokenKey(chain_id, clean_addr),
            )
        return assets_cache[clean_addr]

    pools: list[PoolDescriptor] = []
    for p in raw_pools:
        p_id = p["pool_id"]
        c0 = get_asset(p["currency0"])
        c1 = get_asset(p["currency1"])
        fee_pips = int(p.get("fee_pips", 500))
        tick_spacing = int(p.get("tick_spacing", 10))
        venue_addr = p.get("venue_address", "0x" + "f" * 40)

        key = PoolKey(
            chain_id=chain_id,
            protocol_id="uniswap_v3",
            venue_kind="factory",
            venue_address=venue_addr,
            pool_id_kind="address",
            pool_id=p_id,
        )
        desc = PoolDescriptor(
            key=key,
            currency0=c0,
            currency1=c1,
            fee_model=FeeModel.static(fee_pips),
            tick_spacing=tick_spacing,
            deployment_status=p.get("deployment_status", "deployed"),
        )
        pools.append(desc)

    raw_base_assets = data.get("base_assets", [])
    base_assets: list[AssetRef] = []
    if raw_base_assets:
        for b_addr in raw_base_assets:
            base_assets.append(get_asset(b_addr))
    else:
        # Default to first unique asset if none specified
        if assets_cache:
            base_assets.append(next(iter(assets_cache.values())))

    return pools, base_assets


def _parse_snapshots(
    data: dict[str, Any], default_chain_id: int = 4663
) -> tuple[StateVersion, list[PoolStateSnapshot]]:
    chain_id = data.get("chain_id", default_chain_id)
    raw_sv = data.get("state_version", {})
    if not isinstance(raw_sv, dict) or not raw_sv:
        raise ValueError("Input manifest must contain a valid 'state_version' object")

    raw_cursor = raw_sv.get("applied_cursor")
    cursor: Cursor | None = None
    if isinstance(raw_cursor, dict) and raw_cursor.get("block_hash"):
        cursor = Cursor(
            block_hash=raw_cursor["block_hash"],
            transaction_hash=raw_cursor.get("transaction_hash"),
            transaction_index=raw_cursor.get("transaction_index"),
            log_index=raw_cursor.get("log_index"),
        )

    state_ver = StateVersion(
        chain_id=raw_sv.get("chain_id", chain_id),
        block_domain=raw_sv.get("block_domain", "l2"),
        parent_hash=raw_sv.get("parent_hash"),
        stale_reasons=raw_sv.get("stale_reasons", ()),
        block_number=raw_sv.get("block_number", 0),
        block_hash=raw_sv["block_hash"],
        received_at_ms=raw_sv.get("received_at_ms", 1000),
        applied_cursor=cursor,
        complete_through_block=raw_sv.get("complete_through_block", raw_sv.get("block_number", 0)),
        completeness=raw_sv.get("completeness", "ready"),
    )

    raw_snaps = data.get("pool_snapshots", [])
    if not isinstance(raw_snaps, list) or not raw_snaps:
        raise ValueError("Input manifest must contain a non-empty 'pool_snapshots' list")

    snapshots: list[PoolStateSnapshot] = []
    for s in raw_snaps:
        snap = PoolStateSnapshot(
            pool_id=s["pool_id"],
            sqrt_price_x96=int(s["sqrt_price_x96"]),
            tick=int(s["tick"]),
            liquidity=int(s["liquidity"]),
            fee_pips=int(s["fee_pips"]),
            tick_spacing=int(s["tick_spacing"]),
            block_number=int(s["block_number"]),
            block_hash=s["block_hash"],
        )
        snapshots.append(snap)

    return state_ver, snapshots


def run_shadow_replay(
    manifest_path: str,
    registry_path: str,
    output_dir_str: str,
    amounts: Sequence[int] = (1000, 10000, 50000),
    data_mode: str = DataMode.SYNTHETIC,
) -> dict[str, Any]:
    """Executes offline state graph shadow evaluation and writes output files."""
    manifest_data = _load_non_empty_json(manifest_path)
    registry_data = _load_non_empty_json(registry_path)

    pools, base_assets = _parse_registry(registry_data)
    raw_decimals = registry_data.get("token_decimals")
    if not isinstance(raw_decimals, dict) or not raw_decimals:
        raise ValueError("Registry requires token_decimals for all route assets")
    decimals: dict[TokenKey, int] = {}
    chain_id = registry_data.get("chain_id", 4663)
    for address, value in raw_decimals.items():
        token = TokenKey(chain_id, address)
        if type(value) is not int or not 0 <= value <= 255:
            raise ValueError("Invalid registry token decimals")
        if token in decimals and decimals[token] != value:
            raise ValueError("Conflicting registry token decimals")
        decimals[token] = value
    for asset in base_assets + [
        asset for pool in pools for asset in (pool.currency0, pool.currency1)
    ]:
        resolve_token_decimals(asset, decimals)
    state_ver, snapshots = _parse_snapshots(manifest_data)

    store = StateStore()
    epoch = store.publish_epoch(state_ver, snapshots)

    graph = PoolGraph(pools)
    routes = find_cycles(graph, base_assets, allowed_hops=(2, 3))

    index = DirtyRouteIndex(routes)
    all_indexed_routes = index.all_routes()

    output_dir = Path(output_dir_str)
    output_dir.mkdir(parents=True, exist_ok=True)

    quotes_path = output_dir / "quotes.jsonl"
    quotes_records: list[QuoteEvidence] = []

    with open(quotes_path, "w", encoding="utf-8") as f:
        for route in all_indexed_routes:
            for amt in amounts:
                amount_in = Amount(
                    route.base_asset, int(amt), resolve_token_decimals(route.base_asset, decimals)
                )
                evidence = evaluate_route_exact_input(
                    route=route,
                    amount_in=amount_in,
                    epoch=epoch,
                    data_mode=data_mode,
                    token_decimals=decimals,
                )
                quotes_records.append(evidence)

                record = ContractRecord(
                    schema_id="arbitrage-evidence",
                    schema_version="1.0.0",
                    record_type="quote_evidence",
                    run_id=f"shadow-{epoch.epoch_id[-16:]}",
                    data_mode=data_mode,
                    provenance={"witness": "state_graph_shadow"},
                    payload=evidence,
                )
                f.write(encode_record_json(record) + "\n")

    successful_count = sum(1 for q in quotes_records if q.status == QuoteStatus.QUOTED)
    summary = {
        "status": "success",
        "epoch_id": epoch.epoch_id,
        "block_number": state_ver.block_number,
        "total_routes_discovered": len(all_indexed_routes),
        "total_quotes_evaluated": len(quotes_records),
        "successful_quotes": successful_count,
        "output_quotes_file": str(quotes_path),
    }

    summary_path = output_dir / "summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="State Graph Shadow Replay CLI")
    parser.add_argument("--mode", default="replay", choices=["replay"], help="Operation mode")
    parser.add_argument("--input-manifest", required=True, help="Path to input snapshot manifest")
    parser.add_argument("--registry", required=True, help="Path to pool registry JSON")
    parser.add_argument("--output-dir", required=True, help="Directory to save output files")
    parser.add_argument(
        "--amounts", default="1000,10000,50000", help="Comma-separated test input amounts in atoms"
    )
    parser.add_argument("--data-mode", default=DataMode.SYNTHETIC, help="Data mode label")

    args = parser.parse_args()

    try:
        amounts = [int(a.strip()) for a in args.amounts.split(",") if a.strip()]
        summary = run_shadow_replay(
            manifest_path=args.input_manifest,
            registry_path=args.registry,
            output_dir_str=args.output_dir,
            amounts=amounts,
            data_mode=args.data_mode,
        )
        print(f"SHADOW_REPLAY_SUCCESS: evaluated {summary['total_quotes_evaluated']} quotes")
        return 0
    except ValueError as exc:
        print(f"INPUT_VALIDATION_ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"EXECUTION_ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
