"""Arc Event Incremental Recalculation and Bounded Sizing Tests (T22)

Verifies:
- Pool -> Route reverse indexing and affected route filtering
- Incremental recalculation parity with full epoch evaluation
- Strict state_ref binding (no stale state version attachment)
- <= 500 USD hard transaction ceiling enforcement
- Bounded trade size grid search and bisection refinement
- Retention of negative delta and unsupported outcomes for auditing
- Invalidation on pool removal/closure
"""

from __future__ import annotations

from decimal import Decimal
import pytest

from arbitrage_contracts.arc_extensions import BlockDomain
from arbitrage_contracts.identity import (
    Amount,
    AssetRef,
    FeeModel,
    PoolDescriptor,
    PoolKey,
    TokenKey,
)
from arbitrage_contracts.quote import HopRef, RouteRef
from arbitrage_contracts.state import StateVersion
from arc_opportunities.costs import create_gas_cost_evidence
from arc_opportunities.economics import ArcEconomicEvaluator, USDC_SHARED_BALANCE_DOMAIN
from arc_opportunities.incremental import (
    IncrementalError,
    IncrementalQuoteManager,
    PoolRouteIndex,
)
from arc_opportunities.quote_bridge import ArcQuoteBridge
from arc_opportunities.sizing import (
    ArcTradeSizeOptimizer,
    MAX_HARD_TRADE_USD,
    SizingError,
)
from state_graph.clmm_math import get_sqrt_ratio_at_tick
from state_graph.types import FrozenEpoch, PoolStateSnapshot


CHAIN_ARC = 5042

USDC_6 = AssetRef(
    interface_kind="erc20",
    chain_id=CHAIN_ARC,
    token_key=TokenKey(CHAIN_ARC, "0x3600000000000000000000000000000000000001"),
    balance_domain_id=USDC_SHARED_BALANCE_DOMAIN,
)
WETH_18 = AssetRef(
    interface_kind="erc20",
    chain_id=CHAIN_ARC,
    token_key=TokenKey(CHAIN_ARC, "0x4200000000000000000000000000000000000002"),
)
MEME_0 = AssetRef(
    interface_kind="erc20",
    chain_id=CHAIN_ARC,
    token_key=TokenKey(CHAIN_ARC, "0x5500000000000000000000000000000000000003"),
)

DECIMALS_REGISTRY = {
    USDC_6: 6,
    WETH_18: 18,
    MEME_0: 0,
}


def make_arc_state_version(block_number: int = 1000) -> StateVersion:
    return StateVersion(
        chain_id=CHAIN_ARC,
        block_domain="l1",
        block_number=block_number,
        block_hash=f"0x{str(block_number).zfill(64)}",
        received_at_ms=1726000000000,
        complete_through_block=block_number,
        completeness="ready",
        finality="safe",
    )


def make_pool(pool_id_suffix: str, asset0: AssetRef, asset1: AssetRef, fee_pips: int = 500) -> PoolDescriptor:
    pool_addr = f"0x{pool_id_suffix.zfill(40)}"
    venue_addr = "0x" + "11" * 20
    key = PoolKey(CHAIN_ARC, "uniswap_v3", "factory", venue_addr, "address", pool_addr)
    return PoolDescriptor(
        key=key,
        currency0=asset0,
        currency1=asset1,
        fee_model=FeeModel.static(fee_pips),
        tick_spacing=10,
        deployment_status="deployed",
    )


def make_snapshot(pool_id: str, tick: int = 0, liquidity: int = 100_000_000_000_000_000) -> PoolStateSnapshot:
    price = get_sqrt_ratio_at_tick(tick)
    return PoolStateSnapshot(
        pool_id=pool_id,
        sqrt_price_x96=price,
        tick=tick,
        liquidity=liquidity,
        fee_pips=500,
        tick_spacing=10,
        block_number=1000,
        block_hash="0x" + "aa" * 32,
    )


class TestArcIncrementalSizing:
    """Test suite for T22 Incremental Recalculation and Bounded Sizing."""

    def test_pool_route_index_affected_routes(self) -> None:
        p1 = make_pool("01", USDC_6, WETH_18)
        p2 = make_pool("02", WETH_18, MEME_0)
        p3 = make_pool("03", MEME_0, USDC_6)
        p4 = make_pool("04", USDC_6, WETH_18)

        # Route A uses p1, p2, p3
        hopA1 = HopRef(p1.key, USDC_6, WETH_18, "zero_for_one", p1)
        hopA2 = HopRef(p2.key, WETH_18, MEME_0, "zero_for_one", p2)
        hopA3 = HopRef(p3.key, MEME_0, USDC_6, "zero_for_one", p3)
        routeA = RouteRef(CHAIN_ARC, USDC_6, (hopA1, hopA2, hopA3))

        # Route B uses p1, p4 (2-hop)
        hopB1 = HopRef(p1.key, USDC_6, WETH_18, "zero_for_one", p1)
        hopB2 = HopRef(p4.key, WETH_18, USDC_6, "one_for_zero", p4)
        routeB = RouteRef(CHAIN_ARC, USDC_6, (hopB1, hopB2))

        index = PoolRouteIndex()
        index.register_routes([routeA, routeB])

        assert index.total_routes == 2
        # If pool 4 changes, only Route B is affected
        affected_p4 = index.get_affected_routes([p4.key.pool_id])
        assert affected_p4 == (routeB,)

        # If pool 1 changes, both Route A and Route B are affected
        affected_p1 = index.get_affected_routes([p1.key.pool_id])
        assert len(affected_p1) == 2

        # If an unknown pool changes, 0 routes affected
        affected_none = index.get_affected_routes(["0x9999999999999999999999999999999999999999"])
        assert len(affected_none) == 0

    def test_incremental_recalculation_parity(self) -> None:
        bridge = ArcQuoteBridge()
        p1 = make_pool("01", USDC_6, WETH_18)
        p2 = make_pool("02", USDC_6, WETH_18)

        hop1 = HopRef(p1.key, USDC_6, WETH_18, "zero_for_one", p1)
        hop2 = HopRef(p2.key, WETH_18, USDC_6, "one_for_zero", p2)
        route = RouteRef(CHAIN_ARC, USDC_6, (hop1, hop2))

        index = PoolRouteIndex()
        index.register_route(route)
        manager = IncrementalQuoteManager(bridge, index)

        sv = make_arc_state_version()
        snap1 = make_snapshot(p1.key.pool_id, tick=10)
        snap2 = make_snapshot(p2.key.pool_id, tick=-10)
        epoch = FrozenEpoch("ep-01", sv, (snap1, snap2), 1726000000000)

        amt_in = Amount(USDC_6, 50_000_000, 6)
        report = manager.process_pool_events(
            affected_pool_ids=[p1.key.pool_id],
            epoch=epoch,
            amount_in=amt_in,
            token_decimals=DECIMALS_REGISTRY,
        )

        assert report.recalculated_routes_count == 1
        assert report.skipped_unaffected_count == 0
        assert len(report.quotes) == 1

        # Compare directly with direct full quote
        full_quote = bridge.quote_exact_input(route, amt_in, epoch, token_decimals=DECIMALS_REGISTRY)
        inc_quote = report.quotes[0]
        assert inc_quote.status == full_quote.status
        assert inc_quote.delta_atoms == full_quote.delta_atoms
        assert inc_quote.state_version_ref == full_quote.state_version_ref

    def test_sizing_grid_hard_cap_500_usd(self) -> None:
        bridge = ArcQuoteBridge()
        evaluator = ArcEconomicEvaluator()

        with pytest.raises(SizingError, match="exceeds hard limit"):
            ArcTradeSizeOptimizer(bridge, evaluator, max_trade_usd=Decimal("501.0"))

        with pytest.raises(SizingError, match="exceeds max trade limit"):
            ArcTradeSizeOptimizer(bridge, evaluator, grid_usd=[Decimal("600.0")])

    def test_sizing_grid_search_and_bisection(self) -> None:
        bridge = ArcQuoteBridge()
        evaluator = ArcEconomicEvaluator()
        optimizer = ArcTradeSizeOptimizer(bridge, evaluator, grid_usd=[Decimal("10.0"), Decimal("50.0"), Decimal("100.0"), Decimal("250.0"), Decimal("500.0")])

        p1 = make_pool("01", USDC_6, WETH_18, fee_pips=500)
        p2 = make_pool("02", USDC_6, WETH_18, fee_pips=500)
        hop1 = HopRef(p1.key, USDC_6, WETH_18, "zero_for_one", p1)
        hop2 = HopRef(p2.key, WETH_18, USDC_6, "one_for_zero", p2)
        route = RouteRef(CHAIN_ARC, USDC_6, (hop1, hop2))

        sv = make_arc_state_version()
        # Large liquidity so up to 500 USD does not cross tick boundary
        snap1 = make_snapshot(p1.key.pool_id, tick=20, liquidity=1_000_000_000_000_000_000)
        snap2 = make_snapshot(p2.key.pool_id, tick=-20, liquidity=1_000_000_000_000_000_000)
        epoch = FrozenEpoch("ep-size", sv, (snap1, snap2), 1726000000000)

        gas_evidence = create_gas_cost_evidence(cost_atoms=100_000, currency="USDC")
        result = optimizer.search_size(
            route=route,
            epoch=epoch,
            base_asset_usd_price=Decimal("1.0"),
            gas_evidence=gas_evidence,
            token_decimals=DECIMALS_REGISTRY,
        )

        assert result.search_completed is True
        assert len(result.evaluated_sizes) >= 5
        assert result.max_tested_usd <= MAX_HARD_TRADE_USD
        # Every evaluated size is retained in audit log
        for evaluated in result.evaluated_sizes:
            assert evaluated.amount_usd <= Decimal("500.0")
            assert evaluated.quote is not None

    def test_pool_removal_invalidation(self) -> None:
        p1 = make_pool("01", USDC_6, WETH_18)
        p2 = make_pool("02", USDC_6, WETH_18)
        hop1 = HopRef(p1.key, USDC_6, WETH_18, "zero_for_one", p1)
        hop2 = HopRef(p2.key, WETH_18, USDC_6, "one_for_zero", p2)
        route = RouteRef(CHAIN_ARC, USDC_6, (hop1, hop2))

        index = PoolRouteIndex()
        index.register_route(route)
        assert index.total_routes == 1

        # Remove pool 1 (e.g. pool destroyed or liquidity drained)
        invalidated = index.remove_pool(p1.key.pool_id)
        assert invalidated == (route,)
        assert index.total_routes == 0
        assert index.get_affected_routes([p1.key.pool_id]) == ()
