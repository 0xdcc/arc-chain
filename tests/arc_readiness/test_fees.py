"""Tests covering Arc fee accounting and gas cost normalization (C04, C05)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from arc_readiness.errors import ArcValidationError
from arc_readiness.fees import (
    atoms_to_decimal_string,
    calculate_receipt_fee_atoms,
    estimate_max_fee_atoms,
    validate_fee_calculation,
)
from arc_readiness.models import ARC_TESTNET_CHAIN_ID

TEST_BLOCK_HASH = "0x" + "aa" * 32


# ==============================================================================
# C04: Exact Fee Calculation & GasPrice Sabotage Rejection
# ==============================================================================


def test_c04_exact_receipt_fee_atoms() -> None:
    """C04: gas_used=21000, price=21 Gwei -> total=441,000,000,000,000 atoms (0.000441 USDC)."""
    gas_used = 21000
    price_wei = 21_000_000_000  # 21 Gwei in wei

    obs = calculate_receipt_fee_atoms(
        gas_used=gas_used,
        effective_gas_price_wei=price_wei,
        chain_id=ARC_TESTNET_CHAIN_ID,
        block_number=1000,
        block_hash=TEST_BLOCK_HASH,
        base_fee_gwei=20,
        priority_fee_gwei=1,
    )

    expected_total = 441_000_000_000_000
    assert obs.total_fee_atoms == expected_total
    assert obs.gas_used == gas_used
    assert obs.effective_gas_price_wei == price_wei
    assert obs.fee_source == "receipt"
    assert obs.is_estimate is False

    # Formatting verification without float imprecision
    decimal_str = atoms_to_decimal_string(expected_total, decimals=18)
    assert decimal_str == "0.000441"


def test_c04_sabotage_gas_price_alone_rejected() -> None:
    """C04: Rejecting the mistake of treating 21 Gwei gas_price alone as the total fee."""
    gas_used = 21000
    gas_price_wei = 21_000_000_000

    # If someone claims the total fee is just 21_000_000_000 atoms (21 Gwei)
    with pytest.raises(ArcValidationError, match="equals gas_price alone"):
        validate_fee_calculation(
            gas_used=gas_used,
            gas_price_wei=gas_price_wei,
            reported_total=gas_price_wei,
        )

    # Legitimate total matches
    validate_fee_calculation(
        gas_used=gas_used,
        gas_price_wei=gas_price_wei,
        reported_total=gas_used * gas_price_wei,
    )


# ==============================================================================
# C05: Fee Missing Fields & Estimate Separation
# ==============================================================================


def test_c05_missing_fields_and_estimate_separation() -> None:
    """C05: Estimate vs Receipt separation, missing fields preserved as None."""
    # Worst case estimate
    max_fee = estimate_max_fee_atoms(gas_limit=100_000, max_fee_per_gas_wei=30_000_000_000)
    assert max_fee == 3_000_000_000_000_000  # 0.003 USDC

    # Receipt with optional fields omitted
    obs = calculate_receipt_fee_atoms(
        gas_used=50_000,
        effective_gas_price_wei=20_000_000_000,
        chain_id=ARC_TESTNET_CHAIN_ID,
        block_number=2000,
        block_hash=TEST_BLOCK_HASH,
        base_fee_gwei=None,
        priority_fee_gwei=None,
    )
    assert obs.base_fee_gwei is None
    assert obs.priority_fee_gwei is None
    assert obs.total_fee_atoms == 1_000_000_000_000_000  # 0.001 USDC
    assert obs.is_estimate is False


def test_atoms_to_decimal_string_edge_cases() -> None:
    """Verify lossless decimal formatting for zero, negative, and large integers."""
    assert atoms_to_decimal_string(0, 18) == "0.0"
    assert atoms_to_decimal_string(10**18, 18) == "1.0"
    assert atoms_to_decimal_string(10**18 + 5 * 10**17, 18) == "1.5"
    assert atoms_to_decimal_string(123, 6) == "0.000123"
    assert atoms_to_decimal_string(-(10**18), 18) == "-1.0"

    with pytest.raises(ArcValidationError, match="atoms must be an integer"):
        atoms_to_decimal_string("1000", 18)  # type: ignore[arg-type]


# ==============================================================================
# Fixture-driven Verification
# ==============================================================================


def test_fixture_driven_fee_vectors() -> None:
    """Verify C04 vectors from tests/fixtures/arc_readiness/balance_vectors.json."""
    fixture_path = (
        Path(__file__).resolve().parents[1] / "fixtures" / "arc_readiness" / "balance_vectors.json"
    )
    data = json.loads(fixture_path.read_text(encoding="utf-8"))

    for vec in data["vectors"]:
        if "gas_used" not in vec:
            continue
        obs = calculate_receipt_fee_atoms(
            gas_used=vec["gas_used"],
            effective_gas_price_wei=vec["effective_gas_price_wei"],
            chain_id=ARC_TESTNET_CHAIN_ID,
            block_number=3000,
            block_hash=TEST_BLOCK_HASH,
        )
        assert obs.total_fee_atoms is not None
        assert obs.total_fee_atoms == vec["expected_total_atoms"]
        assert atoms_to_decimal_string(obs.total_fee_atoms, 18) == vec["expected_decimal_usdc"]
