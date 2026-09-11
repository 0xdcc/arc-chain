"""Bounded 2/3-hop closed-cycle enumeration algorithm for arbitrage."""

from __future__ import annotations

from collections.abc import Sequence

from arbitrage_contracts.identity import AssetRef
from arbitrage_contracts.quote import HopRef, RouteRef
from state_graph.graph import PoolGraph


def find_cycles(
    graph: PoolGraph,
    base_assets: Sequence[AssetRef],
    allowed_hops: Sequence[int] = (2, 3),
    max_routes: int | None = None,
) -> list[RouteRef]:
    """Enumerates bounded 2-hop and 3-hop closed arbitrage cycles anchored at base_assets.

    Enforces:
    - 2-hop cycles: base -> intermediate -> base (must use two distinct pools).
    - 3-hop cycles: base -> b -> c -> base (three distinct assets, three distinct pools).
    - Parallel edge preservation: multiple pools between the same pair are all explored.
    - Strict hop limits: only 2 and 3 hops are supported; 1-hop and >=4-hop are rejected.
    - Deterministic output: sorted by deterministic route_id.
    """
    allowed_set = set(allowed_hops)
    if not allowed_set.issubset({2, 3}):
        raise ValueError(
            f"allowed_hops must be a subset of (2, 3). Got {allowed_hops}. "
            "4-hop cycles are strictly deferred per Roadmap v2 and CR-W4-SCOPE-v1."
        )

    discovered_routes: dict[str, RouteRef] = {}

    for base_asset in base_assets:
        target_chain_id = base_asset.chain_id

        # 1. Two-hop cycles (base -> intermediate -> base)
        if 2 in allowed_set:
            for edge1 in graph.get_outgoing_edges(base_asset):
                if edge1.pool_key.chain_id != target_chain_id:
                    continue
                intermediate = edge1.asset_out
                if intermediate == base_asset:
                    continue

                for edge2 in graph.get_outgoing_edges(intermediate):
                    if (
                        edge2.pool_key.chain_id != target_chain_id
                        or edge2.asset_out != base_asset
                    ):
                        continue
                    # Reject same-pool roundtrip (A -> B -> A in single pool is not arbitrage)
                    if edge2.pool_key == edge1.pool_key:
                        continue

                    hop1 = HopRef(
                        pool_key=edge1.pool_key,
                        asset_in=edge1.asset_in,
                        asset_out=edge1.asset_out,
                        direction=edge1.direction,
                        pool_descriptor=edge1.pool_descriptor,
                    )
                    hop2 = HopRef(
                        pool_key=edge2.pool_key,
                        asset_in=edge2.asset_in,
                        asset_out=edge2.asset_out,
                        direction=edge2.direction,
                        pool_descriptor=edge2.pool_descriptor,
                    )
                    route = RouteRef(
                        chain_id=target_chain_id,
                        base_asset=base_asset,
                        hops=(hop1, hop2),
                        max_hops=3,
                    )
                    discovered_routes[route.route_id] = route

        # 2. Three-hop cycles (base -> b -> c -> base)
        if 3 in allowed_set:
            for edge1 in graph.get_outgoing_edges(base_asset):
                if edge1.pool_key.chain_id != target_chain_id:
                    continue
                b = edge1.asset_out
                if b == base_asset:
                    continue

                for edge2 in graph.get_outgoing_edges(b):
                    if edge2.pool_key.chain_id != target_chain_id:
                        continue
                    c = edge2.asset_out
                    if c == base_asset or c == b:
                        continue
                    if edge2.pool_key == edge1.pool_key:
                        continue

                    for edge3 in graph.get_outgoing_edges(c):
                        if (
                            edge3.pool_key.chain_id != target_chain_id
                            or edge3.asset_out != base_asset
                        ):
                            continue
                        if edge3.pool_key in (edge1.pool_key, edge2.pool_key):
                            continue

                        hop1 = HopRef(
                            pool_key=edge1.pool_key,
                            asset_in=edge1.asset_in,
                            asset_out=edge1.asset_out,
                            direction=edge1.direction,
                            pool_descriptor=edge1.pool_descriptor,
                        )
                        hop2 = HopRef(
                            pool_key=edge2.pool_key,
                            asset_in=edge2.asset_in,
                            asset_out=edge2.asset_out,
                            direction=edge2.direction,
                            pool_descriptor=edge2.pool_descriptor,
                        )
                        hop3 = HopRef(
                            pool_key=edge3.pool_key,
                            asset_in=edge3.asset_in,
                            asset_out=edge3.asset_out,
                            direction=edge3.direction,
                            pool_descriptor=edge3.pool_descriptor,
                        )
                        route = RouteRef(
                            chain_id=target_chain_id,
                            base_asset=base_asset,
                            hops=(hop1, hop2, hop3),
                            max_hops=3,
                        )
                        discovered_routes[route.route_id] = route

    sorted_routes = sorted(discovered_routes.values(), key=lambda r: r.route_id)
    if max_routes is not None and max_routes >= 0:
        return sorted_routes[:max_routes]
    return sorted_routes
