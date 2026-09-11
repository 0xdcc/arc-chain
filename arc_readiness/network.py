"""Network identity verification, block ordering, and L1 state validation for Arc."""

from __future__ import annotations

from arc_readiness.errors import ArcNetworkMismatchError, ArcValidationError
from arc_readiness.models import (
    ARC_TESTNET_CHAIN_ID,
    validate_non_negative_int,
    validate_positive_int,
)


def validate_network_identity(
    observed_chain_id: int,
    expected_chain_id: int = ARC_TESTNET_CHAIN_ID,
) -> None:
    """Verify observed chain ID matches the expected Arc network identity.

    Fails-closed if connected to a legacy/wrong chain (e.g. historical 5040).
    """
    obs = validate_positive_int(observed_chain_id, "observed_chain_id")
    exp = validate_positive_int(expected_chain_id, "expected_chain_id")

    if obs != exp:
        raise ArcNetworkMismatchError(
            f"Chain ID mismatch: observed {obs}, expected {exp}. Rejecting network identity."
        )


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
