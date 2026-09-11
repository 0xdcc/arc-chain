"""Network identity verification, block ordering, and L1 state validation for Arc."""

from __future__ import annotations

from typing import TYPE_CHECKING

from arc_readiness.errors import ArcNetworkMismatchError, ArcValidationError
from arc_readiness.models import (
    validate_non_negative_int,
    validate_positive_int,
)

if TYPE_CHECKING:
    from arbitrage_contracts.arc_extensions import NetworkProfile

ARC_MAINNET_CHAIN_ID: int = 5042
ARC_TESTNET_CHAIN_ID: int = 5042002


def validate_network_identity(
    observed_chain_id: int,
    expected_chain_id: int = ARC_MAINNET_CHAIN_ID,
) -> None:
    """Verify observed chain ID matches the expected Arc network identity.

    Fails-closed if connected to a wrong/unexpected chain.
    Silent fallback between mainnet (5042) and testnet (5042002) is strictly forbidden.
    """
    obs = validate_positive_int(observed_chain_id, "observed_chain_id")
    exp = validate_positive_int(expected_chain_id, "expected_chain_id")

    if obs != exp:
        raise ArcNetworkMismatchError(
            f"Chain ID mismatch: observed {obs}, expected {exp}. "
            "Silent fallback between mainnet and testnet is strictly forbidden."
        )


def validate_profile_alignment(
    profile: NetworkProfile,
    observed_chain_id: int,
) -> None:
    """Ensure a NetworkProfile strictly matches the observed on-chain ID."""
    obs = validate_positive_int(observed_chain_id, "observed_chain_id")
    if profile.chain_id != obs:
        raise ArcNetworkMismatchError(
            f"Profile chain_id {profile.chain_id} ({profile.name}) does not match observed chain {obs}."
        )
    if obs == ARC_MAINNET_CHAIN_ID and profile.is_testnet:
        raise ArcValidationError("Mainnet chain 5042 cannot use a profile with is_testnet=True.")
    if obs == ARC_TESTNET_CHAIN_ID and not profile.is_testnet:
        raise ArcValidationError("Testnet chain 5042002 must use a profile with is_testnet=True.")


def validate_block_timestamp_order(
    prev_timestamp: int,
    curr_timestamp: int,
    prev_block: int,
    curr_block: int,
) -> None:
    """Validate Arc block ordering invariants.

    Arc protocol rule:
    Block timestamps are non-decreasing (curr >= prev), NOT strictly increasing,
    because sub-second blocks (~0.5s) may share a wall-clock second timestamp.
    Block numbers, however, must be strictly increasing (curr > prev).
    """
    p_ts = validate_non_negative_int(prev_timestamp, "prev_timestamp")
    c_ts = validate_non_negative_int(curr_timestamp, "curr_timestamp")
    p_b = validate_non_negative_int(prev_block, "prev_block")
    c_b = validate_non_negative_int(curr_block, "curr_block")

    if c_b <= p_b:
        raise ArcValidationError(
            f"Block numbers must be strictly increasing: prev_block={p_b}, curr_block={c_b}"
        )

    if c_ts < p_ts:
        raise ArcValidationError(
            f"Block timestamps must be non-decreasing: prev_ts={p_ts}, curr_ts={c_ts}"
        )
