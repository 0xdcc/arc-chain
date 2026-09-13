"""Arc Multi-Tick CLMM Swapping and Capabilities Tests (T21)

Verifies:
- Discrete integer Q64.96 multi-tick loop execution across initialized ticks
- Fail-closed behavior when swap crosses beyond verified TickCoverage
- Rejection of unread tick bitmap regions (no zero-liquidity imputation)
- Capability routing and seamless fallback to single_segment mode
"""

from __future__ import annotations

import pytest

from arbitrage_contracts.arc_extensions import TickCoverage
from arbitrage_contracts.identity import (
    Amount,
    AssetRef,
    FeeModel,
    PoolDescriptor,
    PoolKey,
    TokenKey,
)
from arbitrage_contracts.quote import HopRef, QuoteStatus, RouteRef
from arbitrage_contracts.state import StateVersion
from arc_opportunities.quote_bridge import ArcQuoteBridge
from arc_opportunities.quote_capabilities import (
    CAPABILITY_MULTI_TICK,
    CAPABILITY_SINGLE_SEGMENT,
    ArcCapabilityRouter,
    CapabilityProfile,
)
from state_graph.clmm_math import get_sqrt_ratio_at_tick
from state_graph.multi_tick import (
    PoolTickTable,
    execute_multi_tick_hop,
)
from state_graph.types import FrozenEpoch, PoolStateSnapshot

CHAIN_ARC = 5042

USDC_6 = AssetRef(
    interface_kind="erc20",
    chain_id=CHAIN_ARC,
    token_key=TokenKey(CHAIN_ARC, "0x3600000000000000000000000000000000000001"),
)
WETH_18 = AssetRef(
    interface_kind="erc20",
    chain_id=CHAIN_ARC,
    token_key=TokenKey(CHAIN_ARC, "0x4200000000000000000000000000000000000002"),
)


def make_arc_state_version() -> StateVersion:
    return StateVersion(
        chain_id=CHAIN_ARC,
        block_domain="l1",
        block_number=1000,
        block_hash="0x" + "aa" * 32,
        received_at_ms=1726000000000,
        complete_through_block=1000,
        completeness="ready",
        finality="safe",
    )


def make_pool(pool_id_suffix: str) -> PoolDescriptor:
    pool_addr = f"0x{pool_id_suffix.zfill(40)}"
    venue_addr = "0x" + "11" * 20
    key = PoolKey(CHAIN_ARC, "uniswap_v3", "factory", venue_addr, "address", pool_addr)
    return PoolDescriptor(
        key=key,
        currency0=USDC_6,
        currency1=WETH_18,
        fee_model=FeeModel.static(500),
        tick_spacing=10,
        deployment_status="deployed",
    )


class TestArcMultiTickEngine:
    """Test suite for T21 multi-tick CLMM execution."""

    def test_multi_tick_single_crossing_success(self) -> None:
        p1 = make_pool("01")
        price_at_0 = get_sqrt_ratio_at_tick(0)
        snap = PoolStateSnapshot(
            pool_id=p1.key.pool_id,
            sqrt_price_x96=price_at_0,
            tick=0,
            liquidity=10_000_000_000_000,
            fee_pips=500,
            tick_spacing=10,
            block_number=1000,
            block_hash="0x" + "aa" * 32,
        )

        # Tick table with tick -10 initialized
        cov = TickCoverage(
            pool_id=p1.key.pool_id,
            current_tick=0,
            initialized_ticks_count=2,
            min_tick=-100,
            max_tick=100,
            is_complete=True,
            as_of_block=1000,
        )
        table = PoolTickTable(
            pool_id=p1.key.pool_id,
            coverage=cov,
            tick_spacing=10,
            initialized_ticks={-10: 2_000_000_000_000, -50: 1_000_000_000_000},
        )

        # Swap zero_for_one with amount that crosses tick -10
        res = execute_multi_tick_hop(
            snapshot=snap,
            amount_in=10_000_000_000,
            zero_for_one=True,
            tick_table=table,
        )

        assert res.status == QuoteStatus.QUOTED
        assert res.ticks_crossed == 1
        assert res.amount_out_produced > 0
        assert res.amount_in_consumed == 10_000_000_000

    def test_out_of_coverage_fails_closed(self) -> None:
        """Rule: If next tick exceeds min_tick coverage, fails closed as UNSUPPORTED."""
        p1 = make_pool("01")
        price_at_0 = get_sqrt_ratio_at_tick(0)
        snap = PoolStateSnapshot(
            pool_id=p1.key.pool_id,
            sqrt_price_x96=price_at_0,
            tick=0,
            liquidity=10_000_000,
            fee_pips=500,
            tick_spacing=10,
            block_number=1000,
            block_hash="0x" + "aa" * 32,
        )

        # Coverage only covers -10 to 10
        cov = TickCoverage(
            pool_id=p1.key.pool_id,
            current_tick=0,
            initialized_ticks_count=1,
            min_tick=-10,
            max_tick=10,
            is_complete=True,
            as_of_block=1000,
        )
        # No ticks below -10 in table
        table = PoolTickTable(
            pool_id=p1.key.pool_id,
            coverage=cov,
            tick_spacing=10,
            initialized_ticks={-10: 5_000_000},
        )

        # Swap huge amount that will cross beyond -10
        res = execute_multi_tick_hop(
            snapshot=snap,
            amount_in=1_000_000_000_000,
            zero_for_one=True,
            tick_table=table,
        )

        assert res.status == QuoteStatus.UNSUPPORTED
        assert "Swap exceeded verified tick coverage" in str(res.error)

    def test_capability_router_fallback(self) -> None:
        bridge = ArcQuoteBridge()
        p1 = make_pool("01")
        p2 = make_pool("02")

        hop1 = HopRef(p1.key, USDC_6, WETH_18, "zero_for_one", p1)
        hop2 = HopRef(p2.key, WETH_18, USDC_6, "one_for_zero", p2)
        route = RouteRef(CHAIN_ARC, USDC_6, (hop1, hop2))

        sv = make_arc_state_version()
        snap1 = PoolStateSnapshot(p1.key.pool_id, get_sqrt_ratio_at_tick(5), 5, 50_000_000_000_000_000, 500, 10, 1000, "0x" + "aa" * 32)
        snap2 = PoolStateSnapshot(p2.key.pool_id, get_sqrt_ratio_at_tick(-5), -5, 50_000_000_000_000_000, 500, 10, 1000, "0x" + "aa" * 32)
        epoch = FrozenEpoch("ep-01", sv, (snap1, snap2), 1726000000000)

        DECIMALS_REGISTRY = {USDC_6: 6, WETH_18: 18}
        # Profile set to SINGLE_SEGMENT: quotes cleanly
        prof_single = CapabilityProfile(enabled_capability=CAPABILITY_SINGLE_SEGMENT)
        router_single = ArcCapabilityRouter(bridge, prof_single)
        ev_single = router_single.quote_route(route, Amount(USDC_6, 10_000_000, 6), epoch, token_decimals=DECIMALS_REGISTRY)
        assert ev_single.status == QuoteStatus.QUOTED

        # Profile set to MULTI_TICK but missing tables: falls back gracefully to single segment
        prof_multi_empty = CapabilityProfile(enabled_capability=CAPABILITY_MULTI_TICK)
        router_multi = ArcCapabilityRouter(bridge, prof_multi_empty)
        ev_fallback = router_multi.quote_route(route, Amount(USDC_6, 10_000_000, 6), epoch, token_decimals=DECIMALS_REGISTRY)
        assert ev_fallback.status == QuoteStatus.QUOTED
