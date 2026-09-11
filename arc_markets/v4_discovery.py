"""Uniswap V4 market discovery, multi-pool-per-manager indexing, and pool identity guards."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from arc_markets.deployments import DeploymentsRegistry
from arc_markets.v4_events import V4InitializeEvent, V4PoolKey
from arc_readiness.errors import ArcMarketIneligibleError, ArcValidationError


@dataclass(frozen=True)
class V4DiscoveredPool:
    """Tracked state of a discovered Uniswap V4 pool bound to its PoolManager and 32-byte PoolId."""

    pool_id: str
    manager_address: str
    v4_key: V4PoolKey
    chain_id: int
    created_at_block: int

    @property
    def is_dynamic_fee(self) -> bool:
        return self.v4_key.is_dynamic_fee

    @property
    def has_hooks(self) -> bool:
        return self.v4_key.has_hooks


class V4MarketDiscoveryEngine:
    """Manages V4 pool discovery anchored to PoolManager deployments.

    Invariants:
    - Zero 'one-address-one-pool' assumptions: a single PoolManager hosts unlimited independent PoolIds.
    - Two different PoolIds on the same PoolManager are strictly distinct pools.
    - The same PoolId across two different PoolManagers represents two completely distinct pools.
    - PoolId is a bytes32 hash, NEVER an executable contract address.
    """

    def __init__(
        self,
        chain_id: int = 5042,
        deployments_registry: DeploymentsRegistry | None = None,
    ) -> None:
        self.chain_id = chain_id
        self.deployments = deployments_registry or DeploymentsRegistry(target_chain_id=chain_id)
        # Composite index: (manager_address, pool_id) -> V4DiscoveredPool
        self._pools_by_manager_and_id: dict[tuple[str, str], V4DiscoveredPool] = {}

    @staticmethod
    def assert_not_contract_address(identifier: str) -> None:
        """Reject code query or routing attempts treating a 32-byte PoolId as a 20-byte contract address."""
        clean = identifier.strip().lower()
        if len(clean) == 66 and clean.startswith("0x"):
            raise ArcValidationError(
                f"V4 identity error: {identifier} is a 32-byte PoolId, NOT a contract address. "
                "Calling getCode or sending transactions directly to a PoolId is strictly forbidden."
            )

    def process_initialize(self, event: V4InitializeEvent) -> V4DiscoveredPool:
        """Process a verified V4InitializeEvent, creating an independent discovered pool entry."""
        # 1. Verify manager is a registered manager contract
        manager_rec = self.deployments.get_any(event.manager_address)
        if manager_rec is None or manager_rec.role != "manager":
            raise ArcMarketIneligibleError(
                f"Unauthorized or unknown PoolManager deployment: {event.manager_address}"
            )

        v4_key = V4PoolKey(
            currency0=event.currency0,
            currency1=event.currency1,
            fee=event.fee,
            tick_spacing=event.tick_spacing,
            hooks=event.hooks,
        )

        composite_key = (event.manager_address.lower(), event.pool_id.lower())
        if composite_key in self._pools_by_manager_and_id:
            raise ArcValidationError(
                f"V4 Pool {event.pool_id} already discovered on manager {event.manager_address}"
            )

        discovered = V4DiscoveredPool(
            pool_id=event.pool_id.lower(),
            manager_address=event.manager_address.lower(),
            v4_key=v4_key,
            chain_id=self.chain_id,
            created_at_block=event.block_number,
        )

        self._pools_by_manager_and_id[composite_key] = discovered
        return discovered

    def get_pool(self, manager_address: str, pool_id: str) -> V4DiscoveredPool | None:
        """Query a pool by its exact (manager, pool_id) composite key."""
        return self._pools_by_manager_and_id.get((manager_address.lower(), pool_id.lower()))

    def list_pools_for_manager(self, manager_address: str) -> list[V4DiscoveredPool]:
        norm = manager_address.lower()
        return [p for (m, _), p in self._pools_by_manager_and_id.items() if m == norm]

    def list_all_pools(self) -> list[V4DiscoveredPool]:
        return list(self._pools_by_manager_and_id.values())
