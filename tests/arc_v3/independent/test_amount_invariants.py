"""Independent unit tests for legacy guard funds safety obligations.

Decoupled from tax catalog harness. Addresses genuine safety gaps and behavioral
changes identified during the audit of tests/test_guard.py against current production APIs:
- atomic_execution.policy: ExecutionPolicy, evaluate_execution_policy, calculate_output_floor, validate_atoms

Integrated in G3 from candidate_amount_invariants.py (verified in G2 /tmp/arc-g2-amount-review/RESULT.md).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from atomic_execution.policy import (
    ExcessiveAmountError,
    ExecutionPolicy,
    PolicyRejectionReason,
    calculate_output_floor,
    evaluate_execution_policy,
    validate_atoms,
)


def test_candidate_guard_execution_policy_hard_cap_initialization_fail_closed() -> None:
    """Verify that configuring max_amount_usd > 500U raises ExcessiveAmountError fail-closed.

    Replaces legacy WalletGuard silent clamping (guard.max_amount_usd == 500.0)
    with strict fail-closed refusal per AGENTS.md Section 2.4 (behavioral change).
    """
    # Boundary exactly at 500.0 USD passes
    policy_500 = ExecutionPolicy(max_amount_usd=Decimal("500.0"))
    assert policy_500.max_amount_usd == Decimal("500.0")

    # Lower limit (e.g. 250 USD) passes
    policy_250 = ExecutionPolicy(max_amount_usd=Decimal("250.0"))
    assert policy_250.max_amount_usd == Decimal("250.0")

    # Exceeding hard limit raises ExcessiveAmountError immediately at init
    with pytest.raises(ExcessiveAmountError, match=r"exceeds hard limit"):
        ExecutionPolicy(max_amount_usd=Decimal("500.01"))

    with pytest.raises(ExcessiveAmountError, match=r"exceeds hard limit"):
        ExecutionPolicy(max_amount_usd=Decimal("2000.0"))


def test_candidate_guard_evaluate_policy_non_positive_amount_in_rejected() -> None:
    """Verify that zero or negative amount_in fails closed.

    Directly addresses gap from legacy test_non_positive_amount, ensuring
    evaluate_execution_policy returns POLICY_VIOLATION without throwing.
    """
    policy = ExecutionPolicy()

    # Zero amount_in rejected
    decision_zero = evaluate_execution_policy(
        amount_in=0,
        expected_out=1_000_000,
        decimals=18,
        base_asset_usd_price=Decimal("2500.0"),
        conservative_gas_usd=Decimal("0.50"),
        policy=policy,
    )
    assert decision_zero.approved is False
    assert decision_zero.reason == PolicyRejectionReason.POLICY_VIOLATION
    assert "amount_in must be a positive uint256" in decision_zero.message

    # Negative amount_in rejected
    decision_neg = evaluate_execution_policy(
        amount_in=-50_000_000,
        expected_out=1_000_000,
        decimals=18,
        base_asset_usd_price=Decimal("2500.0"),
        conservative_gas_usd=Decimal("0.50"),
        policy=policy,
    )
    assert decision_neg.approved is False
    assert decision_neg.reason == PolicyRejectionReason.POLICY_VIOLATION

    # Direct atom validator raises ValueError on non-positive
    with pytest.raises(ValueError, match=r"amount_in out of bounds \[1, 2\^256-1\]: 0"):
        validate_atoms(0, "amount_in", positive=True)

    with pytest.raises(ValueError, match=r"amount_in out of bounds \[1, 2\^256-1\]: -100"):
        validate_atoms(-100, "amount_in", positive=True)


def test_candidate_guard_calculate_output_floor_non_positive_inputs_rejected() -> None:
    """Verify calculate_output_floor strictly rejects non-positive amount_in, expected_out, or 0 slippage."""
    # Zero amount_in
    with pytest.raises(ValueError, match=r"amount_in out of bounds \[1, 2\^256-1\]: 0"):
        calculate_output_floor(
            amount_in=0,
            expected_out=1_000_000,
            slippage_bps=50,
            decimals=18,
            base_asset_usd_price=Decimal("2500.0"),
            conservative_gas_usd=Decimal("0.50"),
        )

    # Zero expected_out
    with pytest.raises(ValueError, match=r"expected_out out of bounds \[1, 2\^256-1\]: 0"):
        calculate_output_floor(
            amount_in=1_000_000,
            expected_out=0,
            slippage_bps=50,
            decimals=18,
            base_asset_usd_price=Decimal("2500.0"),
            conservative_gas_usd=Decimal("0.50"),
        )

    # Zero slippage (iron rule: zero slippage forbidden)
    with pytest.raises(ValueError, match=r"slippage_bps must be between 1 and 500, got 0"):
        calculate_output_floor(
            amount_in=1_000_000,
            expected_out=1_000_000,
            slippage_bps=0,
            decimals=18,
            base_asset_usd_price=Decimal("2500.0"),
            conservative_gas_usd=Decimal("0.50"),
        )
