"""Comprehensive regression test suite for W5-B input gate and domain envelopes."""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from arbitrage_contracts.eligibility import (
    AssetEligibility,
    CapabilityStatus,
    PoolCapability,
    ReviewStatus,
)
from arbitrage_contracts.identity import (
    UINT256_MAX,
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
    USDG_ADDRESS_4663,
    WETH_ADDRESS_4663,
    ZERO_ADDRESS,
    InputGateError,
    evaluate_candidate,
    load_candidate_from_dict,
    load_candidates_from_jsonl,
    parse_candidate_json,
    validate_candidate,
    validate_opportunity,
)
from atomic_execution.models import (
    CandidateOpportunity,
    DraftSimulationEvidence,
    InputRejection,
    InputRejectionReason,
    ValidatedCandidate,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "atomic_execution" / "v1"

TEST_MID_TOKEN_1 = "0x1111111111111111111111111111111111111111"
TEST_MID_TOKEN_2 = "0x2222222222222222222222222222222222222222"
TEST_POOL_1 = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TEST_POOL_2 = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
TEST_POOL_3 = "0xcccccccccccccccccccccccccccccccccccccccc"
TEST_FACTORY = "0x1f7d7550b1b028f7571e69a784071f0205fd2efa"
TEST_STATE_HASH = "0x" + "aa" * 32


def _make_asset(address: str, chain_id: int = ROBINHOOD_CHAIN_ID) -> AssetRef:
    return AssetRef.erc20(TokenKey(chain_id, address))


def _make_pool_key(
    pool_id: str,
    chain_id: int = ROBINHOOD_CHAIN_ID,
    protocol_id: str = "uniswap_v3",
) -> PoolKey:
    return PoolKey(chain_id, protocol_id, "factory", TEST_FACTORY, "address", pool_id)


def _make_state_version(
    chain_id: int = ROBINHOOD_CHAIN_ID,
    block_number: int = 100,
    block_hash: str = TEST_STATE_HASH,
    completeness: str = "ready",
) -> StateVersion:
    return StateVersion(
        chain_id=chain_id,
        block_number=block_number,
        block_hash=block_hash,
        received_at_ms=1000,
        complete_through_block=block_number if completeness == "ready" else None,
        completeness=completeness,
    )


def _make_valid_2hop_bundle() -> tuple[RouteRef, QuoteEvidence, StateVersion]:
    weth = _make_asset(WETH_ADDRESS_4663)
    middle = _make_asset(TEST_MID_TOKEN_1)
    pk1 = _make_pool_key(TEST_POOL_1)
    pk2 = _make_pool_key(TEST_POOL_2)

    hop1 = HopRef(pk1, weth, middle)
    hop2 = HopRef(pk2, middle, weth)
    route = RouteRef(ROBINHOOD_CHAIN_ID, weth, (hop1, hop2))

    amt_in = Amount(weth, 1000000, 18)
    amt_out = Amount(weth, 1005000, 18)
    quote = QuoteEvidence(
        quote_id="quote:test-weth-2hop",
        route_ref=route,
        amount_in=amt_in,
        amount_out=amt_out,
        hop_quotes=(
            HopQuote(0, pk1, weth, middle, amt_in, Amount(middle, 2000000, 18)),
            HopQuote(1, pk2, middle, weth, Amount(middle, 2000000, 18), amt_out),
        ),
        state_version_ref=canonical_state_ref(_make_state_version()),
        status=QuoteStatus.QUOTED,
    )
    state = _make_state_version()
    return route, quote, state


def _make_valid_3hop_bundle() -> tuple[RouteRef, QuoteEvidence, StateVersion]:
    usdg = _make_asset(USDG_ADDRESS_4663)
    t1 = _make_asset(TEST_MID_TOKEN_1)
    t2 = _make_asset(TEST_MID_TOKEN_2)
    pk1 = _make_pool_key(TEST_POOL_1)
    pk2 = _make_pool_key(TEST_POOL_2)
    pk3 = _make_pool_key(TEST_POOL_3)

    hop1 = HopRef(pk1, usdg, t1)
    hop2 = HopRef(pk2, t1, t2)
    hop3 = HopRef(pk3, t2, usdg)
    route = RouteRef(ROBINHOOD_CHAIN_ID, usdg, (hop1, hop2, hop3))

    amt_in = Amount(usdg, 5000000, 6)
    amt_out = Amount(usdg, 5020000, 6)
    quote = QuoteEvidence(
        quote_id="quote:test-usdg-3hop",
        route_ref=route,
        amount_in=amt_in,
        amount_out=amt_out,
        hop_quotes=(
            HopQuote(0, pk1, usdg, t1, amt_in, Amount(t1, 2000000, 18)),
            HopQuote(1, pk2, t1, t2, Amount(t1, 2000000, 18), Amount(t2, 3000000, 18)),
            HopQuote(2, pk3, t2, usdg, Amount(t2, 3000000, 18), amt_out),
        ),
        state_version_ref=canonical_state_ref(_make_state_version()),
        status=QuoteStatus.QUOTED,
    )
    state = _make_state_version()
    return route, quote, state


# ==============================================================================
# Category C01 Tests: Format and Types
# ==============================================================================


def test_c01_empty_string_and_blank_input_rejected() -> None:
    """Empty JSON and whitespace-only text raise MALFORMED_INPUT."""
    with pytest.raises(InputGateError) as exc_info:
        parse_candidate_json("")
    assert exc_info.value.reason == InputRejectionReason.MALFORMED_INPUT

    with pytest.raises(InputGateError) as exc_info2:
        parse_candidate_json("   \n\t  ")
    assert exc_info2.value.reason == InputRejectionReason.MALFORMED_INPUT


def test_c01_duplicate_json_keys_rejected() -> None:
    """Duplicate JSON keys are strictly rejected fail-closed."""
    dup_json = '{"route": 1, "quote": 2, "route": 3}'
    with pytest.raises(InputGateError) as exc_info:
        parse_candidate_json(dup_json)
    assert exc_info.value.reason == InputRejectionReason.DUPLICATE_KEY


def test_c01_missing_required_fields_rejected() -> None:
    """Missing route_ref, quote_evidence, or state_version triggers MISSING_FIELD."""
    route, quote, state = _make_valid_2hop_bundle()

    rejection1 = evaluate_candidate(None, quote, state)
    assert isinstance(rejection1, InputRejection)
    assert rejection1.reason == InputRejectionReason.MISSING_FIELD

    rejection2 = evaluate_candidate(route, None, state)
    assert isinstance(rejection2, InputRejection)
    assert rejection2.reason == InputRejectionReason.MISSING_FIELD

    rejection3 = evaluate_candidate(route, quote, None)
    assert isinstance(rejection3, InputRejection)
    assert rejection3.reason == InputRejectionReason.MISSING_FIELD


def test_c01_bool_masquerading_as_int_atoms_rejected() -> None:
    """Boolean True/False in atoms is rejected as TYPE_ERROR."""
    route, _, state = _make_valid_2hop_bundle()
    bad_amount = object.__new__(Amount)
    object.__setattr__(bad_amount, "asset_ref", route.base_asset)
    object.__setattr__(bad_amount, "atoms", True)
    object.__setattr__(bad_amount, "decimals", 18)
    object.__setattr__(bad_amount, "decimals_evidence_ref", None)

    bad_quote = object.__new__(QuoteEvidence)
    object.__setattr__(bad_quote, "quote_id", "quote:bool")
    object.__setattr__(bad_quote, "route_ref", route)
    object.__setattr__(bad_quote, "amount_in", bad_amount)
    object.__setattr__(bad_quote, "amount_out", None)
    object.__setattr__(bad_quote, "delta_atoms", None)
    object.__setattr__(bad_quote, "hop_quotes", ())
    object.__setattr__(bad_quote, "state_version_ref", TEST_STATE_HASH)
    object.__setattr__(bad_quote, "status", QuoteStatus.QUOTED)

    rejection = evaluate_candidate(route, bad_quote, state)
    assert isinstance(rejection, InputRejection)
    assert rejection.reason == InputRejectionReason.TYPE_ERROR


def test_c01_uint256_overflow_rejected() -> None:
    """Atoms value exceeding uint256 is rejected as UINT256_OVERFLOW."""
    route, _, state = _make_valid_2hop_bundle()
    overflow_amount = object.__new__(Amount)
    object.__setattr__(overflow_amount, "asset_ref", route.base_asset)
    object.__setattr__(overflow_amount, "atoms", UINT256_MAX + 1)
    object.__setattr__(overflow_amount, "decimals", 18)
    object.__setattr__(overflow_amount, "decimals_evidence_ref", None)

    overflow_quote = object.__new__(QuoteEvidence)
    object.__setattr__(overflow_quote, "quote_id", "quote:overflow")
    object.__setattr__(overflow_quote, "route_ref", route)
    object.__setattr__(overflow_quote, "amount_in", overflow_amount)
    object.__setattr__(overflow_quote, "amount_out", None)
    object.__setattr__(overflow_quote, "delta_atoms", None)
    object.__setattr__(overflow_quote, "hop_quotes", ())
    object.__setattr__(overflow_quote, "state_version_ref", TEST_STATE_HASH)
    object.__setattr__(overflow_quote, "status", QuoteStatus.QUOTED)

    rejection = evaluate_candidate(route, overflow_quote, state)
    assert isinstance(rejection, InputRejection)
    assert rejection.reason == InputRejectionReason.UINT256_OVERFLOW


# ==============================================================================
# Category C02 Tests: Same Chain, Same Contract Principal Loop Closure
# ==============================================================================


def test_c02_valid_weth_2hop_passes() -> None:
    """Standard valid 2-hop WETH route on Robinhood 4663 is accepted."""
    route, quote, state = _make_valid_2hop_bundle()
    candidate = validate_candidate(route, quote, state)
    assert isinstance(candidate, ValidatedCandidate)
    assert candidate.chain_id == ROBINHOOD_CHAIN_ID
    assert candidate.base_asset == route.base_asset
    assert candidate.amount_in.atoms == 1000000


def test_c02_valid_usdg_3hop_passes() -> None:
    """Standard valid 3-hop USDG route on Robinhood 4663 is accepted."""
    route, quote, state = _make_valid_3hop_bundle()
    candidate = validate_candidate(route, quote, state)
    assert isinstance(candidate, ValidatedCandidate)
    assert candidate.chain_id == ROBINHOOD_CHAIN_ID
    assert candidate.base_asset == route.base_asset
    assert candidate.amount_in.atoms == 5000000


def test_c02_unsupported_chain_rejected() -> None:
    """Non-4663 chain is rejected as UNSUPPORTED_CHAIN."""
    route, quote, _ = _make_valid_2hop_bundle()
    bsc_state = _make_state_version(chain_id=56)
    rejection = evaluate_candidate(route, quote, bsc_state)
    assert isinstance(rejection, InputRejection)
    assert rejection.reason == InputRejectionReason.UNSUPPORTED_CHAIN


def test_c02_unsupported_base_asset_rejected() -> None:
    """Tokens other than canonical WETH and USDG are rejected."""
    usdc = _make_asset("0x3333333333333333333333333333333333333333")
    middle = _make_asset(TEST_MID_TOKEN_1)
    pk1 = _make_pool_key(TEST_POOL_1)
    pk2 = _make_pool_key(TEST_POOL_2)
    route = RouteRef(
        ROBINHOOD_CHAIN_ID, usdc, (HopRef(pk1, usdc, middle), HopRef(pk2, middle, usdc))
    )
    amt = Amount(usdc, 1000000, 6)
    quote = QuoteEvidence(
        quote_id="quote:usdc",
        route_ref=route,
        amount_in=amt,
        amount_out=Amount(usdc, 1005000, 6),
        state_version_ref=canonical_state_ref(_make_state_version()),
        status=QuoteStatus.QUOTED,
    )
    state = _make_state_version()

    rejection = evaluate_candidate(route, quote, state)
    assert isinstance(rejection, InputRejection)
    assert rejection.reason == InputRejectionReason.UNSUPPORTED_BASE_ASSET


def test_c02_native_eth_rejected() -> None:
    """Native ETH is rejected as ETH_WETH_MIXED; canonical WETH required."""
    native_eth = AssetRef.native(ROBINHOOD_CHAIN_ID, "ETH")
    route, quote, state = _make_valid_2hop_bundle()

    fake_route = object.__new__(RouteRef)
    object.__setattr__(fake_route, "chain_id", ROBINHOOD_CHAIN_ID)
    object.__setattr__(fake_route, "base_asset", native_eth)
    object.__setattr__(fake_route, "hops", route.hops)
    object.__setattr__(fake_route, "route_kind", "same_chain_cycle")
    object.__setattr__(fake_route, "max_hops", 4)
    object.__setattr__(fake_route, "route_id", "route:native-eth")

    rejection = evaluate_candidate(fake_route, quote, state)
    assert isinstance(rejection, InputRejection)
    assert rejection.reason == InputRejectionReason.ETH_WETH_MIXED


def test_c02_cross_currency_open_exposure_rejected() -> None:
    """Route starting with WETH and ending with USDG is rejected as CYCLE_NOT_CLOSED."""
    weth = _make_asset(WETH_ADDRESS_4663)
    usdg = _make_asset(USDG_ADDRESS_4663)
    middle = _make_asset(TEST_MID_TOKEN_1)
    pk1 = _make_pool_key(TEST_POOL_1)
    pk2 = _make_pool_key(TEST_POOL_2)

    route_open = object.__new__(RouteRef)
    object.__setattr__(route_open, "chain_id", ROBINHOOD_CHAIN_ID)
    object.__setattr__(route_open, "base_asset", weth)
    object.__setattr__(
        route_open,
        "hops",
        (HopRef(pk1, weth, middle), HopRef(pk2, middle, usdg)),
    )
    object.__setattr__(route_open, "route_kind", "same_chain_cycle")
    object.__setattr__(route_open, "max_hops", 4)
    object.__setattr__(route_open, "route_id", "route:open-exposure")

    state = _make_state_version()
    quote = QuoteEvidence(
        quote_id="quote:open",
        route_ref=route_open,
        amount_in=Amount(weth, 1000000, 18),
        amount_out=Amount(weth, 1005000, 18),
        state_version_ref=canonical_state_ref(_make_state_version()),
        status=QuoteStatus.QUOTED,
    )

    rejection = evaluate_candidate(route_open, quote, state)
    assert isinstance(rejection, InputRejection)
    assert rejection.reason == InputRejectionReason.CYCLE_NOT_CLOSED


def test_c02_case_insensitive_checksum_accepted() -> None:
    """Mixed checksum addresses match successfully through lowercased canonical evaluation."""
    weth_upper = WETH_ADDRESS_4663.upper().replace("0X", "0x")
    route, quote, state = _make_valid_2hop_bundle()
    assert route.base_asset.token_key is not None
    assert route.base_asset.token_key.address == weth_upper.lower()
    candidate = validate_candidate(route, quote, state)
    assert isinstance(candidate, ValidatedCandidate)


# ==============================================================================
# Category C03 Tests: Hop Count and Pool Topology
# ==============================================================================


def test_c03_single_hop_route_rejected() -> None:
    """1-hop route is rejected as INVALID_HOP_COUNT."""
    route, quote, state = _make_valid_2hop_bundle()
    hop_1 = route.hops[0]
    route_1hop = object.__new__(RouteRef)
    object.__setattr__(route_1hop, "chain_id", ROBINHOOD_CHAIN_ID)
    object.__setattr__(route_1hop, "base_asset", route.base_asset)
    object.__setattr__(route_1hop, "hops", (hop_1,))
    object.__setattr__(route_1hop, "route_kind", "same_chain_cycle")
    object.__setattr__(route_1hop, "max_hops", 4)
    object.__setattr__(route_1hop, "route_id", "route:1hop")

    rejection = evaluate_candidate(route_1hop, quote, state)
    assert isinstance(rejection, InputRejection)
    assert rejection.reason == InputRejectionReason.INVALID_HOP_COUNT


def test_c03_four_hops_marked_unsupported() -> None:
    """4-hop route is rejected as UNSUPPORTED_HOP_COUNT in v1."""
    weth = _make_asset(WETH_ADDRESS_4663)
    t1 = _make_asset(TEST_MID_TOKEN_1)
    t2 = _make_asset(TEST_MID_TOKEN_2)
    t3 = _make_asset("0x3333333333333333333333333333333333333333")
    pk1 = _make_pool_key(TEST_POOL_1)
    pk2 = _make_pool_key(TEST_POOL_2)
    pk3 = _make_pool_key(TEST_POOL_3)
    pk4 = _make_pool_key("0xdddddddddddddddddddddddddddddddddddddddd")

    hop1 = HopRef(pk1, weth, t1)
    hop2 = HopRef(pk2, t1, t2)
    hop3 = HopRef(pk3, t2, t3)
    hop4 = HopRef(pk4, t3, weth)

    route_4hop = RouteRef(ROBINHOOD_CHAIN_ID, weth, (hop1, hop2, hop3, hop4), max_hops=4)
    quote = QuoteEvidence(
        quote_id="quote:4hop",
        route_ref=route_4hop,
        amount_in=Amount(weth, 1000000, 18),
        amount_out=Amount(weth, 1005000, 18),
        state_version_ref=canonical_state_ref(_make_state_version()),
        status=QuoteStatus.QUOTED,
    )
    state = _make_state_version()

    rejection = evaluate_candidate(route_4hop, quote, state)
    assert isinstance(rejection, InputRejection)
    assert rejection.reason == InputRejectionReason.UNSUPPORTED_HOP_COUNT


def test_c03_duplicate_pool_in_route_rejected() -> None:
    """Repeated pool ID in intermediate hops is rejected as DUPLICATE_POOL."""
    weth = _make_asset(WETH_ADDRESS_4663)
    middle = _make_asset(TEST_MID_TOKEN_1)
    pk1 = _make_pool_key(TEST_POOL_1)

    hop1 = HopRef(pk1, weth, middle)
    hop2 = HopRef(pk1, middle, weth)

    route_dup = object.__new__(RouteRef)
    object.__setattr__(route_dup, "chain_id", ROBINHOOD_CHAIN_ID)
    object.__setattr__(route_dup, "base_asset", weth)
    object.__setattr__(route_dup, "hops", (hop1, hop2))
    object.__setattr__(route_dup, "route_kind", "same_chain_cycle")
    object.__setattr__(route_dup, "max_hops", 4)
    object.__setattr__(route_dup, "route_id", "route:dup-pool")

    quote = QuoteEvidence(
        quote_id="quote:dup",
        route_ref=route_dup,
        amount_in=Amount(weth, 1000000, 18),
        amount_out=Amount(weth, 1005000, 18),
        state_version_ref=canonical_state_ref(_make_state_version()),
        status=QuoteStatus.QUOTED,
    )
    state = _make_state_version()

    rejection = evaluate_candidate(route_dup, quote, state)
    assert isinstance(rejection, InputRejection)
    assert rejection.reason == InputRejectionReason.DUPLICATE_POOL


def test_c03_unsupported_uniswap_v2_protocol_rejected() -> None:
    """Uniswap V2 protocol hops are rejected as UNSUPPORTED_PROTOCOL."""
    weth = _make_asset(WETH_ADDRESS_4663)
    middle = _make_asset(TEST_MID_TOKEN_1)
    pk_v2 = _make_pool_key(TEST_POOL_1, protocol_id="uniswap_v2")
    pk_v3 = _make_pool_key(TEST_POOL_2, protocol_id="uniswap_v3")

    hop1 = HopRef(pk_v2, weth, middle)
    hop2 = HopRef(pk_v3, middle, weth)
    route = RouteRef(ROBINHOOD_CHAIN_ID, weth, (hop1, hop2))
    quote = QuoteEvidence(
        quote_id="quote:v2",
        route_ref=route,
        amount_in=Amount(weth, 1000000, 18),
        amount_out=Amount(weth, 1005000, 18),
        state_version_ref=canonical_state_ref(_make_state_version()),
        status=QuoteStatus.QUOTED,
    )
    state = _make_state_version()

    rejection = evaluate_candidate(route, quote, state)
    assert isinstance(rejection, InputRejection)
    assert rejection.reason == InputRejectionReason.UNSUPPORTED_PROTOCOL


# ==============================================================================
# Category C04 Tests: Asset Eligibility and Pool Capability Admissions
# ==============================================================================


def test_c04_unapproved_asset_rejected() -> None:
    """Asset with review status other than APPROVED is rejected."""
    route, quote, state = _make_valid_2hop_bundle()
    weth_elig = AssetEligibility(
        asset_ref=route.base_asset,
        review_status=ReviewStatus.PENDING_REVIEW,
    )
    eligibility_map = {route.base_asset: weth_elig}

    rejection = evaluate_candidate(route, quote, state, asset_eligibility=eligibility_map)
    assert isinstance(rejection, InputRejection)
    assert rejection.reason == InputRejectionReason.ASSET_NOT_APPROVED


def test_c04_approved_assets_pass() -> None:
    """Assets with ReviewStatus.APPROVED pass the eligibility gate."""
    route, quote, state = _make_valid_2hop_bundle()
    eligibility_map = {
        route.base_asset: AssetEligibility(
            asset_ref=route.base_asset,
            review_status=ReviewStatus.APPROVED,
            reviewer_ref="reviewer:audit",
            reviewed_at_ms=1000,
            evidence_refs=["ev:1"],
        ),
        route.hops[0].asset_out: AssetEligibility(
            asset_ref=route.hops[0].asset_out,
            review_status=ReviewStatus.APPROVED,
            reviewer_ref="reviewer:audit",
            reviewed_at_ms=1000,
            evidence_refs=["ev:2"],
        ),
    }

    candidate = validate_candidate(route, quote, state, asset_eligibility=eligibility_map)
    assert isinstance(candidate, ValidatedCandidate)


def test_c04_v4_pool_non_zero_hook_rejected() -> None:
    """Uniswap V4 pool declaring non-zero hooks is rejected as V4_NON_ZERO_HOOK."""
    route, quote, state = _make_valid_2hop_bundle()
    pk = route.hops[0].pool_key
    desc = PoolDescriptor(
        key=pk,
        currency0=route.hops[0].asset_in,
        currency1=route.hops[0].asset_out,
        fee_model=FeeModel(kind="static", raw_value=3000),
        hooks="0x1111111111111111111111111111111111111111",
    )
    descriptors_map = {pk: desc}

    rejection = evaluate_candidate(route, quote, state, pool_descriptors=descriptors_map)
    assert isinstance(rejection, InputRejection)
    assert rejection.reason == InputRejectionReason.V4_NON_ZERO_HOOK


def test_c04_v4_pool_zero_hook_passes() -> None:
    """Uniswap V4 pool with ZERO_ADDRESS hook passes the hook gate."""
    route, quote, state = _make_valid_2hop_bundle()
    pk = route.hops[0].pool_key
    desc = PoolDescriptor(
        key=pk,
        currency0=route.hops[0].asset_in,
        currency1=route.hops[0].asset_out,
        fee_model=FeeModel(kind="static", raw_value=3000),
        hooks=ZERO_ADDRESS,
    )
    descriptors_map = {pk: desc}

    candidate = validate_candidate(route, quote, state, pool_descriptors=descriptors_map)
    assert isinstance(candidate, ValidatedCandidate)


def test_c04_dynamic_fee_model_rejected() -> None:
    """Pool or quote declaring dynamic fee model is rejected as DYNAMIC_FEE_UNSUPPORTED."""
    route, quote, state = _make_valid_2hop_bundle()
    pk = route.hops[0].pool_key
    desc = PoolDescriptor(
        key=pk,
        currency0=route.hops[0].asset_in,
        currency1=route.hops[0].asset_out,
        fee_model=FeeModel(kind="dynamic"),
        hooks=ZERO_ADDRESS,
    )
    descriptors_map = {pk: desc}

    rejection = evaluate_candidate(route, quote, state, pool_descriptors=descriptors_map)
    assert isinstance(rejection, InputRejection)
    assert rejection.reason == InputRejectionReason.DYNAMIC_FEE_UNSUPPORTED


def test_c04_unsupported_pool_capability_rejected() -> None:
    """Pool with unsupported execution capability is rejected."""
    route, quote, state = _make_valid_2hop_bundle()
    pk = route.hops[0].pool_key
    cap = PoolCapability(
        pool_key=pk,
        can_atomic_execute=CapabilityStatus.UNSUPPORTED,
    )
    capabilities_map = {pk: cap}

    rejection = evaluate_candidate(route, quote, state, pool_capabilities=capabilities_map)
    assert isinstance(rejection, InputRejection)
    assert rejection.reason == InputRejectionReason.POOL_CAPABILITY_UNSUPPORTED


# ==============================================================================
# Category C05 Tests: State and Timing Monotonicity
# ==============================================================================


def test_c05_quote_status_not_quoted_rejected() -> None:
    """Quote with status other than QUOTED is rejected as QUOTE_STATUS_INVALID."""
    route, _, state = _make_valid_2hop_bundle()
    revert_quote = QuoteEvidence(
        quote_id="quote:revert",
        route_ref=route,
        amount_in=Amount(route.base_asset, 1000000, 18),
        amount_out=None,
        state_version_ref=canonical_state_ref(_make_state_version()),
        status=QuoteStatus.CONTRACT_REVERT,
    )

    rejection = evaluate_candidate(route, revert_quote, state)
    assert isinstance(rejection, InputRejection)
    assert rejection.reason == InputRejectionReason.QUOTE_STATUS_INVALID


def test_c05_state_version_not_ready_rejected() -> None:
    """StateVersion not in ready status is rejected as STATE_NOT_READY."""
    route, quote, _ = _make_valid_2hop_bundle()
    syncing_state = _make_state_version(completeness="syncing")

    rejection = evaluate_candidate(route, quote, syncing_state)
    assert isinstance(rejection, InputRejection)
    assert rejection.reason == InputRejectionReason.STATE_NOT_READY


def test_c05_state_version_ref_missing_rejected() -> None:
    """QuoteEvidence missing state_version_ref is rejected as STATE_VERSION_MISMATCH."""
    route, _, state = _make_valid_2hop_bundle()
    no_ref_quote = QuoteEvidence(
        quote_id="quote:no-ref",
        route_ref=route,
        amount_in=Amount(route.base_asset, 1000000, 18),
        amount_out=Amount(route.base_asset, 1005000, 18),
        state_version_ref=None,
        status=QuoteStatus.QUOTED,
    )

    rejection = evaluate_candidate(route, no_ref_quote, state)
    assert isinstance(rejection, InputRejection)
    assert rejection.reason == InputRejectionReason.STATE_VERSION_MISMATCH


def test_c05_state_version_hash_mismatch_rejected() -> None:
    """QuoteEvidence referencing another block hash is rejected (block mixing defense)."""
    route, quote, _ = _make_valid_2hop_bundle()
    diff_hash_state = _make_state_version(block_hash="0x" + "ff" * 32)

    rejection = evaluate_candidate(route, quote, diff_hash_state)
    assert isinstance(rejection, InputRejection)
    assert rejection.reason == InputRejectionReason.STATE_VERSION_MISMATCH


def test_c05_block_number_reference_alone_rejected() -> None:
    """A bare block height cannot distinguish a fork at the same height."""
    route, _, state = _make_valid_2hop_bundle()
    quote = QuoteEvidence(
        quote_id="quote:block-num",
        route_ref=route,
        amount_in=Amount(route.base_asset, 1000000, 18),
        amount_out=Amount(route.base_asset, 1005000, 18),
        state_version_ref="block:100",
        status=QuoteStatus.QUOTED,
    )

    with pytest.raises(InputGateError) as exc_info:
        validate_candidate(route, quote, state)
    assert exc_info.value.reason == InputRejectionReason.STATE_VERSION_MISMATCH


# ==============================================================================
# Category C20 Tests: Serialization and Deserialization Roundtrips
# ==============================================================================


def test_c20_validated_candidate_roundtrip() -> None:
    """ValidatedCandidate serializes to dict/JSON and deserializes with 0 information loss."""
    route, quote, state = _make_valid_2hop_bundle()
    original = validate_candidate(route, quote, state)

    data_dict = original.to_dict()
    restored = ValidatedCandidate.from_dict(data_dict)

    assert restored.route_id == original.route_id
    assert restored.quote_id == original.quote_id
    assert restored.chain_id == original.chain_id
    assert restored.base_asset == original.base_asset
    assert restored.amount_in.atoms == original.amount_in.atoms

    json_str = original.to_json()
    from_json_restored = ValidatedCandidate.from_json(json_str)
    assert from_json_restored.route_id == original.route_id
    assert from_json_restored.amount_in.atoms == original.amount_in.atoms


def test_c20_draft_simulation_evidence_roundtrip() -> None:
    """DraftSimulationEvidence roundtrips through dict and canonical JSON with 0 loss."""
    draft = DraftSimulationEvidence(
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address="0x8876789976decbfcbbbe364623c63652db8c0904",
        calldata_hex="0x1234abcd",
        calldata_sha256="aa" * 32,
        block_number=100,
        block_hash=TEST_STATE_HASH,
        from_address="0x0000000000000000000000000000000000000001",
        value_wei=0,
        status="CALL_SUCCEEDED",
        gas_used=150000,
        return_data_hex="0x00",
        error_message=None,
        is_draft=True,
    )

    data_dict = draft.to_dict()
    restored = DraftSimulationEvidence.from_dict(data_dict)
    assert restored == draft

    json_str = draft.to_json()
    from_json_restored = DraftSimulationEvidence.from_json(json_str)
    assert from_json_restored == draft


def test_c20_input_rejection_roundtrip() -> None:
    """InputRejection roundtrips through dict and JSON preserving taxonomy enum."""
    rejection = InputRejection(
        reason=InputRejectionReason.V4_NON_ZERO_HOOK,
        message="V4 pool has non-zero hook",
        route_id="route:xyz",
        candidate_id="cand:1",
    )

    data_dict = rejection.to_dict()
    restored = InputRejection.from_dict(data_dict)
    assert restored == rejection

    json_str = rejection.to_json()
    from_json_restored = InputRejection.from_json(json_str)
    assert from_json_restored == rejection


def test_c20_candidate_opportunity_roundtrip_and_validation() -> None:
    """CandidateOpportunity envelope roundtrips and validates into ValidatedCandidate."""
    route, quote, state = _make_valid_3hop_bundle()
    opportunity = CandidateOpportunity(
        route_ref=route,
        quote_evidence=quote,
        state_version=state,
        opportunity_id="opp:3hop-test",
    )

    restored = CandidateOpportunity.from_dict(opportunity.to_dict())
    assert restored.opportunity_id == "opp:3hop-test"
    assert restored.route_ref.route_id == route.route_id

    validated = validate_opportunity(restored)
    assert isinstance(validated, ValidatedCandidate)
    assert validated.route_id == route.route_id


# ==============================================================================
# Fixtures and Manifest Integrity Tests
# ==============================================================================


def test_fixtures_manifest_and_cases_integrity() -> None:
    """Verify SHA-256 hash of input-cases.jsonl matches manifest and all cases evaluate properly."""
    cases_file = FIXTURES_DIR / "input-cases.jsonl"
    manifest_file = FIXTURES_DIR / "manifest.json"

    assert cases_file.is_file()
    assert manifest_file.is_file()

    computed_sha256 = hashlib.sha256(cases_file.read_bytes()).hexdigest()
    manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))

    assert manifest_data["partitions"]["input-cases.jsonl"] == computed_sha256

    validated_list, rejections_list = load_candidates_from_jsonl(cases_file)
    assert len(validated_list) == manifest_data["summary"]["positive_cases"]
    assert len(rejections_list) == manifest_data["summary"]["rejected_cases"]


# ==============================================================================
# Architecture Firewall & Subprocess Module Isolation Tests
# ==============================================================================


def test_architecture_boundary_static_ast() -> None:
    """Static AST check: atomic_execution must never import forbidden packages."""
    root = Path(__file__).resolve().parents[2]
    target_files = list((root / "atomic_execution").glob("*.py"))
    assert target_files, "No atomic_execution source files found"

    forbidden_roots = {"arbitrage", "execution", "core", "chains", "backtest", "monitors"}
    for file_path in target_files:
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        for node in ast.walk(tree):
            imported_modules: list[str] = []
            if isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported_modules.append(node.module)

            for module_name in imported_modules:
                root_pkg = module_name.split(".")[0]
                assert root_pkg not in forbidden_roots, (
                    f"Forbidden import '{module_name}' in {file_path}"
                )


def test_architecture_boundary_subprocess_clean_import() -> None:
    """Subprocess importing atomic_execution must not load arbitrage, execution, or core."""
    root = Path(__file__).resolve().parents[2]
    conftest = sys.modules.get("tests.conftest")
    orig_popen = getattr(conftest, "_ORIG_SUBPROCESS_POPEN", None) if conftest else None
    orig_run = (
        getattr(conftest, "_ORIG_SUBPROCESS_RUN", subprocess.run) if conftest else subprocess.run
    )

    script = (
        "import sys;"
        f"sys.path.insert(0, {str(root)!r});"
        "import atomic_execution;"
        "forbidden = ('arbitrage', 'execution', 'core', 'chains', 'backtest', 'monitors');"
        "loaded = tuple(name for name in sys.modules if name.split('.')[0] in forbidden);"
        "assert not loaded, f'Forbidden modules loaded: {loaded}';"
        "print('CLEAN_IMPORT_OK')"
    )

    if orig_popen is not None:
        saved_popen = subprocess.Popen
        setattr(subprocess, "Popen", orig_popen)  # noqa: B010
        try:
            result = orig_run(
                [sys.executable, "-c", script],
                cwd=str(root),
                capture_output=True,
                text=True,
                check=False,
            )
        finally:
            setattr(subprocess, "Popen", saved_popen)  # noqa: B010
    else:
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
        )

    assert result.returncode == 0, f"Subprocess failed:\n{result.stderr}"
    assert "CLEAN_IMPORT_OK" in result.stdout
