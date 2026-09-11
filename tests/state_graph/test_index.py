"""Unit tests for DirtyRouteIndex reverse index and incremental invalidation."""

from __future__ import annotations

from arbitrage_contracts.identity import (
    AssetRef,
    FeeModel,
    PoolDescriptor,
    PoolKey,
    TokenKey,
)
from state_graph.cycles import find_cycles
from state_graph.graph import PoolGraph
from state_graph.index import DirtyRouteIndex


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


def test_dirty_route_index_basic_query() -> None:
    weth = make_asset("weth")
    usdg = make_asset("usdg")
    dai = make_asset("dai")

    p1 = make_pool(weth, usdg, "p1", fee_pips=500)
    p2 = make_pool(weth, usdg, "p2", fee_pips=3000)
    p_triangle_bc = make_pool(usdg, dai, "p_bc")
    p_triangle_ca = make_pool(dai, weth, "p_ca")

    graph = PoolGraph([p1, p2, p_triangle_bc, p_triangle_ca])
    routes = find_cycles(graph, [weth], allowed_hops=(2, 3))
    assert len(routes) > 0

    index = DirtyRouteIndex(routes)
    assert index.total_routes() == len(routes)

    # Query dirty routes for p1 only
    p1_routes = index.get_affected_routes([p1.key])
    assert len(p1_routes) > 0
    # Every returned route must traverse p1
    for r in p1_routes:
        assert any(h.pool_key == p1.key for h in r.hops)


def test_zero_evaluation_on_disjoint_pool() -> None:
    """A dirty pool not present in any route must trigger ZERO route evaluations."""
    weth = make_asset("weth")
    usdg = make_asset("usdg")
    link = make_asset("link")
    uni = make_asset("uni")

    p1 = make_pool(weth, usdg, "p1", fee_pips=500)
    p2 = make_pool(weth, usdg, "p2", fee_pips=3000)
    disjoint_pool = make_pool(link, uni, "disjoint_pool")

    graph = PoolGraph([p1, p2])
    routes = find_cycles(graph, [weth], allowed_hops=(2,))
    index = DirtyRouteIndex(routes)

    # Querying with disjoint pool must return exactly empty list
    affected = index.get_affected_routes([disjoint_pool.key])
    assert affected == []


def test_multi_pool_dirty_union_without_duplicates() -> None:
    weth = make_asset("weth")
    usdg = make_asset("usdg")
    dai = make_asset("dai")

    p1 = make_pool(weth, usdg, "p1", fee_pips=500)
    p2 = make_pool(weth, usdg, "p2", fee_pips=3000)
    p3 = make_pool(usdg, dai, "p3")
    p4 = make_pool(dai, weth, "p4")

    graph = PoolGraph([p1, p2, p3, p4])
    routes = find_cycles(graph, [weth], allowed_hops=(2, 3))
    index = DirtyRouteIndex(routes)

    # Query with p1 and p3
    affected = index.get_affected_routes([p1.key, p3.key])
    route_ids = [r.route_id for r in affected]
    # Check no duplicate routes
    assert len(route_ids) == len(set(route_ids))
    for r in affected:
        assert any(h.pool_key in (p1.key, p3.key) for h in r.hops)


def test_pool_invalidation_removes_dependent_routes() -> None:
    """When a pool is removed or revoked, all dependent routes must be purged."""
    weth = make_asset("weth")
    usdg = make_asset("usdg")
    p1 = make_pool(weth, usdg, "p1", fee_pips=500)
    p2 = make_pool(weth, usdg, "p2", fee_pips=3000)

    graph = PoolGraph([p1, p2])
    routes = find_cycles(graph, [weth], allowed_hops=(2,))
    index = DirtyRouteIndex(routes)
    assert index.total_routes() == 2

    # Invalidate p1
    invalidated_ids = index.invalidate_pool(p1.key)
    assert len(invalidated_ids) == 2
    assert index.total_routes() == 0
    assert index.get_affected_routes([p1.key]) == []
    assert index.get_affected_routes([p2.key]) == []


def test_incremental_topology_add_and_remove() -> None:
    """Adding a pool incrementally updates graph and discovers only newly formed routes."""
    weth = make_asset("weth")
    usdg = make_asset("usdg")
    usdc = make_asset("usdc")

    p1 = make_pool(weth, usdg, "p1", fee_pips=500)
    p2 = make_pool(usdg, usdc, "p2", fee_pips=100)

    graph = PoolGraph([p1, p2])
    # Initially no closed cycle
    initial_routes = find_cycles(graph, [weth], allowed_hops=(2, 3))
    assert len(initial_routes) == 0

    index = DirtyRouteIndex(initial_routes)
    assert index.total_routes() == 0

    # Incrementally add closing pool p3: USDC -> WETH
    p3 = make_pool(usdc, weth, "p3", fee_pips=500)
    newly_discovered = index.update_topology_add_pool(p3, graph, [weth], allowed_hops=(2, 3))

    assert len(newly_discovered) > 0
    assert index.total_routes() == len(newly_discovered)

    # Verify equivalence with full graph re-scan
    full_routes = find_cycles(graph, [weth], allowed_hops=(2, 3))
    assert {r.route_id for r in index.all_routes()} == {r.route_id for r in full_routes}

    # Now remove p2
    invalidated = index.update_topology_remove_pool(p2.key, graph)
    assert len(invalidated) > 0
    assert index.total_routes() == 0
    assert not graph.has_pool(p2.key)
