"""Internal types and data containers for state mirror and routing."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from arbitrage_contracts.state import StateVersion


class MirrorStatus(StrEnum):
    """Internal sync barrier status of the local state mirror."""

    BOOTSTRAPPING = "bootstrapping"
    SYNCING = "syncing"
    READY = "ready"
    STALE = "stale"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class PoolStateSnapshot:
    """Immutable snapshot of one CLMM pool at a fixed block."""

    pool_id: str
    sqrt_price_x96: int
    tick: int
    liquidity: int
    fee_pips: int
    tick_spacing: int
    block_number: int
    block_hash: str


@dataclass(frozen=True, slots=True)
class SwapStepResult:
    """Result of one single discrete swap step in CLMM math."""

    next_sqrt_price_x96: int
    amount_in: int
    amount_out: int
    fee_amount: int


@dataclass(frozen=True, slots=True)
class FrozenEpoch:
    """Deep-immutable state snapshot anchor binding StateVersion and pool state snapshots."""

    epoch_id: str
    state_version: StateVersion
    snapshots: tuple[PoolStateSnapshot, ...]
    created_at_ms: int

    def get_snapshot(self, pool_id: str) -> PoolStateSnapshot | None:
        """Retrieves a pool snapshot by pool_id."""
        for snap in self.snapshots:
            if snap.pool_id == pool_id:
                return snap
        return None

    def pool_ids(self) -> tuple[str, ...]:
        """Returns all pool IDs recorded in this epoch."""
        return tuple(s.pool_id for s in self.snapshots)
