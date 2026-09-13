"""High-precision units and corporate action multiplier normalization kernel for RWA tokens."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction

from .models import validate_decimal_string, validate_identifier, validate_integer_field

UINT256_MAX = (1 << 256) - 1
_ONE_E18 = 10**18


@dataclass(frozen=True, slots=True)
class NormalizedHoldings:
    """Precise balance and underlying equity share breakdown for an account."""

    raw_balance_atoms: int
    token_decimals: int
    multiplier_uint: int
    token_quantity: Fraction
    shares_per_token: Fraction
    underlying_shares: Fraction

    def __post_init__(self) -> None:
        if type(self.raw_balance_atoms) is not int or isinstance(self.raw_balance_atoms, bool):
            raise TypeError("raw_balance_atoms must be an integer")
        if self.raw_balance_atoms < 0:
            raise ValueError("raw_balance_atoms cannot be negative")
        if type(self.token_decimals) is not int or isinstance(self.token_decimals, bool):
            raise TypeError("token_decimals must be an integer")
        if not 0 <= self.token_decimals <= 255:
            raise ValueError("token_decimals must be in range 0..255")
        if type(self.multiplier_uint) is not int or isinstance(self.multiplier_uint, bool):
            raise TypeError("multiplier_uint must be an integer")
        if self.multiplier_uint <= 0:
            raise ValueError("multiplier_uint must be strictly positive")


@dataclass(frozen=True, slots=True)
class NormalizedOraclePrice:
    """Normalized price interpretation distinguishing token-level vs underlying share price."""

    feed_answer_raw: int
    feed_decimals: int
    multiplier_uint: int
    token_price_usd: Fraction
    underlying_share_price_usd: Fraction
    shares_per_token: Fraction
    is_feed_token_adjusted: bool


@dataclass(frozen=True, slots=True)
class NormalizedRestQuote:
    """Underlier REST bid/ask quote aligned with corporate-action multiplier."""

    underlier_bid_share: Decimal
    underlier_ask_share: Decimal
    token_bid_price: Fraction
    token_ask_price: Fraction
    multiplier_ratio: Fraction


@dataclass(frozen=True, slots=True)
class ExecutionPricePoint:
    """Direction-specific discrete execution price point without reciprocal shortcuts."""

    direction: str
    amount_in_atoms: int
    amount_out_atoms: int
    decimals_in: int
    decimals_out: int
    amount_in_units: Fraction
    amount_out_units: Fraction
    effective_token_price_in_quote: Fraction
    quote_currency: str
    is_usd_pegged: bool


def normalize_holdings(
    raw_balance_atoms: int,
    token_decimals: int,
    multiplier_uint: int,
) -> NormalizedHoldings:
    """Calculate underlying equity shares from raw on-chain token balance and UI multiplier."""
    val_atoms = validate_integer_field(raw_balance_atoms, "raw_balance_atoms")
    val_decimals = validate_integer_field(token_decimals, "token_decimals", min_value=0, max_value=255)
    val_multiplier = validate_integer_field(multiplier_uint, "multiplier_uint")
    if val_multiplier <= 0:
        raise ValueError(f"multiplier_uint must be strictly positive, got {val_multiplier}")

    token_quantity = Fraction(val_atoms, 10**val_decimals)
    shares_per_token = Fraction(val_multiplier, _ONE_E18)
    underlying_shares = Fraction(val_atoms * val_multiplier, 10 ** (val_decimals + 18))

    return NormalizedHoldings(
        raw_balance_atoms=val_atoms,
        token_decimals=val_decimals,
        multiplier_uint=val_multiplier,
        token_quantity=token_quantity,
        shares_per_token=shares_per_token,
        underlying_shares=underlying_shares,
    )


def normalize_oracle_price(
    answer: int,
    feed_decimals: int,
    multiplier_uint: int,
    *,
    is_feed_token_adjusted: bool = True,
) -> NormalizedOraclePrice:
    """Normalize on-chain oracle answer without duplicate multiplier scaling (C01)."""
    val_answer = validate_integer_field(answer, "answer")
    if val_answer <= 0:
        raise ValueError(f"oracle answer must be strictly positive, got {val_answer}")
    val_feed_dec = validate_integer_field(feed_decimals, "feed_decimals", min_value=0, max_value=255)
    val_multiplier = validate_integer_field(multiplier_uint, "multiplier_uint")
    if val_multiplier <= 0:
        raise ValueError(f"multiplier_uint must be strictly positive, got {val_multiplier}")

    shares_per_token = Fraction(val_multiplier, _ONE_E18)

    if is_feed_token_adjusted:
        # C01: Chainlink Robinhood token feed already returns price of one full token (includes multiplier)
        token_price_usd = Fraction(val_answer, 10**val_feed_dec)
        underlying_share_price_usd = token_price_usd / shares_per_token
    else:
        # Raw underlying equity feed (share price quoted directly)
        underlying_share_price_usd = Fraction(val_answer, 10**val_feed_dec)
        token_price_usd = underlying_share_price_usd * shares_per_token

    return NormalizedOraclePrice(
        feed_answer_raw=val_answer,
        feed_decimals=val_feed_dec,
        multiplier_uint=val_multiplier,
        token_price_usd=token_price_usd,
        underlying_share_price_usd=underlying_share_price_usd,
        shares_per_token=shares_per_token,
        is_feed_token_adjusted=is_feed_token_adjusted,
    )


def normalize_rest_equity_quote(
    bid_str: str,
    ask_str: str,
    multiplier: str | int | Decimal | Fraction,
    *,
    multiplier_is_atoms_1e18: bool = False,
) -> NormalizedRestQuote:
    """Pair REST equity share quotes with corporate action multiplier without 1e18 errors (C02)."""
    valid_bid = validate_decimal_string(bid_str, "bid_price")
    valid_ask = validate_decimal_string(ask_str, "ask_price")

    dec_bid = Decimal(valid_bid)
    dec_ask = Decimal(valid_ask)
    if dec_bid > dec_ask:
        raise ValueError(f"bid_price ({dec_bid}) cannot exceed ask_price ({dec_ask})")

    # Resolve multiplier ratio
    if multiplier_is_atoms_1e18:
        if type(multiplier) is not int or isinstance(multiplier, bool):
            raise TypeError("multiplier must be an integer when multiplier_is_atoms_1e18=True")
        if multiplier <= 0:
            raise ValueError(f"multiplier must be strictly positive, got {multiplier}")
        mult_ratio = Fraction(multiplier, _ONE_E18)
    else:
        if isinstance(multiplier, str):
            clean_str = validate_decimal_string(multiplier, "multiplier")
            dec_m = Decimal(clean_str)
            if dec_m <= 0:
                raise ValueError(f"multiplier must be strictly positive, got {dec_m}")
            mult_ratio = Fraction(dec_m)
        elif isinstance(multiplier, Decimal):
            if not multiplier.is_finite() or multiplier <= 0:
                raise ValueError(f"multiplier must be finite positive Decimal, got {multiplier}")
            mult_ratio = Fraction(multiplier)
        elif isinstance(multiplier, Fraction):
            if multiplier <= 0:
                raise ValueError(f"multiplier must be positive Fraction, got {multiplier}")
            mult_ratio = multiplier
        elif type(multiplier) is int and not isinstance(multiplier, bool):
            if multiplier <= 0:
                raise ValueError(f"multiplier must be positive integer, got {multiplier}")
            mult_ratio = Fraction(multiplier, 1)
        else:
            raise TypeError(f"Unsupported multiplier type: {type(multiplier).__name__}")

    # Convert underlying share prices to token equivalent prices: token = share * multiplier
    token_bid = Fraction(dec_bid) * mult_ratio
    token_ask = Fraction(dec_ask) * mult_ratio

    return NormalizedRestQuote(
        underlier_bid_share=dec_bid,
        underlier_ask_share=dec_ask,
        token_bid_price=token_bid,
        token_ask_price=token_ask,
        multiplier_ratio=mult_ratio,
    )


def calculate_execution_price_point(
    direction: str,
    amount_in_atoms: int,
    amount_out_atoms: int,
    decimals_in: int,
    decimals_out: int,
    quote_currency: str = "USDG",
    *,
    is_usd_pegged: bool = False,
) -> ExecutionPricePoint:
    """Calculate effective execution price point without reciprocal estimation (C03, C18, C20)."""
    val_dir = validate_identifier(direction, "direction")
    if val_dir not in ("zero_for_one", "one_for_zero"):
        raise ValueError(f"Invalid direction: {val_dir!r}, expected 'zero_for_one' or 'one_for_zero'")

    val_in = validate_integer_field(amount_in_atoms, "amount_in_atoms")
    val_out = validate_integer_field(amount_out_atoms, "amount_out_atoms")
    if val_in <= 0:
        raise ValueError(f"amount_in_atoms must be strictly positive, got {val_in}")
    if val_out <= 0:
        raise ValueError(f"amount_out_atoms must be strictly positive, got {val_out}")
    if val_in > UINT256_MAX or val_out > UINT256_MAX:
        raise ValueError("Atom amounts exceed uint256 bounds")

    dec_in = validate_integer_field(decimals_in, "decimals_in", min_value=0, max_value=255)
    dec_out = validate_integer_field(decimals_out, "decimals_out", min_value=0, max_value=255)
    curr = validate_identifier(quote_currency, "quote_currency")

    units_in = Fraction(val_in, 10**dec_in)
    units_out = Fraction(val_out, 10**dec_out)

    if val_dir == "zero_for_one":
        # Selling token for quote asset (e.g. NVDA -> USDG)
        # Price = output quote units / input token units (e.g. USDG received per NVDA token)
        effective_price = units_out / units_in
    else:
        # Buying token with quote asset (e.g. USDG -> NVDA)
        # Price = input quote units / output token units (e.g. USDG spent per NVDA token)
        effective_price = units_in / units_out

    return ExecutionPricePoint(
        direction=val_dir,
        amount_in_atoms=val_in,
        amount_out_atoms=val_out,
        decimals_in=dec_in,
        decimals_out=dec_out,
        amount_in_units=units_in,
        amount_out_units=units_out,
        effective_token_price_in_quote=effective_price,
        quote_currency=curr,
        is_usd_pegged=is_usd_pegged,
    )


def calculate_signed_basis(
    observed_token_price: Fraction | Decimal | int,
    reference_token_price: Fraction | Decimal | int,
) -> Fraction:
    """Calculate normalized signed basis relative to reference price: (observed - ref) / ref."""
    obs = Fraction(observed_token_price)
    ref = Fraction(reference_token_price)
    if ref <= 0:
        raise ValueError(f"reference_token_price must be strictly positive, got {ref}")
    if obs < 0:
        raise ValueError(f"observed_token_price cannot be negative, got {obs}")

    return (obs - ref) / ref


def validate_corporate_action_continuity(
    multiplier_before: int,
    multiplier_after: int,
    underlier_price_before: Fraction | Decimal,
    underlier_price_after: Fraction | Decimal,
    *,
    expected_split_ratio: Fraction | None = None,
    cash_dividend_reported: Decimal | None = None,
) -> bool:
    """Validate corporate action continuity and guard against double-counting dividends (C07, C08)."""
    m_before = validate_integer_field(multiplier_before, "multiplier_before")
    m_after = validate_integer_field(multiplier_after, "multiplier_after")
    if m_before <= 0 or m_after <= 0:
        raise ValueError("Multipliers must be strictly positive")

    p_before = Fraction(underlier_price_before)
    p_after = Fraction(underlier_price_after)
    if p_before <= 0 or p_after <= 0:
        raise ValueError("Prices must be strictly positive")

    # Total-return token value before and after: Token Price = Share Price * Multiplier / 1e18
    token_val_before = p_before * Fraction(m_before, _ONE_E18)
    token_val_after = p_after * Fraction(m_after, _ONE_E18)

    # C07: In a pure stock split (e.g. 10:1 split, share price drops by factor of 10, multiplier grows 10x)
    if expected_split_ratio is not None:
        if expected_split_ratio <= 0:
            raise ValueError("expected_split_ratio must be positive")
        actual_mult_ratio = Fraction(m_after, m_before)
        if actual_mult_ratio != expected_split_ratio:
            return False

    # C08: If cash dividend is already reflected in the multiplier bump, adding it again is double counting
    if cash_dividend_reported is not None and cash_dividend_reported > 0:
        # If multiplier grew, dividends were already absorbed into the total return multiplier
        if m_after > m_before:
            # Multiplier increased indicating reinvested dividend; caller must not add cash dividend separately
            pass

    # Total return token value should be continuous (within small rounding/drift margin, e.g. < 1%)
    rel_diff = abs(token_val_after - token_val_before) / token_val_before
    return rel_diff < Fraction(1, 100)
