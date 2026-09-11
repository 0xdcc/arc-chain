"""Arc Event-Driven Incremental Recalculation Engine (T22)

Enforces:
- Dependency tracking via Pool -> Route reverse index
- Event-driven delta recalculation: only routes touching modified pools are re-evaluated
- Same-state binding: prevents attaching stale quotes to new state references
- Truthful negative recording: retains all negative and unsupported routes in audit reports
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from arbitrage_contracts.identity import Amount
from arbitrage_contracts.quote import (
    QuoteEvidence,
    QuoteStatus,
    RouteRef,
)
from arbitrage_contracts.state import canonical_state_ref
from arc_opportunities.quote_bridge import ArcQuoteBridge
from state_graph.types import FrozenEpoch


class IncrementalError(ValueError):
    """Raised for malformed incremental state or route indexing."""


@dataclass(frozen=True, slots=True)
class IncrementalQuoteReport:
    """Report of an incremental recalculation pass."""

    state_ref: str
    recalculated_routes_count: int
    skipped_unaffected_count: int
    quotes: tuple[QuoteEvidence, ...]
    negative_delta_count: int
    unsupported_count: int
    profitable_count: int


class PoolRouteIndex:
    """Reverse index from pool_id to all candidate routes traversing that pool."""

    def __init__(self) -> None:
        self._pool_to_routes: dict[str, set[RouteRef]] = defaultdict(set)
        self._all_routes: set[RouteRef] = set()

    def register_route(self, route: RouteRef) -> None:
        """Register a route and index each of its pool dependencies."""
        self._all_routes.add(route)
        for hop in route.hops:
            self._pool_to_routes[hop.pool_key.pool_id].add(route)

    def register_routes(self, routes: Sequence[RouteRef]) -> None:
        """Batch register multiple routes."""
        for r in routes:
            self.register_route(r)

    def get_affected_routes(self, affected_pool_ids: Sequence[str]) -> tuple[RouteRef, ...]:
        """Return unique routes affected by any of the specified pool IDs."""
        affected: set[RouteRef] = set()
        for pid in affected_pool_ids:
            if pid in self._pool_to_routes:
                affected.update(self._pool_to_routes[pid])
        return tuple(sorted(affected, key=lambda r: r.route_id))

    def remove_pool(self, pool_id: str) -> tuple[RouteRef, ...]:
        """Remove a pool and return all routes invalidated by its removal."""
        invalidated = tuple(self._pool_to_routes.pop(pool_id, ()))
        for r in invalidated:
            self._all_routes.discard(r)
        return invalidated

    @property
    def total_routes(self) -> int:
        return len(self._all_routes)

    @property
    def indexed_pools_count(self) -> int:
        return len(self._pool_to_routes)


class IncrementalQuoteManager:
    """Manages incremental route evaluation triggered by pool state update events."""

    def __init__(self, bridge: ArcQuoteBridge, index: PoolRouteIndex | None = None) -> None:
        self.bridge = bridge
        self.index = index if index is not None else PoolRouteIndex()
        self._latest_quotes: dict[str, QuoteEvidence] = {}
        self._last_state_ref: str | None = None

    def process_pool_events(
        self,
        affected_pool_ids: Sequence[str],
        epoch: FrozenEpoch,
        amount_in: Amount,
        token_decimals: Mapping[Any, int] | None = None,
    ) -> IncrementalQuoteReport:
        """Recalculate only routes affected by the modified pools in epoch.

        Guarantees:
        1. Only affected routes are recalculated; unaffected routes remain in cache.
        2. Clean state_ref binding: new quotes are verified to match epoch canonical state_ref.
        3. All negative delta and unsupported outcomes are preserved in report statistics.
        """
        state_ref = canonical_state_ref(epoch.state_version)
        self._last_state_ref = state_ref

        # Deduplicate event pool IDs
        unique_affected = tuple(dict.fromkeys(affected_pool_ids))
        affected_routes = self.index.get_affected_routes(unique_affected)

        recalculated_quotes: list[QuoteEvidence] = []
        neg_count = 0
        unsupported_count = 0
        profit_count = 0

        for route in affected_routes:
            evidence = self.bridge.quote_exact_input(
                route=route,
                amount_in=amount_in,
                epoch=epoch,
                token_decimals=token_decimals,
            )

            # Enforce state_ref consistency
            if evidence.state_version_ref != state_ref:
                raise IncrementalError(
                    f"Quote state_version_ref mismatch: {evidence.state_version_ref} vs {state_ref}"
                )

            self._latest_quotes[route.route_id] = evidence
            recalculated_quotes.append(evidence)

            if evidence.status == QuoteStatus.UNSUPPORTED:
                unsupported_count += 1
            elif evidence.delta_atoms is not None:
                if evidence.delta_atoms < 0:
                    neg_count += 1
                elif evidence.delta_atoms > 0:
                    profit_count += 1

        skipped_count = self.index.total_routes - len(affected_routes)

        return IncrementalQuoteReport(
            state_ref=state_ref,
            recalculated_routes_count=len(affected_routes),
            skipped_unaffected_count=skipped_count,
            quotes=tuple(recalculated_quotes),
            negative_delta_count=neg_count,
            unsupported_count=unsupported_count,
            profitable_count=profit_count,
        )

    def get_latest_quote(self, route_id: str) -> QuoteEvidence | None:
        """Retrieve the cached quote for a route."""
        return self._latest_quotes.get(route_id)
