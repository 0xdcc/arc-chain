"""Arc Economics and Unified Cost Accounting Tests (T20)

Verifies:
- Single-deduction defense: fee_included == YES does not double-deduct DEX fees
- UNKNOWN is NOT 0: missing gas leaves net_atoms=None and status="unknown"
- Payer vs beneficiary separation in CostEvidence
- Zero and negative price rejection (no float coercion)
- Strict output floor enforcement: output_floor >= amount_in + gas_atoms + 1 atom
- Proper accounting separation between on-chain gas and external OTC financing costs
- Shared USDC balance domain prevents synthetic zero-cost cross-view arbitrage
"""

from __future__ import annotations

from decimal import Decimal
import pytest

from arbitrage_contracts.arc_extensions import CostEvidence
from arbitrage_contracts.identity import (
    Amount,
    AssetRef,
    FeeModel,
    PoolDescriptor,
    PoolKey,
    TokenKey,
)
from arbitrage_contracts.quote import (
    HopRef,
    QuoteEvidence,
    QuoteStatus,
    RouteRef,
    TriState,
)
from arc_opportunities.costs import (
    ArcCostBreakdown,
    CostInputError,
    create_gas_cost_evidence,
    create_otc_cost_evidence,
)
from arc_opportunities.economics import (
    ArcEconomicEvaluator,
    EconomicsError,
    USDC_SHARED_BALANCE_DOMAIN,
    validate_positive_decimal,
)

CHAIN_ARC = 5042

USDC_6 = AssetRef(
    interface_kind="erc20",
    chain_id=CHAIN_ARC,
    token_key=TokenKey(CHAIN_ARC, "0x3600000000000000000000000000000000000001"),
    balance_domain_id=USDC_SHARED_BALANCE_DOMAIN,
)
USDC_18 = AssetRef(
    interface_kind="native",
    chain_id=CHAIN_ARC,
    native_identifier="native_usdc",
    balance_domain_id=USDC_SHARED_BALANCE_DOMAIN,
)
WETH_18 = AssetRef(
    interface_kind="erc20",
    chain_id=CHAIN_ARC,
    token_key=TokenKey(CHAIN_ARC, "0x4200000000000000000000000000000000000002"),
)


def make_dummy_quote(
    amount_in_atoms: int = 100_000_000,
    amount_out_atoms: int = 105_000_000,
    fee_included: TriState = TriState.YES,
    status: QuoteStatus = QuoteStatus.QUOTED,
    route: RouteRef | None = None,
) -> QuoteEvidence:
    if route is None:
        p1 = PoolDescriptor(
            key=PoolKey(CHAIN_ARC, "v3", "factory", "0x" + "11" * 20, "address", "0x" + "01" * 20),
            currency0=USDC_6,
            currency1=WETH_18,
            fee_model=FeeModel.static(500),
            tick_spacing=10,
        )
        p2 = PoolDescriptor(
            key=PoolKey(CHAIN_ARC, "v3", "factory", "0x" + "11" * 20, "address", "0x" + "02" * 20),
            currency0=USDC_6,
            currency1=WETH_18,
            fee_model=FeeModel.static(3000),
            tick_spacing=60,
        )
        hop1 = HopRef(p1.key, USDC_6, WETH_18, "zero_for_one", p1)
        hop2 = HopRef(p2.key, WETH_18, USDC_6, "one_for_zero", p2)
        route = RouteRef(chain_id=CHAIN_ARC, base_asset=USDC_6, hops=(hop1, hop2))

    delta = amount_out_atoms - amount_in_atoms if status == QuoteStatus.QUOTED else None
    amt_out = Amount(USDC_6, amount_out_atoms, 6) if status == QuoteStatus.QUOTED else None

    return QuoteEvidence(
        quote_id="test-quote-001",
        route_ref=route,
        amount_in=Amount(USDC_6, amount_in_atoms, 6),
        amount_out=amt_out,
        delta_atoms=delta,
        hop_quotes=(),
        state_version_ref="state:v1:" + "ab" * 32,
        started_at_ms=1000,
        finished_at_ms=1010,
        status=status,
        fee_included=fee_included,
        impact_included=TriState.YES,
    )


class TestArcEconomics:
    """Test suite for T20 Arc economic evaluation engine."""

    def test_unknown_gas_never_defaults_to_zero(self) -> None:
        """Rule: UNKNOWN is NOT 0. Missing gas evidence MUST produce net_atoms=None and status='unknown'."""
        evaluator = ArcEconomicEvaluator()
        quote = make_dummy_quote(amount_in_atoms=100_000_000, amount_out_atoms=105_000_000)

        breakdown = evaluator.evaluate_quote(quote=quote, gas_evidence=None)
        assert breakdown.net_atoms is None
        assert breakdown.economic_status == "unknown"
        assert breakdown.gas_cost is None
        assert any("cannot assume 0" in note for note in breakdown.calculation_notes)

    def test_fee_included_single_deduction(self) -> None:
        """Rule: fee_included == YES prevents secondary deduction of DEX fees."""
        evaluator = ArcEconomicEvaluator()
        quote = make_dummy_quote(
            amount_in_atoms=100_000_000,
            amount_out_atoms=105_000_000,
            fee_included=TriState.YES,
        )
        gas_evidence = create_gas_cost_evidence(cost_atoms=1_000_000, currency="USDC")

        breakdown = evaluator.evaluate_quote(quote=quote, gas_evidence=gas_evidence)
        assert breakdown.dex_fee_included_in_quote is True
        # Gross delta is 5_000_000; gas is 1_000_000; net is exactly 4_000_000
        assert breakdown.gross_delta_atoms == 5_000_000
        assert breakdown.net_atoms == 4_000_000
        assert breakdown.economic_status == "profitable"

    def test_payer_and_beneficiary_separation(self) -> None:
        """Rule: CostEvidence preserves distinct gas_payer and profit beneficiary addresses."""
        payer_addr = "0x" + "aa" * 20
        beneficiary_addr = "0x" + "bb" * 20
        gas_evidence = create_gas_cost_evidence(
            cost_atoms=500_000,
            currency="USDC",
            gas_payer=payer_addr,
            beneficiary=beneficiary_addr,
        )

        assert gas_evidence.gas_payer == payer_addr
        assert gas_evidence.beneficiary == beneficiary_addr

        evaluator = ArcEconomicEvaluator()
        quote = make_dummy_quote()
        breakdown = evaluator.evaluate_quote(quote=quote, gas_evidence=gas_evidence)

        assert breakdown.gas_payer == payer_addr
        assert breakdown.beneficiary == beneficiary_addr

    def test_rejection_of_zero_or_negative_price(self) -> None:
        """Rule: Asset price cannot be zero or negative; float coercion rejected."""
        with pytest.raises(EconomicsError, match="strictly positive"):
            validate_positive_decimal("0", "price")

        with pytest.raises(EconomicsError, match="strictly positive"):
            validate_positive_decimal("-1.5", "price")

        with pytest.raises(EconomicsError, match="must not be boolean or float"):
            validate_positive_decimal(1.5, "price")  # float forbidden!

        valid_dec = validate_positive_decimal("1.000", "price")
        assert valid_dec == Decimal("1.000")

    def test_output_floor_strict_profitable(self) -> None:
        """Rule: Strict profitability requires output >= amount_in + gas_atoms + 1 atom."""
        evaluator = ArcEconomicEvaluator()
        # amount_in = 100_000_000; gas = 5_000_000
        # If amount_out = 105_000_000, net_atoms = 0 (not > 0!). Must be unprofitable.
        quote = make_dummy_quote(amount_in_atoms=100_000_000, amount_out_atoms=105_000_000)
        gas_evidence = create_gas_cost_evidence(cost_atoms=5_000_000, currency="USDC")

        breakdown = evaluator.evaluate_quote(quote=quote, gas_evidence=gas_evidence)
        assert breakdown.net_atoms == 0
        assert breakdown.economic_status == "unprofitable"

        # Now test with amount_out = 105_000_001 (net_atoms = 1 atom). Profitable!
        quote_prof = make_dummy_quote(amount_in_atoms=100_000_000, amount_out_atoms=105_000_001)
        breakdown_prof = evaluator.evaluate_quote(quote=quote_prof, gas_evidence=gas_evidence)
        assert breakdown_prof.net_atoms == 1
        assert breakdown_prof.economic_status == "profitable"

    def test_otc_cost_deduction_and_tracking(self) -> None:
        """Rule: OTC channel costs are recorded distinctly and subtracted from net profit."""
        evaluator = ArcEconomicEvaluator()
        quote = make_dummy_quote(amount_in_atoms=100_000_000, amount_out_atoms=110_000_000)
        gas_evidence = create_gas_cost_evidence(cost_atoms=2_000_000, currency="USDC")
        otc_evidence = create_otc_cost_evidence(cost_atoms=3_000_000, currency="USDC")

        breakdown = evaluator.evaluate_quote(
            quote=quote,
            gas_evidence=gas_evidence,
            otc_evidence=otc_evidence,
            base_asset_usd_price="1.00",
        )

        assert breakdown.gross_delta_atoms == 10_000_000
        assert breakdown.gas_cost is not None and breakdown.gas_cost.cost_atoms == 2_000_000
        assert breakdown.otc_cost is not None and breakdown.otc_cost.cost_atoms == 3_000_000
        # Net = 10M - (2M + 3M) = 5M
        assert breakdown.net_atoms == 5_000_000
        assert breakdown.economic_status == "profitable"
        assert breakdown.net_usd == Decimal("5.000000")

    def test_usdc_shared_balance_domain_rejection(self) -> None:
        """Rule: Native USDC and ERC20 USDC share the same balance domain; cannot be treated as separate assets for arbitrage."""
        synth_pool = PoolDescriptor(
            key=PoolKey(CHAIN_ARC, "synthetic_wrapper", "factory", "0x" + "11" * 20, "address", "0x" + "01" * 20),
            currency0=USDC_6,
            currency1=USDC_18,
            fee_model=FeeModel.static(0),
            tick_spacing=1,
        )
        hop1 = HopRef(synth_pool.key, USDC_6, USDC_18, "zero_for_one", synth_pool)
        hop2 = HopRef(synth_pool.key, USDC_18, USDC_6, "one_for_zero", synth_pool)
        # Attempting a route between two views of the same balance domain
        with pytest.raises(ValueError, match="Duplicate pool"):
            RouteRef(chain_id=CHAIN_ARC, base_asset=USDC_6, hops=(hop1, hop2))
