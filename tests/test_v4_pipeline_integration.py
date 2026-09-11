"""Integration tests for V3 + V4 combined live arbitrage pipeline."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from apps.live_pipeline import (
    MULTICALL2_ADDRESS,
    POOL_MANAGER_ADDRESS,
    STATE_VIEW_ADDRESS,
    LiveArbitragePipeline,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_v4_catalog_integrity() -> None:
    """Verify data/v4_pools_live_catalog.json has valid schema and addresses."""
    v4_cat_file = PROJECT_ROOT / "data" / "v4_pools_live_catalog.json"
    assert v4_cat_file.exists(), "v4 catalog file missing"

    with v4_cat_file.open("r", encoding="utf-8") as f:
        pools = json.load(f)

    assert len(pools) >= 200, f"Expected at least 200 V4 pools, got {len(pools)}"

    for p in pools:
        addr = p["address"]
        assert len(addr) == 66 and addr.startswith("0x"), f"Invalid V4 pool id: {addr}"
        assert p["dex"] == "uniswap-v4"
        assert p["fee_pips"] > 0
        assert p["tick_spacing"] > 0
        assert len(p["decimals"]) == 2
        assert 0 < p["dec0"] <= 18, f"Invalid dec0: {p['dec0']}"
        assert 0 < p["dec1"] <= 18, f"Invalid dec1: {p['dec1']}"


def test_combined_pipeline_initialization() -> None:
    """Verify LiveArbitragePipeline successfully initializes with both V3 and V4 catalogs."""
    v3_cat = PROJECT_ROOT / "data" / "v3_pools_live_catalog.json"
    v4_cat = PROJECT_ROOT / "data" / "v4_pools_live_catalog.json"
    ledger_tmp = Path("/tmp/test_combined_ledger.jsonl")

    assert v3_cat.exists()
    assert v4_cat.exists()

    pipeline = LiveArbitragePipeline(
        catalog_path=[v3_cat, v4_cat],
        ledger_path=ledger_tmp,
        min_net_usd=Decimal("0.10"),
        max_amount_usd=Decimal("500.0"),
    )

    v3_cnt = sum(1 for p in pipeline.pools_meta if len(p["address"]) == 42)
    v4_cnt = sum(1 for p in pipeline.pools_meta if len(p["address"]) == 66)

    assert v3_cnt == 137, f"Expected 137 V3 pools, got {v3_cnt}"
    assert v4_cnt >= 200, f"Expected at least 200 V4 pools, got {v4_cnt}"
    assert len(pipeline.candidate_cycles) > 5000, f"Expected > 5000 cycles, got {len(pipeline.candidate_cycles)}"

    # Check 2-hop cycles include mixed V3 and V4 pairs
    two_hops = [c for c in pipeline.candidate_cycles if len(c.hops) == 2]
    assert len(two_hops) >= 500, f"Expected >= 500 2-hop cycles, got {len(two_hops)}"

    # Clean up test ledger
    if ledger_tmp.exists():
        ledger_tmp.unlink()
