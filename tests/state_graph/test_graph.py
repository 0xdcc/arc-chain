"""Unit tests for PoolGraph directed multigraph data structure."""

from __future__ import annotations

import pytest

from arbitrage_contracts.identity import (
    AssetRef,
    FeeModel,
    PoolDescriptor,
    PoolKey,
    TokenKey,
)
from state_graph.graph import PoolGraph


def make_asset(name: str, chain_id: int = 4663) -> AssetRef:
    # 40-char hex address derived from name
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
    deployment_status: str = "deployed",
) -> PoolDescriptor:
    chain_id = token0.chain_id
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
        deployment_status=deployment_status,
    )


def test_empty_graph() -> None:
    graph = PoolGraph()
    assert graph.pool_count() == 0
    assert graph.edge_count() == 0
    assert len(graph.nodes()) == 0
    assert len(graph.all_pools()) == 0


def test_add_pool_and_query_edges() -> None:
    weth = make_asset("weth")
    usdg = make_asset("usdg")
    pool1 = make_pool(weth, usdg, "pool1", fee_pips=500)

    graph = PoolGraph()
    graph.add_pool(pool1)

    assert graph.pool_count() == 1
    assert graph.edge_count() == 2
    assert graph.has_pool(pool1.key)
    assert graph.get_pool(pool1.key) == pool1

    weth_edges = graph.get_outgoing_edges(weth)
    assert len(weth_edges) == 1
    assert weth_edges[0].asset_in == weth
    assert weth_edges[0].asset_out == usdg
    assert weth_edges[0].direction == "zero_for_one"

    usdg_edges = graph.get_outgoing_edges(usdg)
    assert len(usdg_edges) == 1
    assert usdg_edges[0].asset_in == usdg
    assert usdg_edges[0].asset_out == weth
    assert usdg_edges[0].direction == "one_for_zero"


def test_parallel_edges_preservation() -> None:
    """Verifies that multiple parallel pools for the same pair are all retained."""
    weth = make_asset("weth")
    usdg = make_asset("usdg")

    pool_500 = make_pool(weth, usdg, "pool_500", fee_pips=500)
    pool_3000 = make_pool(weth, usdg, "pool_3000", fee_pips=3000)
    pool_10000 = make_pool(weth, usdg, "pool_10000", fee_pips=10000)

    graph = PoolGraph([pool_500, pool_3000, pool_10000])

    assert graph.pool_count() == 3
    assert graph.edge_count() == 6

    weth_edges = graph.get_outgoing_edges(weth)
    assert len(weth_edges) == 3
    pool_keys = {e.pool_key for e in weth_edges}
    assert pool_keys == {pool_500.key, pool_3000.key, pool_10000.key}


def test_remove_pool() -> None:
    weth = make_asset("weth")
    usdg = make_asset("usdg")
    pool1 = make_pool(weth, usdg, "pool1")
    pool2 = make_pool(weth, usdg, "pool2")

    graph = PoolGraph([pool1, pool2])
    assert graph.pool_count() == 2
    assert graph.edge_count() == 4

    removed = graph.remove_pool(pool1.key)
    assert removed is True
    assert graph.pool_count() == 1
    assert graph.edge_count() == 2
    assert not graph.has_pool(pool1.key)
    assert graph.has_pool(pool2.key)

    # Removing already removed pool returns False
    assert graph.remove_pool(pool1.key) is False

    # Removing pool2 cleans up nodes
    graph.remove_pool(pool2.key)
    assert graph.pool_count() == 0
    assert graph.edge_count() == 0
    assert len(graph.nodes()) == 0


def test_non_deployed_pool_ignored() -> None:
    weth = make_asset("weth")
    usdg = make_asset("usdg")
    pending_pool = make_pool(weth, usdg, "pending", deployment_status="pending")

    graph = PoolGraph([pending_pool])
    assert graph.pool_count() == 0
    assert graph.edge_count() == 0


def test_idempotent_add_pool() -> None:
    weth = make_asset("weth")
    usdg = make_asset("usdg")
    pool1 = make_pool(weth, usdg, "pool1", fee_pips=500)

    graph = PoolGraph()
    graph.add_pool(pool1)
    assert graph.pool_count() == 1
    assert graph.edge_count() == 2

    # Re-add exact same pool
    graph.add_pool(pool1)
    assert graph.pool_count() == 1
    assert graph.edge_count() == 2
