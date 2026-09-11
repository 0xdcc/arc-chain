"""Reverse index and dirty route invalidation manager for state graph."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from arbitrage_contracts.identity import AssetRef, PoolDescriptor, PoolKey
from arbitrage_contracts.quote import RouteRef
from state_graph.cycles import find_cycles
from state_graph.graph import PoolGraph


class DirtyRouteIndex:
    """Maintains a bidirectional mapping between PoolKey and RouteRef.

    Enables:
    - O(1) retrieval of routes affected by specific dirty pools without full graph scans.
    - Zero evaluation overhead for unaffected routes when disjoint subgraphs change.
    - Complete and deterministic route invalidation on pool removal or state revocation.
    - Incremental route discovery when new pools are deployed.
    """

    def __init__(self, routes: Sequence[RouteRef] | None = None) -> None:
        self._routes_by_id: dict[str, RouteRef] = {}
        self._route_ids_by_pool: dict[PoolKey, set[str]] = {}
        self._pools_by_route_id: dict[str, set[PoolKey]] = {}
        if routes:
            self.add_routes(routes)

    def add_route(self, route: RouteRef) -> bool:
        """Adds a route to the index. Returns True if newly added, False if already present."""
        if route.route_id in self._routes_by_id:
            return False

        self._routes_by_id[route.route_id] = route
        pool_keys: set[PoolKey] = set()

        for hop in route.hops:
            pool_keys.add(hop.pool_key)
            self._route_ids_by_pool.setdefault(hop.pool_key, set()).add(route.route_id)

        self._pools_by_route_id[route.route_id] = pool_keys
        return True

    def add_routes(self, routes: Iterable[RouteRef]) -> int:
        """Adds multiple routes to the index. Returns count of newly added routes."""
        added = 0
        for r in routes:
            if self.add_route(r):
                added += 1
        return added

    def remove_route(self, route_id: str) -> bool:
        """Removes a single route by ID from all indices."""
        if route_id not in self._routes_by_id:
            return False

        del self._routes_by_id[route_id]
        associated_pools = self._pools_by_route_id.pop(route_id, set())

        for p_key in associated_pools:
            routes_set = self._route_ids_by_pool.get(p_key)
            if routes_set:
                routes_set.discard(route_id)
                if not routes_set:
                    del self._route_ids_by_pool[p_key]
        return True

    def get_affected_routes(self, dirty_pools: Iterable[PoolKey]) -> list[RouteRef]:
        """Returns all routes that traverse at least one of the supplied dirty pools.

        Results are deterministic and sorted by route_id.
        If dirty_pools has zero intersection with any route, returns an empty list.
        """
        affected_ids: set[str] = set()
        for p_key in dirty_pools:
            r_ids = self._route_ids_by_pool.get(p_key)
            if r_ids:
                affected_ids.update(r_ids)

        if not affected_ids:
            return []

        affected_routes = [self._routes_by_id[rid] for rid in affected_ids if rid in self._routes_by_id]
        affected_routes.sort(key=lambda r: r.route_id)
        return affected_routes

    def invalidate_pool(self, pool_key: PoolKey) -> list[str]:
        """Invalidates and removes all routes depending on the given pool key.

        Returns list of invalidated route_ids.
        """
        affected_ids = set(self._route_ids_by_pool.get(pool_key, ()))
        for rid in affected_ids:
            self.remove_route(rid)
        return sorted(affected_ids)

    def update_topology_add_pool(
        self,
        new_pool: PoolDescriptor,
        graph: PoolGraph,
        base_assets: Sequence[AssetRef],
        allowed_hops: Sequence[int] = (2, 3),
    ) -> list[RouteRef]:
        """Updates graph and incrementally discovers and indexes newly formed routes containing new_pool."""
        graph.add_pool(new_pool)
        all_new_routes = find_cycles(graph, base_assets, allowed_hops=allowed_hops)
        # Filter for routes that actually traverse new_pool.key
        truly_new_routes: list[RouteRef] = []
        for r in all_new_routes:
            if any(h.pool_key == new_pool.key for h in r.hops):
                if self.add_route(r):
                    truly_new_routes.append(r)
        return truly_new_routes

    def update_topology_remove_pool(
        self,
        pool_key: PoolKey,
        graph: PoolGraph,
    ) -> list[str]:
        """Removes pool from graph and invalidates all associated routes from index."""
        graph.remove_pool(pool_key)
        return self.invalidate_pool(pool_key)

    def total_routes(self) -> int:
        """Returns total count of indexed routes."""
        return len(self._routes_by_id)

    def indexed_pool_count(self) -> int:
        """Returns total count of distinct pools referenced by indexed routes."""
        return len(self._route_ids_by_pool)

    def has_route(self, route_id: str) -> bool:
        """Checks if a route ID is indexed."""
        return route_id in self._routes_by_id

    def get_route(self, route_id: str) -> RouteRef | None:
        """Retrieves an indexed route by ID."""
        return self._routes_by_id.get(route_id)

    def all_routes(self) -> list[RouteRef]:
        """Returns all indexed routes sorted by route_id."""
        routes = list(self._routes_by_id.values())
        routes.sort(key=lambda r: r.route_id)
        return routes

    def clear(self) -> None:
        """Clears all indexed routes."""
        self._routes_by_id.clear()
        self._route_ids_by_pool.clear()
        self._pools_by_route_id.clear()
