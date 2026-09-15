"""Frozen historical V3/V4 catalog topology; no live execution capability."""

from __future__ import annotations

import json
from pathlib import Path

from atomic_execution.inputs import USDG_ADDRESS_4663, WETH_ADDRESS_4663
from research.catalog_topology import HistoricalCatalogTopology

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_v4_catalog_integrity() -> None:
    """Verify data/v4_pools_live_catalog.json has valid schema and addresses."""
    v4_cat_file = PROJECT_ROOT / "tests" / "fixtures" / "historical" / "robinhood" / "v4_pools_live_catalog.json"
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
    """Verify historical catalog initialization and real connectivity counts; no live transport."""
    v3_cat = PROJECT_ROOT / "tests" / "fixtures" / "historical" / "robinhood" / "v3_pools_live_catalog.json"
    v4_cat = PROJECT_ROOT / "tests" / "fixtures" / "historical" / "robinhood" / "v4_pools_live_catalog.json"

    assert v3_cat.exists()
    assert v4_cat.exists()

    pipeline = HistoricalCatalogTopology(
        catalog_paths=[v3_cat, v4_cat], chain_id=4663,
        base_tokens=[WETH_ADDRESS_4663, USDG_ADDRESS_4663, "0x" + "00" * 20],
        protocol_schemas={"uniswap-v3": "v3", "up-v3": "v3", "giga-v3": "v3",
                          "ramses-v3": "v3", "uniswap-v4": "v4"},
    )

    v3_cnt = sum(1 for p in pipeline.pools_meta if len(p["address"]) == 42)
    v4_cnt = sum(1 for p in pipeline.pools_meta if len(p["address"]) == 66)

    assert v3_cnt == 137, f"Expected 137 V3 pools, got {v3_cnt}"
    assert v4_cnt >= 200, f"Expected at least 200 V4 pools, got {v4_cnt}"
    assert len(pipeline.candidate_cycles) > 5000, f"Expected > 5000 cycles, got {len(pipeline.candidate_cycles)}"

    # Check 2-hop cycles include mixed V3 and V4 pairs
    two_hops = [c for c in pipeline.candidate_cycles if len(c.hops) == 2]
    assert len(two_hops) >= 500, f"Expected >= 500 2-hop cycles, got {len(two_hops)}"


