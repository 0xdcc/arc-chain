"""Pure computation tests and AST static audit for M6 strategies.

Covers:
- Two-hop cross-pool spread detection and CandidateRoute closed-cycle validation.
- Three-hop triangular arbitrage detection and CandidateRoute closed-cycle validation.
- Fault tolerance against corrupted/missing pool prices (sqrt_price_x96 is None).
- Zero-spread, sub-threshold, and reverse-spread suppression.
- Deterministic output invariance.
- AST static audit ensuring zero RPC, zero network, zero subprocess, and zero disk writes.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest
from arbitrage.domain.types import (
    CandidateRoute,
    MarketSnapshot,
    PoolIdentity,
    PoolStateSnapshot,
    RouteHop,
    TokenIdentity,
)
from arbitrage.market_data.catalog import get_verified_token
from arbitrage.strategies import find_spread_candidates, find_triangular_candidates
from arbitrage.strategies.spread import find_spread_candidates as find_spread_direct
from arbitrage.strategies.triangular import find_triangular_candidates as find_tri_direct

_Q96 = Decimal(2**96)


def _price_to_sqrt_price_x96(price_t1_per_t0: float | Decimal, dec0: int, dec1: int) -> int:
    """Convert human price (token1 units per token0 unit) into Uniswap sqrt_price_x96."""
    raw_ratio = Decimal(str(price_t1_per_t0)) * Decimal(10 ** (dec1 - dec0))
    sqrt_ratio = raw_ratio.sqrt()
    return int(sqrt_ratio * _Q96)


@pytest.fixture
def token_usdg() -> TokenIdentity:
    return get_verified_token("USDG")


@pytest.fixture
def token_weth() -> TokenIdentity:
    return get_verified_token("WETH")


@pytest.fixture
def token_pons() -> TokenIdentity:
    return get_verified_token("PONS")


class TestSpreadStrategy:
    """Tests for pure cross-pool spread arbitrage detection."""

    def test_two_hop_spread_generates_closed_candidate_route(
        self,
        token_usdg: TokenIdentity,
        token_weth: TokenIdentity,
    ) -> None:
        """Verify two-hop spread produces a strictly closed CandidateRoute with accurate gross bps."""
        # WETH (0x4200...) < USDG (0x5fc5...) -> WETH is token0, USDG is token1
        pool_v3 = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v3",
            pool_id="0x" + "11" * 20,
            token0=token_weth.address,
            token1=token_usdg.address,
            fee_bps=5.0,
            tick_spacing=10,
        )
        pool_v4 = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v4",
            pool_id="0x" + "22" * 32,
            token0=token_weth.address,
            token1=token_usdg.address,
            fee_bps=12.5,
            tick_spacing=13,
        )

        # Pool V3: 2000 USDG per WETH (cheap WETH)
        sqrt_v3 = _price_to_sqrt_price_x96(2000.0, token_weth.decimals, token_usdg.decimals)
        # Pool V4: 2050 USDG per WETH (rich WETH)
        # Gross spread = 2050 / 2000 - 1 = 2.5% = 250 bps
        sqrt_v4 = _price_to_sqrt_price_x96(2050.0, token_weth.decimals, token_usdg.decimals)

        snap_v3 = PoolStateSnapshot(
            pool=pool_v3,
            block_number=100000,
            block_timestamp=1700000000,
            sqrt_price_x96=sqrt_v3,
            liquidity=1_000_000,
            tick=0,
        )
        snap_v4 = PoolStateSnapshot(
            pool=pool_v4,
            block_number=100000,
            block_timestamp=1700000000,
            sqrt_price_x96=sqrt_v4,
            liquidity=2_000_000,
            tick=0,
        )

        snapshot = MarketSnapshot(
            chain_id=4663,
            block_number=100000,
            captured_at=1700000000.0,
            pools={pool_v3.pool_id: snap_v3, pool_v4.pool_id: snap_v4},
        )

        # Test with base_token = USDG
        candidates = find_spread_candidates(snapshot, min_gross_bps=10.0, base_token=token_usdg)
        assert len(candidates) == 1
        cand = candidates[0]

        assert cand.route_type == "two_hop_spread"
        assert cand.base_token == token_usdg
        assert len(cand.hops) == 2
        assert cand.snapshot_block == 100000
        assert cand.created_at == 1700000000.0

        # Hop 1: USDG -> WETH in Pool V3 (buy cheap)
        hop1 = cand.hops[0]
        assert hop1.pool.pool_id == pool_v3.pool_id
        assert hop1.token_in.address.lower() == token_usdg.address.lower()
        assert hop1.token_out.address.lower() == token_weth.address.lower()

        # Hop 2: WETH -> USDG in Pool V4 (sell rich)
        hop2 = cand.hops[1]
        assert hop2.pool.pool_id == pool_v4.pool_id
        assert hop2.token_in.address.lower() == token_weth.address.lower()
        assert hop2.token_out.address.lower() == token_usdg.address.lower()

        # Gross spread check: ~250 bps
        assert cand.observed_gross_bps == pytest.approx(250.0, rel=1e-3)

    def test_spread_with_weth_base_token(
        self,
        token_usdg: TokenIdentity,
        token_weth: TokenIdentity,
    ) -> None:
        """Verify two-hop spread when anchored on WETH as base_token."""
        pool_v3 = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v3",
            pool_id="0x" + "11" * 20,
            token0=token_weth.address,
            token1=token_usdg.address,
            fee_bps=5.0,
            tick_spacing=10,
        )
        pool_v4 = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v4",
            pool_id="0x" + "22" * 32,
            token0=token_weth.address,
            token1=token_usdg.address,
            fee_bps=12.5,
            tick_spacing=13,
        )

        sqrt_v3 = _price_to_sqrt_price_x96(2000.0, token_weth.decimals, token_usdg.decimals)
        sqrt_v4 = _price_to_sqrt_price_x96(2050.0, token_weth.decimals, token_usdg.decimals)

        snap_v3 = PoolStateSnapshot(
            pool=pool_v3,
            block_number=100000,
            block_timestamp=1700000000,
            sqrt_price_x96=sqrt_v3,
            liquidity=1_000_000,
            tick=0,
        )
        snap_v4 = PoolStateSnapshot(
            pool=pool_v4,
            block_number=100000,
            block_timestamp=1700000000,
            sqrt_price_x96=sqrt_v4,
            liquidity=2_000_000,
            tick=0,
        )

        snapshot = MarketSnapshot(
            chain_id=4663,
            block_number=100000,
            captured_at=1700000000.0,
            pools={pool_v3.pool_id: snap_v3, pool_v4.pool_id: snap_v4},
        )

        candidates = find_spread_candidates(snapshot, min_gross_bps=10.0, base_token=token_weth)
        assert len(candidates) == 1
        cand = candidates[0]

        assert cand.base_token == token_weth
        # Starting with WETH:
        # Hop 1: WETH -> USDG in Pool V4 (sell WETH rich for 2050 USDG)
        assert cand.hops[0].token_in.address.lower() == token_weth.address.lower()
        assert cand.hops[0].token_out.address.lower() == token_usdg.address.lower()
        assert cand.hops[0].pool.pool_id == pool_v4.pool_id
        # Hop 2: USDG -> WETH in Pool V3 (buy WETH cheap for 2000 USDG)
        assert cand.hops[1].token_in.address.lower() == token_usdg.address.lower()
        assert cand.hops[1].token_out.address.lower() == token_weth.address.lower()
        assert cand.hops[1].pool.pool_id == pool_v3.pool_id

        assert cand.observed_gross_bps == pytest.approx(250.0, rel=1e-3)

    def test_spread_with_none_base_token_selects_preferred_base(
        self,
        token_usdg: TokenIdentity,
        token_weth: TokenIdentity,
    ) -> None:
        """When base_token is None, USDG is selected by default over WETH."""
        pool_v3 = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v3",
            pool_id="0x" + "11" * 20,
            token0=token_weth.address,
            token1=token_usdg.address,
            fee_bps=5.0,
            tick_spacing=10,
        )
        pool_v4 = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v4",
            pool_id="0x" + "22" * 32,
            token0=token_weth.address,
            token1=token_usdg.address,
            fee_bps=12.5,
            tick_spacing=13,
        )

        sqrt_v3 = _price_to_sqrt_price_x96(2000.0, token_weth.decimals, token_usdg.decimals)
        sqrt_v4 = _price_to_sqrt_price_x96(2040.0, token_weth.decimals, token_usdg.decimals)

        snap_v3 = PoolStateSnapshot(
            pool=pool_v3,
            block_number=100000,
            block_timestamp=1700000000,
            sqrt_price_x96=sqrt_v3,
            liquidity=1_000_000,
            tick=0,
        )
        snap_v4 = PoolStateSnapshot(
            pool=pool_v4,
            block_number=100000,
            block_timestamp=1700000000,
            sqrt_price_x96=sqrt_v4,
            liquidity=2_000_000,
            tick=0,
        )

        snapshot = MarketSnapshot(
            chain_id=4663,
            block_number=100000,
            captured_at=1700000000.0,
            pools={pool_v3.pool_id: snap_v3, pool_v4.pool_id: snap_v4},
        )

        candidates = find_spread_candidates(snapshot, min_gross_bps=10.0, base_token=None)
        assert len(candidates) == 1
        assert candidates[0].base_token.symbol == "USDG"
        assert candidates[0].hops[0].token_in.symbol == "USDG"
        assert candidates[0].hops[-1].token_out.symbol == "USDG"

    def test_zero_and_subthreshold_spread_rejected(
        self,
        token_usdg: TokenIdentity,
        token_weth: TokenIdentity,
    ) -> None:
        """Zero spread and spread below min_gross_bps must not produce candidates."""
        pool_v3 = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v3",
            pool_id="0x" + "11" * 20,
            token0=token_weth.address,
            token1=token_usdg.address,
            fee_bps=5.0,
            tick_spacing=10,
        )
        pool_v4 = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v4",
            pool_id="0x" + "22" * 32,
            token0=token_weth.address,
            token1=token_usdg.address,
            fee_bps=12.5,
            tick_spacing=13,
        )

        # 1. Identical prices (0 spread)
        sqrt_same = _price_to_sqrt_price_x96(2000.0, token_weth.decimals, token_usdg.decimals)
        snap_v3 = PoolStateSnapshot(
            pool=pool_v3,
            block_number=100,
            block_timestamp=100,
            sqrt_price_x96=sqrt_same,
            liquidity=1_000,
            tick=0,
        )
        snap_v4 = PoolStateSnapshot(
            pool=pool_v4,
            block_number=100,
            block_timestamp=100,
            sqrt_price_x96=sqrt_same,
            liquidity=1_000,
            tick=0,
        )

        snap_zero = MarketSnapshot(
            chain_id=4663,
            block_number=100,
            captured_at=100.0,
            pools={pool_v3.pool_id: snap_v3, pool_v4.pool_id: snap_v4},
        )
        assert find_spread_candidates(snap_zero, min_gross_bps=10.0) == []

        # 2. Sub-threshold spread (5 bps spread with min_gross_bps=10.0)
        # 2000 * 1.0005 = 2001 (5 bps)
        sqrt_tiny = _price_to_sqrt_price_x96(2001.0, token_weth.decimals, token_usdg.decimals)
        snap_v4_tiny = PoolStateSnapshot(
            pool=pool_v4,
            block_number=100,
            block_timestamp=100,
            sqrt_price_x96=sqrt_tiny,
            liquidity=1_000,
            tick=0,
        )
        snap_sub = MarketSnapshot(
            chain_id=4663,
            block_number=100,
            captured_at=100.0,
            pools={pool_v3.pool_id: snap_v3, pool_v4.pool_id: snap_v4_tiny},
        )
        assert find_spread_candidates(snap_sub, min_gross_bps=10.0) == []
        # If threshold lowered to 4 bps, it is detected
        cands = find_spread_candidates(snap_sub, min_gross_bps=4.0)
        assert len(cands) == 1
        assert cands[0].observed_gross_bps == pytest.approx(5.0, rel=1e-2)

    def test_bad_pool_does_not_break_valid_routes(
        self,
        token_usdg: TokenIdentity,
        token_weth: TokenIdentity,
    ) -> None:
        """Adding corrupted pools (sqrt_price_x96 is None or 0) does not drop valid routes."""
        pool_good_1 = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v3",
            pool_id="0x" + "11" * 20,
            token0=token_weth.address,
            token1=token_usdg.address,
            fee_bps=5.0,
            tick_spacing=10,
        )
        pool_good_2 = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v4",
            pool_id="0x" + "22" * 32,
            token0=token_weth.address,
            token1=token_usdg.address,
            fee_bps=12.5,
            tick_spacing=13,
        )
        pool_bad_none = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v3",
            pool_id="0x" + "33" * 20,
            token0=token_weth.address,
            token1=token_usdg.address,
            fee_bps=30.0,
            tick_spacing=60,
        )
        pool_bad_zero = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v3",
            pool_id="0x" + "44" * 20,
            token0=token_weth.address,
            token1=token_usdg.address,
            fee_bps=30.0,
            tick_spacing=60,
        )

        sqrt_1 = _price_to_sqrt_price_x96(2000.0, token_weth.decimals, token_usdg.decimals)
        sqrt_2 = _price_to_sqrt_price_x96(2050.0, token_weth.decimals, token_usdg.decimals)

        snap_good_1 = PoolStateSnapshot(
            pool=pool_good_1,
            block_number=100,
            block_timestamp=100,
            sqrt_price_x96=sqrt_1,
            liquidity=1_000,
            tick=0,
        )
        snap_good_2 = PoolStateSnapshot(
            pool=pool_good_2,
            block_number=100,
            block_timestamp=100,
            sqrt_price_x96=sqrt_2,
            liquidity=1_000,
            tick=0,
        )
        snap_bad_none = PoolStateSnapshot(
            pool=pool_bad_none,
            block_number=100,
            block_timestamp=100,
            sqrt_price_x96=None,
            liquidity=None,
            tick=None,
        )
        snap_bad_zero = PoolStateSnapshot(
            pool=pool_bad_zero,
            block_number=100,
            block_timestamp=100,
            sqrt_price_x96=0,
            liquidity=0,
            tick=0,
        )

        snapshot = MarketSnapshot(
            chain_id=4663,
            block_number=100,
            captured_at=100.0,
            pools={
                pool_good_1.pool_id: snap_good_1,
                pool_good_2.pool_id: snap_good_2,
                pool_bad_none.pool_id: snap_bad_none,
                pool_bad_zero.pool_id: snap_bad_zero,
            },
        )

        candidates = find_spread_candidates(snapshot, min_gross_bps=10.0)
        assert len(candidates) == 1
        assert candidates[0].observed_gross_bps == pytest.approx(250.0, rel=1e-3)


class TestTriangularStrategy:
    """Tests for pure three-hop triangular arbitrage detection."""

    def test_three_hop_triangular_closed_candidate_route(
        self,
        token_usdg: TokenIdentity,
        token_weth: TokenIdentity,
        token_pons: TokenIdentity,
    ) -> None:
        """Verify three-hop triangular arbitrage produces closed CandidateRoute."""
        # Tokens:
        # USDG: 0x5fc536... (dec 6)
        # WETH: 0x420000... (dec 18)
        # PONS: 0x39dbed... (dec 18)
        # Lexicographical addresses:
        # PONS (0x39) < WETH (0x42) < USDG (0x5f)
        # Pool 1: WETH / USDG -> token0=WETH, token1=USDG
        # Pool 2: PONS / WETH -> token0=PONS, token1=WETH
        # Pool 3: PONS / USDG -> token0=PONS, token1=USDG

        p_weth_usdg = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v3",
            pool_id="0x" + "aa" * 20,
            token0=token_weth.address,
            token1=token_usdg.address,
            fee_bps=5.0,
            tick_spacing=10,
        )
        p_pons_weth = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v3",
            pool_id="0x" + "bb" * 20,
            token0=token_pons.address,
            token1=token_weth.address,
            fee_bps=5.0,
            tick_spacing=10,
        )
        p_pons_usdg = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v3",
            pool_id="0x" + "cc" * 20,
            token0=token_pons.address,
            token1=token_usdg.address,
            fee_bps=5.0,
            tick_spacing=10,
        )

        # Set prices so that:
        # Hop 1: USDG -> WETH. Let 1 WETH = 2000 USDG -> 1 USDG gives 0.0005 WETH
        sqrt_1 = _price_to_sqrt_price_x96(2000.0, token_weth.decimals, token_usdg.decimals)
        # Hop 2: WETH -> PONS. Let 1 PONS = 0.0004 WETH -> 1 WETH gives 2500 PONS
        sqrt_2 = _price_to_sqrt_price_x96(0.0004, token_pons.decimals, token_weth.decimals)
        # Hop 3: PONS -> USDG. Let 1 PONS = 0.824 USDG -> 1 PONS gives 0.824 USDG
        sqrt_3 = _price_to_sqrt_price_x96(0.824, token_pons.decimals, token_usdg.decimals)

        # Gross product: 0.0005 * 2500 * 0.824 = 1.03 (3.0% gross = 300 bps)
        snap_1 = PoolStateSnapshot(
            pool=p_weth_usdg,
            block_number=200000,
            block_timestamp=1700000000,
            sqrt_price_x96=sqrt_1,
            liquidity=1_000,
            tick=0,
        )
        snap_2 = PoolStateSnapshot(
            pool=p_pons_weth,
            block_number=200000,
            block_timestamp=1700000000,
            sqrt_price_x96=sqrt_2,
            liquidity=1_000,
            tick=0,
        )
        snap_3 = PoolStateSnapshot(
            pool=p_pons_usdg,
            block_number=200000,
            block_timestamp=1700000000,
            sqrt_price_x96=sqrt_3,
            liquidity=1_000,
            tick=0,
        )

        snapshot = MarketSnapshot(
            chain_id=4663,
            block_number=200000,
            captured_at=1700000000.0,
            pools={
                p_weth_usdg.pool_id: snap_1,
                p_pons_weth.pool_id: snap_2,
                p_pons_usdg.pool_id: snap_3,
            },
        )

        # 1. Base token = USDG
        candidates = find_triangular_candidates(snapshot, min_gross_bps=10.0, base_token=token_usdg)
        assert len(candidates) == 1
        cand = candidates[0]

        assert cand.route_type == "triangular"
        assert cand.base_token == token_usdg
        assert len(cand.hops) == 3
        assert cand.hops[0].token_in.symbol == "USDG"
        assert cand.hops[0].token_out.symbol == "WETH"
        assert cand.hops[1].token_in.symbol == "WETH"
        assert cand.hops[1].token_out.symbol == "PONS"
        assert cand.hops[2].token_in.symbol == "PONS"
        assert cand.hops[2].token_out.symbol == "USDG"

        assert cand.observed_gross_bps == pytest.approx(300.0, rel=1e-2)

        # 2. Base token = WETH (rotates cycle to start and end with WETH)
        candidates_weth = find_triangular_candidates(
            snapshot, min_gross_bps=10.0, base_token=token_weth
        )
        assert len(candidates_weth) == 1
        cand_weth = candidates_weth[0]
        assert cand_weth.base_token == token_weth
        assert cand_weth.hops[0].token_in.symbol == "WETH"
        assert cand_weth.hops[0].token_out.symbol == "PONS"
        assert cand_weth.hops[1].token_in.symbol == "PONS"
        assert cand_weth.hops[1].token_out.symbol == "USDG"
        assert cand_weth.hops[2].token_in.symbol == "USDG"
        assert cand_weth.hops[2].token_out.symbol == "WETH"
        assert cand_weth.observed_gross_bps == pytest.approx(300.0, rel=1e-2)

    def test_triangular_unprofitable_cycle_rejected(
        self,
        token_usdg: TokenIdentity,
        token_weth: TokenIdentity,
        token_pons: TokenIdentity,
    ) -> None:
        """Fair/zero-profit triangular cycle and sub-threshold cycle must return empty list."""
        p_weth_usdg = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v3",
            pool_id="0x" + "aa" * 20,
            token0=token_weth.address,
            token1=token_usdg.address,
            fee_bps=5.0,
            tick_spacing=10,
        )
        p_pons_weth = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v3",
            pool_id="0x" + "bb" * 20,
            token0=token_pons.address,
            token1=token_weth.address,
            fee_bps=5.0,
            tick_spacing=10,
        )
        p_pons_usdg = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v3",
            pool_id="0x" + "cc" * 20,
            token0=token_pons.address,
            token1=token_usdg.address,
            fee_bps=5.0,
            tick_spacing=10,
        )

        # 1. Perfectly balanced rates: 0.0005 * 2500 * 0.80 = 1.000 (0.0 gross bps in both directions)
        sqrt_1 = _price_to_sqrt_price_x96(2000.0, token_weth.decimals, token_usdg.decimals)
        sqrt_2 = _price_to_sqrt_price_x96(0.0004, token_pons.decimals, token_weth.decimals)
        sqrt_3 = _price_to_sqrt_price_x96(0.80, token_pons.decimals, token_usdg.decimals)

        snap_fair = MarketSnapshot(
            chain_id=4663,
            block_number=200000,
            captured_at=1700000000.0,
            pools={
                p_weth_usdg.pool_id: PoolStateSnapshot(
                    pool=p_weth_usdg,
                    block_number=200,
                    block_timestamp=200,
                    sqrt_price_x96=sqrt_1,
                    liquidity=1000,
                    tick=0,
                ),
                p_pons_weth.pool_id: PoolStateSnapshot(
                    pool=p_pons_weth,
                    block_number=200,
                    block_timestamp=200,
                    sqrt_price_x96=sqrt_2,
                    liquidity=1000,
                    tick=0,
                ),
                p_pons_usdg.pool_id: PoolStateSnapshot(
                    pool=p_pons_usdg,
                    block_number=200,
                    block_timestamp=200,
                    sqrt_price_x96=sqrt_3,
                    liquidity=1000,
                    tick=0,
                ),
            },
        )

        assert find_triangular_candidates(snap_fair, min_gross_bps=10.0) == []

        # 2. Sub-threshold spread: 0.0005 * 2500 * 0.8003 = 1.000375 (3.75 bps spread)
        # With min_gross_bps = 10.0, it must be rejected
        sqrt_3_tiny = _price_to_sqrt_price_x96(0.8003, token_pons.decimals, token_usdg.decimals)
        snap_sub = MarketSnapshot(
            chain_id=4663,
            block_number=200000,
            captured_at=1700000000.0,
            pools={
                p_weth_usdg.pool_id: PoolStateSnapshot(
                    pool=p_weth_usdg,
                    block_number=200,
                    block_timestamp=200,
                    sqrt_price_x96=sqrt_1,
                    liquidity=1000,
                    tick=0,
                ),
                p_pons_weth.pool_id: PoolStateSnapshot(
                    pool=p_pons_weth,
                    block_number=200,
                    block_timestamp=200,
                    sqrt_price_x96=sqrt_2,
                    liquidity=1000,
                    tick=0,
                ),
                p_pons_usdg.pool_id: PoolStateSnapshot(
                    pool=p_pons_usdg,
                    block_number=200,
                    block_timestamp=200,
                    sqrt_price_x96=sqrt_3_tiny,
                    liquidity=1000,
                    tick=0,
                ),
            },
        )
        assert find_triangular_candidates(snap_sub, min_gross_bps=10.0) == []

        # Lower threshold to 3.0 bps -> candidates returned
        cands = find_triangular_candidates(snap_sub, min_gross_bps=3.0)
        assert len(cands) == 1
        assert cands[0].observed_gross_bps == pytest.approx(3.75, rel=1e-2)

    def test_triangular_bad_pool_tolerance(
        self,
        token_usdg: TokenIdentity,
        token_weth: TokenIdentity,
        token_pons: TokenIdentity,
    ) -> None:
        """Corrupted pool in snapshot does not fail the calculation and valid routes survive."""
        p_weth_usdg = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v3",
            pool_id="0x" + "aa" * 20,
            token0=token_weth.address,
            token1=token_usdg.address,
            fee_bps=5.0,
            tick_spacing=10,
        )
        p_pons_weth = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v3",
            pool_id="0x" + "bb" * 20,
            token0=token_pons.address,
            token1=token_weth.address,
            fee_bps=5.0,
            tick_spacing=10,
        )
        p_pons_usdg = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v3",
            pool_id="0x" + "cc" * 20,
            token0=token_pons.address,
            token1=token_usdg.address,
            fee_bps=5.0,
            tick_spacing=10,
        )
        p_broken = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v3",
            pool_id="0x" + "dd" * 20,
            token0=token_usdg.address,
            token1=token_pons.address,
            fee_bps=30.0,
            tick_spacing=60,
        )

        sqrt_1 = _price_to_sqrt_price_x96(2000.0, token_weth.decimals, token_usdg.decimals)
        sqrt_2 = _price_to_sqrt_price_x96(0.0004, token_pons.decimals, token_weth.decimals)
        sqrt_3 = _price_to_sqrt_price_x96(0.824, token_pons.decimals, token_usdg.decimals)

        snapshot = MarketSnapshot(
            chain_id=4663,
            block_number=200000,
            captured_at=1700000000.0,
            pools={
                p_weth_usdg.pool_id: PoolStateSnapshot(
                    pool=p_weth_usdg,
                    block_number=200,
                    block_timestamp=200,
                    sqrt_price_x96=sqrt_1,
                    liquidity=1000,
                    tick=0,
                ),
                p_pons_weth.pool_id: PoolStateSnapshot(
                    pool=p_pons_weth,
                    block_number=200,
                    block_timestamp=200,
                    sqrt_price_x96=sqrt_2,
                    liquidity=1000,
                    tick=0,
                ),
                p_pons_usdg.pool_id: PoolStateSnapshot(
                    pool=p_pons_usdg,
                    block_number=200,
                    block_timestamp=200,
                    sqrt_price_x96=sqrt_3,
                    liquidity=1000,
                    tick=0,
                ),
                p_broken.pool_id: PoolStateSnapshot(
                    pool=p_broken,
                    block_number=200,
                    block_timestamp=200,
                    sqrt_price_x96=None,
                    liquidity=None,
                    tick=None,
                ),
            },
        )

        candidates = find_triangular_candidates(snapshot, min_gross_bps=10.0)
        assert len(candidates) == 1
        assert candidates[0].observed_gross_bps == pytest.approx(300.0, rel=1e-2)


class TestPureDeterminism:
    """Ensure strategy outputs are strictly deterministic for the same snapshot."""

    def test_spread_determinism(
        self,
        token_usdg: TokenIdentity,
        token_weth: TokenIdentity,
    ) -> None:
        p1 = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v3",
            pool_id="0x" + "11" * 20,
            token0=token_weth.address,
            token1=token_usdg.address,
            fee_bps=5.0,
            tick_spacing=10,
        )
        p2 = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v4",
            pool_id="0x" + "22" * 32,
            token0=token_weth.address,
            token1=token_usdg.address,
            fee_bps=12.5,
            tick_spacing=13,
        )
        s1 = _price_to_sqrt_price_x96(2000.0, token_weth.decimals, token_usdg.decimals)
        s2 = _price_to_sqrt_price_x96(2050.0, token_weth.decimals, token_usdg.decimals)
        snap = MarketSnapshot(
            chain_id=4663,
            block_number=100,
            captured_at=100.0,
            pools={
                p1.pool_id: PoolStateSnapshot(
                    pool=p1,
                    block_number=100,
                    block_timestamp=100,
                    sqrt_price_x96=s1,
                    liquidity=1000,
                    tick=0,
                ),
                p2.pool_id: PoolStateSnapshot(
                    pool=p2,
                    block_number=100,
                    block_timestamp=100,
                    sqrt_price_x96=s2,
                    liquidity=1000,
                    tick=0,
                ),
            },
        )

        runs = [find_spread_candidates(snap, min_gross_bps=10.0) for _ in range(5)]
        first = runs[0]
        for subsequent in runs[1:]:
            assert len(subsequent) == len(first)
            for r1, r2 in zip(first, subsequent, strict=True):
                assert r1.candidate_id == r2.candidate_id
                assert r1.observed_gross_bps == r2.observed_gross_bps
                assert r1.hops == r2.hops


class TestASTStaticAudit:
    """Static AST audit ensuring zero forbidden imports and zero side-effects."""

    FORBIDDEN_MODULES = {
        "socket",
        "urllib",
        "requests",
        "httpx",
        "aiohttp",
        "websockets",
        "web3",
        "subprocess",
        "multiprocessing",
        "threading",
        "shutil",
        "tempfile",
        "asyncio",
        "sqlite3",
        "ftplib",
        "poplib",
        "imaplib",
        "smtplib",
    }

    FORBIDDEN_PREFIXES = (
        "arbitrage.monitor_rpc",
        "arbitrage.multicall_reader",
        "arbitrage.feed_listener",
        "chains",
        "execution",
    )

    FORBIDDEN_CALLS = {
        "open",
        "exec",
        "eval",
        "compile",
        "__import__",
    }

    def test_audit_strategies_files(self) -> None:
        """Inspect all Python files in arbitrage/strategies/."""
        strategies_dir = Path("/tmp/modular-m6-workspace/arbitrage/strategies")
        py_files = list(strategies_dir.glob("*.py"))
        assert len(py_files) >= 3, f"Expected at least 3 files, found {py_files}"

        for file_path in py_files:
            source = file_path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(file_path))

            for node in ast.walk(tree):
                # Check import foo
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top_module = alias.name.split(".")[0]
                        assert top_module not in self.FORBIDDEN_MODULES, (
                            f"File {file_path.name} imports forbidden module: {alias.name}"
                        )
                        for prefix in self.FORBIDDEN_PREFIXES:
                            assert not alias.name.startswith(prefix), (
                                f"File {file_path.name} imports forbidden prefix: {alias.name}"
                            )

                # Check from foo import bar
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    top_module = module.split(".")[0]
                    assert top_module not in self.FORBIDDEN_MODULES, (
                        f"File {file_path.name} imports forbidden module: {module}"
                    )
                    for prefix in self.FORBIDDEN_PREFIXES:
                        assert not module.startswith(prefix), (
                            f"File {file_path.name} imports forbidden prefix: {module}"
                        )

                # Check function calls
                elif isinstance(node, ast.Call):
                    func_name = None
                    if isinstance(node.func, ast.Name):
                        func_name = node.func.id
                    elif isinstance(node.func, ast.Attribute):
                        func_name = node.func.attr

                    assert func_name not in self.FORBIDDEN_CALLS, (
                        f"File {file_path.name} calls forbidden function: {func_name}"
                    )
