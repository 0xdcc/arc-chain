"""Unit tests for explicit fee units and canonical DEX fee conversions.

Validates compliance with:
- FeeUnitError subclassing ValueError
- Immutable FeeValue with raw/kind/fee_bps
- Original 5 numerical assertions and 4 raises
- Manual conversion assertions (100 bps -> 10000 ppm, 100 ppm -> 1 bps)
- Rejection of 'auto' unit guessing, bool, NaN, Inf, negative, and uint24 overflows
- Delegation to and from FeeModel
- Strict sub-ppm fractional precision rejection (e.g. 1.00000000000000000000000000001)
- Context precision independence (accurate and unrounded under prec=1, prec=2, etc.)
- Strict uint24 lower/upper bounds orthogonality
- Prevention of whitening unknown FeeModel semantics
"""

from __future__ import annotations

import decimal
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from arbitrage_contracts.identity import FeeModel
from research.market_data.fee_units import (
    CANONICAL_UNISWAP_DEXES,
    DYNAMIC_FEE_FLAG,
    MAX_FEE_PPM,
    MAX_UINT24,
    FeeUnitError,
    FeeValue,
    bps_to_raw,
    convert_fee,
    fee_to_bps,
    fee_to_raw,
    raw_to_bps,
    uniswap_fee,
)


def test_fee_unit_error_is_value_error() -> None:
    """FeeUnitError must inherit from ValueError for standard exception hierarchies."""
    assert issubclass(FeeUnitError, ValueError)
    err = FeeUnitError("test error")
    assert isinstance(err, ValueError)


# --- 1. Original 5 numerical assertions and 4 raises preserved exactly ---


def test_original_five_numerical_assertions() -> None:
    """Preserve exactly the 5 numerical assertions from tests/test_remaining_protocols.py."""
    # 1. uniswap_fee(100, "uniswap-v3").fee_bps == Decimal(1)
    assert uniswap_fee(100, "uniswap-v3").fee_bps == Decimal(1)
    # 2. uniswap_fee(3000, "uniswap-v3").fee_bps == Decimal(30)
    assert uniswap_fee(3000, "uniswap-v3").fee_bps == Decimal(30)
    # 3. uniswap_fee(0, "uniswap-v3").fee_bps == 0
    assert uniswap_fee(0, "uniswap-v3").fee_bps == 0
    # 4. bps_to_raw(100) == 10000
    assert bps_to_raw(100) == 10000
    # 5. bps_to_raw(0) == 0
    assert bps_to_raw(0) == 0


def test_original_four_raises_preserved() -> None:
    """Preserve exactly the 4 raises obligations (3 in remaining_protocols + 1 manual auto)."""
    # Raise 1: non-canonical dex identifier rejected
    with pytest.raises(FeeUnitError, match="Unsupported or non-canonical dex"):
        uniswap_fee(100, "giga-v3")

    # Raise 2: V4 dynamic fee pool raises when attempting to read static fee_bps
    with pytest.raises(FeeUnitError, match="Dynamic fee pool .* does not have a static fee_bps"):
        _ = uniswap_fee(0x800000, "uniswap-v4").fee_bps

    # Raise 3: bps value not converting to integer ppm rejected
    with pytest.raises(FeeUnitError, match="does not convert to an exact integer ppm"):
        bps_to_raw(Decimal(".0001"))

    # Raise 4: manual unit 'auto' guessing rejected
    with pytest.raises(FeeUnitError, match="Automatic fee unit guessing .* strictly prohibited"):
        convert_fee(100, fee_unit="auto", target_unit="ppm")


# --- 2. Manual unit conversion obligations (100 bps -> 10000 ppm, 100 ppm -> 1 bps) ---


def test_manual_unit_conversions() -> None:
    """Validate 100 bps -> 10000 ppm and 100 ppm -> 1 bps without guessing."""
    # 100 bps -> 10000 ppm
    assert convert_fee(100, fee_unit="bps", target_unit="ppm") == 10000
    assert fee_to_raw(100, fee_unit="bps") == 10000

    # 100 ppm -> 1 bps
    assert convert_fee(100, fee_unit="ppm", target_unit="bps") == Decimal(1)
    assert fee_to_bps(100, fee_unit="ppm") == Decimal(1)


# --- 3. Immutable FeeValue behavior ---


def test_fee_value_immutability_and_attributes() -> None:
    """FeeValue must be frozen and immutable."""
    fv = FeeValue(raw=3000, kind="static")
    assert fv.raw == 3000
    assert fv.kind == "static"
    assert fv.fee_bps == Decimal(30)
    assert fv.fee_ppm == 3000
    assert not fv.is_dynamic

    with pytest.raises(FrozenInstanceError):
        fv.raw = 500  # type: ignore[misc]

    with pytest.raises(FrozenInstanceError):
        fv.kind = "dynamic"  # type: ignore[misc]


def test_fee_value_dynamic_behavior() -> None:
    """Dynamic FeeValue raises on fee_bps and fee_ppm access."""
    dynamic_fv = FeeValue(raw=DYNAMIC_FEE_FLAG, kind="dynamic")
    assert dynamic_fv.is_dynamic
    assert dynamic_fv.kind == "dynamic"
    assert dynamic_fv.raw == DYNAMIC_FEE_FLAG

    with pytest.raises(FeeUnitError, match="Dynamic fee pool"):
        _ = dynamic_fv.fee_bps

    with pytest.raises(FeeUnitError, match="Dynamic fee pool"):
        _ = dynamic_fv.fee_ppm


def test_fee_value_model_delegation() -> None:
    """FeeValue delegates to FeeModel and can be constructed from FeeModel."""
    fv_static = FeeValue(raw=500, kind="static")
    model = fv_static.to_fee_model()
    assert isinstance(model, FeeModel)
    assert model.kind == "static"
    assert model.raw_value == 500
    assert model.unit == "ppm"

    reconstructed = FeeValue.from_fee_model(model)
    assert reconstructed == fv_static

    fv_dyn = FeeValue(raw=DYNAMIC_FEE_FLAG | 100, kind="dynamic")
    dyn_model = fv_dyn.to_fee_model()
    assert dyn_model.kind == "dynamic"
    assert dyn_model.raw_value == DYNAMIC_FEE_FLAG | 100
    assert FeeValue.from_fee_model(dyn_model) == fv_dyn


# --- 4. Canonical Uniswap fee validation ---


@pytest.mark.parametrize("dex", sorted(CANONICAL_UNISWAP_DEXES))
def test_canonical_uniswap_static_tiers(dex: str) -> None:
    """Canonical V3 and V4 static tiers map accurately to basis points."""
    assert uniswap_fee(0, dex).fee_bps == 0
    assert uniswap_fee(100, dex).fee_bps == Decimal(1)
    assert uniswap_fee(500, dex).fee_bps == Decimal(5)
    assert uniswap_fee(3000, dex).fee_bps == Decimal(30)
    assert uniswap_fee(10000, dex).fee_bps == Decimal(100)


@pytest.mark.parametrize(
    "invalid_dex",
    ["giga-v3", "ramses-v3", "sushiswap-v3", "pancakeswap-v3", "uniswap-v2", "aerodrome", ""],
)
def test_reject_non_canonical_dex(invalid_dex: str) -> None:
    """Non-canonical DEX strings must fail closed immediately."""
    with pytest.raises(FeeUnitError, match="Unsupported or non-canonical dex"):
        uniswap_fee(500, invalid_dex)


def test_uniswap_v3_rejects_dynamic_bit() -> None:
    """Uniswap V3 does not support dynamic fees; bit 0x800000 must raise."""
    with pytest.raises(FeeUnitError, match="Uniswap V3 does not support dynamic fee flag"):
        uniswap_fee(DYNAMIC_FEE_FLAG, "uniswap-v3")


def test_uniswap_v4_supports_dynamic_bit() -> None:
    """Uniswap V4 recognises bit 0x800000 as dynamic fee."""
    fv = uniswap_fee(DYNAMIC_FEE_FLAG | 250, "uniswap-v4")
    assert fv.is_dynamic
    assert fv.kind == "dynamic"
    with pytest.raises(FeeUnitError, match="Dynamic fee pool"):
        _ = fv.fee_bps


# --- 5. Strict rejection of invalid types, NaN, Inf, negative, out-of-bounds ---


@pytest.mark.parametrize("bad_val", [True, False])
def test_reject_boolean_inputs(bad_val: bool) -> None:
    """Boolean inputs must never masquerade as 0 or 1 integers."""
    with pytest.raises(FeeUnitError, match="Boolean"):
        uniswap_fee(bad_val, "uniswap-v3")
    with pytest.raises(FeeUnitError, match="Boolean"):
        bps_to_raw(bad_val)
    with pytest.raises(FeeUnitError, match="Boolean"):
        raw_to_bps(bad_val)
    with pytest.raises(FeeUnitError, match="Boolean"):
        convert_fee(bad_val, fee_unit="bps")


@pytest.mark.parametrize(
    "bad_val",
    [float("nan"), float("inf"), float("-inf"), Decimal("NaN"), Decimal("Infinity")],
)
def test_reject_nan_and_inf(bad_val: float | Decimal) -> None:
    """NaN and Infinite values must fail closed immediately."""
    with pytest.raises(FeeUnitError, match="NaN or Inf"):
        bps_to_raw(bad_val)
    with pytest.raises(FeeUnitError, match="NaN or Inf"):
        convert_fee(bad_val, fee_unit="bps")


@pytest.mark.parametrize("neg_val", [-1, -100, Decimal("-0.01"), -0.5])
def test_reject_negative_values(neg_val: int | Decimal | float) -> None:
    """Negative fee values must raise FeeUnitError."""
    raw_match = "negative" if isinstance(neg_val, int) else "integer"
    with pytest.raises(FeeUnitError, match=raw_match):
        uniswap_fee(neg_val, "uniswap-v3")  # type: ignore[arg-type]
    with pytest.raises(FeeUnitError, match="negative"):
        bps_to_raw(neg_val)
    with pytest.raises(FeeUnitError, match=raw_match):
        raw_to_bps(neg_val)  # type: ignore[arg-type]
    with pytest.raises(FeeUnitError, match="negative"):
        convert_fee(neg_val, fee_unit="bps")


@pytest.mark.parametrize("overflow_val", [MAX_UINT24 + 1, 0x1000000, 100_000_000])
def test_reject_uint24_overflow(overflow_val: int) -> None:
    """Values exceeding 24 bits must fail closed."""
    with pytest.raises(FeeUnitError, match="uint24 bounds"):
        uniswap_fee(overflow_val, "uniswap-v3")
    with pytest.raises(FeeUnitError, match="uint24 bounds"):
        raw_to_bps(overflow_val)
    with pytest.raises(FeeUnitError, match="uint24 bounds"):
        convert_fee(overflow_val, fee_unit="ppm")


def test_reject_static_fee_exceeding_100_percent() -> None:
    """Static pool fee cannot exceed 100% (1,000,000 ppm)."""
    with pytest.raises(FeeUnitError, match="exceeds maximum allowed"):
        uniswap_fee(MAX_FEE_PPM + 1, "uniswap-v3")
    with pytest.raises(FeeUnitError, match="exceeds maximum allowed"):
        uniswap_fee(MAX_FEE_PPM + 1, "uniswap-v4")


# --- 6. Explicit conversion and rejection of 'auto' guessing ---


def test_reject_auto_and_unsupported_units() -> None:
    """Heuristic guessing 'auto' or invalid unit strings must raise FeeUnitError."""
    with pytest.raises(FeeUnitError, match="Automatic fee unit guessing .* strictly prohibited"):
        convert_fee(100, fee_unit="auto", target_unit="ppm")
    with pytest.raises(FeeUnitError, match="Automatic fee unit guessing .* strictly prohibited"):
        convert_fee(100, fee_unit="auto", target_unit="bps")

    with pytest.raises(FeeUnitError, match="Invalid fee_unit"):
        convert_fee(100, fee_unit="percent", target_unit="ppm")

    with pytest.raises(FeeUnitError, match="Invalid target_unit"):
        convert_fee(100, fee_unit="bps", target_unit="percent")


def test_exact_conversions_and_identity() -> None:
    """Validate cross-unit and same-unit identity conversions."""
    # bps -> bps identity
    assert convert_fee(Decimal("30"), fee_unit="bps", target_unit="bps") == Decimal("30")
    # ppm -> ppm identity
    assert convert_fee(3000, fee_unit="ppm", target_unit="ppm") == 3000

    # raw_to_bps function
    assert raw_to_bps(10000) == Decimal(100)
    assert raw_to_bps(3000) == Decimal(30)
    assert raw_to_bps(500) == Decimal(5)
    assert raw_to_bps(100) == Decimal(1)
    assert raw_to_bps(0) == Decimal(0)


# --- 7. Precision hardening regressions (sub-ppm residual rejection) ---


@pytest.mark.parametrize(
    "fractional_bps",
    [
        Decimal("1.00000000000000000000000000001"),
        Decimal("100.00000000000000000000000000000000000000001"),
        Decimal("0.00000000000000000000000000001"),
        Decimal("99.99999999999999999999999999999999999999999"),
        Decimal("1.0001"),
        Decimal("0.005"),
        Decimal("0.001"),
        Decimal("0.00001"),
    ],
)
def test_sub_ppm_precision_counterexamples_rejected(fractional_bps: Decimal) -> None:
    """Real counterexamples where bps has sub-ppm precision must raise FeeUnitError without rounding."""
    with pytest.raises(FeeUnitError, match="does not convert to an exact integer ppm"):
        bps_to_raw(fractional_bps)
    with pytest.raises(FeeUnitError, match="does not convert to an exact integer ppm"):
        convert_fee(fractional_bps, fee_unit="bps", target_unit="ppm")


@pytest.mark.parametrize(
    ("valid_bps", "expected_ppm"),
    [
        (Decimal("0"), 0),
        (Decimal("0.01"), 1),
        (Decimal("0.02"), 2),
        (Decimal("1.01"), 101),
        (Decimal("1.50"), 150),
        (Decimal("30.00"), 3000),
        (Decimal("100.00"), 10000),
        (Decimal("167772.15"), 16777215),
    ],
)
def test_exact_bps_to_ppm_boundaries(valid_bps: Decimal, expected_ppm: int) -> None:
    """Exact integer ppm boundaries convert cleanly without precision errors."""
    assert bps_to_raw(valid_bps) == expected_ppm
    assert convert_fee(valid_bps, fee_unit="bps", target_unit="ppm") == expected_ppm


# --- 8. Context precision independence (prec=1, prec=2, prec=5) ---


@pytest.mark.parametrize("prec", [1, 2, 5, 9, 28])
def test_low_context_precision_independence(prec: int) -> None:
    """Calculations and rejections must remain exact independent of decimal context precision."""
    with decimal.localcontext(decimal.Context(prec=prec)):
        # Valid conversions remain exact
        assert bps_to_raw(Decimal("100")) == 10000
        assert bps_to_raw(Decimal("1.01")) == 101
        assert bps_to_raw(Decimal("0.01")) == 1
        assert bps_to_raw(Decimal("0")) == 0
        assert bps_to_raw(Decimal("167772.15")) == 16777215

        assert raw_to_bps(105) == Decimal("1.05")
        assert raw_to_bps(10000) == Decimal(100)
        assert raw_to_bps(1) == Decimal("0.01")
        assert raw_to_bps(0) == Decimal(0)
        assert raw_to_bps(16777215) == Decimal("167772.15")

        assert FeeValue(raw=105, kind="static").fee_bps == Decimal("1.05")
        assert FeeValue(raw=10000, kind="static").fee_bps == Decimal(100)
        assert uniswap_fee(105, "uniswap-v3").fee_bps == Decimal("1.05")

        assert convert_fee(Decimal("1.05"), fee_unit="bps", target_unit="ppm") == 105
        assert convert_fee(105, fee_unit="ppm", target_unit="bps") == Decimal("1.05")
        assert convert_fee(16777215, fee_unit="ppm", target_unit="bps") == Decimal("167772.15")

        # Non-integer ppm still strictly rejected
        with pytest.raises(FeeUnitError, match="does not convert to an exact integer ppm"):
            bps_to_raw(Decimal("1.001"))
        with pytest.raises(FeeUnitError, match="does not convert to an exact integer ppm"):
            bps_to_raw(Decimal("1.00000000000000000000000000001"))
        with pytest.raises(FeeUnitError, match="must be an integer"):
            convert_fee(Decimal("100.5"), fee_unit="ppm", target_unit="bps")
        with pytest.raises(FeeUnitError, match="must be an integer"):
            convert_fee(Decimal("100.00000000000000000000000000001"), fee_unit="ppm", target_unit="bps")


# --- 9. uint24 lower/upper bounds orthogonality ---


def test_uint24_boundaries_and_orthogonality() -> None:
    """Orthogonal checks on lower and upper bounds of 24-bit integer domain."""
    # Lower bound (0)
    assert bps_to_raw(0) == 0
    assert bps_to_raw(Decimal("0")) == 0
    assert bps_to_raw(Decimal("0.00")) == 0
    assert raw_to_bps(0) == Decimal(0)
    assert convert_fee(0, fee_unit="bps", target_unit="ppm") == 0
    assert convert_fee(0, fee_unit="ppm", target_unit="bps") == Decimal(0)
    assert FeeValue(raw=0, kind="static").fee_bps == Decimal(0)

    # Negative values rejected
    with pytest.raises(FeeUnitError, match="negative"):
        bps_to_raw(-1)
    with pytest.raises(FeeUnitError, match="negative"):
        bps_to_raw(Decimal("-0.01"))
    with pytest.raises(FeeUnitError, match="negative"):
        raw_to_bps(-1)
    with pytest.raises(FeeUnitError, match="negative"):
        FeeValue(raw=-1, kind="static")

    # Upper bound (MAX_UINT24 = 16_777_215)
    assert bps_to_raw(Decimal("167772.15")) == MAX_UINT24
    assert raw_to_bps(MAX_UINT24) == Decimal("167772.15")
    assert convert_fee(Decimal("167772.15"), fee_unit="bps", target_unit="ppm") == MAX_UINT24
    assert convert_fee(MAX_UINT24, fee_unit="ppm", target_unit="bps") == Decimal("167772.15")
    assert FeeValue(raw=MAX_UINT24, kind="dynamic").raw == MAX_UINT24
    assert FeeValue(raw=MAX_FEE_PPM, kind="static").fee_bps == Decimal(10000)

    # Upper bound overflow (MAX_UINT24 + 1 = 16_777_216)
    with pytest.raises(FeeUnitError, match="uint24 bounds"):
        bps_to_raw(Decimal("167772.16"))
    with pytest.raises(FeeUnitError, match="uint24 bounds"):
        bps_to_raw(167773)
    with pytest.raises(FeeUnitError, match="uint24 bounds"):
        raw_to_bps(MAX_UINT24 + 1)
    with pytest.raises(FeeUnitError, match="uint24 bounds"):
        FeeValue(raw=MAX_UINT24 + 1, kind="static")
    with pytest.raises(FeeUnitError, match="uint24 bounds"):
        convert_fee(MAX_UINT24 + 1, fee_unit="ppm", target_unit="bps")
    with pytest.raises(FeeUnitError, match="uint24 bounds"):
        uniswap_fee(MAX_UINT24 + 1, "uniswap-v3")
    with pytest.raises(FeeUnitError, match="uint24 bounds"):
        uniswap_fee(MAX_UINT24 + 1, "uniswap-v4")


# --- 10. FeeModel semantics and whiten prevention ---


def test_fee_model_semantics_and_whiten_prevention() -> None:
    """FeeModel unknown kind and unsupported units must fail closed rather than statically whitened."""
    # unknown model without raw_value
    with pytest.raises(FeeUnitError, match="has no raw_value"):
        FeeValue.from_fee_model(FeeModel.unknown())

    # unknown model kind with raw_value must still reject (no static whitening)
    with pytest.raises(FeeUnitError, match="unknown"):
        FeeValue.from_fee_model(FeeModel(kind="unknown", raw_value=500))

    # unsupported unit
    with pytest.raises(FeeUnitError, match="Unsupported FeeModel unit"):
        FeeValue.from_fee_model(FeeModel(kind="static", raw_value=500, unit="arbitrary"))

    # bps unit accurately converted
    fv_bps = FeeValue.from_fee_model(FeeModel.static(raw_value=100, unit="bps"))
    assert fv_bps.raw == 10000
    assert fv_bps.fee_bps == Decimal(100)

    # hundredths_of_bip (ppm equivalent) accurately handled
    fv_hob = FeeValue.from_fee_model(FeeModel.static(raw_value=3000, unit="hundredths_of_bip"))
    assert fv_hob.raw == 3000
    assert fv_hob.fee_bps == Decimal(30)
