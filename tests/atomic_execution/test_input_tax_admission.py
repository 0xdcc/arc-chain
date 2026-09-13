"""Unit tests for input gate token tax admission and pool capability verification.

Verifies:
1. Assets with tax=VERIFIED_FALSE are admitted when review_status=APPROVED.
2. Assets with tax=VERIFIED_TRUE, tax=UNKNOWN, or omitted tax are rejected as ASSET_NOT_APPROVED.
3. Pool capabilities with can_quote=SUPPORTED and can_simulate=UNKNOWN / can_atomic_execute=UNKNOWN pass.
4. Pool capabilities with can_quote=UNKNOWN or can_quote=UNSUPPORTED are rejected as POOL_CAPABILITY_UNSUPPORTED.
5. Pool capabilities with can_simulate=UNSUPPORTED or can_atomic_execute=UNSUPPORTED are rejected.
6. InputRejection preserves taxonomy reason and is distinct from economic profitability judgment.
"""

from __future__ import annotations

import pytest

from arbitrage_contracts.eligibility import (
    AssetEligibility,
    CapabilityStatus,
    PoolCapability,
    RestrictionStatus,
    ReviewStatus,
)
from arbitrage_contracts.identity import (
    Amount,
    AssetRef,
    FeeModel,
    PoolDescriptor,
    PoolKey,
    TokenKey,
)
from arbitrage_contracts.quote import HopQuote, HopRef, QuoteEvidence, QuoteStatus, RouteRef
from arbitrage_contracts.state import StateVersion, canonical_state_ref
from atomic_execution.inputs import (
    ROBINHOOD_CHAIN_ID,
    WETH_ADDRESS_4663,
    ZERO_ADDRESS,
    InputGateError,
    evaluate_candidate,
    validate_candidate,
)
from atomic_execution.models import InputRejection, InputRejectionReason, ValidatedCandidate

TEST_MID_TOKEN = "0x1111111111111111111111111111111111111111"
TEST_POOL_1 = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TEST_POOL_2 = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
TEST_FACTORY = "0x1f7d7550b1b028f7571e69a784071f0205fd2efa"
TEST_STATE_HASH = "0x" + "aa" * 32


def _make_asset(address: str) -> AssetRef:
    return AssetRef.erc20(TokenKey(ROBINHOOD_CHAIN_ID, address))


def _make_pool_key(pool_id: str) -> PoolKey:
    return PoolKey(ROBINHOOD_CHAIN_ID, "uniswap_v3", "factory", TEST_FACTORY, "address", pool_id)


def _make_state_version() -> StateVersion:
    return StateVersion(
        chain_id=ROBINHOOD_CHAIN_ID,
        block_number=100,
        block_hash=TEST_STATE_HASH,
        received_at_ms=1000,
        completeness="ready",
        complete_through_block=100,
    )


def _make_standard_bundle() -> tuple[
    RouteRef,
    QuoteEvidence,
    StateVersion,
    dict[PoolKey, PoolDescriptor],
    AssetRef,
    AssetRef,
    PoolKey,
    PoolKey,
]:
    weth = _make_asset(WETH_ADDRESS_4663)
    mid = _make_asset(TEST_MID_TOKEN)
    pk1 = _make_pool_key(TEST_POOL_1)
    pk2 = _make_pool_key(TEST_POOL_2)

    hop1 = HopRef(pk1, weth, mid)
    hop2 = HopRef(pk2, mid, weth)
    route = RouteRef(ROBINHOOD_CHAIN_ID, weth, (hop1, hop2))

    state = _make_state_version()
    state_ref = canonical_state_ref(state)

    amt_in = Amount(weth, 1000000, 18)
    amt_mid = Amount(mid, 2000000, 18)
    amt_out = Amount(weth, 1005000, 18)

    quote = QuoteEvidence(
        quote_id="quote:test-tax-admission",
        route_ref=route,
        amount_in=amt_in,
        amount_out=amt_out,
        hop_quotes=(
            HopQuote(0, pk1, weth, mid, amt_in, amt_mid),
            HopQuote(1, pk2, mid, weth, amt_mid, amt_out),
        ),
        state_version_ref=state_ref,
        status=QuoteStatus.QUOTED,
    )

    desc1 = PoolDescriptor(
        key=pk1,
        currency0=weth,
        currency1=mid,
        fee_model=FeeModel(kind="static", raw_value=3000),
        hooks=ZERO_ADDRESS,
    )
    desc2 = PoolDescriptor(
        key=pk2,
        currency0=mid,
        currency1=weth,
        fee_model=FeeModel(kind="static", raw_value=3000),
        hooks=ZERO_ADDRESS,
    )
    pool_descriptors = {pk1: desc1, pk2: desc2}

    return route, quote, state, pool_descriptors, weth, mid, pk1, pk2


def _make_eligibility(
    asset: AssetRef,
    restrictions: dict[str, str] | None = None,
    review_status: str = ReviewStatus.APPROVED,
) -> AssetEligibility:
    return AssetEligibility(
        asset_ref=asset,
        decimals_status="verified",
        decimals_evidence_ref="ev_dec_01",
        contract_restrictions=restrictions or {},
        review_status=review_status,
        reviewer_ref="reviewer:security_audit",
        reviewed_at_ms=1000,
        evidence_refs=["ev_audit_doc_01"],
    )


def test_approved_assets_with_verified_false_tax_pass() -> None:
    """Assets with review_status=APPROVED and tax=verified_false pass input gate."""
    route, quote, state, descriptors, weth, mid, pk1, pk2 = _make_standard_bundle()

    asset_eligibility = {
        weth: _make_eligibility(weth, {"tax": RestrictionStatus.VERIFIED_FALSE}),
        mid: _make_eligibility(mid, {"tax": RestrictionStatus.VERIFIED_FALSE}),
    }
    pool_capabilities = {
        pk1: PoolCapability(pk1, can_quote=CapabilityStatus.SUPPORTED, can_simulate=CapabilityStatus.SUPPORTED, can_atomic_execute=CapabilityStatus.SUPPORTED),
        pk2: PoolCapability(pk2, can_quote=CapabilityStatus.SUPPORTED, can_simulate=CapabilityStatus.SUPPORTED, can_atomic_execute=CapabilityStatus.SUPPORTED),
    }

    result = evaluate_candidate(
        route,
        quote,
        state,
        asset_eligibility=asset_eligibility,
        pool_capabilities=pool_capabilities,
        pool_descriptors=descriptors,
    )
    assert isinstance(result, ValidatedCandidate)
    assert result.route_ref.route_id == route.route_id


def test_pool_capability_quote_supported_simulate_unknown_passes() -> None:
    """Pools with can_quote=supported and can_simulate/execute=unknown must be admitted."""
    route, quote, state, descriptors, weth, mid, pk1, pk2 = _make_standard_bundle()

    asset_eligibility = {
        weth: _make_eligibility(weth, {"tax": RestrictionStatus.VERIFIED_FALSE}),
        mid: _make_eligibility(mid, {"tax": RestrictionStatus.VERIFIED_FALSE}),
    }
    pool_capabilities = {
        pk1: PoolCapability(pk1, can_quote=CapabilityStatus.SUPPORTED, can_simulate=CapabilityStatus.UNKNOWN, can_atomic_execute=CapabilityStatus.UNKNOWN),
        pk2: PoolCapability(pk2, can_quote=CapabilityStatus.SUPPORTED, can_simulate=CapabilityStatus.UNKNOWN, can_atomic_execute=CapabilityStatus.UNKNOWN),
    }

    result = evaluate_candidate(
        route,
        quote,
        state,
        asset_eligibility=asset_eligibility,
        pool_capabilities=pool_capabilities,
        pool_descriptors=descriptors,
    )
    assert isinstance(result, ValidatedCandidate)


def test_pool_capability_quote_unknown_rejected() -> None:
    """Pool capability with can_quote=unknown is rejected as POOL_CAPABILITY_UNSUPPORTED."""
    route, quote, state, descriptors, weth, mid, pk1, pk2 = _make_standard_bundle()

    asset_eligibility = {
        weth: _make_eligibility(weth, {"tax": RestrictionStatus.VERIFIED_FALSE}),
        mid: _make_eligibility(mid, {"tax": RestrictionStatus.VERIFIED_FALSE}),
    }
    pool_capabilities = {
        pk1: PoolCapability(pk1, can_quote=CapabilityStatus.UNKNOWN, can_simulate=CapabilityStatus.UNKNOWN, can_atomic_execute=CapabilityStatus.UNKNOWN),
        pk2: PoolCapability(pk2, can_quote=CapabilityStatus.SUPPORTED, can_simulate=CapabilityStatus.UNKNOWN, can_atomic_execute=CapabilityStatus.UNKNOWN),
    }

    result = evaluate_candidate(
        route,
        quote,
        state,
        asset_eligibility=asset_eligibility,
        pool_capabilities=pool_capabilities,
        pool_descriptors=descriptors,
    )
    assert isinstance(result, InputRejection)
    assert result.reason == InputRejectionReason.POOL_CAPABILITY_UNSUPPORTED
    assert "Pool at hop 0 capability is unsupported" in result.message


def test_pool_capability_quote_unsupported_rejected() -> None:
    """Pool capability with can_quote=unsupported is rejected as POOL_CAPABILITY_UNSUPPORTED."""
    route, quote, state, descriptors, weth, mid, pk1, pk2 = _make_standard_bundle()

    asset_eligibility = {
        weth: _make_eligibility(weth, {"tax": RestrictionStatus.VERIFIED_FALSE}),
        mid: _make_eligibility(mid, {"tax": RestrictionStatus.VERIFIED_FALSE}),
    }
    pool_capabilities = {
        pk1: PoolCapability(pk1, can_quote=CapabilityStatus.UNSUPPORTED),
        pk2: PoolCapability(pk2, can_quote=CapabilityStatus.SUPPORTED),
    }

    result = evaluate_candidate(
        route,
        quote,
        state,
        asset_eligibility=asset_eligibility,
        pool_capabilities=pool_capabilities,
        pool_descriptors=descriptors,
    )
    assert isinstance(result, InputRejection)
    assert result.reason == InputRejectionReason.POOL_CAPABILITY_UNSUPPORTED


def test_pool_capability_simulate_unsupported_rejected() -> None:
    """Pool capability with can_simulate=unsupported is rejected."""
    route, quote, state, descriptors, weth, mid, pk1, pk2 = _make_standard_bundle()

    asset_eligibility = {
        weth: _make_eligibility(weth, {"tax": RestrictionStatus.VERIFIED_FALSE}),
        mid: _make_eligibility(mid, {"tax": RestrictionStatus.VERIFIED_FALSE}),
    }
    pool_capabilities = {
        pk1: PoolCapability(pk1, can_quote=CapabilityStatus.SUPPORTED, can_simulate=CapabilityStatus.UNSUPPORTED),
        pk2: PoolCapability(pk2, can_quote=CapabilityStatus.SUPPORTED),
    }

    result = evaluate_candidate(
        route,
        quote,
        state,
        asset_eligibility=asset_eligibility,
        pool_capabilities=pool_capabilities,
        pool_descriptors=descriptors,
    )
    assert isinstance(result, InputRejection)
    assert result.reason == InputRejectionReason.POOL_CAPABILITY_UNSUPPORTED


def test_pool_capability_atomic_execute_unsupported_rejected() -> None:
    """Pool capability with can_atomic_execute=unsupported is rejected."""
    route, quote, state, descriptors, weth, mid, pk1, pk2 = _make_standard_bundle()

    asset_eligibility = {
        weth: _make_eligibility(weth, {"tax": RestrictionStatus.VERIFIED_FALSE}),
        mid: _make_eligibility(mid, {"tax": RestrictionStatus.VERIFIED_FALSE}),
    }
    pool_capabilities = {
        pk1: PoolCapability(pk1, can_quote=CapabilityStatus.SUPPORTED, can_atomic_execute=CapabilityStatus.UNSUPPORTED),
        pk2: PoolCapability(pk2, can_quote=CapabilityStatus.SUPPORTED),
    }

    result = evaluate_candidate(
        route,
        quote,
        state,
        asset_eligibility=asset_eligibility,
        pool_capabilities=pool_capabilities,
        pool_descriptors=descriptors,
    )
    assert isinstance(result, InputRejection)
    assert result.reason == InputRejectionReason.POOL_CAPABILITY_UNSUPPORTED


def test_asset_tax_verified_true_rejected() -> None:
    """Asset with tax=verified_true is rejected as ASSET_NOT_APPROVED."""
    route, quote, state, descriptors, weth, mid, pk1, pk2 = _make_standard_bundle()

    asset_eligibility = {
        weth: _make_eligibility(weth, {"tax": RestrictionStatus.VERIFIED_FALSE}),
        mid: _make_eligibility(mid, {"tax": RestrictionStatus.VERIFIED_TRUE}),
    }
    pool_capabilities = {
        pk1: PoolCapability(pk1, can_quote=CapabilityStatus.SUPPORTED),
        pk2: PoolCapability(pk2, can_quote=CapabilityStatus.SUPPORTED),
    }

    result = evaluate_candidate(
        route,
        quote,
        state,
        asset_eligibility=asset_eligibility,
        pool_capabilities=pool_capabilities,
        pool_descriptors=descriptors,
    )
    assert isinstance(result, InputRejection)
    assert result.reason == InputRejectionReason.ASSET_NOT_APPROVED
    assert "tax restriction is verified_true, expected verified_false" in result.message


def test_asset_tax_unknown_rejected() -> None:
    """Asset with tax=unknown explicitly declared is rejected as ASSET_NOT_APPROVED."""
    route, quote, state, descriptors, weth, mid, pk1, pk2 = _make_standard_bundle()

    asset_eligibility = {
        weth: _make_eligibility(weth, {"tax": RestrictionStatus.VERIFIED_FALSE}),
        mid: _make_eligibility(mid, {"tax": RestrictionStatus.UNKNOWN}),
    }
    pool_capabilities = {
        pk1: PoolCapability(pk1, can_quote=CapabilityStatus.SUPPORTED),
        pk2: PoolCapability(pk2, can_quote=CapabilityStatus.SUPPORTED),
    }

    result = evaluate_candidate(
        route,
        quote,
        state,
        asset_eligibility=asset_eligibility,
        pool_capabilities=pool_capabilities,
        pool_descriptors=descriptors,
    )
    assert isinstance(result, InputRejection)
    assert result.reason == InputRejectionReason.ASSET_NOT_APPROVED
    assert "tax restriction is unknown, expected verified_false" in result.message


def test_asset_tax_omitted_rejected() -> None:
    """Asset omitting tax restriction from contract_restrictions is rejected as ASSET_NOT_APPROVED."""
    route, quote, state, descriptors, weth, mid, pk1, pk2 = _make_standard_bundle()

    asset_eligibility = {
        weth: _make_eligibility(weth, {"tax": RestrictionStatus.VERIFIED_FALSE}),
        mid: _make_eligibility(mid, {}),
    }
    pool_capabilities = {
        pk1: PoolCapability(pk1, can_quote=CapabilityStatus.SUPPORTED),
        pk2: PoolCapability(pk2, can_quote=CapabilityStatus.SUPPORTED),
    }

    result = evaluate_candidate(
        route,
        quote,
        state,
        asset_eligibility=asset_eligibility,
        pool_capabilities=pool_capabilities,
        pool_descriptors=descriptors,
    )
    assert isinstance(result, InputRejection)
    assert result.reason == InputRejectionReason.ASSET_NOT_APPROVED
    assert "tax restriction is unknown, expected verified_false" in result.message


def test_base_asset_tax_verified_true_rejected() -> None:
    """Base asset having tax=verified_true is rejected as ASSET_NOT_APPROVED."""
    route, quote, state, descriptors, weth, mid, pk1, pk2 = _make_standard_bundle()

    asset_eligibility = {
        weth: _make_eligibility(weth, {"tax": RestrictionStatus.VERIFIED_TRUE}),
        mid: _make_eligibility(mid, {"tax": RestrictionStatus.VERIFIED_FALSE}),
    }
    pool_capabilities = {
        pk1: PoolCapability(pk1, can_quote=CapabilityStatus.SUPPORTED),
        pk2: PoolCapability(pk2, can_quote=CapabilityStatus.SUPPORTED),
    }

    result = evaluate_candidate(
        route,
        quote,
        state,
        asset_eligibility=asset_eligibility,
        pool_capabilities=pool_capabilities,
        pool_descriptors=descriptors,
    )
    assert isinstance(result, InputRejection)
    assert result.reason == InputRejectionReason.ASSET_NOT_APPROVED
    assert "tax restriction is verified_true, expected verified_false" in result.message


def test_asset_tax_unknown_with_alias_false_rejected() -> None:
    """Asset with tax=unknown and transfer_tax=verified_false is rejected (no whitewashing)."""
    route, quote, state, descriptors, weth, mid, pk1, pk2 = _make_standard_bundle()

    asset_eligibility = {
        weth: _make_eligibility(weth, {"tax": RestrictionStatus.VERIFIED_FALSE}),
        mid: _make_eligibility(
            mid,
            {
                "tax": RestrictionStatus.UNKNOWN,
                "transfer_tax": RestrictionStatus.VERIFIED_FALSE,
            },
        ),
    }
    pool_capabilities = {
        pk1: PoolCapability(pk1, can_quote=CapabilityStatus.SUPPORTED),
        pk2: PoolCapability(pk2, can_quote=CapabilityStatus.SUPPORTED),
    }

    result = evaluate_candidate(
        route,
        quote,
        state,
        asset_eligibility=asset_eligibility,
        pool_capabilities=pool_capabilities,
        pool_descriptors=descriptors,
    )
    assert isinstance(result, InputRejection)
    assert result.reason == InputRejectionReason.ASSET_NOT_APPROVED
    assert "tax restriction is unknown, expected verified_false" in result.message


def test_asset_tax_false_with_alias_true_rejected() -> None:
    """Asset with tax=verified_false and transfer_tax=verified_true is rejected (conflict)."""
    route, quote, state, descriptors, weth, mid, pk1, pk2 = _make_standard_bundle()

    asset_eligibility = {
        weth: _make_eligibility(weth, {"tax": RestrictionStatus.VERIFIED_FALSE}),
        mid: _make_eligibility(
            mid,
            {
                "tax": RestrictionStatus.VERIFIED_FALSE,
                "transfer_tax": RestrictionStatus.VERIFIED_TRUE,
            },
        ),
    }
    pool_capabilities = {
        pk1: PoolCapability(pk1, can_quote=CapabilityStatus.SUPPORTED),
        pk2: PoolCapability(pk2, can_quote=CapabilityStatus.SUPPORTED),
    }

    result = evaluate_candidate(
        route,
        quote,
        state,
        asset_eligibility=asset_eligibility,
        pool_capabilities=pool_capabilities,
        pool_descriptors=descriptors,
    )
    assert isinstance(result, InputRejection)
    assert result.reason == InputRejectionReason.ASSET_NOT_APPROVED
    assert "transfer_tax restriction is verified_true, expected verified_false" in result.message


def test_asset_tax_false_with_alias_unknown_rejected() -> None:
    """Asset with tax=verified_false and transfer_tax=unknown is rejected (conflict / unverified)."""
    route, quote, state, descriptors, weth, mid, pk1, pk2 = _make_standard_bundle()

    asset_eligibility = {
        weth: _make_eligibility(weth, {"tax": RestrictionStatus.VERIFIED_FALSE}),
        mid: _make_eligibility(
            mid,
            {
                "tax": RestrictionStatus.VERIFIED_FALSE,
                "transfer_tax": RestrictionStatus.UNKNOWN,
            },
        ),
    }
    pool_capabilities = {
        pk1: PoolCapability(pk1, can_quote=CapabilityStatus.SUPPORTED),
        pk2: PoolCapability(pk2, can_quote=CapabilityStatus.SUPPORTED),
    }

    result = evaluate_candidate(
        route,
        quote,
        state,
        asset_eligibility=asset_eligibility,
        pool_capabilities=pool_capabilities,
        pool_descriptors=descriptors,
    )
    assert isinstance(result, InputRejection)
    assert result.reason == InputRejectionReason.ASSET_NOT_APPROVED
    assert "transfer_tax restriction is unknown, expected verified_false" in result.message


def test_asset_missing_tax_with_alias_false_rejected() -> None:
    """Asset omitting tax but declaring transfer_tax=verified_false is rejected."""
    route, quote, state, descriptors, weth, mid, pk1, pk2 = _make_standard_bundle()

    asset_eligibility = {
        weth: _make_eligibility(weth, {"tax": RestrictionStatus.VERIFIED_FALSE}),
        mid: _make_eligibility(mid, {"transfer_tax": RestrictionStatus.VERIFIED_FALSE}),
    }
    pool_capabilities = {
        pk1: PoolCapability(pk1, can_quote=CapabilityStatus.SUPPORTED),
        pk2: PoolCapability(pk2, can_quote=CapabilityStatus.SUPPORTED),
    }

    result = evaluate_candidate(
        route,
        quote,
        state,
        asset_eligibility=asset_eligibility,
        pool_capabilities=pool_capabilities,
        pool_descriptors=descriptors,
    )
    assert isinstance(result, InputRejection)
    assert result.reason == InputRejectionReason.ASSET_NOT_APPROVED
    assert "tax restriction is unknown, expected verified_false" in result.message


def test_asset_tax_false_and_alias_false_passes() -> None:
    """Asset declaring both tax=verified_false and transfer_tax=verified_false passes."""
    route, quote, state, descriptors, weth, mid, pk1, pk2 = _make_standard_bundle()

    asset_eligibility = {
        weth: _make_eligibility(
            weth,
            {
                "tax": RestrictionStatus.VERIFIED_FALSE,
                "transfer_tax": RestrictionStatus.VERIFIED_FALSE,
            },
        ),
        mid: _make_eligibility(
            mid,
            {
                "tax": RestrictionStatus.VERIFIED_FALSE,
                "transfer_tax": RestrictionStatus.VERIFIED_FALSE,
            },
        ),
    }
    pool_capabilities = {
        pk1: PoolCapability(pk1, can_quote=CapabilityStatus.SUPPORTED),
        pk2: PoolCapability(pk2, can_quote=CapabilityStatus.SUPPORTED),
    }

    result = evaluate_candidate(
        route,
        quote,
        state,
        asset_eligibility=asset_eligibility,
        pool_capabilities=pool_capabilities,
        pool_descriptors=descriptors,
    )
    assert isinstance(result, ValidatedCandidate)


def test_validate_candidate_raises_input_gate_error_on_tax_rejection() -> None:
    """validate_candidate raises InputGateError containing InputRejection when tax is unverified."""
    route, quote, state, descriptors, weth, mid, pk1, pk2 = _make_standard_bundle()

    asset_eligibility = {
        weth: _make_eligibility(weth, {"tax": RestrictionStatus.VERIFIED_FALSE}),
        mid: _make_eligibility(mid, {"tax": RestrictionStatus.VERIFIED_TRUE}),
    }

    with pytest.raises(InputGateError) as exc_info:
        validate_candidate(
            route,
            quote,
            state,
            asset_eligibility=asset_eligibility,
            pool_descriptors=descriptors,
        )
    assert exc_info.value.reason == InputRejectionReason.ASSET_NOT_APPROVED
    assert "tax restriction is verified_true, expected verified_false" in str(exc_info.value)


def test_taxonomy_classification_not_economic_evaluation() -> None:
    """Gate failure is classified by taxonomy reason and does not perform economic evaluation."""
    route, quote, state, descriptors, weth, mid, pk1, pk2 = _make_standard_bundle()

    asset_eligibility = {
        weth: _make_eligibility(weth, {"tax": RestrictionStatus.VERIFIED_TRUE}),
        mid: _make_eligibility(mid, {"tax": RestrictionStatus.VERIFIED_FALSE}),
    }

    result = evaluate_candidate(
        route,
        quote,
        state,
        asset_eligibility=asset_eligibility,
        pool_descriptors=descriptors,
    )
    assert isinstance(result, InputRejection)
    # Ensure taxonomy classification reason is strictly ASSET_NOT_APPROVED
    assert result.reason == InputRejectionReason.ASSET_NOT_APPROVED
    # Route attribution preserved
    assert result.route_id == route.route_id
    # Economic viability (delta_atoms > 0) is not assessed when taxonomy rejects
    assert quote.delta_atoms == 5000
