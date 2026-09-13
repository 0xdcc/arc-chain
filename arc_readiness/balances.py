"""Dual-interface balance reconciliation, dust tracking, and domain validation for Arc."""

from __future__ import annotations

from arc_readiness.errors import ArcDualInterfaceMismatchError, ArcValidationError
from arc_readiness.models import (
    ArcBalanceObservation,
    ArcPermissionStatus,
    validate_address,
    validate_bytes32,
    validate_non_negative_int,
    validate_optional_non_negative_int,
    validate_positive_int,
)

SCALE_FACTOR: int = 1_000_000_000_000  # 10^12: ratio between 18-decimal native and 6-decimal ERC-20


def prevent_balance_double_counting(
    native_atoms: int | None,
    erc20_atoms: int | None,
) -> None:
    """Explicit guard rejecting any operation attempting to sum dual-interface balance views."""
    raise ArcValidationError(
        "Fatal double-counting violation: Native 18d atoms and ERC-20 6d atoms represent "
        "the SAME underlying asset domain and MUST NEVER be added together."
    )


def reconcile_dual_interface_balance(
    account: str,
    chain_id: int,
    block_number: int,
    block_hash: str,
    native_atoms: int | None,
    erc20_atoms: int | None,
    balance_domain_id: str | None = None,
    strict_raise: bool = False,
) -> ArcBalanceObservation:
    """Reconcile observed native (18d) and ERC-20 (6d) balances for the same underlying account.

    Invariants:
    1. If both N and E are observed: E == N // SCALE_FACTOR, dust == N % SCALE_FACTOR.
    2. N and E must NEVER be added together (that would be double-counting).
    3. If only N is observed: E and dust are deterministically derived.
    4. If only E is observed: N cannot be uniquely known (lies in [E*S, (E+1)*S - 1]);
       dust cannot be assumed to be 0.
    """
    valid_account = validate_address(account, "account")
    valid_chain_id = validate_positive_int(chain_id, "chain_id")
    valid_block_number = validate_non_negative_int(block_number, "block_number")
    valid_block_hash = validate_bytes32(block_hash, "block_hash")
    valid_native = validate_optional_non_negative_int(native_atoms, "native_atoms")
    valid_erc20 = validate_optional_non_negative_int(erc20_atoms, "erc20_atoms")

    reasons: list[str] = []
    dust_atoms: int | None = None
    verified_consistency: bool = False

    if valid_native is not None and valid_erc20 is not None:
        expected_erc20 = valid_native // SCALE_FACTOR
        dust_atoms = valid_native % SCALE_FACTOR
        if valid_erc20 == expected_erc20:
            verified_consistency = True
        else:
            verified_consistency = False
            err_msg = (
                f"Dual-interface balance mismatch on block {valid_block_number}: "
                f"native={valid_native} (expected erc20={expected_erc20}), observed erc20={valid_erc20}"
            )
            if strict_raise:
                raise ArcDualInterfaceMismatchError(err_msg)
            reasons.append(err_msg)
    elif valid_native is not None and valid_erc20 is None:
        dust_atoms = valid_native % SCALE_FACTOR
        verified_consistency = True
        reasons.append("derived_erc20_from_native")
    elif valid_native is None and valid_erc20 is not None:
        # N is unknown; only bounded to [E * S, (E + 1) * S - 1]
        dust_atoms = None  # Crucial: dust cannot be assumed 0!
        verified_consistency = False
        reasons.append("native_atoms_unknown_range_bounded")
    else:
        verified_consistency = False
        reasons.append("no_balance_observed")

    return ArcBalanceObservation(
        account_address=valid_account,
        chain_id=valid_chain_id,
        block_number=valid_block_number,
        block_hash=valid_block_hash,
        balance_domain_id=balance_domain_id or "arc_usdc_unified",
        native_atoms=valid_native,
        erc20_atoms=valid_erc20,
        dust_atoms=dust_atoms,
        verified_consistency=verified_consistency,
        stale_or_incomplete_reasons=tuple(reasons),
    )


def calculate_native_bounds_from_erc20(erc20_atoms: int) -> tuple[int, int]:
    """Calculate the inclusive [min, max] native atoms represented by an ERC-20 6-decimal balance."""
    val = validate_non_negative_int(erc20_atoms, "erc20_atoms")
    min_native = val * SCALE_FACTOR
    max_native = (val + 1) * SCALE_FACTOR - 1
    return min_native, max_native


def validate_trading_pair_domain(base_domain_id: str | None, quote_domain_id: str | None) -> None:
    """Ensure base and quote assets do not share the exact same underlying balance domain.

    Trading native USDC against ERC-20 USDC in a pool is meaningless and economically circular.
    """
    if base_domain_id and quote_domain_id and base_domain_id == quote_domain_id:
        raise ArcValidationError(
            f"Trading pair assets share identical balance domain {base_domain_id!r}: circular pair forbidden"
        )


def check_spending_permission(
    account: str,
    target: str,
    required_atoms: int,
    is_native: bool,
    permission_status: ArcPermissionStatus,
) -> bool:
    """Check if an action is authorized, enforcing physical separation between ERC-20 and native authority."""
    req = validate_non_negative_int(required_atoms, "required_atoms")
    acc = validate_address(account, "account")
    tgt = validate_address(target, "target")

    if permission_status.account_address != acc or permission_status.target_contract != tgt:
        raise ArcValidationError("PermissionStatus account or target mismatch")

    if is_native:
        # Native transfer permission is an explicit protocol capability, NEVER derived from ERC-20 allowance
        return permission_status.native_spending_authorized
    else:
        # ERC-20 transferFrom requires sufficient allowance
        if permission_status.erc20_allowance_atoms is None:
            return False
        return permission_status.erc20_allowance_atoms >= req


def validate_spending_authorization(
    interface_kind: str,
    has_erc20_allowance: bool,
    has_native_authorization: bool,
) -> None:
    """Verify spending authorization respecting the physical separation between native and ERC-20."""
    norm_kind = interface_kind.lower()
    if norm_kind == "native":
        if not has_native_authorization:
            if has_erc20_allowance:
                raise ArcValidationError(
                    "ERC-20 allowance does not grant native spending authorization. "
                    "Native transfers require explicit native authorization."
                )
            raise ArcValidationError("Native spending authorization missing.")
    elif norm_kind == "erc20":
        if not has_erc20_allowance:
            raise ArcValidationError("ERC-20 allowance missing or insufficient.")
    else:
        raise ArcValidationError(f"Unknown interface_kind: {interface_kind}")
