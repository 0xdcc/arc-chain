"""Qualification, composite identity, and quote catalog bridge for Arc V3 and V4 markets."""

from __future__ import annotations

from dataclasses import dataclass, field

from arbitrage_contracts.identity import AssetRef
from arc_markets.decimals import DecimalsRegistry
from arc_markets.deployments import DeploymentsRegistry
from arc_markets.v3_discovery import V3DiscoveredPool
from arc_markets.v4_discovery import V4DiscoveredPool
from arc_markets.v4_events import _HEX_BYTES32_RE
from arc_readiness.errors import ArcMarketIneligibleError, ArcValidationError

FORBIDDEN_ROBINHOOD_CHAIN_ID: int = 4663
ROBINHOOD_MARKET_SUBSTRINGS: tuple[str, ...] = ("robinhood", "4663")


def _get_asset_address(asset: AssetRef) -> str:
    if asset.token_key is not None:
        return asset.token_key.address.lower()
    return "0x0000000000000000000000000000000000000000"


@dataclass(frozen=True)
class PoolDescriptor:
    """Canonical descriptor of a verified, quote-eligible CLMM pool on Arc."""

    composite_id: str
    pool_id: str
    venue_name: str
    manager_or_factory: str
    token0: str
    token1: str
    fee: int
    tick_spacing: int
    hooks: str
    decimals0: int
    decimals1: int
    is_v4: bool

    def __post_init__(self) -> None:
        if not self.composite_id:
            raise ArcValidationError("composite_id cannot be empty")
        if self.decimals0 < 0 or self.decimals1 < 0:
            raise ArcValidationError("Decimals must be non-negative")
        if self.token0.lower() == self.token1.lower():
            raise ArcValidationError("Self-pairing of identical token addresses is strictly prohibited")


@dataclass
class QualificationReport:
    """Detailed outcome of catalog qualification pass."""

    qualified: list[PoolDescriptor] = field(default_factory=list)
    rejected: dict[str, str] = field(default_factory=dict)


class QuoteCatalogBridge:
    """Bridges discovered V3/V4 pools with deployment proofs and decimals into quote descriptors."""

    def __init__(
        self,
        deployments: DeploymentsRegistry,
        decimals: DecimalsRegistry,
        chain_id: int = 5042,
    ) -> None:
        self.deployments = deployments
        self.decimals = decimals
        self.chain_id = chain_id

    @staticmethod
    def compute_composite_id(venue: str, manager_or_factory: str, pool_id: str) -> str:
        """Derive a collision-free composite pool identifier scoped to venue, host contract, and pool ID."""
        return f"{venue.lower()}:{manager_or_factory.lower()}:{pool_id.lower()}"

    def qualify_v3_pool(self, pool: V3DiscoveredPool) -> PoolDescriptor:
        """Qualify a V3 discovered pool into a PoolDescriptor."""
        # 1. Reject Robinhood pollution
        for sub in ROBINHOOD_MARKET_SUBSTRINGS:
            if sub in pool.factory_address.lower() or sub in pool.pool_address.lower():
                raise ArcMarketIneligibleError(f"Foreign Robinhood identifier detected: {pool.pool_address}")

        # 2. Check Factory deployment verification
        rec = self.deployments.get_any(pool.factory_address)
        if rec is None or rec.role != "factory":
            raise ArcMarketIneligibleError(f"V3 Factory not registered or unverified: {pool.factory_address}")

        # 3. Resolve token decimals
        token0 = _get_asset_address(pool.asset0)
        token1 = _get_asset_address(pool.asset1)
        dec0 = self.decimals.get_decimals(token0)
        dec1 = self.decimals.get_decimals(token1)
        if dec0 is None or dec1 is None:
            raise ArcMarketIneligibleError(
                f"Missing token decimals for V3 pool {pool.pool_address}: token0={dec0}, token1={dec1}"
            )

        composite = self.compute_composite_id("uniswap_v3", pool.factory_address, pool.pool_address)
        raw_fee = pool.fee_model.raw_value if pool.fee_model.raw_value is not None else 0

        return PoolDescriptor(
            composite_id=composite,
            pool_id=pool.pool_address.lower(),
            venue_name="uniswap_v3",
            manager_or_factory=pool.factory_address.lower(),
            token0=token0,
            token1=token1,
            fee=raw_fee,
            tick_spacing=pool.tick_spacing,
            hooks="0x0000000000000000000000000000000000000000",
            decimals0=dec0,
            decimals1=dec1,
            is_v4=False,
        )

    def qualify_v4_pool(self, pool: V4DiscoveredPool) -> PoolDescriptor:
        """Qualify a V4 discovered pool into a PoolDescriptor."""
        # 1. Reject Robinhood pollution
        for sub in ROBINHOOD_MARKET_SUBSTRINGS:
            if sub in pool.manager_address.lower() or sub in pool.pool_id.lower():
                raise ArcMarketIneligibleError(f"Foreign Robinhood identifier detected: {pool.pool_id}")

        # 2. Check PoolManager deployment verification
        rec = self.deployments.get_any(pool.manager_address)
        if rec is None or rec.role != "manager":
            raise ArcMarketIneligibleError(f"V4 PoolManager not registered or unverified: {pool.manager_address}")

        # 2.5 Mathematical Keccak-256 derivation and format verification
        if not isinstance(pool.pool_id, str) or not _HEX_BYTES32_RE.match(pool.pool_id):
            raise ArcMarketIneligibleError(
                f"Malformed V4 pool_id format: claimed {pool.pool_id} must be a 66-char hex bytes32 string"
            )
        try:
            expected_pool_id = pool.v4_key.compute_pool_id()
        except RuntimeError as exc:
            raise ArcMarketIneligibleError(
                f"V4 Keccak derivation failed closed due to missing dependency: {exc}"
            ) from exc
        if pool.pool_id.lower() != expected_pool_id.lower():
            raise ArcMarketIneligibleError(
                f"V4 pool_id keccak derivation mismatch: claimed {pool.pool_id}, expected {expected_pool_id}"
            )

        # 3. Reject unverified dynamic fees or unverified hooks without approval
        if pool.is_dynamic_fee:
            raise ArcMarketIneligibleError(f"Dynamic fee hooks currently unsupported for quote engine: {pool.pool_id}")
        if pool.has_hooks:
            # Hooks require explicit deployment record
            hook_rec = self.deployments.get_any(pool.v4_key.hooks)
            if hook_rec is None or hook_rec.role != "hook":
                raise ArcMarketIneligibleError(f"Unverified V4 hook contract: {pool.v4_key.hooks}")

        # 4. Resolve token decimals (note: currency0 may be native address(0))
        c0 = pool.v4_key.currency0
        c1 = pool.v4_key.currency1

        dec0 = 18 if c0 == "0x0000000000000000000000000000000000000000" else self.decimals.get_decimals(c0)
        dec1 = 18 if c1 == "0x0000000000000000000000000000000000000000" else self.decimals.get_decimals(c1)

        if dec0 is None or dec1 is None:
            raise ArcMarketIneligibleError(
                f"Missing token decimals for V4 pool {pool.pool_id}: c0={dec0}, c1={dec1}"
            )

        composite = self.compute_composite_id("uniswap_v4", pool.manager_address, pool.pool_id)

        return PoolDescriptor(
            composite_id=composite,
            pool_id=pool.pool_id.lower(),
            venue_name="uniswap_v4",
            manager_or_factory=pool.manager_address.lower(),
            token0=c0.lower(),
            token1=c1.lower(),
            fee=pool.v4_key.fee,
            tick_spacing=pool.v4_key.tick_spacing,
            hooks=pool.v4_key.hooks.lower(),
            decimals0=dec0,
            decimals1=dec1,
            is_v4=True,
        )

    def qualify_catalog(
        self,
        v3_pools: list[V3DiscoveredPool],
        v4_pools: list[V4DiscoveredPool],
    ) -> QualificationReport:
        """Run full qualification pass over candidate pools, capturing rejection reasons."""
        report = QualificationReport()

        for p in v3_pools:
            try:
                desc = self.qualify_v3_pool(p)
                report.qualified.append(desc)
            except (ArcMarketIneligibleError, ArcValidationError) as e:
                report.rejected[p.pool_address.lower()] = str(e)

        for v4_pool in v4_pools:
            try:
                desc = self.qualify_v4_pool(v4_pool)
                report.qualified.append(desc)
            except (ArcMarketIneligibleError, ArcValidationError) as e:
                report.rejected[v4_pool.pool_id.lower()] = str(e)

        return report
