"""Unit tests for StateStore and FrozenEpoch immutable consistency."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from arbitrage_contracts.state import Cursor, StateVersion
from state_graph.store import ChainReorganizationError, StateStore
from state_graph.types import FrozenEpoch, PoolStateSnapshot


def make_snapshot(
    pool_id: str, block_num: int = 100, block_hash: str = "0x" + "aa" * 32
) -> PoolStateSnapshot:
    return PoolStateSnapshot(
        pool_id=pool_id,
        sqrt_price_x96=1 << 96,
        tick=0,
        liquidity=10**18,
        fee_pips=500,
        tick_spacing=10,
        block_number=block_num,
        block_hash=block_hash,
    )


def test_frozen_epoch_deep_immutability() -> None:
    """Verifies that mutating the input list/objects does not mutate FrozenEpoch."""
    snap1 = make_snapshot("pool_1")
    snap2 = make_snapshot("pool_2")
    input_list = [snap1, snap2]

    cursor = Cursor(block_hash="0x" + "aa" * 32, log_index=5)
    state = StateVersion(
        chain_id=4663,
        block_number=100,
        block_hash="0x" + "aa" * 32,
        received_at_ms=1000,
        applied_cursor=cursor,
        complete_through_block=100,
        completeness="ready",
    )

    store = StateStore()
    epoch = store.publish_epoch(state, input_list)

    assert epoch.pool_ids() == ("pool_1", "pool_2")
    assert epoch.get_snapshot("pool_1") == snap1
    assert epoch.get_snapshot("pool_2") == snap2
    assert epoch.get_snapshot("non_existent") is None

    # External mutation attempt 1: append to input list
    snap3 = make_snapshot("pool_3")
    input_list.append(snap3)
    assert epoch.pool_ids() == ("pool_1", "pool_2")
    assert epoch.get_snapshot("pool_3") is None

    # External mutation attempt 2: clear input list
    input_list.clear()
    assert len(epoch.snapshots) == 2

    # Immutability of epoch itself
    with pytest.raises(FrozenInstanceError):
        epoch.epoch_id = "mutated"  # type: ignore[misc]


def test_state_store_lifecycle_and_is_ready() -> None:
    store = StateStore()
    assert store.current_epoch() is None
    assert store.is_ready() is False
    assert store.epoch_count() == 0

    # 1. Bootstrapping / Syncing
    state_syncing = StateVersion(
        chain_id=4663,
        block_number=100,
        block_hash="0x" + "11" * 32,
        received_at_ms=1000,
        completeness="syncing",
    )
    e1 = store.publish_epoch(
        state_syncing, [make_snapshot("p1", block_hash=state_syncing.block_hash)]
    )
    assert store.current_epoch() == e1
    assert store.is_ready() is False
    assert store.epoch_count() == 1

    # 2. Ready
    state_ready = StateVersion(
        chain_id=4663,
        block_number=101,
        block_hash="0x" + "22" * 32,
        parent_hash=state_syncing.block_hash,
        received_at_ms=2000,
        complete_through_block=101,
        completeness="ready",
    )
    e2 = store.publish_epoch(state_ready, [make_snapshot("p1", 101, state_ready.block_hash)])
    assert store.current_epoch() == e2
    assert store.is_ready() is True
    assert store.epoch_count() == 2
    assert store.history() == (e1, e2)


def test_chain_reorg_detection() -> None:
    """Verifies that an incoming state at same height with different hash triggers reorg error."""
    store = StateStore()

    state_canon = StateVersion(
        chain_id=4663,
        block_number=500,
        block_hash="0x" + "aa" * 32,
        received_at_ms=1000,
        complete_through_block=500,
        completeness="ready",
    )
    store.publish_epoch(state_canon, [make_snapshot("p1", 500)])

    # Fork block at height 500 with different hash
    state_fork = StateVersion(
        chain_id=4663,
        block_number=500,
        block_hash="0x" + "bb" * 32,
        received_at_ms=1001,
        complete_through_block=500,
        completeness="ready",
    )

    with pytest.raises(
        ChainReorganizationError, match="Chain reorganization detected at height 500"
    ):
        store.publish_epoch(state_fork, [make_snapshot("p1", 500, state_fork.block_hash)])


def test_state_store_clear() -> None:
    store = StateStore()
    state = StateVersion(
        chain_id=4663,
        block_number=100,
        block_hash="0x" + "11" * 32,
        received_at_ms=1000,
    )
    store.publish_epoch(state, [])
    assert store.epoch_count() == 1

    store.clear()
    assert store.epoch_count() == 0
    assert store.current_epoch() is None
    assert len(store.history()) == 0
