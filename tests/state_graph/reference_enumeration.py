"""Independent brute-force reference enumeration benchmark for arbitrage cycles.

Implements an unoptimized combinatorial Cartesian search with zero graph data structures,
used as an un-mocked gold standard to verify the correctness of PoolGraph cycle search.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence

from arbitrage_contracts.identity import AssetRef, PoolDescriptor
from arbitrage_contracts.quote import HopRef, RouteRef


def _get_direction_and_out(
    pool: PoolDescriptor, asset_in: AssetRef
) -> tuple[str, AssetRef] | None:
    if pool.currency0 == asset_in:
        return "zero_for_one", pool.currency1
    elif pool.currency1 == asset_in:
        return "one_for_zero", pool.currency0
    return None


def naive_enumerate_cycles(
    pools: Sequence[PoolDescriptor],
    base_assets: Sequence[AssetRef],
    allowed_hops: Sequence[int] = (2, 3),
) -> set[str]:
    """Brute-force enumerates all valid 2/3-hop arbitrage route IDs from pools list."""
    allowed_set = set(allowed_hops)
    valid_pools = [p for p in pools if p.deployment_status == "deployed"]
    discovered_route_ids: set[str] = set()

    for base_asset in base_assets:
        target_chain_id = base_asset.chain_id

        # 2-hop: test all ordered pairs of distinct pools
        if 2 in allowed_set:
            for p1, p2 in itertools.permutations(valid_pools, 2):
                if (
                    p1.key.chain_id != target_chain_id
                    or p2.key.chain_id != target_chain_id
                ):
                    continue
                step1 = _get_direction_and_out(p1, base_asset)
                if not step1:
                    continue
                dir1, b = step1
                if b == base_asset:
                    continue

                step2 = _get_direction_and_out(p2, b)
                if not step2:
                    continue
                dir2, target = step2
                if target != base_asset:
                    continue

                route = RouteRef(
                    chain_id=target_chain_id,
                    base_asset=base_asset,
                    hops=(
                        HopRef(p1.key, base_asset, b, dir1, p1),
                        HopRef(p2.key, b, base_asset, dir2, p2),
                    ),
                    max_hops=3,
                )
                discovered_route_ids.add(route.route_id)

        # 3-hop: test all ordered triplets of distinct pools
        if 3 in allowed_set:
            for p1, p2, p3 in itertools.permutations(valid_pools, 3):
                if (
                    p1.key.chain_id != target_chain_id
                    or p2.key.chain_id != target_chain_id
                    or p3.key.chain_id != target_chain_id
                ):
                    continue

                step1 = _get_direction_and_out(p1, base_asset)
                if not step1:
                    continue
                dir1, b = step1
                if b == base_asset:
                    continue

                step2 = _get_direction_and_out(p2, b)
                if not step2:
                    continue
                dir2, c = step2
                if c == base_asset or c == b:
                    continue

                step3 = _get_direction_and_out(p3, c)
                if not step3:
                    continue
                dir3, target = step3
                if target != base_asset:
                    continue

                route = RouteRef(
                    chain_id=target_chain_id,
                    base_asset=base_asset,
                    hops=(
                        HopRef(p1.key, base_asset, b, dir1, p1),
                        HopRef(p2.key, b, c, dir2, p2),
                        HopRef(p3.key, c, base_asset, dir3, p3),
                    ),
                    max_hops=3,
                )
                discovered_route_ids.add(route.route_id)

    return discovered_route_ids
