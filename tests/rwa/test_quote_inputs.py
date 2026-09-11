"""Unit tests for discrete quote inputs, pool identity isolation, and error classification (C16-C19, C22-C23)."""

from __future__ import annotations

from fractions import Fraction

import pytest

from arbitrage_contracts import AssetRef, PoolKey, TokenKey
from rwa_research import (
    AmountQuotePoint,
    assemble_bidirectional_quotes,
    parse_quote_point_from_dict,
    validate_quote_point,
)


@pytest.fixture
def token_nvda() -> TokenKey:
    return TokenKey(4663, "0x1111111111111111111111111111111111111111")


@pytest.fixture
def token_usdg() -> TokenKey:
    return TokenKey(4663, "0x5fc5360d0400a0fd4f2af552add042d716f1d168")


def test_c16_pool_identity_isolation_different_manager_same_id(
    token_nvda: TokenKey, token_usdg: TokenKey
) -> None:
    """C16: Pools with identical pool_id on different managers/venues must NOT be merged."""
    common_pool_id = "0x" + "aa" * 32
    pk1 = PoolKey(
        chain_id=4663,
        protocol_id="uniswap_v4",
        venue_kind="manager",
        venue_address="0x2222222222222222222222222222222222222222",
        pool_id_kind="bytes32",
        pool_id=common_pool_id,
    )
    pk2 = PoolKey(
        chain_id=4663,
        protocol_id="uniswap_v4",
        venue_kind="manager",
        venue_address="0x3333333333333333333333333333333333333333",  # Different manager
        pool_id_kind="bytes32",
        pool_id=common_pool_id,
    )

    ain = AssetRef.erc20(token_nvda)
    aout = AssetRef.erc20(token_usdg)

    q1 = AmountQuotePoint(
        pool_key=pk1,
        direction="zero_for_one",
        asset_in=ain,
        asset_out=aout,
        amount_in_atoms=10**18,
        amount_out_atoms=125 * 10**6,
        quote_id="q1",
    )
    q2 = AmountQuotePoint(
        pool_key=pk2,
        direction="zero_for_one",
        asset_in=ain,
        asset_out=aout,
        amount_in_atoms=10**18,
        amount_out_atoms=128 * 10**6,
        quote_id="q2",
    )

    pairs = assemble_bidirectional_quotes([q1, q2])
    assert len(pairs) == 2
    assert pk1 in pairs
    assert pk2 in pairs
    assert pairs[pk1].zero_for_one_quote == q1
    assert pairs[pk2].zero_for_one_quote == q2


def test_c17_bidirectional_requirement_no_reciprocal_falsification(
    token_nvda: TokenKey, token_usdg: TokenKey
) -> None:
    """C17: A pool with only zero_for_one quote cannot derive one_for_zero via reciprocal math."""
    pk = PoolKey(
        chain_id=4663,
        protocol_id="uniswap_v4",
        venue_kind="manager",
        venue_address="0x2222222222222222222222222222222222222222",
        pool_id_kind="bytes32",
        pool_id="0x" + "bb" * 32,
    )
    ain = AssetRef.erc20(token_nvda)
    aout = AssetRef.erc20(token_usdg)

    q_sell = AmountQuotePoint(
        pool_key=pk,
        direction="zero_for_one",
        asset_in=ain,
        asset_out=aout,
        amount_in_atoms=10**18,
        amount_out_atoms=125 * 10**6,
        quote_id="q_sell_only",
    )

    pairs = assemble_bidirectional_quotes([q_sell])
    pair = pairs[pk]

    assert pair.is_bidirectional_valid is False
    assert pair.one_for_zero_quote is None
    assert any("reciprocal" in r for r in pair.reasons)


def test_c18_cross_decimal_independent_price_points(
    token_nvda: TokenKey, token_usdg: TokenKey
) -> None:
    """C18: Precise multi-decimal calculation for both directions on the same pool."""
    pk = PoolKey(
        chain_id=4663,
        protocol_id="uniswap_v4",
        venue_kind="manager",
        venue_address="0x2222222222222222222222222222222222222222",
        pool_id_kind="bytes32",
        pool_id="0x" + "cc" * 32,
    )
    ain = AssetRef.erc20(token_nvda)
    aout = AssetRef.erc20(token_usdg)

    # 1 token (18 dec) -> 125 USDG (6 dec)
    q_sell = AmountQuotePoint(
        pool_key=pk,
        direction="zero_for_one",
        asset_in=ain,
        asset_out=aout,
        amount_in_atoms=10**18,
        amount_out_atoms=125 * 10**6,
        quote_id="q_sell",
    )
    # 126 USDG (6 dec) -> 1 token (18 dec)
    q_buy = AmountQuotePoint(
        pool_key=pk,
        direction="one_for_zero",
        asset_in=aout,
        asset_out=ain,
        amount_in_atoms=126 * 10**6,
        amount_out_atoms=10**18,
        quote_id="q_buy",
    )

    res_sell = validate_quote_point(q_sell, token_decimals=18, quote_decimals=6)
    assert res_sell.is_valid is True
    assert res_sell.effective_price_point is not None
    assert res_sell.effective_price_point.effective_token_price_in_quote == Fraction(125, 1)

    res_buy = validate_quote_point(q_buy, token_decimals=18, quote_decimals=6)
    assert res_buy.is_valid is True
    assert res_buy.effective_price_point is not None
    assert res_buy.effective_price_point.effective_token_price_in_quote == Fraction(126, 1)

    pairs = assemble_bidirectional_quotes([q_sell, q_buy])
    assert pairs[pk].is_bidirectional_valid is True


def test_c19_state_version_anchor_mismatch_rejection(
    token_nvda: TokenKey, token_usdg: TokenKey
) -> None:
    """C19: Reject quote if its state_version_ref does not match expected state anchor."""
    pk = PoolKey(
        chain_id=4663,
        protocol_id="uniswap_v4",
        venue_kind="manager",
        venue_address="0x2222222222222222222222222222222222222222",
        pool_id_kind="bytes32",
        pool_id="0x" + "dd" * 32,
    )
    ain = AssetRef.erc20(token_nvda)
    aout = AssetRef.erc20(token_usdg)

    q = AmountQuotePoint(
        pool_key=pk,
        direction="zero_for_one",
        asset_in=ain,
        asset_out=aout,
        amount_in_atoms=10**18,
        amount_out_atoms=125 * 10**6,
        quote_id="q_state",
        state_version_ref="sha256:1111111111111111111111111111111111111111111111111111111111111111",
    )

    res = validate_quote_point(q, expected_state_version_ref="sha256:9999999999999999999999999999999999999999999999999999999999999999")
    assert res.is_valid is False
    assert res.status == "state_version_mismatch"


def test_c22_error_classification_revert_timeout_zero_output(
    token_nvda: TokenKey, token_usdg: TokenKey
) -> None:
    """C22: Revert or failure produces amount_out=None and specific status, never zero profit."""
    pk = PoolKey(
        chain_id=4663,
        protocol_id="uniswap_v4",
        venue_kind="manager",
        venue_address="0x2222222222222222222222222222222222222222",
        pool_id_kind="bytes32",
        pool_id="0x" + "ee" * 32,
    )
    ain = AssetRef.erc20(token_nvda)
    aout = AssetRef.erc20(token_usdg)

    # 1. Revert with amount_out_atoms=None
    q_revert = AmountQuotePoint(
        pool_key=pk,
        direction="zero_for_one",
        asset_in=ain,
        asset_out=aout,
        amount_in_atoms=10**18,
        amount_out_atoms=None,
        quote_id="q_revert",
        status="reverted",
        error="NotEnoughLiquidity()",
    )
    res_revert = validate_quote_point(q_revert)
    assert res_revert.is_valid is False
    assert res_revert.status == "reverted"

    # 2. Tampered quote (revert status but amount_out is not None) -> fail-closed error
    with pytest.raises(ValueError, match="must have amount_out_atoms=None"):
        AmountQuotePoint(
            pool_key=pk,
            direction="zero_for_one",
            asset_in=ain,
            asset_out=aout,
            amount_in_atoms=10**18,
            amount_out_atoms=100,  # invalid for reverted
            quote_id="q_bad",
            status="reverted",
        )

    # 3. Quoted with zero output atoms -> marked zero_output
    q_zero = AmountQuotePoint(
        pool_key=pk,
        direction="zero_for_one",
        asset_in=ain,
        asset_out=aout,
        amount_in_atoms=10**18,
        amount_out_atoms=0,
        quote_id="q_zero",
        status="quoted",
    )
    res_zero = validate_quote_point(q_zero)
    assert res_zero.is_valid is False
    assert res_zero.status == "zero_output"


def test_c23_gas_evidence_kind_unauthorized_guard(
    token_nvda: TokenKey, token_usdg: TokenKey
) -> None:
    """C23: Single-hop quote cannot claim atomic_simulation or confirmed_actual gas."""
    pk = PoolKey(
        chain_id=4663,
        protocol_id="uniswap_v4",
        venue_kind="manager",
        venue_address="0x2222222222222222222222222222222222222222",
        pool_id_kind="bytes32",
        pool_id="0x" + "ff" * 32,
    )
    ain = AssetRef.erc20(token_nvda)
    aout = AssetRef.erc20(token_usdg)

    q = AmountQuotePoint(
        pool_key=pk,
        direction="zero_for_one",
        asset_in=ain,
        asset_out=aout,
        amount_in_atoms=10**18,
        amount_out_atoms=125 * 10**6,
        quote_id="q_gas",
        gas_evidence_kind="atomic_simulation",  # Unauthorized upgrade
    )

    res = validate_quote_point(q)
    assert res.is_valid is False
    assert res.status == "unauthorized_gas_evidence"
