"""Comprehensive unit and reference comparison tests for 2/3-hop cycle search."""

from __future__ import annotations

import pytest

from arbitrage_contracts.identity import (
    AssetRef,
    FeeModel,
    PoolDescriptor,
    PoolKey,
    TokenKey,
)
from state_graph.cycles import find_cycles
from state_graph.graph import PoolGraph
from tests.state_graph.reference_enumeration import naive_enumerate_cycles


def make_asset(name: str, chain_id: int = 4663) -> AssetRef:
    hex_addr = "0x" + name.encode().hex().rjust(40, "0")[:40]
    return AssetRef(
        interface_kind="erc20",
        chain_id=chain_id,
        token_key=TokenKey(chain_id, hex_addr),
    )


def make_pool(
    token0: AssetRef,
    token1: AssetRef,
    pool_id_str: str,
    fee_pips: int = 500,
    chain_id: int = 4663,
) -> PoolDescriptor:
    pool_addr = "0x" + pool_id_str.encode().hex().rjust(40, "0")[:40]
    venue_addr = "0x" + b"factory".hex().rjust(40, "0")[:40]
    key = PoolKey(
        chain_id=chain_id,
        protocol_id="uniswap_v3",
        venue_kind="factory",
        venue_address=venue_addr,
        pool_id_kind="address",
        pool_id=pool_addr,
    )
    return PoolDescriptor(
        key=key,
        currency0=token0,
        currency1=token1,
        fee_model=FeeModel.static(fee_pips),
        tick_spacing=10,
        deployment_status="deployed",
    )


def test_allowed_hops_validation() -> None:
    graph = PoolGraph()
    weth = make_asset("weth")

    with pytest.raises(ValueError, match="allowed_hops must be a subset of \\(2, 3\\)"):
        find_cycles(graph, [weth], allowed_hops=(1, 2))

    with pytest.raises(ValueError, match="allowed_hops must be a subset of \\(2, 3\\)"):
        find_cycles(graph, [weth], allowed_hops=(2, 3, 4))

    with pytest.raises(ValueError, match="allowed_hops must be a subset of \\(2, 3\\)"):
        find_cycles(graph, [weth], allowed_hops=(4,))


def test_two_hop_single_pool_produces_no_cycles() -> None:
    """A single pool between A and B cannot form a 2-hop route because a pool cannot be reused."""
    weth = make_asset("weth")
    usdg = make_asset("usdg")
    pool1 = make_pool(weth, usdg, "pool1")

    graph = PoolGraph([pool1])
    routes = find_cycles(graph, [weth], allowed_hops=(2,))
    assert len(routes) == 0


def test_two_hop_parallel_pools_produce_arbitrage_routes() -> None:
    """Two parallel pools between A and B produce exactly two 2-hop routes."""
    weth = make_asset("weth")
    usdg = make_asset("usdg")
    pool_500 = make_pool(weth, usdg, "pool_500", fee_pips=500)
    pool_3000 = make_pool(weth, usdg, "pool_3000", fee_pips=3000)

    graph = PoolGraph([pool_500, pool_3000])
    routes = find_cycles(graph, [weth], allowed_hops=(2,))

    assert len(routes) == 2
    for r in routes:
        assert len(r.hops) == 2
        assert r.base_asset == weth
        assert r.hops[0].asset_in == weth
        assert r.hops[0].asset_out == usdg
        assert r.hops[1].asset_in == usdg
        assert r.hops[1].asset_out == weth
        assert r.hops[0].pool_key != r.hops[1].pool_key

    # Equivalence with reference brute-force
    ref_ids = naive_enumerate_cycles(graph.all_pools(), [weth], allowed_hops=(2,))
    assert {r.route_id for r in routes} == ref_ids


def test_three_hop_triangle() -> None:
    """Single triangle A -> B -> C -> A produces 2 routes (clockwise and counterclockwise)."""
    weth = make_asset("weth")
    usdg = make_asset("usdg")
    dai = make_asset("dai")

    p_ab = make_pool(weth, usdg, "p_ab")
    p_bc = make_pool(usdg, dai, "p_bc")
    p_ca = make_pool(dai, weth, "p_ca")

    graph = PoolGraph([p_ab, p_bc, p_ca])
    routes = find_cycles(graph, [weth], allowed_hops=(3,))

    assert len(routes) == 2
    for r in routes:
        assert len(r.hops) == 3
        assert r.base_asset == weth
        assert len({h.pool_key for h in r.hops}) == 3
        assert len({h.asset_in for h in r.hops}) == 3

    ref_ids = naive_enumerate_cycles(graph.all_pools(), [weth], allowed_hops=(3,))
    assert {r.route_id for r in routes} == ref_ids


def test_complex_multi_edge_graph_parity_with_reference() -> None:
    """Tests a complex topology with parallel edges, triangles, and disconnected pools."""
    weth = make_asset("weth")
    usdg = make_asset("usdg")
    usdc = make_asset("usdc")
    wbtc = make_asset("wbtc")
    link = make_asset("link")

    pools = [
        # Parallel pools WETH - USDG
        make_pool(weth, usdg, "p_weth_usdg_1", fee_pips=500),
        make_pool(weth, usdg, "p_weth_usdg_2", fee_pips=3000),
        # USDG - USDC
        make_pool(usdg, usdc, "p_usdg_usdc", fee_pips=100),
        # USDC - WETH
        make_pool(usdc, weth, "p_usdc_weth", fee_pips=500),
        # WETH - WBTC
        make_pool(weth, wbtc, "p_weth_wbtc_1", fee_pips=500),
        make_pool(weth, wbtc, "p_weth_wbtc_2", fee_pips=3000),
        # WBTC - USDG
        make_pool(wbtc, usdg, "p_wbtc_usdg", fee_pips=3000),
        # Disconnected pool
        make_pool(link, usdc, "p_link_usdc", fee_pips=3000),
    ]

    graph = PoolGraph(pools)

    # 1. Test 2-hop parity
    routes_2 = find_cycles(graph, [weth], allowed_hops=(2,))
    ref_2 = naive_enumerate_cycles(pools, [weth], allowed_hops=(2,))
    assert {r.route_id for r in routes_2} == ref_2
    assert len(routes_2) == 4  # 2 for WETH-USDG, 2 for WETH-WBTC

    # 2. Test 3-hop parity
    routes_3 = find_cycles(graph, [weth], allowed_hops=(3,))
    ref_3 = naive_enumerate_cycles(pools, [weth], allowed_hops=(3,))
    assert {r.route_id for r in routes_3} == ref_3
    assert len(routes_3) > 0

    # 3. Test combined (2, 3) parity with multiple base assets
    routes_all = find_cycles(graph, [weth, usdg], allowed_hops=(2, 3))
    ref_all = naive_enumerate_cycles(pools, [weth, usdg], allowed_hops=(2, 3))
    assert {r.route_id for r in routes_all} == ref_all

    # 4. Test deterministic order
    sorted_ids = [r.route_id for r in routes_all]
    assert sorted_ids == sorted(sorted_ids)


def test_max_routes_truncation() -> None:
    weth = make_asset("weth")
    usdg = make_asset("usdg")
    usdc = make_asset("usdc")

    pools = [
        make_pool(weth, usdg, "p1"),
        make_pool(weth, usdg, "p2"),
        make_pool(usdg, usdc, "p3"),
        make_pool(usdc, weth, "p4"),
    ]
    graph = PoolGraph(pools)

    all_routes = find_cycles(graph, [weth], allowed_hops=(2, 3))
    assert len(all_routes) > 2

    truncated = find_cycles(graph, [weth], allowed_hops=(2, 3), max_routes=2)
    assert len(truncated) == 2
    assert truncated == all_routes[:2]


def test_cross_chain_isolation() -> None:
    """Edges on different chains must not be merged into the base asset's chain cycle."""
    weth_4663 = make_asset("weth", chain_id=4663)
    usdg_4663 = make_asset("usdg", chain_id=4663)
    weth_56 = make_asset("weth", chain_id=56)
    usdg_56 = make_asset("usdg", chain_id=56)

    p1 = make_pool(weth_4663, usdg_4663, "p1", chain_id=4663)
    p2_bsc = make_pool(usdg_56, weth_56, "p2", chain_id=56)

    graph = PoolGraph([p1, p2_bsc])
    routes = find_cycles(graph, [weth_4663], allowed_hops=(2,))
    assert len(routes) == 0


def test_fixture_graph_json_loading() -> None:
    """Verifies that the registered fixture graph.json can be loaded and matches expected counts."""
    import json
    from pathlib import Path

    from arbitrage_contracts.identity import TokenKey

    fixture_path = (
        Path(__file__).resolve().parent.parent / "fixtures" / "state_graph" / "v2" / "graph.json"
    )
    assert fixture_path.is_file()
    data = json.loads(fixture_path.read_text())

    chain_id = data["chain_id"]
    assets_map = {
        item["address"]: AssetRef(
            interface_kind="erc20",
            chain_id=chain_id,
            token_key=TokenKey(chain_id, item["address"]),
        )
        for item in data["assets"]
    }

    pools = [
        PoolDescriptor(
            key=PoolKey(chain_id, "uniswap_v3", "factory", "0x" + "f"*40, "address", p["pool_id"]),
            currency0=assets_map[p["currency0"]],
            currency1=assets_map[p["currency1"]],
            fee_model=FeeModel.static(p["fee_pips"]),
            tick_spacing=p["tick_spacing"],
            deployment_status="deployed",
        )
        for p in data["pools"]
    ]

    graph = PoolGraph(pools)
    weth_addr = data["base_assets"][0]
    weth = assets_map[weth_addr]

    routes_2 = find_cycles(graph, [weth], allowed_hops=(2,))
    assert len(routes_2) == data["expected_cycle_counts"]["2_hop_weth"]

    routes_3 = find_cycles(graph, [weth], allowed_hops=(3,))
    assert len(routes_3) == data["expected_cycle_counts"]["3_hop_weth"]

    routes_total = find_cycles(graph, [weth], allowed_hops=(2, 3))
    assert len(routes_total) == data["expected_cycle_counts"]["total_weth"]

