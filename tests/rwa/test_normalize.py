"""Comprehensive unit tests for W7 normalization kernel, covering C01-C08 and C20."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest

from rwa_research import (
    calculate_execution_price_point,
    calculate_signed_basis,
    normalize_holdings,
    normalize_oracle_price,
    normalize_rest_equity_quote,
    validate_corporate_action_continuity,
)


def test_c01_oracle_feed_already_token_adjusted_no_double_multiply() -> None:
    """C01: Chainlink Robinhood feed already includes multiplier.

    Token price must NOT be multiplied again by multiplier; underlying share price is derived by dividing.
    """
    # Answer: $125.50 with 8 decimals (12550000000), Multiplier: 1.05e18 (1.05 shares/token)
    answer = 12550000000
    feed_decimals = 8
    multiplier_uint = 1050000000000000000  # 1.05 * 10^18

    norm = normalize_oracle_price(
        answer=answer,
        feed_decimals=feed_decimals,
        multiplier_uint=multiplier_uint,
        is_feed_token_adjusted=True,
    )

    # Token price is directly feed price ($125.50)
    assert norm.token_price_usd == Fraction(1255, 10)  # 125.5
    # MUST NOT be double-multiplied (125.5 * 1.05 = 131.775 is WRONG)
    assert norm.token_price_usd != Fraction(131775, 1000)

    # Underlying share price = 125.5 / 1.05 = 125.5 * (100 / 105) = 2510 / 21
    expected_share_price = Fraction(1255, 10) / Fraction(105, 100)
    assert norm.underlying_share_price_usd == expected_share_price
    assert norm.shares_per_token == Fraction(105, 100)


def test_c02_rest_decimal_multiplier_no_1e18_double_division() -> None:
    """C02: REST /rhj/assets gives decimal string multiplier (e.g. '1.050000000000000000').

    It must NOT be divided by 1e18 a second time; REST /rhj/prices gives raw share price.
    """
    bid_str = "120.00"
    ask_str = "120.50"
    multiplier_str = "1.050000000000000000"

    norm = normalize_rest_equity_quote(
        bid_str=bid_str,
        ask_str=ask_str,
        multiplier=multiplier_str,
        multiplier_is_atoms_1e18=False,
    )

    # Share prices preserved
    assert norm.underlier_bid_share == Decimal("120.00")
    assert norm.underlier_ask_share == Decimal("120.50")

    # Token prices = share * multiplier
    # Bid: 120 * 1.05 = 126.00
    # Ask: 120.50 * 1.05 = 126.525
    assert norm.token_bid_price == Fraction(126, 1)
    assert norm.token_ask_price == Fraction(126525, 1000)
    # Must NOT have 1e-18 scaling error
    assert norm.token_bid_price > Fraction(100, 1)

    # When multiplier is provided as 1e18 uint atoms with flag
    norm_uint = normalize_rest_equity_quote(
        bid_str=bid_str,
        ask_str=ask_str,
        multiplier=1050000000000000000,
        multiplier_is_atoms_1e18=True,
    )
    assert norm_uint.token_bid_price == norm.token_bid_price
    assert norm_uint.token_ask_price == norm.token_ask_price


def test_c03_cross_decimal_and_independent_directions() -> None:
    """C03: Token (18 dec) to USDG (6 dec) cross-decimal calculation without reciprocal shortcuts."""
    # 1. Selling token (zero_for_one): In: 1 token (10^18), Out: 125 USDG (125 * 10^6)
    pt_sell = calculate_execution_price_point(
        direction="zero_for_one",
        amount_in_atoms=10**18,
        amount_out_atoms=125 * 10**6,
        decimals_in=18,
        decimals_out=6,
        quote_currency="USDG",
    )
    assert pt_sell.amount_in_units == Fraction(1, 1)
    assert pt_sell.amount_out_units == Fraction(125, 1)
    # Sell price = 125 USDG / 1 Token = 125
    assert pt_sell.effective_token_price_in_quote == Fraction(125, 1)

    # 2. Buying token (one_for_zero): In: 126 USDG (126 * 10^6), Out: 1 token (10^18)
    pt_buy = calculate_execution_price_point(
        direction="one_for_zero",
        amount_in_atoms=126 * 10**6,
        amount_out_atoms=10**18,
        decimals_in=6,
        decimals_out=18,
        quote_currency="USDG",
    )
    assert pt_buy.amount_in_units == Fraction(126, 1)
    assert pt_buy.amount_out_units == Fraction(1, 1)
    # Buy price = 126 USDG / 1 Token = 126
    assert pt_buy.effective_token_price_in_quote == Fraction(126, 1)

    # MUST NOT be reciprocal: 126 != 1 / 125
    assert pt_buy.effective_token_price_in_quote != Fraction(1, pt_sell.effective_token_price_in_quote)


def test_c04_rejection_of_non_positive_multipliers_and_answers() -> None:
    """C04: Reject multiplier <= 0 or oracle answer <= 0 with fail-closed ValueError."""
    # Multiplier = 0
    with pytest.raises(ValueError, match="multiplier_uint must be strictly positive"):
        normalize_holdings(raw_balance_atoms=1000, token_decimals=18, multiplier_uint=0)

    # Negative multiplier
    with pytest.raises(ValueError, match="multiplier_uint must be non-negative"):
        normalize_holdings(raw_balance_atoms=1000, token_decimals=18, multiplier_uint=-100)

    # Oracle answer = 0
    with pytest.raises(ValueError, match="oracle answer must be strictly positive"):
        normalize_oracle_price(answer=0, feed_decimals=8, multiplier_uint=10**18)

    # Oracle answer < 0
    with pytest.raises(ValueError, match="answer must be non-negative"):
        normalize_oracle_price(answer=-10, feed_decimals=8, multiplier_uint=10**18)


def test_c05_rejection_of_boolean_float_nan_atoms() -> None:
    """C05: Reject boolean and float masquerading as atoms or integers."""
    with pytest.raises(TypeError, match="raw_balance_atoms must be an integer, got bool"):
        normalize_holdings(raw_balance_atoms=True, token_decimals=18, multiplier_uint=10**18)

    with pytest.raises(TypeError, match="raw_balance_atoms must be an integer, got float"):
        normalize_holdings(raw_balance_atoms=100.5, token_decimals=18, multiplier_uint=10**18)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="multiplier_uint must be an integer, got bool"):
        normalize_holdings(raw_balance_atoms=1000, token_decimals=18, multiplier_uint=False)


def test_c06_precision_large_uint_and_indivisible_fractions() -> None:
    """C06: Lossless precision for large uint256 atoms and indivisible fractions."""
    large_atoms = (1 << 256) - 1
    # 3 shares per token
    mult = 3 * 10**18
    holdings = normalize_holdings(large_atoms, 18, mult)

    assert holdings.raw_balance_atoms == large_atoms
    assert holdings.shares_per_token == Fraction(3, 1)
    assert holdings.underlying_shares == Fraction(large_atoms * 3, 10**18)

    # 1 atom holdings
    one_atom_holdings = normalize_holdings(1, 18, 10**18)
    assert one_atom_holdings.token_quantity == Fraction(1, 10**18)
    assert one_atom_holdings.underlying_shares == Fraction(1, 10**18)

    # Indivisible fraction in basis calculation
    # e.g., observed = $100/3, reference = $99/7
    basis = calculate_signed_basis(Fraction(100, 3), Fraction(99, 7))
    expected_basis = (Fraction(100, 3) - Fraction(99, 7)) / Fraction(99, 7)
    assert basis == expected_basis
    assert isinstance(basis, Fraction)


def test_c07_corporate_action_split_continuity() -> None:
    """C07: Stock split (10:1) preserves total-return token value.

    Stock price drops 90% (from $200 to $20), multiplier expands 10x (1.0 to 10.0).
    Token price remains continuous at $200; stock price drop is NOT counted as token loss.
    """
    m_before = 1 * 10**18  # 1.0
    m_after = 10 * 10**18  # 10.0
    p_before = Decimal("200.00")
    p_after = Decimal("20.00")

    continuous = validate_corporate_action_continuity(
        multiplier_before=m_before,
        multiplier_after=m_after,
        underlier_price_before=p_before,
        underlier_price_after=p_after,
        expected_split_ratio=Fraction(10, 1),
    )
    assert continuous is True

    # Token value before: 200 * 1.0 = 200
    # Token value after: 20 * 10.0 = 200
    token_val_before = Fraction(p_before) * Fraction(m_before, 10**18)
    token_val_after = Fraction(p_after) * Fraction(m_after, 10**18)
    assert token_val_before == token_val_after == Fraction(200, 1)


def test_c08_dividend_reinvestment_continuity() -> None:
    """C08: Small multiplier bump reflects reinvested dividend without double-counting."""
    m_before = 1000000000000000000  # 1.000
    m_after = 1008000000000000000  # 1.008 (+0.8% reinvested dividend)
    p_before = Decimal("200.00")
    p_after = Decimal("200.00")

    # If cash dividend of $1.60 was already absorbed by multiplier, caller is protected
    continuous = validate_corporate_action_continuity(
        multiplier_before=m_before,
        multiplier_after=m_after,
        underlier_price_before=p_before,
        underlier_price_after=p_after,
        cash_dividend_reported=Decimal("1.60"),
    )
    assert continuous is True


def test_c20_quote_currency_not_hardcoded_to_usd() -> None:
    """C20: When quote asset is USDG or WETH, preserve native units and do not assume 1:1 USD."""
    pt = calculate_execution_price_point(
        direction="zero_for_one",
        amount_in_atoms=10**18,
        amount_out_atoms=125 * 10**6,
        decimals_in=18,
        decimals_out=6,
        quote_currency="USDG",
        is_usd_pegged=False,
    )

    assert pt.quote_currency == "USDG"
    assert pt.is_usd_pegged is False
    assert pt.effective_token_price_in_quote == Fraction(125, 1)
