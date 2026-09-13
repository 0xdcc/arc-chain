"""Tests covering Arc network identity and timestamp ordering invariants (C12, C13)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arc_readiness.errors import ArcNetworkMismatchError, ArcValidationError
from arc_readiness.models import ARC_TESTNET_CHAIN_ID
from arc_readiness.network import (
    ARC_MAINNET_CHAIN_ID,
    validate_block_timestamp_order,
    validate_network_identity,
)

# ==============================================================================
# C12: Arc Chain ID Verification & Rejection of Historical 5040
# ==============================================================================


def test_c12_chain_id_verification() -> None:
    """C12: Validates Arc testnet chain ID 5042002; rejects historical stub 5040 and foreign chains."""
    # Mainnet default guard: default expected_chain_id is ARC_MAINNET_CHAIN_ID (5042)
    validate_network_identity(ARC_MAINNET_CHAIN_ID)
    with pytest.raises(
        ArcNetworkMismatchError, match="Chain ID mismatch: observed 5042002, expected 5042"
    ):
        validate_network_identity(ARC_TESTNET_CHAIN_ID)

    # Valid testnet verification with explicit expected_chain_id
    validate_network_identity(ARC_TESTNET_CHAIN_ID, expected_chain_id=ARC_TESTNET_CHAIN_ID)

    # Rejection of historical stub 5040
    with pytest.raises(ArcNetworkMismatchError, match="Chain ID mismatch: observed 5040"):
        validate_network_identity(5040, expected_chain_id=ARC_TESTNET_CHAIN_ID)

    # Rejection of Ethereum mainnet (1) or BSC (56)
    with pytest.raises(ArcNetworkMismatchError, match="Chain ID mismatch: observed 1"):
        validate_network_identity(1, expected_chain_id=ARC_TESTNET_CHAIN_ID)
    with pytest.raises(ArcNetworkMismatchError, match="Chain ID mismatch: observed 56"):
        validate_network_identity(56, expected_chain_id=ARC_TESTNET_CHAIN_ID)

    # Negative invalid chain ID
    with pytest.raises(ArcValidationError, match="observed_chain_id must be a positive integer"):
        validate_network_identity(-1, expected_chain_id=ARC_TESTNET_CHAIN_ID)


# ==============================================================================
# C13: Non-Decreasing Timestamps & Strict Block Number Ordering
# ==============================================================================


def test_c13_non_decreasing_timestamps_allowed() -> None:
    """C13: Sub-second blocks sharing identical wall-clock timestamp are valid on Arc."""
    # Sub-second consecutive blocks with equal timestamps must pass
    validate_block_timestamp_order(
        prev_timestamp=1757500000,
        curr_timestamp=1757500000,
        prev_block=100,
        curr_block=101,
    )

    # Strictly increasing timestamps pass
    validate_block_timestamp_order(
        prev_timestamp=1757500000,
        curr_timestamp=1757500001,
        prev_block=100,
        curr_block=101,
    )


def test_c13_timestamp_and_block_order_violations() -> None:
    """C13: Retrograde timestamps or non-increasing block numbers must fail-closed."""
    # Retrograde timestamp
    with pytest.raises(ArcValidationError, match="Block timestamps must be non-decreasing"):
        validate_block_timestamp_order(
            prev_timestamp=1757500010,
            curr_timestamp=1757500009,
            prev_block=100,
            curr_block=101,
        )

    # Non-increasing block number
    with pytest.raises(ArcValidationError, match="Block numbers must be strictly increasing"):
        validate_block_timestamp_order(
            prev_timestamp=1757500000,
            curr_timestamp=1757500001,
            prev_block=100,
            curr_block=100,
        )
    with pytest.raises(ArcValidationError, match="Block numbers must be strictly increasing"):
        validate_block_timestamp_order(
            prev_timestamp=1757500000,
            curr_timestamp=1757500001,
            prev_block=101,
            curr_block=100,
        )


# ==============================================================================
# Fixture-driven Verification
# ==============================================================================


def test_fixture_driven_network_vectors() -> None:
    """Verify test cases from rpc_vectors.json fixture."""
    fixture_path = (
        Path(__file__).resolve().parents[1] / "fixtures" / "arc_readiness" / "rpc_vectors.json"
    )
    data = json.loads(fixture_path.read_text(encoding="utf-8"))

    # C12 verification with explicit target expected chain ID
    c12_data = next(v for v in data["vectors"] if v["id"] == "C12_chain_id_cases")
    expected_chain = c12_data["valid"]
    validate_network_identity(c12_data["valid"], expected_chain_id=expected_chain)
    for invalid_chain in c12_data["invalid_chains"]:
        if invalid_chain <= 0:
            with pytest.raises(ArcValidationError):
                validate_network_identity(invalid_chain, expected_chain_id=expected_chain)
        else:
            with pytest.raises(ArcNetworkMismatchError):
                validate_network_identity(invalid_chain, expected_chain_id=expected_chain)
