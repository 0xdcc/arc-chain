"""Arc Market Structure Event Radar Integration Tests (T33)

Verifies:
- Second venue discovery boosts recalculation priority to HIGH
- Frontend alias deduplication (same pool address is not a new venue)
- Bonding curve graduation milestones strictly DO NOT emit buy orders
- Liquidity migrated triggers CRITICAL priority and curve invalidation
- Anti-ticker impersonation: tokens are indexed strictly by contract address
"""

from __future__ import annotations

import pytest

from arbitrage_contracts.arc_extensions import MarketStructureEvent
from arbitrage_contracts.identity import AssetRef, FeeModel, PoolDescriptor, PoolKey, TokenKey
from arbitrage_contracts.quote import HopRef, RouteRef
from arc_research.events.engine import MarketEventRadar
from arc_research.events.rules import (
    EventAction,
    MarketEventType,
    PriorityLevel,
)

CHAIN_ARC = 5042
TOKEN_A = "0x" + "aa" * 20
TOKEN_B = "0x" + "bb" * 20
TOKEN_FAKE_A = "0x" + "cc" * 20  # same ticker, different address!


def make_route(token_in_addr: str, token_out_addr: str, pool_addr1: str, pool_addr2: str) -> RouteRef:
    a_in = AssetRef("erc20", CHAIN_ARC, TokenKey(CHAIN_ARC, token_in_addr))
    a_out = AssetRef("erc20", CHAIN_ARC, TokenKey(CHAIN_ARC, token_out_addr))
    p1 = PoolDescriptor(
        PoolKey(CHAIN_ARC, "v3", "factory", "0x" + "11" * 20, "address", pool_addr1),
        a_in,
        a_out,
        FeeModel.static(500),
        10,
    )
    p2 = PoolDescriptor(
        PoolKey(CHAIN_ARC, "v3", "factory", "0x" + "11" * 20, "address", pool_addr2),
        a_in,
        a_out,
        FeeModel.static(3000),
        60,
    )
    hop = HopRef(p1.key, a_in, a_out, "zero_for_one", p1)
    hop_rev = HopRef(p2.key, a_out, a_in, "one_for_zero", p2)
    return RouteRef(CHAIN_ARC, a_in, (hop, hop_rev))


class TestArcMarketEventRadar:
    """Test suite for T33 Market Event Radar and Rules."""

    def test_second_venue_discovery_promotes_priority(self) -> None:
        route = make_route(TOKEN_A, TOKEN_B, "0x" + "01" * 20, "0x" + "02" * 20)
        radar = MarketEventRadar(candidate_routes=[route])

        # Register existing pool on Venue 1
        radar.register_known_pool(TOKEN_A, TOKEN_B, venue="uniswap_v3", pool_address="0x" + "01" * 20)

        # Event: New pool discovered on Venue 2
        event = MarketStructureEvent(
            event_id="evt-001",
            chain_id=CHAIN_ARC,
            block_number=1234,
            timestamp=1726000000.0,
            event_type=MarketEventType.SECOND_VENUE_CREATED,
            pool_id="0x" + "03" * 20,
            affected_assets=(TOKEN_A, TOKEN_B),
            details=(
                ("venue_name", "sushiswap_v3"),
                ("pool_address", "0x" + "03" * 20),
            ),
        )

        report = radar.process_event(event)
        assert report.priority == PriorityLevel.HIGH
        assert report.action == EventAction.RECALCULATE_PRIORITY
        assert report.affected_routes_count == 1
        assert report.buy_order_emitted is False

    def test_frontend_alias_not_second_venue(self) -> None:
        """Rule: Different frontend pointing to the same underlying pool is an alias, not a second venue."""
        radar = MarketEventRadar()
        radar.register_known_pool(TOKEN_A, TOKEN_B, venue="uniswap_frontend_a", pool_address="0x" + "01" * 20)

        event = MarketStructureEvent(
            event_id="evt-002",
            chain_id=CHAIN_ARC,
            block_number=1235,
            timestamp=1726000005.0,
            event_type=MarketEventType.SECOND_VENUE_CREATED,
            pool_id="0x" + "01" * 20,  # SAME pool address!
            affected_assets=(TOKEN_A, TOKEN_B),
            details=(
                ("venue_name", "dex_frontend_b"),
                ("pool_address", "0x" + "01" * 20),
            ),
        )

        report = radar.process_event(event)
        assert report.priority == PriorityLevel.LOW
        assert report.action == EventAction.OBSERVE_ONLY

    def test_graduation_milestone_strictly_no_buy_order(self) -> None:
        """CRITICAL: High curve progress milestone must NEVER trigger automated buy orders."""
        radar = MarketEventRadar()
        event = MarketStructureEvent(
            event_id="evt-003",
            chain_id=CHAIN_ARC,
            block_number=1236,
            timestamp=1726000010.0,
            event_type=MarketEventType.GRADUATION_MILESTONE,
            pool_id="0x" + "03" * 20,
            affected_assets=(TOKEN_A,),
            details=(
                ("curve_pool_address", "0x" + "03" * 20),
                ("bonding_progress_bps", 9850),  # 98.5% progress!
            ),
        )

        report = radar.process_event(event)
        assert report.action == EventAction.OBSERVE_ONLY
        assert report.buy_order_emitted is False

    def test_liquidity_migrated_critical_action(self) -> None:
        radar = MarketEventRadar()
        event = MarketStructureEvent(
            event_id="evt-004",
            chain_id=CHAIN_ARC,
            block_number=1237,
            timestamp=1726000015.0,
            event_type=MarketEventType.LIQUIDITY_MIGRATED,
            pool_id="0x" + "03" * 20,
            affected_assets=(TOKEN_A,),
            details=(
                ("source_curve_address", "0x" + "03" * 20),
                ("target_amm_address", "0x" + "04" * 20),
            ),
        )

        report = radar.process_event(event)
        assert report.priority == PriorityLevel.CRITICAL
        assert report.action == EventAction.INVALIDATE_OLD_CURVE

    def test_ticker_collision_isolation(self) -> None:
        """Rule: Tokens with identical symbols but different contract addresses must never be linked."""
        route_real = make_route(TOKEN_A, TOKEN_B, "0x" + "01" * 20, "0x" + "02" * 20)
        radar = MarketEventRadar(candidate_routes=[route_real])

        token_fake_b = "0x" + "dd" * 20
        # Event on fake tokens (TOKEN_FAKE_A, TOKEN_FAKE_B)
        event = MarketStructureEvent(
            event_id="evt-005",
            chain_id=CHAIN_ARC,
            block_number=1238,
            timestamp=1726000020.0,
            event_type=MarketEventType.SECOND_VENUE_CREATED,
            pool_id="0x" + "05" * 20,
            affected_assets=(TOKEN_FAKE_A, token_fake_b),
            details=(
                ("venue_name", "other_venue"),
                ("pool_address", "0x" + "05" * 20),
            ),
        )

        report = radar.process_event(event)
        # Real route touches TOKEN_A & TOKEN_B, event is for fake tokens -> affected routes must be 0!
        assert report.affected_routes_count == 0
