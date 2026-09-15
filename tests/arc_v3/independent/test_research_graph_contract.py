"""Independent unit tests for offline token graph and triangular path contracts.

Migrated and unified from:
- `tests/test_concurrent_reader.py` (Block Skew 6 test cases)
- `tests/test_triangular_arb.py` (Full mathematical and topological triangular obligations)

Financial Semantics & Contract Obligations:
1. Exact Multiplier Formula:
    R = (1 - f1) * (1 - f2) * (1 - f3) * P1 * P2 * P3
2. Block Skew Hard Circuit Breaker:
    skew = max(block_numbers) - min(block_numbers)
    If skew > 1, the quotes are rejected fail-closed (returns None).
    If skew <= 1, the path is allowed with block_skew and block_number recorded.
3. Zero-Address and Malformed Token Defense:
    Any zero address or invalid hex length is rejected at graph ingestion and path calculation.
4. Canonical Rotation Deduplication:
    Triangles are normalized to minimal rotation (u, v, w) to prevent duplicate alerts.
5. Multi-Pool Best Rate Selection:
    When multiple pools exist for the same pair, the edge with highest effective rate is selected.
6. Rate Spread vs Realized Profit:
    `net_profit_pct` is the fee/slippage-adjusted geometric rate spread before gas.
    Gas fees are unknown at graph screening and NOT defaulted or fabricated.
    Sizing is bounded by 500 USD hard cap.
7. Finite Boundary Defense:
    calculate_triangular_path verifies that all input rates, effective rates, fees, and slippage
    are finite, positive numbers within valid ranges.
"""

from __future__ import annotations

import math

import pytest

from research.graph import (
    ZERO_ADDRESS,
    DirectedEdge,
    SwapLeg,
    TokenGraph,
    TriangularArbAlert,
    bellman_ford_arbitrage,
    calculate_triangular_path,
    find_triangular_opportunities,
    is_invalid_token,
)
from research.market_data.multicall import PoolSpec, PriceQuote, V4PoolSpec


def make_v3_pool(suffix: str, label: str = "test-v3", fee_bps: float = 30.0) -> PoolSpec:
    """Construct a 20-byte V3 pool specification."""
    addr = "0x" + suffix.rjust(40, "0")
    return PoolSpec(
        address=addr,
        label=label,
        fee_bps=fee_bps,
        token0="0x" + "a" * 40,
        token1="0x" + "b" * 40,
        dec0=18,
        dec1=18,
    )


def make_v4_pool(suffix: str, label: str = "test-v4", fee_bps: float = 30.0) -> V4PoolSpec:
    """Construct a 32-byte V4 pool specification."""
    addr = "0x" + suffix.rjust(64, "0")
    return V4PoolSpec(
        address=addr,
        label=label,
        fee_bps=fee_bps,
        token0="0x" + "a" * 40,
        token1="0x" + "b" * 40,
        dec0=18,
        dec1=18,
    )


def make_quote(
    pool: PoolSpec | V4PoolSpec,
    base: str,
    quote: str,
    price: float,
    b_sym: str = "",
    q_sym: str = "",
    block_number: int = 0,
) -> PriceQuote:
    """Construct a spot PriceQuote for graph ingestion."""
    return PriceQuote(
        pool=pool,
        base=base,
        quote=quote,
        price=price,
        raw_price_t1_per_t0=price,
        base_symbol=b_sym or base,
        quote_symbol=q_sym or quote,
        block_number=block_number,
    )


# ==============================================================================
# Suite 1: Block Skew Circuit Breaker (6 Cases from test_concurrent_reader.py)
# ==============================================================================


class TestBlockSkewCircuitBreaker:
    """Block Skew cross-block height circuit breaker verification.

    Hard control invariant: max(block) - min(block) > 1 immediately returns None.
    """

    def _make_directed_edge(
        self,
        from_token: str,
        to_token: str,
        rate: float,
        block_number: int,
        fee_bps: float = 10.0,
    ) -> DirectedEdge:
        pool = make_v3_pool("01", label=f"{from_token}/{to_token}", fee_bps=fee_bps)
        eff = rate * (1.0 - fee_bps / 10000.0)
        return DirectedEdge(
            from_token=from_token,
            to_token=to_token,
            pool=pool,
            rate=rate,
            fee_bps=fee_bps,
            effective_rate=eff,
            weight=0.0,
            from_symbol=from_token,
            to_symbol=to_token,
            block_number=block_number,
        )

    def test_block_skew_greater_than_one_fused(self) -> None:
        """Case 1: When block height delta > 1, calculate_triangular_path fuses to None."""
        # Multiplier: 0.0004 * 2550 * 1.0 = 1.02 (+2% spread)
        e1 = self._make_directed_edge("USDG", "WETH", 0.0004, block_number=100)
        e2 = self._make_directed_edge("WETH", "PONS", 2550.0, block_number=102)
        e3 = self._make_directed_edge("PONS", "USDG", 1.0, block_number=100)

        # skew = 102 - 100 = 2 > 1 -> circuit breaker fuses
        alert = calculate_triangular_path(e1, e2, e3)
        assert alert is None

    def test_block_skew_large_gap_fused(self) -> None:
        """Case 2: Larger block gap (skew = 10) strictly fuses to None."""
        e1 = self._make_directed_edge("USDG", "WETH", 0.0004, block_number=100)
        e2 = self._make_directed_edge("WETH", "PONS", 2550.0, block_number=110)
        e3 = self._make_directed_edge("PONS", "USDG", 1.0, block_number=105)

        alert = calculate_triangular_path(e1, e2, e3)
        assert alert is None

    def test_block_skew_equal_to_one_allowed(self) -> None:
        """Case 3: Block height delta == 1 is permitted with block_skew and block_number recorded."""
        e1 = self._make_directed_edge("USDG", "WETH", 0.000403, block_number=500)
        e2 = self._make_directed_edge("WETH", "PONS", 2550.0, block_number=501)
        e3 = self._make_directed_edge("PONS", "USDG", 1.0, block_number=500)

        alert = calculate_triangular_path(e1, e2, e3, slippage_buffer_pct=0.2)
        assert alert is not None
        assert alert.block_skew == 1
        assert alert.block_number == 501
        assert "Block #501 (skew=1)" in str(alert)

    def test_block_skew_zero_allowed(self) -> None:
        """Case 4: Same block height (skew == 0) is permitted and recorded."""
        e1 = self._make_directed_edge("USDG", "WETH", 0.000403, block_number=600)
        e2 = self._make_directed_edge("WETH", "PONS", 2550.0, block_number=600)
        e3 = self._make_directed_edge("PONS", "USDG", 1.0, block_number=600)

        alert = calculate_triangular_path(e1, e2, e3, slippage_buffer_pct=0.2)
        assert alert is not None
        assert alert.block_skew == 0
        assert alert.block_number == 600
        assert "Block #600 (skew=0)" in str(alert)

    def test_token_graph_propagates_block_number(self) -> None:
        """Case 5: TokenGraph.add_quote propagates block_number to both forward and reverse edges."""
        pool = make_v3_pool("01", label="USDG/WETH")
        quote = PriceQuote(
            pool=pool,
            base="0x" + "a" * 40,
            quote="0x" + "b" * 40,
            price=2500.0,
            raw_price_t1_per_t0=2500.0,
            base_symbol="USDG",
            quote_symbol="WETH",
            block_number=99999,
        )

        graph = TokenGraph()
        graph.add_quote(quote)

        edge_fwd = graph.get_best_edge(quote.base.lower(), quote.quote.lower())
        assert edge_fwd is not None
        assert edge_fwd.block_number == 99999

        edge_rev = graph.get_best_edge(quote.quote.lower(), quote.base.lower())
        assert edge_rev is not None
        assert edge_rev.block_number == 99999

    def test_find_triangular_opportunities_filters_skewed_cycles(self) -> None:
        """Case 6: find_triangular_opportunities automatically filters out skew > 1 cycles."""
        p1 = make_v3_pool("01", label="USDG/WETH", fee_bps=1.0)
        p2 = make_v3_pool("02", label="WETH/PONS", fee_bps=1.0)
        p3 = make_v3_pool("03", label="PONS/USDG", fee_bps=1.0)

        q1 = make_quote(p1, "USDG", "WETH", 0.000403, block_number=100)
        q2 = make_quote(p2, "WETH", "PONS", 2550.0, block_number=105)  # skew = 5 > 1
        q3 = make_quote(p3, "PONS", "USDG", 1.0, block_number=100)

        alerts = find_triangular_opportunities([q1, q2, q3], slippage_buffer_pct=0.2)
        assert len(alerts) == 0


# ==============================================================================
# Suite 2: Exact Multiplier & Profit Formula (from test_triangular_arb.py)
# ==============================================================================


class TestTriangularArbitrageFormula:
    """Exact arithmetic and parameter verification for triangular arbitrage."""

    def _make_edge(
        self,
        from_tok: str,
        to_tok: str,
        rate: float,
        fee_bps: float,
        block_number: int = 100,
    ) -> DirectedEdge:
        pool = make_v3_pool("01", label=f"{from_tok}/{to_tok}", fee_bps=fee_bps)
        eff = rate * (1.0 - fee_bps / 10000.0)
        return DirectedEdge(
            from_token=from_tok,
            to_token=to_tok,
            pool=pool,
            rate=rate,
            fee_bps=fee_bps,
            effective_rate=eff,
            weight=-math.log(eff) if eff > 0 else float("inf"),
            from_symbol=from_tok,
            to_symbol=to_tok,
            block_number=block_number,
        )

    def test_exact_formula_calculation(self) -> None:
        """Case 7: Verify mathematical correctness of gross, fee, and net multipliers."""
        p1, f1_bps = 0.0004, 30.0
        p2, f2_bps = 2500.0, 30.0
        p3, f3_bps = 1.05, 30.0

        e1 = self._make_edge("USDG", "WETH", p1, f1_bps)
        e2 = self._make_edge("WETH", "PONS", p2, f2_bps)
        e3 = self._make_edge("PONS", "USDG", p3, f3_bps)

        alert = calculate_triangular_path(e1, e2, e3, slippage_buffer_pct=0.6)
        assert alert is not None

        expected_gross = p1 * p2 * p3
        expected_fee_mult = (
            (1.0 - f1_bps / 10000.0)
            * (1.0 - f2_bps / 10000.0)
            * (1.0 - f3_bps / 10000.0)
        )
        expected_r = expected_gross * expected_fee_mult

        assert isinstance(alert, TriangularArbAlert)
        assert alert.gross_multiplier == pytest.approx(expected_gross, rel=1e-9)
        assert alert.fee_multiplier == pytest.approx(expected_fee_mult, rel=1e-9)
        assert alert.expected_multiplier == pytest.approx(expected_r, rel=1e-9)

        expected_gross_pct = (expected_gross - 1.0) * 100.0
        expected_fee_pct = (1.0 - expected_fee_mult) * 100.0
        expected_net_pct = (expected_r - 1.0) * 100.0 - 0.6

        assert alert.gross_profit_pct == pytest.approx(expected_gross_pct, rel=1e-9)
        assert alert.total_fee_pct == pytest.approx(expected_fee_pct, rel=1e-9)
        assert alert.net_profit_pct == pytest.approx(expected_net_pct, rel=1e-9)
        assert alert.slippage_buffer_pct == 0.6
        assert alert.cycle == ("USDG", "WETH", "PONS", "USDG")
        assert len(alert.legs) == 3
        assert all(isinstance(leg, SwapLeg) for leg in alert.legs)

    def test_rejects_triangle_eaten_by_fees(self) -> None:
        """Case 8: Profitable gross rate spread eaten by fees produces negative net spread."""
        p1, f1 = 0.0004, 30.0
        p2, f2 = 2500.0, 30.0
        p3, f3 = 1.005, 30.0  # gross = 1.005 (+0.5%), total fee ~0.9%

        e1 = self._make_edge("USDG", "WETH", p1, f1)
        e2 = self._make_edge("WETH", "PONS", p2, f2)
        e3 = self._make_edge("PONS", "USDG", p3, f3)

        alert = calculate_triangular_path(e1, e2, e3, slippage_buffer_pct=0.6)
        assert alert is not None
        assert alert.gross_multiplier > 1.0
        assert alert.expected_multiplier < 1.0
        assert alert.net_profit_pct < 0.0

    def test_rejects_profit_smaller_than_slippage_buffer(self) -> None:
        """Case 9: Profitable after fees but less than slippage buffer yields net_profit_pct < 0."""
        p1, f1 = 0.0004, 5.0
        p2, f2 = 2500.0, 5.0
        p3, f3 = 1.005, 5.0

        e1 = self._make_edge("USDG", "WETH", p1, f1)
        e2 = self._make_edge("WETH", "PONS", p2, f2)
        e3 = self._make_edge("PONS", "USDG", p3, f3)

        alert = calculate_triangular_path(e1, e2, e3, slippage_buffer_pct=0.6)
        assert alert is not None
        assert alert.expected_multiplier > 1.0
        assert alert.net_profit_pct < 0.0

    def test_detects_profitable_triangle_with_v4_mix(self) -> None:
        """Case 10: Mixed V3 and V4 pools yield valid alert with high net spread."""
        v3_p1 = make_v3_pool("01", "USDG/WETH 0.05%", fee_bps=5.0)
        v4_p2 = make_v4_pool("02", "WETH/PONS 0.05%", fee_bps=5.0)
        v3_p3 = make_v3_pool("03", "PONS/USDG 0.05%", fee_bps=5.0)

        e1 = DirectedEdge(
            from_token="USDG",
            to_token="WETH",
            pool=v3_p1,
            rate=0.0004,
            fee_bps=5.0,
            effective_rate=0.0004 * (1.0 - 5.0 / 10000.0),
            weight=0.0,
            from_symbol="USDG",
            to_symbol="WETH",
        )
        e2 = DirectedEdge(
            from_token="WETH",
            to_token="PONS",
            pool=v4_p2,
            rate=2500.0,
            fee_bps=5.0,
            effective_rate=2500.0 * (1.0 - 5.0 / 10000.0),
            weight=0.0,
            from_symbol="WETH",
            to_symbol="PONS",
        )
        e3 = DirectedEdge(
            from_token="PONS",
            to_token="USDG",
            pool=v3_p3,
            rate=1.03,
            fee_bps=5.0,
            effective_rate=1.03 * (1.0 - 5.0 / 10000.0),
            weight=0.0,
            from_symbol="PONS",
            to_symbol="USDG",
        )

        alert = calculate_triangular_path(e1, e2, e3, slippage_buffer_pct=0.6)
        assert alert is not None
        assert alert.net_profit_pct > 2.0


# ==============================================================================
# Suite 3: Bellman-Ford Negative Cycle Arbitrage
# ==============================================================================


class TestBellmanFordArbitrage:
    """Bellman-Ford detection of log-weighted negative cycles."""

    def test_bellman_ford_detects_negative_cycle(self) -> None:
        """Case 11: Bellman-Ford detects triangular arbitrage cycle."""
        p_ab = make_v3_pool("01", "A/B 0.01%", fee_bps=1.0)
        p_bc = make_v3_pool("02", "B/C 0.01%", fee_bps=1.0)
        p_ca = make_v3_pool("03", "C/A 0.01%", fee_bps=1.0)

        # A -> B: 2.0, B -> C: 3.0, C -> A: 0.2 -> 2.0 * 3.0 * 0.2 = 1.2 (+20%)
        quotes = [
            make_quote(p_ab, "A", "B", 2.0),
            make_quote(p_bc, "B", "C", 3.0),
            make_quote(p_ca, "C", "A", 0.2),
        ]
        g = TokenGraph()
        for q in quotes:
            g.add_quote(q)

        alerts = bellman_ford_arbitrage(g, slippage_buffer_pct=0.6)
        assert len(alerts) >= 1
        assert alerts[0].expected_multiplier > 1.0

    def test_bellman_ford_no_cycle_when_fair_market(self) -> None:
        """Case 12: Fair pricing with fees yields no negative cycles."""
        p_ab = make_v3_pool("01", "A/B 0.3%", fee_bps=30.0)
        p_bc = make_v3_pool("02", "B/C 0.3%", fee_bps=30.0)
        p_ca = make_v3_pool("03", "C/A 0.3%", fee_bps=30.0)

        # 2.0 * 3.0 * (1/6) = 1.0 -> minus fees < 1.0
        quotes = [
            make_quote(p_ab, "A", "B", 2.0),
            make_quote(p_bc, "B", "C", 3.0),
            make_quote(p_ca, "C", "A", 1.0 / 6.0),
        ]
        g = TokenGraph()
        for q in quotes:
            g.add_quote(q)

        alerts = bellman_ford_arbitrage(g, slippage_buffer_pct=0.6)
        assert len(alerts) == 0


# ==============================================================================
# Suite 4: Canonical Rotation Deduplication
# ==============================================================================


class TestCanonicalRotation:
    """Canonical rotation deduplication across triangular opportunities."""

    def test_canonical_rotation_deduplication(self) -> None:
        """Case 13: find_triangular_opportunities produces exactly 1 alert per unique cycle."""
        p1 = make_v3_pool("01", "A/B 0.01%", fee_bps=1.0)
        p2 = make_v3_pool("02", "B/C 0.01%", fee_bps=1.0)
        p3 = make_v3_pool("03", "C/A 0.01%", fee_bps=1.0)

        quotes = [
            make_quote(p1, "A", "B", 2.0),
            make_quote(p2, "B", "C", 2.0),
            make_quote(p3, "C", "A", 0.3),  # 2 * 2 * 0.3 = 1.2
        ]
        alerts = find_triangular_opportunities(quotes)
        assert len(alerts) == 1


# ==============================================================================
# Suite 5: Base Token Filtering
# ==============================================================================


class TestBaseTokenFiltering:
    """Filtering opportunities by start base token."""

    def test_base_token_filtering(self) -> None:
        """Case 14: base_token parameter restricts paths to starting at requested token."""
        p1 = make_v3_pool("01", "USDG/WETH 0.01%", fee_bps=1.0)
        p2 = make_v3_pool("02", "WETH/PONS 0.01%", fee_bps=1.0)
        p3 = make_v3_pool("03", "PONS/USDG 0.01%", fee_bps=1.0)

        quotes = [
            make_quote(p1, "USDG", "WETH", 0.0004),
            make_quote(p2, "WETH", "PONS", 2600.0),
            make_quote(p3, "PONS", "USDG", 1.0),
        ]

        alerts_usdg = find_triangular_opportunities(quotes, base_token="USDG")
        assert len(alerts_usdg) >= 1
        for a in alerts_usdg:
            assert a.cycle[0].upper() == "USDG"


# ==============================================================================
# Suite 6: Pool Ingestion & Rate Selection
# ==============================================================================


class TestPoolIngestionAndRateSelection:
    """Edge rate handling, zero price rejection, and best edge selection."""

    def test_zero_or_negative_price_handled_safely(self) -> None:
        """Case 15: Zero and negative prices are safely discarded by add_quote."""
        p1 = make_v3_pool("01", "BAD/ZERO", 30.0)
        g = TokenGraph()

        g.add_quote(make_quote(p1, "A", "B", 0.0))
        assert len(g.nodes) == 0

        g.add_quote(make_quote(p1, "A", "B", -1.5))
        assert len(g.nodes) == 0

    def test_multi_pool_best_rate_selection(self) -> None:
        """Case 16: Multi-pool setup selects edge with highest effective rate."""
        p_hf = make_v3_pool("01", "A/B High Fee 0.3%", fee_bps=30.0)
        p_lf = make_v3_pool("02", "A/B Low Fee 0.01%", fee_bps=1.0)
        p_bc = make_v3_pool("03", "B/C 0.01%", fee_bps=1.0)
        p_ca = make_v3_pool("04", "C/A 0.01%", fee_bps=1.0)

        q_hf = make_quote(p_hf, "A", "B", 10.0)
        q_lf = make_quote(p_lf, "A", "B", 10.0)
        q_bc = make_quote(p_bc, "B", "C", 2.0)
        q_ca = make_quote(p_ca, "C", "A", 0.06)

        alerts = find_triangular_opportunities([q_hf, q_lf, q_bc, q_ca])
        assert len(alerts) >= 1
        first_leg_pool = alerts[0].legs[0].pool
        assert first_leg_pool.fee_bps == 1.0


# ==============================================================================
# Suite 7: Zero-Address, Malformed Token, and Non-Finite Input Defenses
# ==============================================================================


class TestSecurityDefenses:
    """Strict defenses against zero address, invalid hex lengths, and non-finite / non-positive inputs."""

    def test_add_quote_rejects_zero_address(self) -> None:
        """Case 17: add_quote rejects quote containing zero address."""
        g = TokenGraph()
        p = make_v3_pool("01", "ZERO/WETH 0.3%", 30.0)
        q = make_quote(
            p,
            ZERO_ADDRESS,
            "0x0bd7d308f8e1639fab988df18a8011f41eacad73",
            1.0,
        )
        g.add_quote(q)
        assert len(g.nodes) == 0
        assert len(g.adj) == 0

    def test_add_quote_rejects_invalid_hex_length(self) -> None:
        """Case 18: add_quote rejects 0x-prefixed token with abnormal length."""
        assert is_invalid_token("0x12d5ee79") is True
        g = TokenGraph()
        p = make_v3_pool("01", "SHORT/WETH 0.3%", 30.0)
        q = make_quote(p, "0x12d5ee79", "0x0bd7d308f8e1639fab988df18a8011f41eacad73", 1.0)
        g.add_quote(q)
        assert len(g.nodes) == 0

    def test_add_quote_rejects_pool_with_zero_address(self) -> None:
        """Case 19: add_quote rejects quote whose underlying pool token0/currency0 is zero address."""
        g = TokenGraph()
        dirty_v4 = V4PoolSpec(
            address="0x" + "aa" * 32,
            label="AI / WETH 1.004% (uniswap-v4)",
            fee_bps=100.4,
            token0=ZERO_ADDRESS,
            token1="0x2E8c31162b855A2ffa90F6F8634643Ad6F111e18",
        )
        q = make_quote(
            dirty_v4,
            "0x2E8c31162b855A2ffa90F6F8634643Ad6F111e18",
            "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168",
            1.0,
        )
        g.add_quote(q)
        assert len(g.nodes) == 0

    def test_calculate_triangular_path_rejects_zero_address(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Case 20: calculate_triangular_path logs [INVALID_PATH_TOKEN] and returns None on zero address."""
        caplog.set_level("WARNING")
        p1 = make_v3_pool("01", "USDG/WETH 0.01%", 1.0)
        p2 = make_v3_pool("02", "WETH/ZERO 0.3%", 30.0)
        p3 = make_v3_pool("03", "ZERO/USDG 0.3%", 30.0)

        zero_addr = ZERO_ADDRESS
        weth_addr = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
        usdg_addr = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"

        e1 = DirectedEdge(
            from_token=usdg_addr,
            to_token=weth_addr,
            pool=p1,
            rate=0.0004,
            fee_bps=1.0,
            effective_rate=0.0004,
            weight=0.0,
        )
        e2 = DirectedEdge(
            from_token=weth_addr,
            to_token=zero_addr,
            pool=p2,
            rate=2500.0,
            fee_bps=30.0,
            effective_rate=2500.0,
            weight=0.0,
        )
        e3 = DirectedEdge(
            from_token=zero_addr,
            to_token=usdg_addr,
            pool=p3,
            rate=1.05,
            fee_bps=30.0,
            effective_rate=1.05,
            weight=0.0,
        )

        alert = calculate_triangular_path(e1, e2, e3)
        assert alert is None
        assert "[INVALID_PATH_TOKEN]" in caplog.text

    def test_triangular_graph_with_zero_token_generates_no_alert(self) -> None:
        """Case 21: Quotes containing zero address token generate zero alerts."""
        p1 = make_v3_pool("01", "USDG/WETH 0.01%", 1.0)
        p2 = make_v3_pool("02", "WETH/ZERO 0.3%", 30.0)
        p3 = make_v3_pool("03", "ZERO/USDG 0.3%", 30.0)

        zero_addr = ZERO_ADDRESS
        weth_addr = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
        usdg_addr = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"

        q1 = make_quote(p1, usdg_addr, weth_addr, 0.0004)
        q2 = make_quote(p2, weth_addr, zero_addr, 2500.0)
        q3 = make_quote(p3, zero_addr, usdg_addr, 1.05)

        alerts = find_triangular_opportunities([q1, q2, q3])
        assert len(alerts) == 0

    def test_calculate_triangular_path_rejects_non_finite_or_non_positive_rates(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Defensive negative test: calculate_triangular_path rejects NaN, Inf, non-positive rates, and invalid fees/slippage."""
        caplog.set_level("WARNING")
        p1 = make_v3_pool("01", "USDG/WETH 0.01%", 1.0)
        p2 = make_v3_pool("02", "WETH/PONS 0.3%", 30.0)
        p3 = make_v3_pool("03", "PONS/USDG 0.3%", 30.0)

        usdg = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
        weth = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
        pons = "0x39dbed3a2bd333467115de45665cc57f813c4571"

        def _base_edges() -> tuple[DirectedEdge, DirectedEdge, DirectedEdge]:
            return (
                DirectedEdge(
                    from_token=usdg,
                    to_token=weth,
                    pool=p1,
                    rate=0.0004,
                    fee_bps=1.0,
                    effective_rate=0.0004 * (1.0 - 1.0 / 10000.0),
                    weight=0.0,
                    block_number=100,
                ),
                DirectedEdge(
                    from_token=weth,
                    to_token=pons,
                    pool=p2,
                    rate=2550.0,
                    fee_bps=30.0,
                    effective_rate=2550.0 * (1.0 - 30.0 / 10000.0),
                    weight=0.0,
                    block_number=100,
                ),
                DirectedEdge(
                    from_token=pons,
                    to_token=usdg,
                    pool=p3,
                    rate=1.0,
                    fee_bps=30.0,
                    effective_rate=1.0 * (1.0 - 30.0 / 10000.0),
                    weight=0.0,
                    block_number=100,
                ),
            )

        # 1. NaN rate
        e1, e2, e3 = _base_edges()
        e1_bad = DirectedEdge(
            from_token=e1.from_token,
            to_token=e1.to_token,
            pool=e1.pool,
            rate=float("nan"),
            fee_bps=e1.fee_bps,
            effective_rate=e1.effective_rate,
            weight=e1.weight,
            block_number=e1.block_number,
        )
        assert calculate_triangular_path(e1_bad, e2, e3) is None

        # 2. Inf rate
        e1_bad = DirectedEdge(
            from_token=e1.from_token,
            to_token=e1.to_token,
            pool=e1.pool,
            rate=float("inf"),
            fee_bps=e1.fee_bps,
            effective_rate=e1.effective_rate,
            weight=e1.weight,
            block_number=e1.block_number,
        )
        assert calculate_triangular_path(e1_bad, e2, e3) is None

        # 3. Non-positive rate (0.0 and -1.0)
        e1_bad = DirectedEdge(
            from_token=e1.from_token,
            to_token=e1.to_token,
            pool=e1.pool,
            rate=0.0,
            fee_bps=e1.fee_bps,
            effective_rate=e1.effective_rate,
            weight=e1.weight,
            block_number=e1.block_number,
        )
        assert calculate_triangular_path(e1_bad, e2, e3) is None

        e1_bad = DirectedEdge(
            from_token=e1.from_token,
            to_token=e1.to_token,
            pool=e1.pool,
            rate=-1.0,
            fee_bps=e1.fee_bps,
            effective_rate=e1.effective_rate,
            weight=e1.weight,
            block_number=e1.block_number,
        )
        assert calculate_triangular_path(e1_bad, e2, e3) is None

        # 4. NaN / non-finite effective_rate
        e1_bad = DirectedEdge(
            from_token=e1.from_token,
            to_token=e1.to_token,
            pool=e1.pool,
            rate=e1.rate,
            fee_bps=e1.fee_bps,
            effective_rate=float("nan"),
            weight=e1.weight,
            block_number=e1.block_number,
        )
        assert calculate_triangular_path(e1_bad, e2, e3) is None

        # 5. NaN / negative fee_bps
        e1_bad = DirectedEdge(
            from_token=e1.from_token,
            to_token=e1.to_token,
            pool=e1.pool,
            rate=e1.rate,
            fee_bps=float("nan"),
            effective_rate=e1.effective_rate,
            weight=e1.weight,
            block_number=e1.block_number,
        )
        assert calculate_triangular_path(e1_bad, e2, e3) is None

        e1_bad = DirectedEdge(
            from_token=e1.from_token,
            to_token=e1.to_token,
            pool=e1.pool,
            rate=e1.rate,
            fee_bps=-5.0,
            effective_rate=e1.effective_rate,
            weight=e1.weight,
            block_number=e1.block_number,
        )
        assert calculate_triangular_path(e1_bad, e2, e3) is None

        # 6. NaN / negative slippage_buffer_pct
        assert calculate_triangular_path(e1, e2, e3, slippage_buffer_pct=float("nan")) is None
        assert calculate_triangular_path(e1, e2, e3, slippage_buffer_pct=-0.5) is None
