"""Uniswap V3 market discovery, pool lifecycle tracking, and minimal log filters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from arc_markets.decimals import DecimalsRegistry
from arc_markets.deployments import DeploymentsRegistry
from arc_markets.v3_events import (
    TOPIC0_V3_INITIALIZE,
    TOPIC0_V3_MINT,
    TOPIC0_V3_POOL_CREATED,
    TOPIC0_V3_SWAP,
    V3InitializeEvent,
    V3MintEvent,
    V3PoolCreatedEvent,
    V3SwapEvent,
)
from arc_readiness.errors import ArcMarketIneligibleError, ArcValidationError
from arbitrage_contracts.identity import (
    AssetRef,
    FeeModel,
    PoolKey,
    TokenKey,
)


@dataclass
class V3DiscoveredPool:
    """Tracked state of a discovered Uniswap V3 liquidity pool on Arc."""

    pool_key: PoolKey
    pool_address: str
    factory_address: str
    asset0: AssetRef
    asset1: AssetRef
    fee_model: FeeModel
    tick_spacing: int
    created_at_block: int
    is_initialized: bool = False
    sqrt_price_x96: int | None = None
    current_tick: int | None = None
    liquidity: int = 0
    liquidity_available: bool = False

    @property
    def is_quoteable(self) -> bool:
        """A pool is strictly quoteable ONLY if initialized and with positive active liquidity."""
        return self.is_initialized and self.liquidity_available and self.liquidity > 0


class V3MarketDiscoveryEngine:
    """Discovers, normalizes, and tracks V3 pools from immutable log events."""

    def __init__(
        self,
        chain_id: int = 5042,
        deployments_registry: DeploymentsRegistry | None = None,
        decimals_registry: DecimalsRegistry | None = None,
    ) -> None:
        self.chain_id = chain_id
        self.deployments = deployments_registry or DeploymentsRegistry(target_chain_id=chain_id)
        self.decimals = decimals_registry or DecimalsRegistry(chain_id=chain_id)
        self._pools_by_address: dict[str, V3DiscoveredPool] = {}
        self._pools_by_key: dict[PoolKey, V3DiscoveredPool] = {}

    def get_minimum_filter_topics(self) -> dict[str, Any]:
        """Generate canonical topic filter set for raw log ingestors."""
        return {
            "topics": [
                [
                    TOPIC0_V3_POOL_CREATED,
                    TOPIC0_V3_INITIALIZE,
                    TOPIC0_V3_MINT,
                    TOPIC0_V3_SWAP,
                ]
            ],
            "filter_version": "v3.1-minimal-arc",
        }

    def process_pool_created(self, event: V3PoolCreatedEvent) -> V3DiscoveredPool:
        """Process a verified V3PoolCreatedEvent and register the initial discovered pool state."""
        # 1. Verify factory is a known deployment
        factory_rec = self.deployments.get_any(event.factory_address)
        if factory_rec is None or factory_rec.role != "factory":
            raise ArcMarketIneligibleError(
                f"Unauthorized or unknown factory address: {event.factory_address}"
            )

        norm_pool_addr = event.pool_address.lower()
        if norm_pool_addr in self._pools_by_address:
            raise ArcValidationError(f"Pool {norm_pool_addr} already discovered")

        # 2. Lookup or default token decimals
        d0 = self.decimals.get_decimals(event.token0)
        d1 = self.decimals.get_decimals(event.token1)
        if d0 is None:
            self.decimals.register_decimals(event.token0, 18, "default_discovery")
        if d1 is None:
            self.decimals.register_decimals(event.token1, 18, "default_discovery")

        tk0 = TokenKey(chain_id=self.chain_id, address=event.token0)
        tk1 = TokenKey(chain_id=self.chain_id, address=event.token1)

        asset0 = AssetRef.erc20(token_key=tk0)
        asset1 = AssetRef.erc20(token_key=tk1)

        fee_model = FeeModel.static(raw_value=event.fee)

        pool_key = PoolKey(
            chain_id=self.chain_id,
            protocol_id="uniswap_v3",
            venue_kind="factory",
            venue_address=event.factory_address,
            pool_id_kind="address",
            pool_id=norm_pool_addr,
        )

        discovered = V3DiscoveredPool(
            pool_key=pool_key,
            pool_address=norm_pool_addr,
            factory_address=event.factory_address.lower(),
            asset0=asset0,
            asset1=asset1,
            fee_model=fee_model,
            tick_spacing=event.tick_spacing,
            created_at_block=event.block_number,
            is_initialized=False,
            liquidity_available=False,
        )

        self._pools_by_address[norm_pool_addr] = discovered
        self._pools_by_key[pool_key] = discovered
        return discovered

    def process_initialize(self, event: V3InitializeEvent) -> V3DiscoveredPool:
        """Apply Initialize event to a discovered pool."""
        norm_pool = event.pool_address.lower()
        if norm_pool not in self._pools_by_address:
            raise ArcMarketIneligibleError(f"Initialize event for undiscovered pool: {norm_pool}")

        pool = self._pools_by_address[norm_pool]
        pool.is_initialized = True
        pool.sqrt_price_x96 = event.sqrt_price_x96
        pool.current_tick = event.tick
        return pool

    def process_mint(self, event: V3MintEvent) -> V3DiscoveredPool:
        """Apply Mint event to track pool active liquidity."""
        norm_pool = event.pool_address.lower()
        if norm_pool not in self._pools_by_address:
            raise ArcMarketIneligibleError(f"Mint event for undiscovered pool: {norm_pool}")

        pool = self._pools_by_address[norm_pool]
        pool.liquidity += event.amount
        if pool.liquidity > 0 and pool.is_initialized:
            pool.liquidity_available = True
        return pool

    def process_swap(self, event: V3SwapEvent) -> V3DiscoveredPool:
        """Apply Swap event updating price, tick, and liquidity."""
        norm_pool = event.pool_address.lower()
        if norm_pool not in self._pools_by_address:
            raise ArcMarketIneligibleError(f"Swap event for undiscovered pool: {norm_pool}")

        pool = self._pools_by_address[norm_pool]
        pool.sqrt_price_x96 = event.sqrt_price_x96
        pool.current_tick = event.tick
        pool.liquidity = event.liquidity
        pool.liquidity_available = pool.liquidity > 0 and pool.is_initialized
        return pool

    def get_pool(self, address: str) -> V3DiscoveredPool | None:
        return self._pools_by_address.get(address.lower())

    def list_all_pools(self) -> list[V3DiscoveredPool]:
        return list(self._pools_by_address.values())

    def list_quoteable_pools(self) -> list[V3DiscoveredPool]:
        return [p for p in self._pools_by_address.values() if p.is_quoteable]
