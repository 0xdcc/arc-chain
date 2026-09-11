"""Pure-functional funds safety policy calculator for atomic execution.

Eliminates legacy float-based slippage and weakly typed calculations.
Uses strictly int (atoms) and Decimal arithmetic for zero-loss financial guarantees.
Enforces <=500 USD trade limit, non-zero positive slippage out, single-deduction defense,
and output_floor principal protection.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from arbitrage_contracts.identity import UINT256_MAX
from arbitrage_contracts.quote import GasEvidence, HopQuote, QuoteStatus

MAX_TRADE_AMOUNT_USD: Decimal = Decimal("500.0")
MIN_SLIPPAGE_BPS: int = 1
MAX_SLIPPAGE_BPS: int = 500
DEFAULT_SLIPPAGE_BPS: int = 50
MAX_PROBE_LOSS_BUDGET_USD: Decimal = Decimal("1.0")
SUPPORTED_DECIMALS: frozenset[int] = frozenset({6, 18})


class ExcessiveAmountError(ValueError):
    """Raised when trade amount exceeds the hard per-transaction limit (500 USD)."""


class PolicyError(ValueError):
    """Base error for execution policy violations."""


class PolicyViolationError(PolicyError):
    """Raised when a candidate route fails funds policy invariants."""


class PolicyMode(StrEnum):
    """Operating mode for execution policy."""

    STRICT_PROFITABLE = "strict_profitable"
    BOUNDED_PROBE = "bounded_probe"


class PolicyRejectionReason(StrEnum):
    """Taxonomy of policy evaluation rejection causes."""

    EXCESSIVE_AMOUNT = "EXCESSIVE_AMOUNT"
    INVALID_SLIPPAGE = "INVALID_SLIPPAGE"
    INVALID_PRICE = "INVALID_PRICE"
    MISSING_PRICE = "MISSING_PRICE"
    MISSING_GAS = "MISSING_GAS"
    UNKNOWN_GAS = "UNKNOWN_GAS"
    INVALID_GAS = "INVALID_GAS"
    UNKNOWN_FEE = "UNKNOWN_FEE"
    UNSUPPORTED_DECIMALS = "UNSUPPORTED_DECIMALS"
    OUTPUT_FLOOR_NOT_MET = "OUTPUT_FLOOR_NOT_MET"
    MIN_AMOUNT_OUT_NON_POSITIVE = "MIN_AMOUNT_OUT_NON_POSITIVE"
    MIN_AMOUNT_OUT_EXCEEDS_QUOTE = "MIN_AMOUNT_OUT_EXCEEDS_QUOTE"
    UNPROFITABLE = "UNPROFITABLE"
    PROBE_MODE_DISABLED = "PROBE_MODE_DISABLED"
    PROBE_BUDGET_EXCEEDED = "PROBE_BUDGET_EXCEEDED"
    QUOTE_STATUS_INVALID = "QUOTE_STATUS_INVALID"
    POLICY_VIOLATION = "POLICY_VIOLATION"


def validate_positive_decimal(value: Any, name: str) -> Decimal:
    """Validate that value is a finite strictly positive Decimal without float coercion."""
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must not be boolean or float; pass Decimal, int, or string")
    if not isinstance(value, (Decimal, int, str)):
        raise TypeError(f"{name} must be Decimal, int, or string, got {type(value).__name__}")
    try:
        dec = Decimal(str(value)) if not isinstance(value, Decimal) else value
    except (InvalidOperation, TypeError) as exc:
        raise ValueError(f"Invalid decimal for {name}: {value!r}") from exc
    if not dec.is_finite():
        raise ValueError(f"{name} must be finite, got {dec}")
    if dec <= Decimal("0"):
        raise ValueError(f"{name} must be strictly positive, got {dec}")
    return dec


def validate_non_negative_decimal(value: Any, name: str) -> Decimal:
    """Validate that value is a finite non-negative Decimal without float coercion."""
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must not be boolean or float; pass Decimal, int, or string")
    if not isinstance(value, (Decimal, int, str)):
        raise TypeError(f"{name} must be Decimal, int, or string, got {type(value).__name__}")
    try:
        dec = Decimal(str(value)) if not isinstance(value, Decimal) else value
    except (InvalidOperation, TypeError) as exc:
        raise ValueError(f"Invalid decimal for {name}: {value!r}") from exc
    if not dec.is_finite():
        raise ValueError(f"{name} must be finite, got {dec}")
    if dec < Decimal("0"):
        raise ValueError(f"{name} must be non-negative, got {dec}")
    return dec


def validate_slippage_bps(slippage_bps: Any) -> int:
    """Validate slippage in integer basis points (1 <= bps <= 500)."""
    if type(slippage_bps) is not int or isinstance(slippage_bps, bool):
        raise TypeError(f"slippage_bps must be an integer, got {type(slippage_bps).__name__}")
    if slippage_bps < MIN_SLIPPAGE_BPS or slippage_bps > MAX_SLIPPAGE_BPS:
        raise ValueError(
            f"slippage_bps must be between {MIN_SLIPPAGE_BPS} and {MAX_SLIPPAGE_BPS}, got {slippage_bps}"
        )
    return slippage_bps


def validate_atoms(value: Any, name: str, *, positive: bool = True) -> int:
    """Validate an EVM integer quantity without floats or booleans."""
    if type(value) is not int or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer, got {type(value).__name__}")
    min_val = 1 if positive else 0
    if value < min_val or value > UINT256_MAX:
        raise ValueError(f"{name} out of bounds [{min_val}, 2^256-1]: {value}")
    return value


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    """Immutable parameterization of trade size, slippage, and profit requirements."""

    mode: str = PolicyMode.STRICT_PROFITABLE
    max_amount_usd: Decimal = Decimal("500.0")
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS
    probe_loss_budget_usd: Decimal = Decimal("0.0")
    probe_mode_enabled: bool = False

    def __init__(
        self,
        mode: PolicyMode | str = PolicyMode.STRICT_PROFITABLE,
        max_amount_usd: Decimal | str | int = Decimal("500.0"),
        slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
        probe_loss_budget_usd: Decimal | str | int = Decimal("0.0"),
        probe_mode_enabled: bool = False,
    ) -> None:
        resolved_mode = str(mode)
        if resolved_mode not in (PolicyMode.STRICT_PROFITABLE, PolicyMode.BOUNDED_PROBE):
            raise ValueError(f"Unsupported policy mode: {resolved_mode!r}")

        val_max_usd = validate_positive_decimal(max_amount_usd, "max_amount_usd")
        if val_max_usd > MAX_TRADE_AMOUNT_USD:
            raise ExcessiveAmountError(
                f"Policy max_amount_usd (${val_max_usd}) exceeds hard limit of ${MAX_TRADE_AMOUNT_USD}"
            )

        val_slippage = validate_slippage_bps(slippage_bps)
        val_probe_budget = validate_non_negative_decimal(
            probe_loss_budget_usd, "probe_loss_budget_usd"
        )
        if val_probe_budget > MAX_PROBE_LOSS_BUDGET_USD:
            raise ValueError(
                f"probe_loss_budget_usd (${val_probe_budget}) exceeds maximum allowable of ${MAX_PROBE_LOSS_BUDGET_USD}"
            )

        if resolved_mode == PolicyMode.BOUNDED_PROBE and not probe_mode_enabled:
            raise ValueError("bounded_probe mode requires probe_mode_enabled=True")

        object.__setattr__(self, "mode", resolved_mode)
        object.__setattr__(self, "max_amount_usd", val_max_usd)
        object.__setattr__(self, "slippage_bps", val_slippage)
        object.__setattr__(self, "probe_loss_budget_usd", val_probe_budget)
        object.__setattr__(self, "probe_mode_enabled", bool(probe_mode_enabled))

    def to_dict(self) -> dict[str, Any]:
        """Serialize policy configuration into dictionary."""
        return {
            "mode": self.mode,
            "max_amount_usd": str(self.max_amount_usd),
            "slippage_bps": self.slippage_bps,
            "probe_loss_budget_usd": str(self.probe_loss_budget_usd),
            "probe_mode_enabled": self.probe_mode_enabled,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ExecutionPolicy:
        """Deserialize policy configuration from dictionary mapping."""
        return cls(
            mode=data.get("mode", PolicyMode.STRICT_PROFITABLE),
            max_amount_usd=Decimal(str(data.get("max_amount_usd", "500.0"))),
            slippage_bps=int(data.get("slippage_bps", DEFAULT_SLIPPAGE_BPS)),
            probe_loss_budget_usd=Decimal(str(data.get("probe_loss_budget_usd", "0.0"))),
            probe_mode_enabled=bool(data.get("probe_mode_enabled", False)),
        )

    def to_json(self) -> str:
        """Serialize policy configuration to compact canonical JSON."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> ExecutionPolicy:
        """Deserialize policy configuration from canonical JSON string."""
        return cls.from_dict(json.loads(text))


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """Audit-ready evaluation result generated by funds policy calculator."""

    approved: bool
    reason: PolicyRejectionReason | None
    message: str
    policy: ExecutionPolicy
    amount_in: int
    expected_out: int
    min_amount_out: int
    slippage_bps: int
    output_floor: int
    conservative_gas_usd: Decimal
    gas_atoms: int
    net_atoms: int
    net_profit_usd: Decimal
    trade_amount_usd: Decimal
    base_asset_usd_price: Decimal

    def to_dict(self) -> dict[str, Any]:
        """Serialize decision into dictionary form."""
        return {
            "approved": self.approved,
            "reason": str(self.reason) if self.reason is not None else None,
            "message": self.message,
            "policy": self.policy.to_dict(),
            "amount_in": str(self.amount_in),
            "expected_out": str(self.expected_out),
            "min_amount_out": str(self.min_amount_out),
            "slippage_bps": self.slippage_bps,
            "output_floor": str(self.output_floor),
            "conservative_gas_usd": str(self.conservative_gas_usd),
            "gas_atoms": str(self.gas_atoms),
            "net_atoms": str(self.net_atoms),
            "net_profit_usd": str(self.net_profit_usd),
            "trade_amount_usd": str(self.trade_amount_usd),
            "base_asset_usd_price": str(self.base_asset_usd_price),
        }

    def to_json(self) -> str:
        """Serialize decision to compact canonical JSON."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def calculate_output_floor(
    amount_in: int,
    expected_out: int,
    slippage_bps: int,
    decimals: int,
    base_asset_usd_price: Decimal | str | int,
    conservative_gas_usd: Decimal | str | int,
    probe_loss_budget_usd: Decimal | str | int = Decimal("0.0"),
    *,
    probe_mode_enabled: bool = False,
) -> int:
    """Calculate minimum acceptable output (output_floor) under policy constraints.

    Enforces:
    - In strict_profitable mode:
      floor = max(slippage_min, amount_in + gas_atoms + 1)
    - In bounded_probe mode:
      floor = max(slippage_min, amount_in + int(ceil((gas - budget) * 10^decimals / price)))
    """
    val_in = validate_atoms(amount_in, "amount_in", positive=True)
    val_out = validate_atoms(expected_out, "expected_out", positive=True)
    val_slippage = validate_slippage_bps(slippage_bps)
    if (
        decimals not in SUPPORTED_DECIMALS
        or type(decimals) is not int
        or isinstance(decimals, bool)
    ):
        raise ValueError(f"decimals must be in {set(SUPPORTED_DECIMALS)}, got {decimals}")

    price = validate_positive_decimal(base_asset_usd_price, "base_asset_usd_price")
    gas_usd = validate_positive_decimal(conservative_gas_usd, "conservative_gas_usd")
    budget_usd = validate_non_negative_decimal(probe_loss_budget_usd, "probe_loss_budget_usd")

    if budget_usd > MAX_PROBE_LOSS_BUDGET_USD:
        raise ValueError(f"probe_loss_budget_usd exceeds {MAX_PROBE_LOSS_BUDGET_USD}")

    if budget_usd > Decimal("0") and not probe_mode_enabled:
        raise ValueError("probe budget specified but probe_mode_enabled is False")

    if budget_usd > Decimal("0") and gas_usd > budget_usd:
        raise ValueError("Gas cost alone exceeds remaining probe budget")

    slippage_min = (val_out * (10000 - val_slippage)) // 10000
    if slippage_min <= 0:
        raise ValueError(f"slippage_min must be positive, got {slippage_min}")
    if slippage_min > val_out:
        raise ValueError(f"slippage_min ({slippage_min}) cannot exceed expected_out ({val_out})")

    scale = Decimal(10**decimals)
    if budget_usd == Decimal("0"):
        gas_atoms = int(((gas_usd * scale) / price).to_integral_value(rounding=ROUND_CEILING))
        required_cost_floor = val_in + gas_atoms + 1
    else:
        offset_usd = gas_usd - budget_usd
        offset_atoms = int(((offset_usd * scale) / price).to_integral_value(rounding=ROUND_CEILING))
        required_cost_floor = val_in + offset_atoms

    floor = max(slippage_min, required_cost_floor)
    if floor <= 0:
        raise ValueError(f"Calculated output floor must be positive, got {floor}")
    if floor > val_out:
        raise ValueError(
            f"Quote cannot satisfy output floor: floor ({floor}) > expected_out ({val_out})"
        )

    return floor


def evaluate_execution_policy(
    amount_in: int,
    expected_out: int,
    decimals: int,
    base_asset_usd_price: Decimal | str | int | None,
    conservative_gas_usd: Decimal | str | int | None = None,
    *,
    policy: ExecutionPolicy | None = None,
    gas_evidence: GasEvidence | None = None,
    hop_quotes: Sequence[HopQuote] = (),
    quote_status: QuoteStatus | str = QuoteStatus.QUOTED,
) -> PolicyDecision:
    """Evaluate funds safety policy for a candidate quote without throwing for business rejections.

    Note: amount_usd > 500.0 raises ExcessiveAmountError immediately per C08 red line.
    """
    effective_policy = policy if policy is not None else ExecutionPolicy()

    # Pre-checks for decimals and basic inputs
    if (
        decimals not in SUPPORTED_DECIMALS
        or type(decimals) is not int
        or isinstance(decimals, bool)
    ):
        return _make_rejection(
            PolicyRejectionReason.UNSUPPORTED_DECIMALS,
            f"Decimals {decimals} not supported; must be in {set(SUPPORTED_DECIMALS)}",
            effective_policy,
            amount_in=amount_in
            if isinstance(amount_in, int) and not isinstance(amount_in, bool)
            else 0,
            expected_out=expected_out
            if isinstance(expected_out, int) and not isinstance(expected_out, bool)
            else 0,
        )

    if (
        type(amount_in) is not int
        or isinstance(amount_in, bool)
        or amount_in <= 0
        or amount_in > UINT256_MAX
    ):
        return _make_rejection(
            PolicyRejectionReason.POLICY_VIOLATION,
            f"amount_in must be a positive uint256 integer, got {amount_in!r}",
            effective_policy,
            amount_in=0,
            expected_out=0,
        )

    if (
        type(expected_out) is not int
        or isinstance(expected_out, bool)
        or expected_out <= 0
        or expected_out > UINT256_MAX
    ):
        return _make_rejection(
            PolicyRejectionReason.MIN_AMOUNT_OUT_NON_POSITIVE,
            f"expected_out must be a positive uint256 integer, got {expected_out!r}",
            effective_policy,
            amount_in=amount_in,
            expected_out=0,
        )

    # Validate quote status
    status_str = str(quote_status).lower()
    if status_str != QuoteStatus.QUOTED:
        return _make_rejection(
            PolicyRejectionReason.QUOTE_STATUS_INVALID,
            f"Quote status is not QUOTED: {quote_status!r}",
            effective_policy,
            amount_in=amount_in,
            expected_out=expected_out,
        )

    # Validate price evidence: finite positive Decimal, no float
    if base_asset_usd_price is None:
        return _make_rejection(
            PolicyRejectionReason.MISSING_PRICE,
            "Missing base_asset_usd_price; explicit evidence is mandatory",
            effective_policy,
            amount_in=amount_in,
            expected_out=expected_out,
        )
    try:
        price = validate_positive_decimal(base_asset_usd_price, "base_asset_usd_price")
    except (ValueError, TypeError) as exc:
        return _make_rejection(
            PolicyRejectionReason.INVALID_PRICE,
            f"Invalid base_asset_usd_price: {exc}",
            effective_policy,
            amount_in=amount_in,
            expected_out=expected_out,
        )

    # C08 Hard limit check: <= 500 USD
    scale = Decimal(10**decimals)
    trade_amount_usd = (Decimal(amount_in) * price) / scale
    if trade_amount_usd > MAX_TRADE_AMOUNT_USD:
        raise ExcessiveAmountError(
            f"Trade amount ${trade_amount_usd:.2f} USD exceeds safety limit of ${MAX_TRADE_AMOUNT_USD:.2f} USD."
        )
    if trade_amount_usd > effective_policy.max_amount_usd:
        raise ExcessiveAmountError(
            f"Trade amount ${trade_amount_usd:.2f} USD exceeds policy limit of ${effective_policy.max_amount_usd:.2f} USD."
        )

    # Single deduction defense & unknown fee interception
    for hop in hop_quotes:
        if hop.fee_model is not None:
            if hop.fee_model.kind == "unknown":
                return _make_rejection(
                    PolicyRejectionReason.UNKNOWN_FEE,
                    f"Hop {hop.hop_index} declares unknown fee model",
                    effective_policy,
                    amount_in=amount_in,
                    expected_out=expected_out,
                    price=price,
                    trade_amount_usd=trade_amount_usd,
                )
            if hop.fee_model.kind == "dynamic":
                return _make_rejection(
                    PolicyRejectionReason.UNKNOWN_FEE,
                    f"Hop {hop.hop_index} declares unsupported dynamic fee model",
                    effective_policy,
                    amount_in=amount_in,
                    expected_out=expected_out,
                    price=price,
                    trade_amount_usd=trade_amount_usd,
                )

    # Resolve conservative gas USD
    gas_usd: Decimal
    if conservative_gas_usd is not None:
        try:
            gas_usd = validate_positive_decimal(conservative_gas_usd, "conservative_gas_usd")
        except (ValueError, TypeError) as exc:
            return _make_rejection(
                PolicyRejectionReason.INVALID_GAS,
                f"Invalid conservative_gas_usd: {exc}",
                effective_policy,
                amount_in=amount_in,
                expected_out=expected_out,
                price=price,
                trade_amount_usd=trade_amount_usd,
            )
    else:
        # Fall back to gas_evidence
        if gas_evidence is None:
            return _make_rejection(
                PolicyRejectionReason.MISSING_GAS,
                "Gas evidence missing and conservative_gas_usd not provided",
                effective_policy,
                amount_in=amount_in,
                expected_out=expected_out,
                price=price,
                trade_amount_usd=trade_amount_usd,
            )
        if gas_evidence.gas_kind == "unknown" or not gas_evidence.gas_kind:
            return _make_rejection(
                PolicyRejectionReason.UNKNOWN_GAS,
                f"Gas evidence kind is unknown: {gas_evidence.gas_kind!r}",
                effective_policy,
                amount_in=amount_in,
                expected_out=expected_out,
                price=price,
                trade_amount_usd=trade_amount_usd,
            )
        if gas_evidence.gas_units is None or gas_evidence.gas_price_atoms is None:
            return _make_rejection(
                PolicyRejectionReason.MISSING_GAS,
                "Gas evidence lacks units or gas_price_atoms",
                effective_policy,
                amount_in=amount_in,
                expected_out=expected_out,
                price=price,
                trade_amount_usd=trade_amount_usd,
            )
        total_native_gas = gas_evidence.gas_units * gas_evidence.gas_price_atoms + (
            gas_evidence.l1_fee_atoms or 0
        )
        if total_native_gas <= 0:
            return _make_rejection(
                PolicyRejectionReason.INVALID_GAS,
                f"Computed native gas cost is non-positive: {total_native_gas}",
                effective_policy,
                amount_in=amount_in,
                expected_out=expected_out,
                price=price,
                trade_amount_usd=trade_amount_usd,
            )
        if decimals == 18:
            gas_usd = (Decimal(total_native_gas) * price) / Decimal(10**18)
        else:
            return _make_rejection(
                PolicyRejectionReason.MISSING_GAS,
                "Non-18-decimal base requires explicit conservative_gas_usd to convert native gas",
                effective_policy,
                amount_in=amount_in,
                expected_out=expected_out,
                price=price,
                trade_amount_usd=trade_amount_usd,
            )

    # Check policy mode and probe budget
    if effective_policy.mode == PolicyMode.BOUNDED_PROBE:
        if not effective_policy.probe_mode_enabled:
            return _make_rejection(
                PolicyRejectionReason.PROBE_MODE_DISABLED,
                "bounded_probe mode requested but probe_mode_enabled is False",
                effective_policy,
                amount_in=amount_in,
                expected_out=expected_out,
                price=price,
                gas_usd=gas_usd,
                trade_amount_usd=trade_amount_usd,
            )
        if effective_policy.probe_loss_budget_usd <= Decimal("0"):
            return _make_rejection(
                PolicyRejectionReason.PROBE_BUDGET_EXCEEDED,
                "bounded_probe mode requires positive probe_loss_budget_usd",
                effective_policy,
                amount_in=amount_in,
                expected_out=expected_out,
                price=price,
                gas_usd=gas_usd,
                trade_amount_usd=trade_amount_usd,
            )
        if gas_usd > effective_policy.probe_loss_budget_usd:
            return _make_rejection(
                PolicyRejectionReason.PROBE_BUDGET_EXCEEDED,
                f"Conservative gas ${gas_usd:.4f} USD exceeds probe budget ${effective_policy.probe_loss_budget_usd:.4f} USD",
                effective_policy,
                amount_in=amount_in,
                expected_out=expected_out,
                price=price,
                gas_usd=gas_usd,
                trade_amount_usd=trade_amount_usd,
            )

    # Slippage minimum check
    slippage_bps = effective_policy.slippage_bps
    slippage_min = (expected_out * (10000 - slippage_bps)) // 10000
    if slippage_min <= 0:
        return _make_rejection(
            PolicyRejectionReason.MIN_AMOUNT_OUT_NON_POSITIVE,
            f"slippage_min must be positive, got {slippage_min}",
            effective_policy,
            amount_in=amount_in,
            expected_out=expected_out,
            price=price,
            gas_usd=gas_usd,
            trade_amount_usd=trade_amount_usd,
        )
    if slippage_min > expected_out:
        return _make_rejection(
            PolicyRejectionReason.MIN_AMOUNT_OUT_EXCEEDS_QUOTE,
            f"slippage_min ({slippage_min}) exceeds expected_out ({expected_out})",
            effective_policy,
            amount_in=amount_in,
            expected_out=expected_out,
            price=price,
            gas_usd=gas_usd,
            trade_amount_usd=trade_amount_usd,
        )

    # Upward rounding (ROUND_CEILING) of gas cost in base atoms
    gas_atoms = int(((gas_usd * scale) / price).to_integral_value(rounding=ROUND_CEILING))
    gross_atoms = expected_out - amount_in
    net_atoms = gross_atoms - gas_atoms
    net_profit_usd = (Decimal(net_atoms) * price) / scale

    # Output floor calculation
    if effective_policy.mode == PolicyMode.STRICT_PROFITABLE:
        cost_floor = amount_in + gas_atoms + 1
        output_floor = max(slippage_min, cost_floor)

        if expected_out < output_floor or net_atoms < 1:
            reason = (
                PolicyRejectionReason.UNPROFITABLE
                if net_atoms < 1
                else PolicyRejectionReason.OUTPUT_FLOOR_NOT_MET
            )
            return _make_rejection(
                reason,
                f"Quote cannot satisfy principal + gas + 1 atom net profit floor: "
                f"expected_out={expected_out}, floor={output_floor}, net_atoms={net_atoms}",
                effective_policy,
                amount_in=amount_in,
                expected_out=expected_out,
                price=price,
                gas_usd=gas_usd,
                gas_atoms=gas_atoms,
                net_atoms=net_atoms,
                net_profit_usd=net_profit_usd,
                trade_amount_usd=trade_amount_usd,
                output_floor=output_floor,
            )
    else:
        # Bounded probe mode
        offset_usd = gas_usd - effective_policy.probe_loss_budget_usd
        offset_atoms = int(((offset_usd * scale) / price).to_integral_value(rounding=ROUND_CEILING))
        cost_floor = amount_in + offset_atoms
        output_floor = max(slippage_min, cost_floor)

        if output_floor <= 0:
            return _make_rejection(
                PolicyRejectionReason.MIN_AMOUNT_OUT_NON_POSITIVE,
                f"Calculated output floor must be positive, got {output_floor}",
                effective_policy,
                amount_in=amount_in,
                expected_out=expected_out,
                price=price,
                gas_usd=gas_usd,
                gas_atoms=gas_atoms,
                net_atoms=net_atoms,
                net_profit_usd=net_profit_usd,
                trade_amount_usd=trade_amount_usd,
            )

        if expected_out < output_floor:
            return _make_rejection(
                PolicyRejectionReason.OUTPUT_FLOOR_NOT_MET,
                f"expected_out ({expected_out}) cannot satisfy probe output floor ({output_floor})",
                effective_policy,
                amount_in=amount_in,
                expected_out=expected_out,
                price=price,
                gas_usd=gas_usd,
                gas_atoms=gas_atoms,
                net_atoms=net_atoms,
                net_profit_usd=net_profit_usd,
                trade_amount_usd=trade_amount_usd,
                output_floor=output_floor,
            )

    return PolicyDecision(
        approved=True,
        reason=None,
        message="Execution policy satisfied with positive floor coverage",
        policy=effective_policy,
        amount_in=amount_in,
        expected_out=expected_out,
        min_amount_out=output_floor,
        slippage_bps=slippage_bps,
        output_floor=output_floor,
        conservative_gas_usd=gas_usd,
        gas_atoms=gas_atoms,
        net_atoms=net_atoms,
        net_profit_usd=net_profit_usd,
        trade_amount_usd=trade_amount_usd,
        base_asset_usd_price=price,
    )


def enforce_execution_policy(
    amount_in: int,
    expected_out: int,
    decimals: int,
    base_asset_usd_price: Decimal | str | int | None,
    conservative_gas_usd: Decimal | str | int | None = None,
    *,
    policy: ExecutionPolicy | None = None,
    gas_evidence: GasEvidence | None = None,
    hop_quotes: Sequence[HopQuote] = (),
    quote_status: QuoteStatus | str = QuoteStatus.QUOTED,
) -> PolicyDecision:
    """Enforce funds policy, raising ExcessiveAmountError or PolicyViolationError on rejection."""
    decision = evaluate_execution_policy(
        amount_in=amount_in,
        expected_out=expected_out,
        decimals=decimals,
        base_asset_usd_price=base_asset_usd_price,
        conservative_gas_usd=conservative_gas_usd,
        policy=policy,
        gas_evidence=gas_evidence,
        hop_quotes=hop_quotes,
        quote_status=quote_status,
    )
    if not decision.approved:
        raise PolicyViolationError(f"[{decision.reason}] {decision.message}")
    return decision


def _make_rejection(
    reason: PolicyRejectionReason,
    message: str,
    policy: ExecutionPolicy,
    *,
    amount_in: int = 0,
    expected_out: int = 0,
    price: Decimal = Decimal("0"),
    gas_usd: Decimal = Decimal("0"),
    gas_atoms: int = 0,
    net_atoms: int = 0,
    net_profit_usd: Decimal = Decimal("0"),
    trade_amount_usd: Decimal = Decimal("0"),
    output_floor: int = 0,
) -> PolicyDecision:
    """Construct an unapproved PolicyDecision payload."""
    return PolicyDecision(
        approved=False,
        reason=reason,
        message=message,
        policy=policy,
        amount_in=amount_in,
        expected_out=expected_out,
        min_amount_out=0,
        slippage_bps=policy.slippage_bps,
        output_floor=output_floor,
        conservative_gas_usd=gas_usd,
        gas_atoms=gas_atoms,
        net_atoms=net_atoms,
        net_profit_usd=net_profit_usd,
        trade_amount_usd=trade_amount_usd,
        base_asset_usd_price=price,
    )
