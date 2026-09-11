"""Unit tests for fixed-epoch hop-by-hop exact-input route evaluation."""

from __future__ import annotations

import pytest

from arbitrage_contracts.identity import (
    Amount,
    AssetRef,
    FeeModel,
    PoolDescriptor,
    PoolKey,
    TokenKey,
)
from arbitrage_contracts.quote import (
    EvidenceLevel,
    HopRef,
    QuoteStatus,
    RouteRef,
)
from arbitrage_contracts.state import StateVersion
from state_graph.clmm_math import get_sqrt_ratio_at_tick
from state_graph.evaluate import evaluate_route_exact_input
from state_graph.types import FrozenEpoch, PoolStateSnapshot


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
        tick_spacing=60 if fee_pips == 3000 else 10,
        deployment_status="deployed",
    )


def make_epoch(snapshots: list[PoolStateSnapshot]) -> FrozenEpoch:
    state = StateVersion(
        chain_id=4663,
        block_number=100,
        block_hash="0x" + "aa" * 32,
        received_at_ms=1000,
        complete_through_block=100,
        completeness="ready",
    )
    return FrozenEpoch(
        epoch_id="epoch:test:100",
        state_version=state,
        snapshots=tuple(snapshots),
        created_at_ms=1000,
    )


def test_evaluate_two_hop_exact_input() -> None:
    weth = make_asset("weth")
    usdg = make_asset("usdg")

    p1 = make_pool(weth, usdg, "p1", fee_pips=500)
    p2 = make_pool(weth, usdg, "p2", fee_pips=3000)

    hop1 = HopRef(p1.key, weth, usdg, "zero_for_one", p1)
    hop2 = HopRef(p2.key, usdg, weth, "one_for_zero", p2)
    route = RouteRef(4663, weth, (hop1, hop2), max_hops=3)

    snap1 = PoolStateSnapshot(
        pool_id=p1.key.pool_id,
        sqrt_price_x96=get_sqrt_ratio_at_tick(5),
        tick=5,
        liquidity=10**22,
        fee_pips=500,
        tick_spacing=10,
        block_number=100,
        block_hash="0x" + "aa" * 32,
    )
    snap2 = PoolStateSnapshot(
        pool_id=p2.key.pool_id,
        sqrt_price_x96=get_sqrt_ratio_at_tick(5),
        tick=5,
        liquidity=10**22,
        fee_pips=3000,
        tick_spacing=60,
        block_number=100,
        block_hash="0x" + "aa" * 32,
    )
    epoch = make_epoch([snap1, snap2])

    amount_in = Amount(weth, 10000, 18)
    evidence = evaluate_route_exact_input(
        route,
        amount_in,
        epoch,
        token_decimals={asset: 18 for hop in route.hops for asset in (hop.asset_in, hop.asset_out)},
    )

    assert evidence.status == QuoteStatus.QUOTED
    assert evidence.amount_out is not None
    assert evidence.amount_out.atoms > 0
    assert evidence.delta_atoms is not None
    # Because of fees (500 + 3000 pips) with identical price, delta must be negative
    assert evidence.delta_atoms < 0
    assert evidence.delta_atoms == evidence.amount_out.atoms - amount_in.atoms
    assert len(evidence.hop_quotes) == 2
    assert evidence.amount_out is not None
    assert evidence.hop_quotes[0].amount_out is not None
    assert evidence.hop_quotes[1].amount_out is not None
    assert evidence.hop_quotes[0].amount_in.atoms == amount_in.atoms
    assert evidence.hop_quotes[1].amount_in.atoms == evidence.hop_quotes[0].amount_out.atoms
    assert evidence.amount_out.atoms == evidence.hop_quotes[1].amount_out.atoms
    assert evidence.evidence_level == EvidenceLevel.LOCAL_QUOTE


def test_evaluate_three_hop_exact_input() -> None:
    weth = make_asset("weth")
    usdg = make_asset("usdg")
    dai = make_asset("dai")

    p1 = make_pool(weth, usdg, "p1", fee_pips=500)
    p2 = make_pool(usdg, dai, "p2", fee_pips=500)
    p3 = make_pool(dai, weth, "p3", fee_pips=500)

    hop1 = HopRef(p1.key, weth, usdg, "zero_for_one", p1)
    hop2 = HopRef(p2.key, usdg, dai, "zero_for_one", p2)
    hop3 = HopRef(p3.key, dai, weth, "zero_for_one", p3)
    route = RouteRef(4663, weth, (hop1, hop2, hop3), max_hops=3)

    snapshots = [
        PoolStateSnapshot(
            p.key.pool_id, get_sqrt_ratio_at_tick(5), 5, 10**22, 500, 10, 100, "0x" + "a" * 64
        )
        for p in (p1, p2, p3)
    ]
    epoch = make_epoch(snapshots)

    amount_in = Amount(weth, 50000, 18)
    evidence = evaluate_route_exact_input(
        route,
        amount_in,
        epoch,
        token_decimals={asset: 18 for hop in route.hops for asset in (hop.asset_in, hop.asset_out)},
    )

    assert evidence.status == QuoteStatus.QUOTED
    assert evidence.amount_out is not None
    assert evidence.delta_atoms is not None
    assert evidence.delta_atoms == evidence.amount_out.atoms - amount_in.atoms
    assert len(evidence.hop_quotes) == 3


def test_missing_pool_snapshot_fails_closed() -> None:
    weth = make_asset("weth")
    usdg = make_asset("usdg")
    p1 = make_pool(weth, usdg, "p1")
    p2 = make_pool(weth, usdg, "p2")

    route = RouteRef(
        4663,
        weth,
        (
            HopRef(p1.key, weth, usdg, "zero_for_one", p1),
            HopRef(p2.key, usdg, weth, "one_for_zero", p2),
        ),
        max_hops=3,
    )

    # Epoch contains only p1, missing p2
    snap1 = PoolStateSnapshot(p1.key.pool_id, 1 << 96, 0, 10**20, 500, 10, 100, "0x" + "a" * 64)
    epoch = make_epoch([snap1])

    evidence = evaluate_route_exact_input(
        route,
        Amount(weth, 1000, 18),
        epoch,
        token_decimals={asset: 18 for hop in route.hops for asset in (hop.asset_in, hop.asset_out)},
    )

    assert evidence.status == QuoteStatus.UNSUPPORTED
    assert evidence.amount_out is None
    assert evidence.delta_atoms is None
    assert "Missing required pool snapshots" in str(evidence.error)


def test_zero_liquidity_fails_closed() -> None:
    weth = make_asset("weth")
    usdg = make_asset("usdg")
    p1 = make_pool(weth, usdg, "p1")
    p2 = make_pool(weth, usdg, "p2")

    route = RouteRef(
        4663,
        weth,
        (
            HopRef(p1.key, weth, usdg, "zero_for_one", p1),
            HopRef(p2.key, usdg, weth, "one_for_zero", p2),
        ),
        max_hops=3,
    )

    # p1 has 0 liquidity
    snap1 = PoolStateSnapshot(p1.key.pool_id, 1 << 96, 0, 0, 500, 10, 100, "0x" + "a" * 64)
    snap2 = PoolStateSnapshot(p2.key.pool_id, 1 << 96, 0, 10**20, 500, 10, 100, "0x" + "a" * 64)
    epoch = make_epoch([snap1, snap2])

    evidence = evaluate_route_exact_input(
        route,
        Amount(weth, 1000, 18),
        epoch,
        token_decimals={asset: 18 for hop in route.hops for asset in (hop.asset_in, hop.asset_out)},
    )
    assert evidence.status == QuoteStatus.UNSUPPORTED
    assert evidence.amount_out is None
    assert evidence.delta_atoms is None


def test_invalid_input_asset_or_amount() -> None:
    weth = make_asset("weth")
    usdg = make_asset("usdg")
    p1 = make_pool(weth, usdg, "p1")
    p2 = make_pool(weth, usdg, "p2")
    route = RouteRef(
        4663,
        weth,
        (
            HopRef(p1.key, weth, usdg, "zero_for_one", p1),
            HopRef(p2.key, usdg, weth, "one_for_zero", p2),
        ),
        max_hops=3,
    )
    epoch = make_epoch([])

    # Wrong asset (usdg instead of base weth)
    with pytest.raises(ValueError, match="does not match route base asset"):
        evaluate_route_exact_input(
            route,
            Amount(usdg, 1000, 6),
            epoch,
            token_decimals={
                asset: 18 for hop in route.hops for asset in (hop.asset_in, hop.asset_out)
            },
        )

    # Non-positive amount
    with pytest.raises(ValueError, match="must be positive"):
        evaluate_route_exact_input(
            route,
            Amount(weth, 0, 18),
            epoch,
            token_decimals={
                asset: 18 for hop in route.hops for asset in (hop.asset_in, hop.asset_out)
            },
        )
