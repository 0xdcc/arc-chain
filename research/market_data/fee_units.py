"""Explicit fee unit representations and conversions for canonical DEX protocols.

Follows AGENTS.md financial safety and pure offline research requirements:
1. Pure explicit fee units: Only 'bps' (basis points) and 'ppm' (parts per million / raw)
   are permitted. Heuristic or automatic guessing ('auto') is strictly prohibited.
2. Canonical protocol verification: Only canonical 'uniswap-v3' and 'uniswap-v4'
   are supported. Forks, unverified adapters, and arbitrary dex identifiers fail closed.
3. Fail-closed on dynamic fees: Dynamic fee pools (marked by DYNAMIC_FEE_FLAG 0x800000)
   do not possess static fee_bps values; attempting to read static fee_bps raises FeeUnitError.
4. Exact numerical conversions: bps_to_raw requires integer ppm precision. Fractional ppm
   like Decimal('.0001') is rejected. 0 is valid.
5. Strict domain boundaries: Rejects boolean, NaN, Inf, negative fees, and uint24 overflows.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, Literal

from arbitrage_contracts.identity import FeeModel

# Uniswap V4 dynamic fee flag (bit 23)
DYNAMIC_FEE_FLAG: Final[int] = 0x800000

# Maximum 24-bit unsigned integer (uint24)
MAX_UINT24: Final[int] = 0xFFFFFF

# Maximum standard fee in parts per million (100% = 1,000,000 ppm = 10,000 bps)
MAX_FEE_PPM: Final[int] = 1_000_000

# Canonical Uniswap DEX identifiers permitted for fee interpretation
CANONICAL_UNISWAP_DEXES: Final[frozenset[str]] = frozenset({"uniswap-v3", "uniswap-v4"})

FeeUnitType = Literal["bps", "ppm"]


class FeeUnitError(ValueError):
    """Raised when fee unit conversion, validation, or interpretation fails."""


@dataclass(frozen=True, slots=True)
class FeeValue:
    """Immutable representation of a fee with explicit unit semantics and protocol awareness."""

    raw: int
    kind: str = "static"

    def __post_init__(self) -> None:
        if isinstance(self.raw, bool):
            raise FeeUnitError("Boolean is not a valid fee integer")
        if not isinstance(self.raw, int):
            raise FeeUnitError(f"raw fee must be an integer, got {type(self.raw).__name__}")
        if self.raw < 0:
            raise FeeUnitError(f"raw fee cannot be negative: {self.raw}")
        if self.raw > MAX_UINT24:
            raise FeeUnitError(f"raw fee {self.raw} out of uint24 bounds [0, {MAX_UINT24}]")
        if self.kind not in ("static", "dynamic"):
            raise FeeUnitError(f"kind must be 'static' or 'dynamic', got {self.kind!r}")
        if self.kind == "static" and (self.raw & DYNAMIC_FEE_FLAG) != 0:
            raise FeeUnitError(f"dynamic fee bit 0x800000 is set on static FeeValue: {hex(self.raw)}")

    @property
    def is_dynamic(self) -> bool:
        """True if the fee is dynamic or flagged with DYNAMIC_FEE_FLAG."""
        return self.kind == "dynamic" or (self.raw & DYNAMIC_FEE_FLAG) != 0

    @property
    def fee_bps(self) -> Decimal:
        """Static fee in basis points (1 bip = 100 ppm).

        Raises FeeUnitError if the fee is dynamic.
        """
        if self.is_dynamic:
            raise FeeUnitError(
                f"Dynamic fee pool (raw={hex(self.raw)}) does not have a static fee_bps value"
            )
        return raw_to_bps(self.raw)

    @property
    def fee_ppm(self) -> int:
        """Static fee in parts per million (raw uint24).

        Raises FeeUnitError if the fee is dynamic.
        """
        if self.is_dynamic:
            raise FeeUnitError(
                f"Dynamic fee pool (raw={hex(self.raw)}) does not have a static fee_ppm value"
            )
        return self.raw

    def to_fee_model(self) -> FeeModel:
        """Delegate to existing system FeeModel for downstream pipeline compatibility."""
        if self.is_dynamic:
            return FeeModel.dynamic(raw_value=self.raw, unit="ppm")
        return FeeModel.static(raw_value=self.raw, unit="ppm")

    @classmethod
    def from_fee_model(cls, model: FeeModel) -> FeeValue:
        """Construct FeeValue from an existing system FeeModel."""
        if model.raw_value is None:
            raise FeeUnitError(f"FeeModel {model} has no raw_value")
        if model.kind not in ("static", "dynamic"):
            raise FeeUnitError(f"Unsupported FeeModel kind: {model.kind}")
        if model.unit in ("ppm", "hundredths_of_bip"):
            raw = model.raw_value
        elif model.unit == "bps" and model.kind == "static":
            raw = bps_to_raw(model.raw_value)
        else:
            raise FeeUnitError(f"Unsupported FeeModel unit: {model.unit}")
        return cls(raw=raw, kind=model.kind)


def _validate_numeric_fee(val: Any) -> Decimal:
    """Validate numeric input rejecting bool, NaN, Inf, and negative numbers."""
    if isinstance(val, bool):
        raise FeeUnitError("Boolean is not a valid fee value")
    if not isinstance(val, (int, Decimal, float)):
        raise FeeUnitError(f"Expected int, Decimal, or float fee, got {type(val).__name__}")
    if isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            raise FeeUnitError("Fee value cannot be NaN or Inf")
        d = Decimal(str(val))
    elif isinstance(val, Decimal):
        if val.is_nan() or val.is_infinite():
            raise FeeUnitError("Fee value cannot be NaN or Inf")
        d = val
    else:
        d = Decimal(val)

    if d < 0:
        raise FeeUnitError(f"Fee value cannot be negative: {d}")
    return d


def uniswap_fee(raw: int, dex: str) -> FeeValue:
    """Interpret raw uint24 fee from a canonical Uniswap V3 or V4 pool.

    Args:
        raw: Raw uint24 fee integer from pool storage or parameters.
        dex: Canonical DEX identifier ('uniswap-v3' or 'uniswap-v4').

    Returns:
        Immutable FeeValue with explicit unit conversion semantics.

    Raises:
        FeeUnitError: If dex is not canonical V3/V4, raw is invalid/negative/out-of-range,
            or static fee exceeds 1,000,000 ppm.
    """
    if dex not in CANONICAL_UNISWAP_DEXES:
        raise FeeUnitError(
            f"Unsupported or non-canonical dex {dex!r}. "
            f"Only canonical dexes {sorted(CANONICAL_UNISWAP_DEXES)} are permitted."
        )

    if isinstance(raw, bool):
        raise FeeUnitError("Boolean is not a valid raw fee integer")
    if not isinstance(raw, int):
        raise FeeUnitError(f"Raw fee must be an integer, got {type(raw).__name__}")
    if raw < 0:
        raise FeeUnitError(f"Raw fee cannot be negative: {raw}")
    if raw > MAX_UINT24:
        raise FeeUnitError(f"Raw fee {raw} out of uint24 bounds [0, {MAX_UINT24}]")

    if dex == "uniswap-v3":
        if (raw & DYNAMIC_FEE_FLAG) != 0:
            raise FeeUnitError(f"Uniswap V3 does not support dynamic fee flag: {hex(raw)}")
        if raw > MAX_FEE_PPM:
            raise FeeUnitError(f"Uniswap V3 fee {raw} ppm exceeds maximum allowed {MAX_FEE_PPM} ppm")
        return FeeValue(raw=raw, kind="static")

    if dex == "uniswap-v4":
        if (raw & DYNAMIC_FEE_FLAG) != 0:
            return FeeValue(raw=raw, kind="dynamic")
        if raw > MAX_FEE_PPM:
            raise FeeUnitError(f"Uniswap V4 static fee {raw} ppm exceeds maximum allowed {MAX_FEE_PPM} ppm")
        return FeeValue(raw=raw, kind="static")

    raise FeeUnitError(f"Unhandled dex: {dex!r}")


def bps_to_raw(bps: int | Decimal | float) -> int:
    """Convert basis points (bps) to raw pool fee in parts per million (ppm).

    Exact conversion: 1 bps = 100 ppm. Fractional ppm is strictly rejected.
    Zero (0 bps -> 0 ppm) is explicitly allowed.

    Args:
        bps: Fee in basis points (e.g. 100 bps = 1%, 30 bps = 0.3%, 0 = 0%).

    Returns:
        Integer raw fee in ppm.

    Raises:
        FeeUnitError: If bps cannot be converted to integer ppm (e.g. Decimal('.0001')),
            is negative, is boolean, is NaN/Inf, or overflows uint24.
    """
    d_bps = _validate_numeric_fee(bps)
    numerator, denominator = d_bps.as_integer_ratio()
    ppm_int, remainder = divmod(numerator * 100, denominator)

    if remainder:
        raise FeeUnitError(
            f"bps value {bps} does not convert to an exact integer ppm"
        )

    if ppm_int > MAX_UINT24:
        raise FeeUnitError(f"Calculated ppm {ppm_int} exceeds uint24 bounds [0, {MAX_UINT24}]")

    return ppm_int


def raw_to_bps(raw: int) -> Decimal:
    """Convert raw pool fee in parts per million (ppm) to basis points (bps).

    Args:
        raw: Raw uint24 fee integer in ppm.

    Returns:
        Fee in basis points as Decimal.
    """
    if isinstance(raw, bool):
        raise FeeUnitError("Boolean is not a valid raw fee integer")
    if not isinstance(raw, int):
        raise FeeUnitError(f"Raw fee must be an integer, got {type(raw).__name__}")
    if raw < 0:
        raise FeeUnitError(f"Raw fee cannot be negative: {raw}")
    if raw > MAX_UINT24:
        raise FeeUnitError(f"Raw fee {raw} out of uint24 bounds [0, {MAX_UINT24}]")

    return Decimal((0, tuple(int(digit) for digit in str(raw)), -2))


def convert_fee(
    value: int | Decimal | float,
    fee_unit: str,
    target_unit: str = "ppm",
) -> int | Decimal:
    """Explicit fee conversion entry point between 'bps' and 'ppm'.

    Strictly forbids 'auto' or heuristic guessing from value magnitude.
    Rejects boolean, NaN, Inf, negative, and uint24 out-of-bounds values.

    Args:
        value: Numeric fee input.
        fee_unit: Source unit, strictly 'bps' or 'ppm'.
        target_unit: Destination unit, strictly 'bps' or 'ppm'. Default 'ppm'.

    Returns:
        Converted value (int for ppm, Decimal for bps).

    Raises:
        FeeUnitError: If fee_unit is 'auto', unrecognised, or conversion fails.
    """
    if fee_unit == "auto":
        raise FeeUnitError(
            "Automatic fee unit guessing ('auto') is strictly prohibited; "
            "unit must be explicitly declared as 'bps' or 'ppm'"
        )

    if fee_unit not in ("bps", "ppm"):
        raise FeeUnitError(
            f"Invalid fee_unit {fee_unit!r}. Must be explicitly 'bps' or 'ppm'"
        )

    if target_unit not in ("bps", "ppm"):
        raise FeeUnitError(
            f"Invalid target_unit {target_unit!r}. Must be explicitly 'bps' or 'ppm'"
        )

    d_val = _validate_numeric_fee(value)

    if fee_unit == "bps":
        if target_unit == "ppm":
            return bps_to_raw(d_val)
        return d_val

    # fee_unit == "ppm"
    if d_val != d_val.to_integral_value():
        raise FeeUnitError(f"ppm value {value} must be an integer, got fractional {d_val}")

    ppm_int = int(d_val)
    if ppm_int > MAX_UINT24:
        raise FeeUnitError(f"ppm fee {ppm_int} exceeds uint24 bounds [0, {MAX_UINT24}]")

    if target_unit == "ppm":
        return ppm_int
    return raw_to_bps(ppm_int)


def fee_to_raw(value: int | Decimal | float, fee_unit: str) -> int:
    """Convenience explicit converter returning integer raw ppm."""
    res = convert_fee(value, fee_unit=fee_unit, target_unit="ppm")
    assert isinstance(res, int)
    return res


def fee_to_bps(value: int | Decimal | float, fee_unit: str) -> Decimal:
    """Convenience explicit converter returning Decimal bps."""
    res = convert_fee(value, fee_unit=fee_unit, target_unit="bps")
    assert isinstance(res, Decimal)
    return res
