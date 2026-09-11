"""Tests covering Arc asset and market eligibility evaluation (C18-C22)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arc_readiness.eligibility import (
    ARC_CANONICAL_EURC_ADDRESS,
    ARC_CANONICAL_FX_ESCROW_ADDRESS,
    ARC_CANONICAL_USYC_ADDRESS,
    evaluate_asset_eligibility,
    evaluate_market_eligibility,
)
from arc_readiness.errors import ArcValidationError
from arc_readiness.models import ARC_USDC_ERC20_ADDRESS

POOL_ADDR = "0x" + "aa" * 20


# ==============================================================================
# C18: Distinct Addresses for Identical Symbols & Circular Domain Rejection
# ==============================================================================


def test_c18_identical_symbols_different_addresses() -> None:
    """C18: Tokens sharing identical symbols with distinct addresses are preserved separately."""
    legit_token = evaluate_asset_eligibility(
        asset_id="arc:cool_legit",
        symbol="COOL",
        decimals=6,
        interface_kind="erc20",
        contract_address="0xacd3c4ae00ddac5cd857d83424cf4eb85f32ad8f",
    )
    fake_token = evaluate_asset_eligibility(
        asset_id="arc:cool_fake",
        symbol="COOL",
        decimals=18,
        interface_kind="erc20",
        contract_address="0x9999999999999999999999999999999999999999",
    )

    assert legit_token.asset_id != fake_token.asset_id
    assert legit_token.contract_address != fake_token.contract_address
    assert legit_token.decimals != fake_token.decimals


def test_c18_circular_usdc_domain_pair_rejection() -> None:
    """C18: Pairing native USDC against ERC-20 USDC fails closed."""
    native_usdc = evaluate_asset_eligibility(
        asset_id="arc:usdc_native",
        symbol="USDC",
        decimals=18,
        interface_kind="native",
    )
    erc20_usdc = evaluate_asset_eligibility(
        asset_id="arc:usdc_erc20",
        symbol="USDC",
        decimals=6,
        interface_kind="erc20",
        contract_address=ARC_USDC_ERC20_ADDRESS,
    )

    assert native_usdc.is_usdc_native_domain is True
    assert erc20_usdc.is_usdc_native_domain is True

    # Pairing them in a pool must be rejected
    with pytest.raises(ArcValidationError, match="circular pair forbidden"):
        evaluate_market_eligibility(
            market_id="invalid_circular_pool",
            protocol_id="uniswap_v3",
            pool_address=POOL_ADDR,
            base_asset=native_usdc,
            quote_asset=erc20_usdc,
        )


# ==============================================================================
# C19: Code Size and Deployment Verification
# ==============================================================================


def test_c19_empty_code_size_rejection() -> None:
    """C19: Non-precompile contracts with 0 code size are rejected."""
    empty_token = evaluate_asset_eligibility(
        asset_id="arc:empty_token",
        symbol="EMPTY",
        decimals=18,
        interface_kind="erc20",
        contract_address="0x" + "44" * 20,
        code_size=0,
    )
    assert empty_token.review_status == "rejected"
    assert "DEPLOYMENT_CODE_EMPTY" in empty_token.reasons

    # Precompile with 0 code size is NOT rejected
    precompile_asset = evaluate_asset_eligibility(
        asset_id="arc:precompile_usdc",
        symbol="USDC",
        decimals=6,
        interface_kind="erc20",
        contract_address=ARC_USDC_ERC20_ADDRESS,
        code_size=0,
    )
    assert precompile_asset.review_status == "verified"


# ==============================================================================
# C20: Unknown Dynamic Hooks and Deprecated Markets
# ==============================================================================


def test_c20_unverified_hooks_and_deprecated_markets() -> None:
    """C20: Unverified hooks drop quote capability to unknown; deprecated markets are unsupported."""
    usdc = evaluate_asset_eligibility(
        asset_id="arc:usdc",
        symbol="USDC",
        decimals=6,
        interface_kind="erc20",
        contract_address=ARC_USDC_ERC20_ADDRESS,
    )
    eurc = evaluate_asset_eligibility(
        asset_id="arc:eurc",
        symbol="EURC",
        decimals=6,
        interface_kind="erc20",
        contract_address=ARC_CANONICAL_EURC_ADDRESS,
    )

    # Market with unverified hooks
    unverified_market = evaluate_market_eligibility(
        market_id="v4_hook_market",
        protocol_id="uniswap_v4",
        pool_address=POOL_ADDR,
        base_asset=usdc,
        quote_asset=eurc,
        hooks_verified=False,
    )
    assert unverified_market.can_quote == "unknown"
    assert "UNVERIFIED_DYNAMIC_HOOKS" in unverified_market.reasons

    # Deprecated market
    deprecated_market = evaluate_market_eligibility(
        market_id="old_venue_market",
        protocol_id="legacy_swap",
        pool_address=POOL_ADDR,
        base_asset=usdc,
        quote_asset=eurc,
        is_deprecated_venue=True,
    )
    assert deprecated_market.can_quote == "unsupported"
    assert "DEPRECATED_MARKET_VENUE" in deprecated_market.reasons


# ==============================================================================
# C21: EURC (Euro) & USYC (Restricted Institutional Fund Share)
# ==============================================================================


def test_c21_eurc_and_usyc_qualification() -> None:
    """C21: EURC is recognized as Euro currency; USYC is flagged as restricted entitlement asset."""
    eurc = evaluate_asset_eligibility(
        asset_id="arc:eurc",
        symbol="EURC",
        decimals=6,
        interface_kind="erc20",
        contract_address=ARC_CANONICAL_EURC_ADDRESS,
    )
    assert eurc.is_usdc_native_domain is False
    assert "canonical_eurc_euro_stablecoin" in eurc.reasons

    # USYC without allowlist
    usyc_unapproved = evaluate_asset_eligibility(
        asset_id="arc:usyc",
        symbol="USYC",
        decimals=6,
        interface_kind="erc20",
        contract_address=ARC_CANONICAL_USYC_ADDRESS,
        has_entitlements=False,
    )
    assert usyc_unapproved.review_status == "provisional"
    assert "USYC_RESTRICTED_INVESTMENT_ALLOWLIST_REQUIRED" in usyc_unapproved.reasons

    # USYC with allowlist
    usyc_approved = evaluate_asset_eligibility(
        asset_id="arc:usyc",
        symbol="USYC",
        decimals=6,
        interface_kind="erc20",
        contract_address=ARC_CANONICAL_USYC_ADDRESS,
        has_entitlements=True,
    )
    assert usyc_approved.review_status == "verified"


# ==============================================================================
# C22: StableFX Permissioned RFQ Classification
# ==============================================================================


def test_c22_stablefx_is_permissioned_rfq_not_atomic_amm() -> None:
    """C22: StableFX is classified as permissioned RFQ and disallowed from atomic execution."""
    usdc = evaluate_asset_eligibility(
        asset_id="arc:usdc",
        symbol="USDC",
        decimals=6,
        interface_kind="erc20",
        contract_address=ARC_USDC_ERC20_ADDRESS,
    )
    eurc = evaluate_asset_eligibility(
        asset_id="arc:eurc",
        symbol="EURC",
        decimals=6,
        interface_kind="erc20",
        contract_address=ARC_CANONICAL_EURC_ADDRESS,
    )

    stablefx_market = evaluate_market_eligibility(
        market_id="stablefx:usdc_eurc",
        protocol_id="stablefx",
        pool_address=ARC_CANONICAL_FX_ESCROW_ADDRESS,
        base_asset=usdc,
        quote_asset=eurc,
    )

    assert stablefx_market.can_atomic_execute == "unsupported"
    assert stablefx_market.can_quote == "unknown"  # Requires offchain API auth
    assert "permissioned_rfq_not_atomic_amm" in stablefx_market.reasons


# ==============================================================================
# Fixture-driven Verification
# ==============================================================================


def test_fixture_driven_catalog_vectors() -> None:
    """Verify test cases from catalog_vectors.json fixture."""
    fixture_path = (
        Path(__file__).resolve().parents[1] / "fixtures" / "arc_readiness" / "catalog_vectors.json"
    )
    data = json.loads(fixture_path.read_text(encoding="utf-8"))

    # C18 distinct tokens
    c18 = next(v for v in data["vectors"] if v["id"] == "C18_token_distinct_addresses")
    t1 = evaluate_asset_eligibility(
        asset_id=c18["tokens"][0]["asset_id"],
        symbol=c18["tokens"][0]["symbol"],
        decimals=c18["tokens"][0]["decimals"],
        interface_kind="erc20",
        contract_address=c18["tokens"][0]["contract_address"],
    )
    t2 = evaluate_asset_eligibility(
        asset_id=c18["tokens"][1]["asset_id"],
        symbol=c18["tokens"][1]["symbol"],
        decimals=c18["tokens"][1]["decimals"],
        interface_kind="erc20",
        contract_address=c18["tokens"][1]["contract_address"],
    )
    assert t1.contract_address != t2.contract_address
