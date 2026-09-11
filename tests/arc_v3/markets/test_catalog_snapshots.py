"""Tests for T18: Quote Catalog Bridge, Pool Descriptors, and Atomic Catalog Snapshots."""

from __future__ import annotations

import pytest

from arc_markets.decimals import DecimalsEntry, DecimalsRegistry
from arc_markets.deployments import (
    ArcDeploymentRecord,
    ContractRole,
    DeploymentsRegistry,
)
from arc_markets.quote_catalog import PoolDescriptor, QuoteCatalogBridge
from arc_markets.snapshots import (
    CatalogSnapshot,
    CatalogSnapshotManager,
    FrozenEpoch,
    PoolSnapshot,
)
from arc_markets.v3_discovery import V3DiscoveredPool
from arc_markets.v4_discovery import V4DiscoveredPool
from arc_markets.v4_events import V4PoolKey
from arc_readiness.errors import ArcMarketIneligibleError, ArcValidationError
from arbitrage_contracts.identity import AssetRef, FeeModel, PoolKey, TokenKey

_VALID_FACTORY = "0x1111111111111111111111111111111111115042"
_VALID_MANAGER_A = "0x2222222222222222222222222222222222225042"
_VALID_MANAGER_B = "0x3333333333333333333333333333333333335042"
_TOKEN_USDC = "0x4444444444444444444444444444444444445042"
_TOKEN_WETH = "0x5555555555555555555555555555555555555042"
_NATIVE_CURRENCY = "0x0000000000000000000000000000000000000000"
_ZERO_HOOKS = "0x0000000000000000000000000000000000000000"


@pytest.fixture
def test_registries() -> tuple[DeploymentsRegistry, DecimalsRegistry]:
    dep_reg = DeploymentsRegistry(target_chain_id=5042)
    dec_reg = DecimalsRegistry(chain_id=5042)

    # Register Factory
    dep_reg.register(
        ArcDeploymentRecord(
            name="arc_v3_factory",
            role=ContractRole.FACTORY,
            address=_VALID_FACTORY,
            chain_id=5042,
            abi_hash="aa" * 32,
            source_proof="genesis:v3_factory",
        )
    )
    # Register Managers
    dep_reg.register(
        ArcDeploymentRecord(
            name="arc_v4_manager_a",
            role=ContractRole.MANAGER,
            address=_VALID_MANAGER_A,
            chain_id=5042,
            abi_hash="bb" * 32,
            source_proof="genesis:v4_manager_a",
        )
    )
    dep_reg.register(
        ArcDeploymentRecord(
            name="arc_v4_manager_b",
            role=ContractRole.MANAGER,
            address=_VALID_MANAGER_B,
            chain_id=5042,
            abi_hash="cc" * 32,
            source_proof="genesis:v4_manager_b",
        )
    )

    # Register Decimals
    dec_reg.register_decimals(token_address=_TOKEN_USDC, decimals=6, source_proof="proof:usdc")
    dec_reg.register_decimals(token_address=_TOKEN_WETH, decimals=18, source_proof="proof:weth")

    return dep_reg, dec_reg


class TestQuoteCatalogBridge:
    """Test suite for catalog qualification and composite identifier mapping."""

    def test_qualify_v3_and_v4_pools_success(self, test_registries) -> None:
        dep_reg, dec_reg = test_registries
        bridge = QuoteCatalogBridge(deployments=dep_reg, decimals=dec_reg, chain_id=5042)

        # V3 pool mock
        v3_pool_addr = "0x" + "66" * 20
        v3_pool = V3DiscoveredPool(
            pool_key=PoolKey(
                chain_id=5042,
                protocol_id="uniswap_v3",
                venue_kind="factory",
                venue_address=_VALID_FACTORY,
                pool_id_kind="address",
                pool_id=v3_pool_addr,
            ),
            pool_address=v3_pool_addr,
            factory_address=_VALID_FACTORY,
            asset0=AssetRef(interface_kind="erc20", chain_id=5042, token_key=TokenKey(5042, _TOKEN_USDC)),
            asset1=AssetRef(interface_kind="erc20", chain_id=5042, token_key=TokenKey(5042, _TOKEN_WETH)),
            fee_model=FeeModel(kind="static", raw_value=3000),
            tick_spacing=60,
            created_at_block=1000,
        )

        # V4 pool mock
        v4_id = "0x" + "77" * 32
        v4_pool = V4DiscoveredPool(
            pool_id=v4_id,
            manager_address=_VALID_MANAGER_A,
            v4_key=V4PoolKey(
                currency0=_NATIVE_CURRENCY,
                currency1=_TOKEN_USDC,
                fee=500,
                tick_spacing=10,
                hooks=_ZERO_HOOKS,
            ),
            chain_id=5042,
            created_at_block=1000,
        )

        report = bridge.qualify_catalog([v3_pool], [v4_pool])
        assert len(report.qualified) == 2
        assert len(report.rejected) == 0

        v3_desc = report.qualified[0]
        assert v3_desc.venue_name == "uniswap_v3"
        assert v3_desc.decimals0 == 6
        assert v3_desc.decimals1 == 18
        assert v3_desc.is_v4 is False
        assert v3_desc.composite_id == f"uniswap_v3:{_VALID_FACTORY.lower()}:{v3_pool_addr.lower()}"

        v4_desc = report.qualified[1]
        assert v4_desc.venue_name == "uniswap_v4"
        assert v4_desc.decimals0 == 18  # Native currency resolved to 18 decimals
        assert v4_desc.decimals1 == 6
        assert v4_desc.is_v4 is True
        assert v4_desc.composite_id == f"uniswap_v4:{_VALID_MANAGER_A.lower()}:{v4_id.lower()}"

    def test_cross_manager_pool_id_collision_prevention(self, test_registries) -> None:
        dep_reg, dec_reg = test_registries
        bridge = QuoteCatalogBridge(deployments=dep_reg, decimals=dec_reg, chain_id=5042)

        identical_pool_id = "0x" + "88" * 32

        pool_a = V4DiscoveredPool(
            pool_id=identical_pool_id,
            manager_address=_VALID_MANAGER_A,
            v4_key=V4PoolKey(_NATIVE_CURRENCY, _TOKEN_USDC, 500, 10, _ZERO_HOOKS),
            chain_id=5042,
            created_at_block=1000,
        )
        pool_b = V4DiscoveredPool(
            pool_id=identical_pool_id,
            manager_address=_VALID_MANAGER_B,
            v4_key=V4PoolKey(_NATIVE_CURRENCY, _TOKEN_USDC, 500, 10, _ZERO_HOOKS),
            chain_id=5042,
            created_at_block=1000,
        )

        desc_a = bridge.qualify_v4_pool(pool_a)
        desc_b = bridge.qualify_v4_pool(pool_b)

        # Composite IDs MUST differ despite identical pool_id:
        assert desc_a.pool_id == desc_b.pool_id
        assert desc_a.composite_id != desc_b.composite_id
        assert _VALID_MANAGER_A.lower() in desc_a.composite_id
        assert _VALID_MANAGER_B.lower() in desc_b.composite_id

    def test_unverified_deployments_and_decimals_rejected(self, test_registries) -> None:
        dep_reg, dec_reg = test_registries
        bridge = QuoteCatalogBridge(deployments=dep_reg, decimals=dec_reg, chain_id=5042)

        bogus_factory = "0x" + "99" * 20
        bogus_pool_addr = "0x" + "aa" * 20
        v3_pool = V3DiscoveredPool(
            pool_key=PoolKey(
                chain_id=5042,
                protocol_id="uniswap_v3",
                venue_kind="factory",
                venue_address=bogus_factory,
                pool_id_kind="address",
                pool_id=bogus_pool_addr,
            ),
            pool_address=bogus_pool_addr,
            factory_address=bogus_factory,
            asset0=AssetRef("erc20", 5042, token_key=TokenKey(5042, _TOKEN_USDC)),
            asset1=AssetRef("erc20", 5042, token_key=TokenKey(5042, _TOKEN_WETH)),
            fee_model=FeeModel(kind="static", raw_value=3000),
            tick_spacing=60,
            created_at_block=1000,
        )

        report = bridge.qualify_catalog([v3_pool], [])
        assert len(report.qualified) == 0
        assert len(report.rejected) == 1
        assert "V3 Factory not registered or unverified" in list(report.rejected.values())[0]

    def test_rejection_of_foreign_robinhood_identifiers(self, test_registries) -> None:
        dep_reg, dec_reg = test_registries
        bridge = QuoteCatalogBridge(deployments=dep_reg, decimals=dec_reg, chain_id=5042)

        v4_pool = V4DiscoveredPool(
            pool_id="0x" + "4663" * 16,
            manager_address=_VALID_MANAGER_A,
            v4_key=V4PoolKey(_NATIVE_CURRENCY, _TOKEN_USDC, 500, 10, _ZERO_HOOKS),
            chain_id=5042,
            created_at_block=1000,
        )

        with pytest.raises(ArcMarketIneligibleError, match="Foreign Robinhood identifier detected"):
            bridge.qualify_v4_pool(v4_pool)


class TestCatalogSnapshots:
    """Test suite for atomic multi-pool snapshots, anti-drift, and failure closure."""

    @pytest.fixture
    def sample_descriptor(self) -> PoolDescriptor:
        return PoolDescriptor(
            composite_id=f"uniswap_v3:{_VALID_FACTORY.lower()}:0x1234",
            pool_id="0x1234",
            venue_name="uniswap_v3",
            manager_or_factory=_VALID_FACTORY.lower(),
            token0=_TOKEN_USDC,
            token1=_TOKEN_WETH,
            fee=3000,
            tick_spacing=60,
            hooks=_ZERO_HOOKS,
            decimals0=6,
            decimals1=18,
            is_v4=False,
        )

    def test_assemble_snapshot_success(self, sample_descriptor: PoolDescriptor) -> None:
        manager = CatalogSnapshotManager(chain_id=5042)
        epoch = FrozenEpoch(block_number=1000, block_hash="0x" + "aa" * 32, timestamp=1700000000.0)

        observations = {
            sample_descriptor.composite_id: {
                "block_number": 1000,
                "block_hash": "0x" + "aa" * 32,
                "sqrt_price_x96": 1 << 96,
                "tick": 100,
                "liquidity": 10_000_000,
                "protocol_fee": 0,
                "lp_fee": 3000,
                "is_active": True,
            }
        }

        snap = manager.assemble_snapshot(epoch, [sample_descriptor], observations)
        assert snap.available_count == 1
        assert snap.unavailable_count == 0

        pool_state = snap.assert_pool_available(sample_descriptor.composite_id)
        assert pool_state.sqrt_price_x96 == 1 << 96
        assert pool_state.tick == 100
        assert pool_state.liquidity == 10_000_000

    def test_temporal_drift_fails_closed(self, sample_descriptor: PoolDescriptor) -> None:
        manager = CatalogSnapshotManager(chain_id=5042)
        epoch = FrozenEpoch(block_number=1000, block_hash="0x" + "aa" * 32, timestamp=1700000000.0)

        # Observation has drifted block number (1001 vs 1000)
        observations = {
            sample_descriptor.composite_id: {
                "block_number": 1001,
                "block_hash": "0x" + "aa" * 32,
                "sqrt_price_x96": 1 << 96,
                "tick": 100,
                "liquidity": 10_000_000,
            }
        }

        snap = manager.assemble_snapshot(epoch, [sample_descriptor], observations)
        assert snap.available_count == 0
        assert snap.unavailable_count == 1

        with pytest.raises(ArcMarketIneligibleError, match="Pool.*is unavailable at block 1000: Temporal drift"):
            snap.assert_pool_available(sample_descriptor.composite_id)

    def test_zero_price_state_rejected_by_invariant(self, sample_descriptor: PoolDescriptor) -> None:
        epoch = FrozenEpoch(block_number=1000, block_hash="0x" + "aa" * 32, timestamp=1700000000.0)

        with pytest.raises(ArcValidationError, match="sqrt_price_x96 must be strictly positive"):
            PoolSnapshot(
                descriptor=sample_descriptor,
                epoch=epoch,
                sqrt_price_x96=0,  # Zero price forbidden!
                tick=0,
                liquidity=100,
                protocol_fee=0,
                lp_fee=0,
                is_active=True,
            )
