"""Arc OTC Wall Normalization, Pricing, and Settlement Tests (T32)

Verifies:
- Price inversion: buying Arc USDC takes asks with added fees; selling hits bids with deducted fees
- Depth limits: partial fill correctly detected when available quantity is insufficient
- Currency peg costs: USDG <-> USDC conversion fee correctly incorporated
- Stress testing: zero-premium collapse scenario flags unviable trades
- Cross-market delivery: concurrent dispatch without bilateral settlement CANNOT claim realized profit
"""

from __future__ import annotations

from decimal import Decimal
import pytest

from arbitrage_contracts.arc_extensions import OtcQuote
from arc_research.wall.normalization import (
    NormalizationError,
    OtcWallNormalizer,
)
from arc_research.wall.premium import (
    PremiumAnalysisError,
    WallPremiumAnalyzer,
)
from arc_research.wall.sources import OtcOrderBook


def make_mock_orderbook() -> OtcOrderBook:
    # 2 bids: Venue buys ArcUSDC at 0.998 and 0.995 USD
    b1 = OtcQuote(
        quote_id="b1",
        venue="circle",
        base_asset="USDC",
        quote_asset="USD",
        side="buy",
        amount_in_atoms=100_000_000,  # 100 USDC
        amount_out_atoms=99_800_000,  # 99.8 USD (0.998 USD/USDC)
        expiry_timestamp=2000.0,
    )
    b2 = OtcQuote(
        quote_id="b2",
        venue="circle",
        base_asset="USDC",
        quote_asset="USD",
        side="buy",
        amount_in_atoms=50_000_000,  # 50 USDC
        amount_out_atoms=49_750_000,  # 49.75 USD (0.995 USD/USDC)
        expiry_timestamp=2000.0,
    )
    # 2 asks: Venue sells ArcUSDC at 1.002 and 1.005 USD
    a1 = OtcQuote(
        quote_id="a1",
        venue="circle",
        base_asset="USDC",
        quote_asset="USD",
        side="sell",
        amount_in_atoms=80_000_000,  # 80 USDC
        amount_out_atoms=80_160_000,  # 80.16 USD (1.002 USD/USDC)
        expiry_timestamp=2000.0,
    )
    a2 = OtcQuote(
        quote_id="a2",
        venue="circle",
        base_asset="USDC",
        quote_asset="USD",
        side="sell",
        amount_in_atoms=100_000_000,  # 100 USDC
        amount_out_atoms=100_500_000,  # 100.5 USD (1.005 USD/USDC)
        expiry_timestamp=2000.0,
    )
    return OtcOrderBook(
        venue="circle",
        endpoint_url="https://api.circle.com/otc",
        collected_at_utc=1000.0,
        raw_payload_hash="0x" + "aa" * 32,
        bids=(b1, b2),
        asks=(a1, a2),
    )


class TestArcWallPricing:
    """Test suite for T32 Wall Pricing, Normalization, and Premium Scenarios."""

    def test_buy_side_aggregation_and_fees(self) -> None:
        """Trader buys 50 ArcUSDC: should fill from cheapest ask a1 (1.002 USD/USDC) + fees."""
        ob = make_mock_orderbook()
        quote = OtcWallNormalizer.aggregate_execution(
            orderbook=ob,
            side="buy_arc_usdc",
            target_base_atoms=50_000_000,
            taker_fee_bps=10,  # 10 bps
            peg_conversion_bps=5,  # 5 bps
            now_utc=1000.0,
        )
        assert quote.is_fully_filled is True
        assert quote.filled_base_atoms == 50_000_000
        # Raw cost: 50 * 1.002 = 50.10 USD (50_100_000 atoms)
        # Fee (10 bps): 50_100 atoms
        # Peg fee (5 bps): 25_050 atoms
        # Total cost: 50_100_000 + 50_100 + 25_050 = 50_175_150 atoms
        assert quote.total_quote_atoms == 50_100_000 + 50_100 + 25_050
        assert quote.all_in_fee_atoms == 50_100
        assert quote.peg_conversion_fee_atoms == 25_050

    def test_sell_side_aggregation_and_price_inversion(self) -> None:
        """Trader sells 120 ArcUSDC: hits b1 (100 @ 0.998) and b2 (20 @ 0.995) - fees."""
        ob = make_mock_orderbook()
        quote = OtcWallNormalizer.aggregate_execution(
            orderbook=ob,
            side="sell_arc_usdc",
            target_base_atoms=120_000_000,
            taker_fee_bps=10,  # 10 bps
            now_utc=1000.0,
        )
        assert quote.is_fully_filled is True
        assert quote.filled_base_atoms == 120_000_000
        assert len(quote.tiers_used) == 2
        # Tier 1: 100 USDC * 0.998 = 99_800_000 atoms, fee 99_800 -> 99_700_200 net
        # Tier 2: 20 USDC * 0.995 = 19_900_000 atoms, fee 19_900 -> 19_880_100 net
        expected_total = (99_800_000 - 99_800) + (19_900_000 - 19_900)
        assert quote.total_quote_atoms == expected_total

    def test_partial_fill_insufficient_depth(self) -> None:
        """Requesting 200 ArcUSDC sell when total bid depth is 150 ArcUSDC -> is_fully_filled is False."""
        ob = make_mock_orderbook()
        quote = OtcWallNormalizer.aggregate_execution(
            orderbook=ob,
            side="sell_arc_usdc",
            target_base_atoms=200_000_000,
            now_utc=1000.0,
        )
        assert quote.is_fully_filled is False
        assert quote.filled_base_atoms == 150_000_000

    def test_zero_premium_stress_scenario(self) -> None:
        """Stress test: check whether on-chain opportunity survives if premium drops to 1.0000."""
        # On-chain receives 105 USDC on 100 USDC input. OTC cost was 99.8 USDC, gas 0.1 USDC (100_000 atoms)
        res = WallPremiumAnalyzer.stress_test_zero_premium(
            onchain_gross_out_atoms=105_000_000,
            amount_in_atoms=100_000_000,
            otc_all_in_cost_atoms=99_800_000,
            gas_cost_atoms=100_000,
        )
        # Under zero premium (on-chain pays 100M):
        # 100_000_000 - 99_800_000 - 100_000 = +100_000 (> 0 -> survives!)
        assert res.survives_zero_premium is True
        assert res.zero_premium_net_atoms == 100_000

        # Now test an opportunity where OTC cost is 100.2 USDC (costs premium to acquire)
        res_fail = WallPremiumAnalyzer.stress_test_zero_premium(
            onchain_gross_out_atoms=105_000_000,
            amount_in_atoms=100_000_000,
            otc_all_in_cost_atoms=100_200_000,
            gas_cost_atoms=100_000,
        )
        # 100M - 100.2M - 0.1M = -300_000 (< 0 -> collapses without premium)
        assert res_fail.survives_zero_premium is False
        assert res_fail.zero_premium_net_atoms == -300_000

    def test_unsettled_delivery_cannot_claim_realized_profit(self) -> None:
        """CRITICAL: Dispatching concurrent orders does not equal locked profit if delivery is pending."""
        rep = WallPremiumAnalyzer.verify_settlement_status(
            pair_id="arc_usdc_otc_01",
            dex_trade_settled=True,
            otc_delivery_settled=False,  # OTC delivery still in flight!
            claimed_profit_atoms=5_000_000,
            amount_in_atoms=100_000_000,
        )
        assert rep.is_realized_profit is False
        assert rep.realized_net_atoms is None
        assert "DELIVERY_PENDING" in rep.audit_verdict

        # Both settled -> Certified
        rep_settled = WallPremiumAnalyzer.verify_settlement_status(
            pair_id="arc_usdc_otc_01",
            dex_trade_settled=True,
            otc_delivery_settled=True,
            claimed_profit_atoms=5_000_000,
            amount_in_atoms=100_000_000,
        )
        assert rep_settled.is_realized_profit is True
        assert rep_settled.realized_net_atoms == 5_000_000
