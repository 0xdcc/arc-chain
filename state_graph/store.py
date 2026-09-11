"""State store and deep-immutable epoch management for CLMM state mirror."""

from __future__ import annotations

from collections.abc import Sequence

from arbitrage_contracts.state import StateVersion, canonical_state_ref
from state_graph.types import FrozenEpoch, PoolStateSnapshot


class StateStoreError(Exception):
    """Base exception for state store operations."""


class ChainReorganizationError(StateStoreError):
    """Raised when an incoming state version conflicts with the current chain canonical head."""


def validate_snapshots(
    state: StateVersion,
    snapshots: Sequence[PoolStateSnapshot],
    required_pool_ids: Sequence[str] = (),
) -> tuple[PoolStateSnapshot, ...]:
    """Validate immutable pool coverage against the exact epoch block."""
    canonical_state_ref(state)
    frozen = tuple(snapshots)
    seen: set[str] = set()
    for snapshot in frozen:
        if not isinstance(snapshot, PoolStateSnapshot):
            raise StateStoreError("Invalid pool snapshot type")
        key = snapshot.pool_id.lower()
        if key in seen:
            raise StateStoreError("Duplicate pool snapshot")
        seen.add(key)
        if snapshot.block_number != state.block_number:
            raise StateStoreError("Mixed-block snapshot rejected")
        if snapshot.block_hash.lower() != state.block_hash.lower():
            raise StateStoreError("Snapshot block hash mismatch")
    if state.is_ready() and (not frozen or state.stale_reasons):
        raise StateStoreError("Ready state requires non-stale pool coverage")
    if {key.lower() for key in required_pool_ids} - seen:
        raise StateStoreError("Missing required pool snapshots")
    return frozen


class StateStore:
    """Single-writer state store that manages epoch transitions and immutable snapshots."""

    def __init__(self, initial_epoch: FrozenEpoch | None = None) -> None:
        self._current_epoch: FrozenEpoch | None = None
        self._history: list[FrozenEpoch] = []
        if initial_epoch is not None:
            self.publish_epoch(
                initial_epoch.state_version, initial_epoch.snapshots, initial_epoch.created_at_ms
            )

    def publish_epoch(
        self,
        state_version: StateVersion,
        snapshots: Sequence[PoolStateSnapshot],
        created_at_ms: int | None = None,
    ) -> FrozenEpoch:
        """Publishes a new deep-immutable epoch, validating transitions and checking reorgs."""
        if not isinstance(state_version, StateVersion):
            raise TypeError(
                f"state_version must be StateVersion, got {type(state_version).__name__}"
            )

        immutable_snapshots = validate_snapshots(state_version, snapshots)

        # Check reorg against current epoch
        if self._current_epoch is not None:
            curr_sv = self._current_epoch.state_version
            if (state_version.chain_id, state_version.block_domain) != (
                curr_sv.chain_id,
                curr_sv.block_domain,
            ):
                raise StateStoreError(
                    f"Chain ID mismatch: current epoch chain {curr_sv.chain_id} != incoming {state_version.chain_id}"
                )
            if state_version.is_reorg_of(curr_sv):
                raise ChainReorganizationError(
                    f"Chain reorganization detected at height {state_version.block_number}: "
                    f"current hash {curr_sv.block_hash} != new hash {state_version.block_hash}"
                )
            if state_version.block_number < curr_sv.block_number:
                raise StateStoreError(
                    f"State regression rejected: incoming block_number {state_version.block_number} "
                    f"is lower than current head {curr_sv.block_number}"
                )

            if state_version.block_number > curr_sv.block_number:
                if state_version.block_number != curr_sv.block_number + 1:
                    raise StateStoreError("Block gap requires explicit resync")
                if (
                    not state_version.parent_hash
                    or state_version.parent_hash.lower() != curr_sv.block_hash.lower()
                ):
                    raise ChainReorganizationError("Incoming parent hash mismatch")
            elif canonical_state_ref(state_version) != canonical_state_ref(curr_sv):
                raise StateStoreError("Conflicting same-height cursor requires explicit resync")

        epoch_id = canonical_state_ref(state_version)
        epoch_ts = created_at_ms if created_at_ms is not None else state_version.received_at_ms

        epoch = FrozenEpoch(
            epoch_id=epoch_id,
            state_version=state_version,
            snapshots=immutable_snapshots,
            created_at_ms=epoch_ts,
        )

        self._current_epoch = epoch
        self._history.append(epoch)
        return epoch

    def current_epoch(self) -> FrozenEpoch | None:
        """Returns the most recently published epoch view."""
        return self._current_epoch

    def is_ready(self) -> bool:
        """Checks whether the state store holds a published epoch with ready completeness."""
        if self._current_epoch is None:
            return False
        return self._current_epoch.state_version.is_ready()

    def epoch_count(self) -> int:
        """Returns total count of published epochs."""
        return len(self._history)

    def history(self) -> tuple[FrozenEpoch, ...]:
        """Returns all published epochs in chronological order."""
        return tuple(self._history)

    def clear(self) -> None:
        """Clears current epoch and history (e.g. on full resync)."""
        self._current_epoch = None
        self._history.clear()
