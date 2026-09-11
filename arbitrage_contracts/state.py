"""State versioning, consistency barriers, and block cursor contracts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from .identity import (
    validate_bytes32,
    validate_non_negative_integer,
    validate_positive_integer,
)


@dataclass(frozen=True, slots=True)
class Cursor:
    """Precise log-level cursor position within an executed block."""

    block_hash: str
    transaction_hash: str | None = None
    transaction_index: int | None = None
    log_index: int | None = None

    def __init__(
        self,
        block_hash: str,
        transaction_hash: str | None = None,
        transaction_index: int | None = None,
        log_index: int | None = None,
    ) -> None:
        validated_block_hash = validate_bytes32(block_hash)
        validated_tx_hash = None
        if transaction_hash is not None:
            validated_tx_hash = validate_bytes32(transaction_hash)
        validated_tx_index = None
        if transaction_index is not None:
            validated_tx_index = validate_non_negative_integer(
                transaction_index, "transaction_index"
            )
        validated_log_index = None
        if log_index is not None:
            validated_log_index = validate_non_negative_integer(log_index, "log_index")

        object.__setattr__(self, "block_hash", validated_block_hash)
        object.__setattr__(self, "transaction_hash", validated_tx_hash)
        object.__setattr__(self, "transaction_index", validated_tx_index)
        object.__setattr__(self, "log_index", validated_log_index)


class CompletenessBarrier(StrEnum):
    """Watermark state representing the completeness of local state."""

    BOOTSTRAPPING = "bootstrapping"
    SYNCING = "syncing"
    READY = "ready"
    STALE = "stale"
    INVALID = "invalid"
    INCOMPLETE = "incomplete"


class FinalityStatus(StrEnum):
    """Consensus finality status of the recorded block."""

    UNSAFE = "unsafe"
    SAFE = "safe"
    FINALIZED = "finalized"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class StateVersion:
    """Strict state snapshot anchor binding chain, block, hash, and completeness barrier."""

    chain_id: int
    block_domain: str = "l2"
    block_number: int = 0
    block_hash: str = ""
    parent_hash: str | None = None
    epoch_id: str | None = None
    source_ref: str | None = None
    received_at_ms: int = 0
    block_timestamp_s: int | None = None
    applied_cursor: Cursor | None = None
    complete_through_block: int | None = None
    completeness: str = "bootstrapping"
    coverage: tuple[str, ...] = ()
    stale_reasons: tuple[str, ...] = ()
    finality: str = "unknown"
    finality_evidence_ref: str | None = None
    optional_l1_anchor: str | None = None

    def __init__(
        self,
        chain_id: int,
        block_number: int,
        block_hash: str,
        received_at_ms: int,
        block_domain: str = "l2",
        parent_hash: str | None = None,
        epoch_id: str | None = None,
        source_ref: str | None = None,
        block_timestamp_s: int | None = None,
        applied_cursor: Cursor | None = None,
        complete_through_block: int | None = None,
        completeness: str = "bootstrapping",
        coverage: Sequence[str] = (),
        stale_reasons: Sequence[str] = (),
        finality: str = "unknown",
        finality_evidence_ref: str | None = None,
        optional_l1_anchor: str | None = None,
    ) -> None:
        validated_chain_id = validate_positive_integer(chain_id, "chain_id")
        if block_domain not in ("l1", "l2"):
            raise ValueError(f"block_domain must be 'l1' or 'l2', got {block_domain!r}")
        validated_block_number = validate_non_negative_integer(block_number, "block_number")
        validated_block_hash = validate_bytes32(block_hash)
        validated_parent_hash = None
        if parent_hash is not None:
            validated_parent_hash = validate_bytes32(parent_hash)
        validated_received = validate_non_negative_integer(received_at_ms, "received_at_ms")
        validated_timestamp = None
        if block_timestamp_s is not None:
            validated_timestamp = validate_non_negative_integer(
                block_timestamp_s, "block_timestamp_s"
            )
        if applied_cursor is not None and not isinstance(applied_cursor, Cursor):
            raise TypeError("applied_cursor must be an instance of Cursor")
        validated_complete_through = None
        if complete_through_block is not None:
            validated_complete_through = validate_non_negative_integer(
                complete_through_block, "complete_through_block"
            )
        if completeness not in (
            "bootstrapping",
            "syncing",
            "ready",
            "stale",
            "invalid",
            "incomplete",
        ):
            raise ValueError(f"Invalid completeness status: {completeness!r}")
        if completeness == "ready":
            if validated_complete_through is None:
                raise ValueError("StateVersion cannot be ready when complete_through_block is None")
            if validated_complete_through < validated_block_number:
                raise ValueError(
                    f"StateVersion cannot be ready when complete_through_block ({validated_complete_through}) "
                    f"< block_number ({validated_block_number})"
                )
        if finality not in ("unsafe", "safe", "finalized", "unknown"):
            raise ValueError(f"Invalid finality status: {finality!r}")

        object.__setattr__(self, "chain_id", validated_chain_id)
        object.__setattr__(self, "block_domain", block_domain)
        object.__setattr__(self, "block_number", validated_block_number)
        object.__setattr__(self, "block_hash", validated_block_hash)
        object.__setattr__(self, "parent_hash", validated_parent_hash)
        object.__setattr__(self, "epoch_id", epoch_id)
        object.__setattr__(self, "source_ref", source_ref)
        object.__setattr__(self, "received_at_ms", validated_received)
        object.__setattr__(self, "block_timestamp_s", validated_timestamp)
        object.__setattr__(self, "applied_cursor", applied_cursor)
        object.__setattr__(self, "complete_through_block", validated_complete_through)
        object.__setattr__(self, "completeness", completeness)
        object.__setattr__(self, "coverage", tuple(coverage))
        object.__setattr__(self, "stale_reasons", tuple(stale_reasons))
        object.__setattr__(self, "finality", finality)
        object.__setattr__(self, "finality_evidence_ref", finality_evidence_ref)
        object.__setattr__(self, "optional_l1_anchor", optional_l1_anchor)

    def is_ready(self) -> bool:
        """Check whether local state has attained ready status."""
        return self.completeness == "ready"

    def is_reorg_of(self, other: object) -> bool:
        """Detect whether another state version represents an incompatible chain reorganization at the same height."""
        if not isinstance(other, StateVersion):
            return False
        return (
            self.chain_id == other.chain_id
            and self.block_number == other.block_number
            and self.block_hash.lower() != other.block_hash.lower()
        )


_STATE_REF = re.compile(r"state:v1:[0-9a-f]{64}\Z")


def canonical_state_ref(state: StateVersion) -> str:
    """Bind chain, domain, block number/hash and cursor; labels are not identities."""
    if not isinstance(state, StateVersion):
        raise TypeError("state must be StateVersion")
    cursor = state.applied_cursor
    cursor_data = None
    if cursor is not None:
        if cursor.block_hash.lower() != state.block_hash.lower():
            raise ValueError("Cursor block hash mismatch")
        cursor_data = [
            cursor.transaction_hash.lower() if cursor.transaction_hash else None,
            cursor.transaction_index,
            cursor.log_index,
        ]
    payload = [
        state.chain_id,
        state.block_domain,
        state.block_number,
        state.block_hash.lower(),
        cursor_data,
    ]
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return "state:v1:" + hashlib.sha256(raw).hexdigest()


def is_canonical_state_ref(reference: object) -> bool:
    """Check syntax only; provider authenticity must be verified separately."""
    return isinstance(reference, str) and _STATE_REF.fullmatch(reference) is not None


def matches_state_ref(reference: object, state: StateVersion) -> bool:
    """Match the full anchor, never a height, source label or hash substring."""
    if not is_canonical_state_ref(reference):
        return False
    try:
        return reference == canonical_state_ref(state)
    except (ValueError, TypeError):
        return False
