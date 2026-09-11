"""Directed multigraph representation of pools and assets for CLMM arbitrage."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from arbitrage_contracts.identity import AssetRef, PoolDescriptor, PoolKey


@dataclass(frozen=True, slots=True)
class PoolEdge:
    """Directed edge representing a single swap direction in a liquidity pool."""

    edge_id: str
    pool_key: PoolKey
    asset_in: AssetRef
    asset_out: AssetRef
    direction: str  # "zero_for_one" or "one_for_zero"
    pool_descriptor: PoolDescriptor


class PoolGraph:
    """Directed multigraph of pools where nodes are assets and edges are swap paths.

    Preserves parallel edges between the same asset pair (e.g. multiple pools with
    different fees or protocols) without performing spot price hard-pruning.
    """

    def __init__(self, pools: Sequence[PoolDescriptor] | None = None) -> None:
        self._adjacency: dict[AssetRef, list[PoolEdge]] = {}
        self._pools: dict[PoolKey, PoolDescriptor] = {}
        if pools:
            for pool in pools:
                self.add_pool(pool)

    def add_pool(self, descriptor: PoolDescriptor) -> None:
        """Adds a pool to the graph, generating both swap directions if deployed."""
        if descriptor.deployment_status != "deployed":
            return

        # Idempotent cleanup if pool already present
        if descriptor.key in self._pools:
            self.remove_pool(descriptor.key)

        self._pools[descriptor.key] = descriptor

        edge_zero_for_one = PoolEdge(
            edge_id=f"{descriptor.key.pool_id}:zero_for_one",
            pool_key=descriptor.key,
            asset_in=descriptor.currency0,
            asset_out=descriptor.currency1,
            direction="zero_for_one",
            pool_descriptor=descriptor,
        )
        edge_one_for_zero = PoolEdge(
            edge_id=f"{descriptor.key.pool_id}:one_for_zero",
            pool_key=descriptor.key,
            asset_in=descriptor.currency1,
            asset_out=descriptor.currency0,
            direction="one_for_zero",
            pool_descriptor=descriptor,
        )

        self._adjacency.setdefault(descriptor.currency0, []).append(edge_zero_for_one)
        self._adjacency.setdefault(descriptor.currency1, []).append(edge_one_for_zero)

    def remove_pool(self, pool_key: PoolKey) -> bool:
        """Removes a pool and all its associated directed edges."""
        if pool_key not in self._pools:
            return False

        del self._pools[pool_key]
        for asset, edges in list(self._adjacency.items()):
            filtered = [e for e in edges if e.pool_key != pool_key]
            if filtered:
                self._adjacency[asset] = filtered
            else:
                del self._adjacency[asset]
        return True

    def has_pool(self, pool_key: PoolKey) -> bool:
        """Checks if a pool is present in the graph."""
        return pool_key in self._pools

    def get_pool(self, pool_key: PoolKey) -> PoolDescriptor | None:
        """Retrieves pool descriptor by key."""
        return self._pools.get(pool_key)

    def get_outgoing_edges(self, asset: AssetRef) -> tuple[PoolEdge, ...]:
        """Returns all directed outgoing edges originating from an asset."""
        return tuple(self._adjacency.get(asset, ()))

    def nodes(self) -> set[AssetRef]:
        """Returns all assets present in the graph with active edges."""
        return set(self._adjacency.keys())

    def pool_count(self) -> int:
        """Returns the number of registered pools."""
        return len(self._pools)

    def edge_count(self) -> int:
        """Returns the total number of directed edges."""
        return sum(len(edges) for edges in self._adjacency.values())

    def all_pools(self) -> tuple[PoolDescriptor, ...]:
        """Returns all registered pool descriptors."""
        return tuple(self._pools.values())
