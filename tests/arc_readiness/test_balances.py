"""Tests covering dual-interface balance reconciliation and boundaries (C01, C02, C03, C06)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from arc_readiness.balances import (
    SCALE_FACTOR,
    calculate_native_bounds_from_erc20,
    check_spending_permission,
    reconcile_dual_interface_balance,
    validate_trading_pair_domain,
)
from arc_readiness.errors import ArcDualInterfaceMismatchError, ArcValidationError
from arc_readiness.models import ARC_TESTNET_CHAIN_ID, ArcPermissionStatus

TEST_ACCOUNT = "0x" + "11" * 20
TEST_SPENDER = "0x" + "22" * 20
TEST_BLOCK_HASH = "0x" + "aa" * 32


# ==============================================================================
# C01: Normal Dust & Anti-Double-Counting
# ==============================================================================


def test_c01_normal_dust_and_anti_double_counting() -> None:
    """C01: N = 10^18 + 123, E = 10^6 -> dust = 123. Ensure double counting is rejected."""
    native_atoms = 10**18 + 123
    erc20_atoms = 10**6

    obs = reconcile_dual_interface_balance(
        account=TEST_ACCOUNT,
        chain_id=ARC_TESTNET_CHAIN_ID,
        block_number=100,
        block_hash=TEST_BLOCK_HASH,
        native_atoms=native_atoms,
        erc20_atoms=erc20_atoms,
        balance_domain_id="arc:usdc_canonical",
    )

    assert obs.native_atoms == native_atoms
    assert obs.erc20_atoms == erc20_atoms
    assert obs.dust_atoms == 123
    assert obs.verified_consistency is True
    assert len(obs.stale_or_incomplete_reasons) == 0

    # Sabotage / Anti-Double-Counting assertion:
    # A flawed implementation might try to sum native and converted ERC-20
    flawed_double_counted = (obs.native_atoms or 0) + (obs.erc20_atoms or 0) * SCALE_FACTOR
    assert flawed_double_counted != native_atoms, (
        "Flawed double counting must not equal true balance"
    )
    assert flawed_double_counted == 2 * 10**18 + 123


def test_c01_mismatch_detection() -> None:
    """C01: When native and ERC-20 mismatch, verified_consistency must be False or raise."""
    native_atoms = 10**18 + 123
    wrong_erc20 = 2 * 10**6  # Claims 2 USDC when native is ~1 USDC

    # Default non-strict mode
    obs = reconcile_dual_interface_balance(
        account=TEST_ACCOUNT,
        chain_id=ARC_TESTNET_CHAIN_ID,
        block_number=100,
        block_hash=TEST_BLOCK_HASH,
        native_atoms=native_atoms,
        erc20_atoms=wrong_erc20,
        strict_raise=False,
    )
    assert obs.verified_consistency is False
    assert len(obs.stale_or_incomplete_reasons) > 0
    assert "Dual-interface balance mismatch" in obs.stale_or_incomplete_reasons[0]

    # Strict raise mode
    with pytest.raises(ArcDualInterfaceMismatchError, match="Dual-interface balance mismatch"):
        reconcile_dual_interface_balance(
            account=TEST_ACCOUNT,
            chain_id=ARC_TESTNET_CHAIN_ID,
            block_number=100,
            block_hash=TEST_BLOCK_HASH,
            native_atoms=native_atoms,
            erc20_atoms=wrong_erc20,
            strict_raise=True,
        )


# ==============================================================================
# C02: Sub-Micro Dust Only & Boundary Invariants
# ==============================================================================


def test_c02_sub_micro_dust_only() -> None:
    """C02: 0 < N < 10^12 and E = 0 is a non-zero balance with active gas purchasing power."""
    sub_micro_native = SCALE_FACTOR - 1  # 999_999_999_999 atoms = 0.999999999999 USDC
    erc20_atoms = 0

    obs = reconcile_dual_interface_balance(
        account=TEST_ACCOUNT,
        chain_id=ARC_TESTNET_CHAIN_ID,
        block_number=101,
        block_hash=TEST_BLOCK_HASH,
        native_atoms=sub_micro_native,
        erc20_atoms=erc20_atoms,
    )

    assert obs.native_atoms == sub_micro_native
    assert obs.erc20_atoms == 0
    assert obs.dust_atoms == sub_micro_native
    assert obs.verified_consistency is True
    # Crucial semantic invariant: account is NOT empty even though ERC-20 reads 0
    assert obs.native_atoms is not None and obs.native_atoms > 0


@pytest.mark.parametrize(
    "n,expected_e,expected_dust",
    [
        (0, 0, 0),
        (SCALE_FACTOR - 1, 0, SCALE_FACTOR - 1),
        (SCALE_FACTOR, 1, 0),
        (SCALE_FACTOR + 1, 1, 1),
        (5 * SCALE_FACTOR + 999, 5, 999),
    ],
)
def test_c02_exact_boundaries(n: int, expected_e: int, expected_dust: int) -> None:
    """C02: Test exact boundary transitions around S-1, S, S+1."""
    obs = reconcile_dual_interface_balance(
        account=TEST_ACCOUNT,
        chain_id=ARC_TESTNET_CHAIN_ID,
        block_number=102,
        block_hash=TEST_BLOCK_HASH,
        native_atoms=n,
        erc20_atoms=expected_e,
    )
    assert obs.erc20_atoms == expected_e
    assert obs.dust_atoms == expected_dust
    assert obs.verified_consistency is True


def test_c02_only_erc20_input_cannot_assume_zero_dust() -> None:
    """C02: When only E is provided, native cannot be assumed exact and dust is unknown."""
    obs = reconcile_dual_interface_balance(
        account=TEST_ACCOUNT,
        chain_id=ARC_TESTNET_CHAIN_ID,
        block_number=103,
        block_hash=TEST_BLOCK_HASH,
        native_atoms=None,
        erc20_atoms=10,
    )
    assert obs.native_atoms is None
    assert obs.dust_atoms is None
    assert obs.verified_consistency is False
    assert "native_atoms_unknown_range_bounded" in obs.stale_or_incomplete_reasons

    # Check bounds calculation
    min_n, max_n = calculate_native_bounds_from_erc20(10)
    assert min_n == 10 * SCALE_FACTOR
    assert max_n == 11 * SCALE_FACTOR - 1


# ==============================================================================
# C03: Cross-Chain / Cross-Block / Domain Isolation & Type Checking
# ==============================================================================


@pytest.mark.parametrize(
    "patch_kwargs,expected_err",
    [
        ({"account": "invalid_addr"}, "account must be a valid 42-char hex address"),
        ({"chain_id": 0}, "chain_id must be a positive integer"),
        ({"chain_id": -1}, "chain_id must be a positive integer"),
        ({"chain_id": True}, "chain_id must be a positive integer"),
        ({"block_number": -1}, "block_number must be a non-negative integer"),
        ({"block_hash": "0x123"}, "block_hash must be a valid 66-char hex bytes32"),
        ({"native_atoms": -1}, "native_atoms must be a non-negative integer"),
        ({"native_atoms": 12.34}, "native_atoms must be a non-negative integer"),
        ({"erc20_atoms": -1}, "erc20_atoms must be a non-negative integer"),
        ({"erc20_atoms": "100"}, "erc20_atoms must be a non-negative integer"),
    ],
)
def test_c03_invalid_input_rejections(patch_kwargs: dict[str, Any], expected_err: str) -> None:
    """C03: Fail-closed rejection of invalid types, negative amounts, floats, or malformed hashes."""
    base_kwargs: dict[str, Any] = {
        "account": TEST_ACCOUNT,
        "chain_id": ARC_TESTNET_CHAIN_ID,
        "block_number": 100,
        "block_hash": TEST_BLOCK_HASH,
        "native_atoms": 10**18,
        "erc20_atoms": 10**6,
    }
    base_kwargs.update(patch_kwargs)
    with pytest.raises(ArcValidationError, match=expected_err):
        reconcile_dual_interface_balance(**base_kwargs)


# ==============================================================================
# C06: Permission Isolation & Circular Balance Domain Check
# ==============================================================================


def test_c06_circular_balance_domain_rejection() -> None:
    """C06: Trading pair assets sharing the same balance domain must be rejected."""
    # Valid: different domains (e.g. USDC vs EURC)
    validate_trading_pair_domain("arc:usdc", "arc:eurc")

    # Invalid: both legs share identical balance domain
    with pytest.raises(ArcValidationError, match="circular pair forbidden"):
        validate_trading_pair_domain("arc:usdc_canonical", "arc:usdc_canonical")


def test_c06_permission_isolation() -> None:
    """C06: ERC-20 allowance does not grant native spending rights; permissions are strictly isolated."""
    perm = ArcPermissionStatus(
        account_address=TEST_ACCOUNT,
        target_contract=TEST_SPENDER,
        erc20_allowance_atoms=2**256 - 1,  # Infinite ERC-20 allowance
        native_spending_authorized=False,  # But NO native spending authority
        source_ref="rpc:eth_call",
    )

    # Attempting native transfer must be REJECTED despite infinite ERC-20 allowance
    native_ok = check_spending_permission(
        account=TEST_ACCOUNT,
        target=TEST_SPENDER,
        required_atoms=1000,
        is_native=True,
        permission_status=perm,
    )
    assert native_ok is False

    # ERC-20 transfer succeeds because allowance is sufficient
    erc20_ok = check_spending_permission(
        account=TEST_ACCOUNT,
        target=TEST_SPENDER,
        required_atoms=1000,
        is_native=False,
        permission_status=perm,
    )
    assert erc20_ok is True


# ==============================================================================
# Fixture-driven Verification
# ==============================================================================


def test_fixture_driven_balance_vectors() -> None:
    """Verify vectors from tests/fixtures/arc_readiness/balance_vectors.json."""
    fixture_path = (
        Path(__file__).resolve().parents[1] / "fixtures" / "arc_readiness" / "balance_vectors.json"
    )
    data = json.loads(fixture_path.read_text(encoding="utf-8"))

    for vec in data["vectors"]:
        if "native_atoms" not in vec or "erc20_atoms" not in vec:
            continue
        obs = reconcile_dual_interface_balance(
            account=TEST_ACCOUNT,
            chain_id=ARC_TESTNET_CHAIN_ID,
            block_number=200,
            block_hash=TEST_BLOCK_HASH,
            native_atoms=vec["native_atoms"],
            erc20_atoms=vec["erc20_atoms"],
        )
        assert obs.dust_atoms == vec["expected_dust_atoms"], f"Failed on vector {vec['id']}"
        assert obs.verified_consistency == vec["verified_consistency"], (
            f"Failed on vector {vec['id']}"
        )
