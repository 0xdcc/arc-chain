"""Regression tests for the W2-C pure economics evaluator."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arbitrage_contracts.identity import Amount, AssetRef, TokenKey
from arbitrage_contracts.quote import (
    ActorScope,
    DataMode,
    EconomicAssessment,
    EvidenceLevel,
    FeeComponent,
    GasEvidence,
    GasEvidenceKind,
    HopRef,
    PoolKey,
    PriceEvidence,
    QuoteEvidence,
    RouteRef,
    TriState,
)
from opportunities.economics import (
    BASE_TO_QUOTE,
    AssetConversionInput,
    EconomicsInputError,
    evaluate_quote_evidence,
)

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "opportunities"
    / "v1"
    / "economic-cases.jsonl"
)
UINT256_MAX = (1 << 256) - 1
CHAIN_ID = 4663
NOW_MS = 2000


def _token(address: str, balance_domain_id: str | None = None) -> AssetRef:
    return AssetRef.erc20(TokenKey(CHAIN_ID, address), balance_domain_id)


BASE = _token("0x00000000000000000000000000000000000000aa")
MIDDLE = _token("0x00000000000000000000000000000000000000bb")
OTHER = _token("0x00000000000000000000000000000000000000cc")
USD = _token("0x00000000000000000000000000000000000000dd")
FEE = _token("0x00000000000000000000000000000000000000ee")
SAME_SYMBOL_OTHER_DOMAIN = _token(
    "0x00000000000000000000000000000000000000ee", "other-balance-domain"
)


def _route() -> RouteRef:
    pool_one = PoolKey(
        CHAIN_ID,
        "v4",
        "manager",
        "0x1000000000000000000000000000000000000001",
        "address",
        "0x3000000000000000000000000000000000000001",
    )
    pool_two = PoolKey(
        CHAIN_ID,
        "v4",
        "manager",
        "0x1000000000000000000000000000000000000001",
        "address",
        "0x3000000000000000000000000000000000000002",
    )
    hops = [
        HopRef(pool_one, BASE, MIDDLE),
        HopRef(pool_two, MIDDLE, BASE),
    ]
    return RouteRef(CHAIN_ID, BASE, hops)


def _conversion(
    source: AssetRef,
    target: AssetRef,
    numerator: int,
    denominator: int,
    *,
    valid_until_ms: int = 3000,
) -> AssetConversionInput:
    return AssetConversionInput(
        PriceEvidence(
            base_asset=source,
            quote_asset=target,
            price_source_kind="signed_execution_feed",
            price_type="actual_fill",
            price_numerator=numerator,
            price_denominator=denominator,
            timestamp_ms=NOW_MS,
            valid_until_ms=valid_until_ms,
            market_status="active",
            source_refs=("fixture:manual-price",),
            evidence_ref="fixture:price-evidence",
        ),
        BASE_TO_QUOTE,
    )


def _gas_evidence(
    *,
    kind: GasEvidenceKind = GasEvidenceKind.RPC_ESTIMATE,
    gas_units: int = 0,
    gas_price_atoms: int = 0,
    valid_until_ms: int | None = 3000,
    payer_asset: AssetRef | None = FEE,
    payer_subject: str | None = "own-engine",
    block_ref: str | None = "block:100",
    already_included: tuple[str, ...] = (),
) -> GasEvidence:
    return GasEvidence(
        gas_kind=kind,
        payer_asset=payer_asset,
        gas_units=gas_units,
        gas_price_atoms=gas_price_atoms,
        l1_fee_atoms=0,
        payer_subject=payer_subject,
        source_refs=("fixture:rpc-gas",),
        already_included_components=already_included,
        quoted_at_ms=NOW_MS,
        valid_until_ms=valid_until_ms,
        block_ref=block_ref,
        evidence_ref="fixture:gas-evidence",
    )


def _quote(
    amount_in_atoms: int,
    amount_out_atoms: int,
    *,
    fee_components: tuple[FeeComponent, ...] | None = None,
    gas_evidence: GasEvidence | None | object = _gas_evidence(),
    fee_included: TriState = TriState.YES,
    impact_included: TriState = TriState.YES,
    actor_scope: ActorScope = ActorScope.OWN_AUTHORIZED,
) -> QuoteEvidence:
    route = _route()
    if fee_components is None:
        fee_components = (FeeComponent("gas-extra", 0, FEE, "gas", "fixture:gas-component"),)
    return QuoteEvidence(
        quote_id="quote-w2-c",
        route_ref=route,
        amount_in=Amount(route.base_asset, amount_in_atoms, 18),
        amount_out=Amount(route.base_asset, amount_out_atoms, 18),
        status="quoted",
        evidence_level=EvidenceLevel.RPC_QUOTE,
        data_mode=DataMode.SYNTHETIC,
        actor_scope=actor_scope,
        fee_included=fee_included,
        impact_included=impact_included,
        gas_evidence=gas_evidence,  # type: ignore[arg-type]
        economic_assessment=EconomicAssessment(
            net_atoms=None,
            fee_components=fee_components,
            is_estimated=True,
        ),
        source_refs=("fixture:synthetic-quote",),
    )


def _expected(case_id: str) -> dict[str, object]:
    cases = [json.loads(line) for line in FIXTURE_PATH.read_text().splitlines()]
    return next(case["expected"] for case in cases if case["case_id"] == case_id)


def _assert_assessment(case_id: str, assessment: EconomicAssessment) -> None:
    expected = _expected(case_id)
    assert assessment.net_atoms == expected["net_atoms"]
    assert assessment.net_usd_micros == expected["net_usd_micros"]
    assert assessment.economic_status == expected["status"]
    assert assessment.gas_cost_atoms == expected["gas_cost_atoms"]
    assert assessment.is_estimated is True


def test_included_fee_and_impact_are_not_deducted_again() -> None:
    """A second 10-atom pool fee deduction is the forbidden mutation."""
    quote = _quote(1000, 1006)
    result = evaluate_quote_evidence(quote, NOW_MS, [])
    _assert_assessment("included_fee_no_second_deduction", result)


def test_zero_fee_and_zero_gas_component_are_legal() -> None:
    zero_gas = FeeComponent("gas-extra", 0, FEE, "gas", "fixture:gas-component")
    quote = _quote(100, 200, fee_components=(zero_gas,))
    _assert_assessment("zero_fee_is_legal", evaluate_quote_evidence(quote, NOW_MS, []))


@pytest.mark.parametrize("field_name", ["fee_included", "impact_included"])
def test_unknown_inclusion_is_fail_closed(field_name: str) -> None:
    fee_included = TriState.UNKNOWN if field_name == "fee_included" else TriState.YES
    impact_included = TriState.UNKNOWN if field_name == "impact_included" else TriState.YES
    quote = _quote(
        100,
        200,
        fee_included=fee_included,
        impact_included=impact_included,
    )
    _assert_assessment("fee_included_unknown", evaluate_quote_evidence(quote, NOW_MS, []))


def test_quoter_gas_estimate_cannot_produce_determined_net() -> None:
    gas_component = FeeComponent("gas-extra", 5, FEE, "gas", "fixture:gas-component")
    quote = _quote(
        100,
        200,
        fee_components=(gas_component,),
        gas_evidence=_gas_evidence(kind=GasEvidenceKind.QUOTER_ESTIMATE),
    )
    _assert_assessment("quoter_gas_is_unknown", evaluate_quote_evidence(quote, NOW_MS, []))


def test_missing_gas_component_is_not_filled_with_zero() -> None:
    quote = _quote(100, 200, fee_components=(), gas_evidence=_gas_evidence())
    _assert_assessment("missing_gas_component", evaluate_quote_evidence(quote, NOW_MS, []))


def test_duplicate_fee_component_id_is_rejected_by_contract() -> None:
    component = FeeComponent("same-id", 1, FEE, "pool_fee", "fixture:fee")
    with pytest.raises(ValueError, match="Duplicate fee component ID"):
        _quote(100, 200, fee_components=(component, component))


def test_gas_subtotal_is_counted_once() -> None:
    gas_component = FeeComponent("gas-extra", 7, FEE, "gas", "fixture:gas-component")
    conversion = _conversion(FEE, BASE, 2, 1)
    complete_gas = _gas_evidence(gas_units=7, gas_price_atoms=1)
    quote = _quote(100, 200, fee_components=(gas_component,), gas_evidence=complete_gas)
    result = evaluate_quote_evidence(quote, NOW_MS, [conversion])
    assert result.gas_cost_atoms == 14
    assert result.net_atoms == 86


def test_revenue_and_costs_round_conservatively() -> None:
    fee_component = FeeComponent("extra-fee", 333, OTHER, "pool_fee", "fixture:fee")
    other_to_base = _conversion(OTHER, BASE, 3, 2)
    quote = _quote(
        1000,
        2001,
        fee_components=(fee_component, FeeComponent("gas-extra", 0, FEE, "gas", "gas")),
        gas_evidence=_gas_evidence(),
    )
    result = evaluate_quote_evidence(quote, NOW_MS, [other_to_base])
    assert result.net_atoms == 1001 - 500


def test_revenue_and_usd_round_down() -> None:
    usd_conversion = _conversion(BASE, USD, 1, 500)
    quote = _quote(0, 1001, gas_evidence=_gas_evidence())
    result = evaluate_quote_evidence(quote, NOW_MS, [usd_conversion], USD)
    _assert_assessment("revenue_and_usd_round_down", result)


def test_negative_usd_rounds_up() -> None:
    usd_conversion = _conversion(BASE, USD, 1, 1)
    fee_component = FeeComponent("extra-fee", 999, BASE, "pool_fee", "fixture:fee")
    quote = _quote(
        0,
        998,
        fee_components=(fee_component, FeeComponent("gas-extra", 0, FEE, "gas", "gas")),
        gas_evidence=_gas_evidence(),
    )
    result = evaluate_quote_evidence(quote, NOW_MS, [usd_conversion], USD)
    _assert_assessment("negative_usd_rounds_up", result)


def test_expired_price_makes_net_and_usd_unknown() -> None:
    usd_conversion = _conversion(BASE, USD, 1, 1, valid_until_ms=NOW_MS)
    quote = _quote(0, 100, gas_evidence=None)
    _assert_assessment(
        "expired_price", evaluate_quote_evidence(quote, NOW_MS + 1, [usd_conversion], USD)
    )


def test_same_symbol_does_not_match_exact_asset() -> None:
    fee_component = FeeComponent(
        "same-symbol", 10, SAME_SYMBOL_OTHER_DOMAIN, "pool_fee", "fixture:fee"
    )
    canonical_conversion = _conversion(BASE, FEE, 1, 1)
    quote = _quote(0, 100, fee_components=(fee_component,), gas_evidence=None)
    _assert_assessment(
        "same_symbol_not_same_asset",
        evaluate_quote_evidence(quote, NOW_MS, [canonical_conversion]),
    )


def test_uint256_boundary_is_preserved() -> None:
    quote = _quote(0, UINT256_MAX, gas_evidence=_gas_evidence())
    _assert_assessment("uint256_boundary", evaluate_quote_evidence(quote, NOW_MS, []))


def test_usdg_hardcoded_parity_is_rejected() -> None:
    quote = _quote(0, 100, gas_evidence=None)
    with pytest.raises(EconomicsInputError):
        AssetConversionInput(
            PriceEvidence(BASE, USD, "usdg_parity", "actual_fill"),
            BASE_TO_QUOTE,
        )
    assert evaluate_quote_evidence(quote, NOW_MS, [], USD).net_usd_micros is None


def test_oracle_reference_is_not_acceptable_as_fill() -> None:
    oracle_price = PriceEvidence(
        BASE,
        USD,
        "oracle_reference",
        "reference",
        1,
        1,
        NOW_MS,
        3000,
        "active",
        ("fixture:oracle",),
        "fixture:oracle-ref",
    )
    with pytest.raises(EconomicsInputError, match="actual_fill"):
        AssetConversionInput(oracle_price, BASE_TO_QUOTE)


def test_third_party_gas_payer_is_fail_closed() -> None:
    gas_component = FeeComponent("gas-extra", 5, FEE, "gas", "fixture:gas-component")
    conversion = _conversion(FEE, BASE, 1, 1)
    quote = _quote(
        100,
        200,
        fee_components=(gas_component,),
        actor_scope=ActorScope.THIRD_PARTY,
    )
    assert evaluate_quote_evidence(quote, NOW_MS, [conversion]).net_atoms is None


def test_synthetic_cannot_be_confirmed_execution() -> None:
    quote = _quote(100, 200, gas_evidence=None)
    with pytest.raises(ValueError, match="Synthetic data mode"):
        QuoteEvidence(
            quote_id="invalid-synthetic",
            route_ref=quote.route_ref,
            amount_in=quote.amount_in,
            amount_out=quote.amount_out,
            evidence_level=EvidenceLevel.CONFIRMED_EXECUTION,
            data_mode=DataMode.SYNTHETIC,
            actor_scope=ActorScope.SYNTHETIC,
            fee_included=TriState.YES,
            impact_included=TriState.YES,
        )


def test_hop_quoter_gas_cannot_claim_atomic_simulation() -> None:
    quote = _quote(100, 200, gas_evidence=_gas_evidence(kind=GasEvidenceKind.QUOTER_ESTIMATE))
    with pytest.raises(ValueError, match="ATOMIC_SIMULATION"):
        QuoteEvidence(
            quote_id="invalid-promotion",
            route_ref=quote.route_ref,
            amount_in=quote.amount_in,
            amount_out=quote.amount_out,
            gas_evidence=quote.gas_evidence,
            evidence_level=EvidenceLevel.ATOMIC_SIMULATION,
            data_mode=DataMode.SYNTHETIC,
            actor_scope=ActorScope.SYNTHETIC,
            fee_included=TriState.YES,
            impact_included=TriState.YES,
        )
