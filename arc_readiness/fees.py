"""Arc fee accounting, single-deduction netting, and gas cost normalization in 18d native USDC."""

from __future__ import annotations

from arc_readiness.errors import ArcValidationError
from arc_readiness.models import (
    ArcFeeObservation,
    validate_bytes32,
    validate_non_negative_int,
    validate_optional_non_negative_int,
    validate_positive_int,
)


def calculate_receipt_fee_atoms(
    gas_used: int,
    effective_gas_price_wei: int,
    chain_id: int,
    block_number: int,
    block_hash: str,
    base_fee_gwei: int | None = None,
    priority_fee_gwei: int | None = None,
) -> ArcFeeObservation:
    """Calculate exact transaction fee from execution receipt in 18-decimal native USDC atoms."""
    used = validate_non_negative_int(gas_used, "gas_used")
    price = validate_non_negative_int(effective_gas_price_wei, "effective_gas_price_wei")
    c_id = validate_positive_int(chain_id, "chain_id")
    b_num = validate_non_negative_int(block_number, "block_number")
    b_hash = validate_bytes32(block_hash, "block_hash")
    b_fee = validate_optional_non_negative_int(base_fee_gwei, "base_fee_gwei")
    p_fee = validate_optional_non_negative_int(priority_fee_gwei, "priority_fee_gwei")

    total_atoms = used * price

    return ArcFeeObservation(
        chain_id=c_id,
        block_number=b_num,
        block_hash=b_hash,
        gas_used=used,
        effective_gas_price_wei=price,
        total_fee_atoms=total_atoms,
        base_fee_gwei=b_fee,
        priority_fee_gwei=p_fee,
        fee_source="receipt",
        is_estimate=False,
    )


def apply_single_deduction_netting(
    gross_quote_out_atoms: int,
    amount_in_atoms: int,
    gas_cost_atoms: int,
    dex_fee_already_deducted_in_quoter: bool = True,
    gas_already_deducted: bool = False,
) -> tuple[int, int]:
    """Single-deduction netting guard ensuring DEX fee and gas are deducted exactly once.

    Invariants:
    - Quoter output already accounts for DEX liquidity pool fees; deducting DEX fee again is strictly prohibited.
    - Gas cost must be subtracted exactly once from gross output.
    - Returns (net_profit_atoms, output_floor).
    """
    if gas_already_deducted:
        raise ArcValidationError("Double-deduction violation: Gas cost has already been deducted.")

    if not dex_fee_already_deducted_in_quoter:
        raise ArcValidationError(
            "Unsupported fee configuration: quoter must natively account for DEX pool fee."
        )

    # Calculate output floor: principal + gas_cost + 1 atom pure profit
    output_floor = amount_in_atoms + gas_cost_atoms + 1

    net_profit_atoms = gross_quote_out_atoms - amount_in_atoms - gas_cost_atoms
    return net_profit_atoms, output_floor


def estimate_max_fee_atoms(gas_limit: int, max_fee_per_gas_wei: int) -> int:
    """Calculate worst-case fee cap from gas limit and max fee per gas."""
    limit = validate_non_negative_int(gas_limit, "gas_limit")
    max_price = validate_non_negative_int(max_fee_per_gas_wei, "max_fee_per_gas_wei")
    return limit * max_price


def validate_fee_calculation(gas_used: int, gas_price_wei: int, reported_total: int) -> None:
    """Guard against confusing gas price alone with total fee or decimal mismatches."""
    expected = gas_used * gas_price_wei
    if reported_total == gas_price_wei and gas_used != 1:
        raise ArcValidationError(
            f"Reported fee {reported_total} equals gas_price alone, ignoring gas_used={gas_used}"
        )
    if reported_total != expected:
        raise ArcValidationError(
            f"Reported fee {reported_total} does not match gas_used * gas_price ({expected})"
        )


def atoms_to_decimal_string(atoms: int, decimals: int = 18) -> str:
    """Format an atomic integer into a human-readable decimal string without floating point rounding."""
    if atoms < 0:
        raise ArcValidationError(f"atoms cannot be negative: {atoms}")
    if decimals <= 0:
        return str(atoms)

    s = str(atoms).zfill(decimals + 1)
    integer_part = s[:-decimals]
    fractional_part = s[-decimals:].rstrip("0")
    if fractional_part:
        return f"{integer_part}.{fractional_part}"
    return integer_part
