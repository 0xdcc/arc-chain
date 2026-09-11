"""Atomic multi-pool snapshots, frozen epochs, and cross-venue composite state index for Arc."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from arc_markets.quote_catalog import PoolDescriptor
from arc_readiness.errors import ArcMarketIneligibleError, ArcValidationError


@dataclass(frozen=True)
class FrozenEpoch:
    """Immutable block height and hash checkpoint anchoring state consistency."""

    block_number: int
    block_hash: str
    timestamp: float
    chain_id: int = 5042

    def __post_init__(self) -> None:
        if self.block_number < 0:
            raise ArcValidationError(f"block_number cannot be negative: {self.block_number}")
        clean_hash = self.block_hash.strip().lower()
        if not clean_hash.startswith("0x") or len(clean_hash) != 66:
            raise ArcValidationError(f"Invalid block_hash in FrozenEpoch: {self.block_hash}")
        if self.chain_id != 5042 and self.chain_id != 5042002:
            raise ArcValidationError(f"Unauthorized chain_id in FrozenEpoch: {self.chain_id}")


@dataclass(frozen=True)
class PoolSnapshot:
    """Consistent point-in-time state observation of a qualified CLMM pool."""

    descriptor: PoolDescriptor
    epoch: FrozenEpoch
    sqrt_price_x96: int
    tick: int
    liquidity: int
    protocol_fee: int
    lp_fee: int
    is_active: bool

    def __post_init__(self) -> None:
        if self.sqrt_price_x96 <= 0:
            raise ArcValidationError(
                f"sqrt_price_x96 must be strictly positive, got {self.sqrt_price_x96}. "
                "Synthesizing zero-price snapshots is strictly forbidden."
            )
        if self.liquidity < 0:
            raise ArcValidationError(f"liquidity cannot be negative: {self.liquidity}")
        if self.protocol_fee < 0 or self.lp_fee < 0:
            raise ArcValidationError("Fee fields cannot be negative")


@dataclass
class CatalogSnapshot:
    """Collection of pool snapshots strictly verified against a single atomic FrozenEpoch."""

    epoch: FrozenEpoch
    pools: dict[str, PoolSnapshot] = field(default_factory=dict)
    unavailable_reasons: dict[str, str] = field(default_factory=dict)

    def add_snapshot(self, snapshot: PoolSnapshot) -> None:
        """Add a pool snapshot enforcing epoch anti-drift invariants."""
        # 1. Block number and hash must strictly match the catalog epoch
        if (
            snapshot.epoch.block_number != self.epoch.block_number
            or snapshot.epoch.block_hash.lower() != self.epoch.block_hash.lower()
        ):
            reason = (
                f"Block drift detected: snapshot block {snapshot.epoch.block_number} "
                f"({snapshot.epoch.block_hash}) does not match catalog epoch "
                f"{self.epoch.block_number} ({self.epoch.block_hash})"
            )
            self.unavailable_reasons[snapshot.descriptor.composite_id] = reason
            return

        self.pools[snapshot.descriptor.composite_id] = snapshot

    def mark_unavailable(self, descriptor: PoolDescriptor, reason: str) -> None:
        """Mark a pool as unavailable with explicit causal reason."""
        self.unavailable_reasons[descriptor.composite_id] = reason
        self.pools.pop(descriptor.composite_id, None)

    def get_snapshot(self, composite_id: str) -> PoolSnapshot | None:
        return self.pools.get(composite_id)

    def assert_pool_available(self, composite_id: str) -> PoolSnapshot:
        """Return the snapshot or raise ArcMarketIneligibleError. Never returns stale or zero state."""
        if composite_id in self.pools:
            return self.pools[composite_id]

        reason = self.unavailable_reasons.get(composite_id, "Pool not included in catalog snapshot")
        raise ArcMarketIneligibleError(
            f"Pool {composite_id} is unavailable at block {self.epoch.block_number}: {reason}. "
            "Routing through unavailable pools is strictly forbidden."
        )

    @property
    def available_count(self) -> int:
        return len(self.pools)

    @property
    def unavailable_count(self) -> int:
        return len(self.unavailable_reasons)


class CatalogSnapshotManager:
    """Orchestrates atomic catalog snapshot creation across pools with strict anti-drift validation."""

    def __init__(self, chain_id: int = 5042) -> None:
        self.chain_id = chain_id

    def create_empty_snapshot(
        self,
        block_number: int,
        block_hash: str,
        timestamp: float,
    ) -> CatalogSnapshot:
        epoch = FrozenEpoch(
            block_number=block_number,
            block_hash=block_hash,
            timestamp=timestamp,
            chain_id=self.chain_id,
        )
        return CatalogSnapshot(epoch=epoch)

    def assemble_snapshot(
        self,
        epoch: FrozenEpoch,
        descriptors: list[PoolDescriptor],
        observations: dict[str, dict[str, Any]],
    ) -> CatalogSnapshot:
        """Assemble an atomic snapshot from pre-sampled observations."""
        snapshot = CatalogSnapshot(epoch=epoch)

        for desc in descriptors:
            cid = desc.composite_id
            obs = observations.get(cid)

            if not obs:
                snapshot.mark_unavailable(desc, "Missing state observation at target epoch")
                continue

            # Check observed block height
            obs_block = obs.get("block_number")
            obs_hash = obs.get("block_hash")
            if obs_block != epoch.block_number or str(obs_hash).lower() != epoch.block_hash.lower():
                snapshot.mark_unavailable(
                    desc,
                    f"Temporal drift: observed at {obs_block}:{obs_hash}, expected {epoch.block_number}:{epoch.block_hash}",
                )
                continue

            try:
                sqrt_p = int(obs["sqrt_price_x96"])
                tick = int(obs["tick"])
                liq = int(obs.get("liquidity", 0))
                p_fee = int(obs.get("protocol_fee", 0))
                lp_fee = int(obs.get("lp_fee", desc.fee))
                is_act = bool(obs.get("is_active", True) and liq > 0)

                pool_snap = PoolSnapshot(
                    descriptor=desc,
                    epoch=epoch,
                    sqrt_price_x96=sqrt_p,
                    tick=tick,
                    liquidity=liq,
                    protocol_fee=p_fee,
                    lp_fee=lp_fee,
                    is_active=is_act,
                )
                snapshot.add_snapshot(pool_snap)
            except (KeyError, ValueError, ArcValidationError) as e:
                snapshot.mark_unavailable(desc, f"State observation invalid or corrupt: {e}")

        return snapshot
