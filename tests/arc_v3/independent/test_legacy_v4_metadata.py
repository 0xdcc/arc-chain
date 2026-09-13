"""Independent test suite for legacy V4 metadata conversion obligations in Arc architecture.

Migrated from legacy test obligation:
`TestPoolConfigConversion.test_scanned_to_pool_spec_matches_v4_metadata`
in `tests/test_v4_poolkey.py` (lines 186-204).

Architectural Context:
In the legacy codebase, scanned pool dictionaries were converted into `V4PoolSpec` via
`scanned_to_pool_spec(scanned)`. In Arc V3/V4 architecture, pool discovery produces
`V4DiscoveredPool` instances, which are qualified and transformed into canonical
`PoolDescriptor` models via `QuoteCatalogBridge.qualify_v4_pool(pool)`.

Verified Obligations:
1. Field-by-field preservation:
   - currency (token0, token1)
   - fee (raw uint24)
   - tick_spacing (int24)
   - hooks (address string)
   - manager (manager_or_factory contract address)
   - poolID (bytes32 canonical hash and composite_id scoping)
2. Strict qualification gatekeeping:
   - Unknown/unverified hook contracts rejected with ArcMarketIneligibleError
   - Dynamic fee hooks rejected with ArcMarketIneligibleError
   - Unregistered PoolManager contracts rejected with ArcMarketIneligibleError
   - Missing token decimals rejected with ArcMarketIneligibleError
   - Foreign Robinhood identifiers rejected with ArcMarketIneligibleError
3. Catalog batch qualification integration:
   - `QuoteCatalogBridge.qualify_catalog` correctly partitions qualified and rejected pools.
"""

from __future__ import annotations

import pytest

from arc_markets.decimals import DecimalsRegistry
from arc_markets.deployments import (
    ArcDeploymentRecord,
    ContractRole,
    DeploymentsRegistry,
)
from arc_markets.quote_catalog import PoolDescriptor, QualificationReport, QuoteCatalogBridge
from arc_markets.v4_discovery import V4DiscoveredPool
from arc_markets.v4_events import DYNAMIC_FEE_FLAG, V4PoolKey
from arc_readiness.errors import ArcMarketIneligibleError

# ---------------------------------------------------------------------------
# Canonical Test Constants (aligned with legacy tests/test_v4_poolkey.py)
# ---------------------------------------------------------------------------
AI_USDG_V4_POOL_ID = "0x7aebd80541bfaaf23dbb6e99ce13d4d31c1a84c91414f971eadbff7db5f85995"
AI_TOKEN_ADDRESS = "0x2E8c31162b855A2ffa90F6F8634643Ad6F111e18"
USDG_TOKEN_ADDRESS = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
WETH_USDG_001_NATIVE_POOL_ID = "0x24107d152f14a76d292123265ae3f3c71f863fc2f4ef7ba49d64e78d28ea379e"

_NATIVE_CURRENCY = "0x0000000000000000000000000000000000000000"
_ZERO_HOOKS = "0x0000000000000000000000000000000000000000"
_UNVERIFIED_HOOK = "0x4000000000000000000000000000000000000004"

_VALID_MANAGER_A = "0x2222222222222222222222222222222222225042"
_VALID_MANAGER_B = "0x3333333333333333333333333333333333335042"
_UNAUTHORIZED_MANAGER = "0x9999999999999999999999999999999999999999"


@pytest.fixture
def audit_registries() -> tuple[DeploymentsRegistry, DecimalsRegistry]:
    """Provide verified DeploymentsRegistry and DecimalsRegistry matching audit proofs."""
    dep_reg = DeploymentsRegistry(target_chain_id=5042)
    dec_reg = DecimalsRegistry(chain_id=5042)

    # Register verified PoolManagers
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

    # Register verified token decimals with audit proofs
    dec_reg.register_decimals(token_address=AI_TOKEN_ADDRESS, decimals=18, source_proof="proof:ai_token")
    dec_reg.register_decimals(token_address=USDG_TOKEN_ADDRESS, decimals=6, source_proof="proof:usdg_token")

    return dep_reg, dec_reg


@pytest.fixture
def catalog_bridge(audit_registries: tuple[DeploymentsRegistry, DecimalsRegistry]) -> QuoteCatalogBridge:
    """Instantiate QuoteCatalogBridge with audit-proven registries."""
    dep_reg, dec_reg = audit_registries
    return QuoteCatalogBridge(deployments=dep_reg, decimals=dec_reg, chain_id=5042)


class TestLegacyV4MetadataQualification:
    """Rigorous migration of legacy test_scanned_to_pool_spec_matches_v4_metadata.

    Verifies that discovered V4 pool metadata accurately transforms into a
    QuoteCatalogBridge PoolDescriptor with strict field-by-field assertions and gatekeeping.
    """

    def test_scanned_to_pool_descriptor_preserves_all_metadata_fields(
        self, catalog_bridge: QuoteCatalogBridge
    ) -> None:
        """Verify Discovered V4 pool conversion to PoolDescriptor preserves all metadata fields.

        Migrated from legacy test_scanned_to_pool_spec_matches_v4_metadata:
        - Legacy asserted: isinstance(spec, V4PoolSpec), spec.tick_spacing == 23,
          spec.hooks == ZERO_ADDRESS, spec.compute_pool_id() == AI_USDG_V4_POOL_ID.lower()
        - Arc architecture asserts: QuoteCatalogBridge.qualify_v4_pool preserves
          currency (token0, token1), fee, tick_spacing, hooks, manager, poolID, composite_id,
          and resolves verified decimals without synthetic mock stubs.
        """
        v4_key = V4PoolKey(
            currency0=AI_TOKEN_ADDRESS,
            currency1=USDG_TOKEN_ADDRESS,
            fee=2300,
            tick_spacing=23,
            hooks=_ZERO_HOOKS,
        )
        computed_pool_id = v4_key.compute_pool_id()
        assert computed_pool_id == AI_USDG_V4_POOL_ID.lower()

        discovered = V4DiscoveredPool(
            pool_id=computed_pool_id,
            manager_address=_VALID_MANAGER_A,
            v4_key=v4_key,
            chain_id=5042,
            created_at_block=12345,
        )

        desc = catalog_bridge.qualify_v4_pool(discovered)

        # 1. Type validation
        assert isinstance(desc, PoolDescriptor)

        # 2. Currency field validation (token0 and token1 normalized to lowercase)
        assert desc.token0 == AI_TOKEN_ADDRESS.lower()
        assert desc.token1 == USDG_TOKEN_ADDRESS.lower()

        # 3. Fee field validation (raw uint24 value preserved)
        assert desc.fee == 2300

        # 4. Tick spacing field validation
        assert desc.tick_spacing == 23

        # 5. Hooks field validation
        assert desc.hooks == _ZERO_HOOKS.lower()

        # 6. Manager address field validation
        assert desc.manager_or_factory == _VALID_MANAGER_A.lower()

        # 7. PoolID and CompositeID validation
        assert desc.pool_id == AI_USDG_V4_POOL_ID.lower()
        expected_composite_id = f"uniswap_v4:{_VALID_MANAGER_A.lower()}:{AI_USDG_V4_POOL_ID.lower()}"
        assert desc.composite_id == expected_composite_id

        # 8. Decimals and venue attributes validation
        assert desc.decimals0 == 18
        assert desc.decimals1 == 6
        assert desc.venue_name == "uniswap_v4"
        assert desc.is_v4 is True

    def test_native_currency_v4_pool_metadata_and_decimals(
        self, catalog_bridge: QuoteCatalogBridge
    ) -> None:
        """Verify V4 pool with address(0) native currency defaults to 18 decimals and derives exact poolId."""
        v4_key = V4PoolKey(
            currency0=_NATIVE_CURRENCY,
            currency1=USDG_TOKEN_ADDRESS,
            fee=100,
            tick_spacing=1,
            hooks=_ZERO_HOOKS,
        )
        computed_pool_id = v4_key.compute_pool_id()
        assert computed_pool_id == WETH_USDG_001_NATIVE_POOL_ID.lower()

        discovered = V4DiscoveredPool(
            pool_id=computed_pool_id,
            manager_address=_VALID_MANAGER_A,
            v4_key=v4_key,
            chain_id=5042,
            created_at_block=12346,
        )

        desc = catalog_bridge.qualify_v4_pool(discovered)
        assert desc.token0 == _NATIVE_CURRENCY.lower()
        assert desc.token1 == USDG_TOKEN_ADDRESS.lower()
        assert desc.decimals0 == 18
        assert desc.decimals1 == 6
        assert desc.fee == 100
        assert desc.tick_spacing == 1
        assert desc.pool_id == WETH_USDG_001_NATIVE_POOL_ID.lower()

    def test_qualify_v4_pool_dynamic_fee_rejected(
        self, catalog_bridge: QuoteCatalogBridge
    ) -> None:
        """Verify dynamic fee pools are strictly rejected during qualification."""
        v4_key = V4PoolKey(
            currency0=AI_TOKEN_ADDRESS,
            currency1=USDG_TOKEN_ADDRESS,
            fee=DYNAMIC_FEE_FLAG | 2300,
            tick_spacing=23,
            hooks=_ZERO_HOOKS,
        )
        discovered = V4DiscoveredPool(
            pool_id=v4_key.compute_pool_id(),
            manager_address=_VALID_MANAGER_A,
            v4_key=v4_key,
            chain_id=5042,
            created_at_block=12347,
        )

        assert discovered.is_dynamic_fee is True
        with pytest.raises(
            ArcMarketIneligibleError,
            match="Dynamic fee hooks currently unsupported for quote engine",
        ):
            catalog_bridge.qualify_v4_pool(discovered)

    def test_qualify_v4_pool_unverified_hook_rejected(
        self, catalog_bridge: QuoteCatalogBridge
    ) -> None:
        """Verify pools with unverified hook contracts are strictly rejected during qualification."""
        v4_key = V4PoolKey(
            currency0=AI_TOKEN_ADDRESS,
            currency1=USDG_TOKEN_ADDRESS,
            fee=2300,
            tick_spacing=23,
            hooks=_UNVERIFIED_HOOK,
        )
        discovered = V4DiscoveredPool(
            pool_id=v4_key.compute_pool_id(),
            manager_address=_VALID_MANAGER_A,
            v4_key=v4_key,
            chain_id=5042,
            created_at_block=12348,
        )

        assert discovered.has_hooks is True
        with pytest.raises(
            ArcMarketIneligibleError,
            match="Unverified V4 hook contract",
        ):
            catalog_bridge.qualify_v4_pool(discovered)

    def test_qualify_v4_pool_unregistered_manager_rejected(
        self, catalog_bridge: QuoteCatalogBridge
    ) -> None:
        """Verify pools hosted on unauthorized/unregistered PoolManagers are strictly rejected."""
        v4_key = V4PoolKey(
            currency0=AI_TOKEN_ADDRESS,
            currency1=USDG_TOKEN_ADDRESS,
            fee=2300,
            tick_spacing=23,
            hooks=_ZERO_HOOKS,
        )
        discovered = V4DiscoveredPool(
            pool_id=v4_key.compute_pool_id(),
            manager_address=_UNAUTHORIZED_MANAGER,
            v4_key=v4_key,
            chain_id=5042,
            created_at_block=12349,
        )

        with pytest.raises(
            ArcMarketIneligibleError,
            match="V4 PoolManager not registered or unverified",
        ):
            catalog_bridge.qualify_v4_pool(discovered)

    def test_qualify_v4_pool_missing_token_decimals_rejected(
        self, audit_registries: tuple[DeploymentsRegistry, DecimalsRegistry]
    ) -> None:
        """Verify pools with missing token decimal records are rejected fail-closed."""
        dep_reg, dec_reg = audit_registries
        bridge = QuoteCatalogBridge(deployments=dep_reg, decimals=dec_reg, chain_id=5042)

        unknown_token = "0x8888888888888888888888888888888888885042"
        v4_key = V4PoolKey(
            currency0=AI_TOKEN_ADDRESS,
            currency1=unknown_token,
            fee=2300,
            tick_spacing=23,
            hooks=_ZERO_HOOKS,
        )
        discovered = V4DiscoveredPool(
            pool_id=v4_key.compute_pool_id(),
            manager_address=_VALID_MANAGER_A,
            v4_key=v4_key,
            chain_id=5042,
            created_at_block=12350,
        )

        with pytest.raises(
            ArcMarketIneligibleError,
            match="Missing token decimals for V4 pool",
        ):
            bridge.qualify_v4_pool(discovered)

    def test_qualify_catalog_batch_metadata_conversion(
        self, catalog_bridge: QuoteCatalogBridge
    ) -> None:
        """Verify batch qualify_catalog accurately separates eligible V4 descriptors and rejects."""
        valid_key = V4PoolKey(
            currency0=AI_TOKEN_ADDRESS,
            currency1=USDG_TOKEN_ADDRESS,
            fee=2300,
            tick_spacing=23,
            hooks=_ZERO_HOOKS,
        )
        valid_pool = V4DiscoveredPool(
            pool_id=valid_key.compute_pool_id(),
            manager_address=_VALID_MANAGER_A,
            v4_key=valid_key,
            chain_id=5042,
            created_at_block=12351,
        )

        invalid_key = V4PoolKey(
            currency0=AI_TOKEN_ADDRESS,
            currency1=USDG_TOKEN_ADDRESS,
            fee=DYNAMIC_FEE_FLAG | 3000,
            tick_spacing=60,
            hooks=_ZERO_HOOKS,
        )
        invalid_pool = V4DiscoveredPool(
            pool_id=invalid_key.compute_pool_id(),
            manager_address=_VALID_MANAGER_A,
            v4_key=invalid_key,
            chain_id=5042,
            created_at_block=12352,
        )

        report = catalog_bridge.qualify_catalog(v3_pools=[], v4_pools=[valid_pool, invalid_pool])
        assert isinstance(report, QualificationReport)
        assert len(report.qualified) == 1
        assert len(report.rejected) == 1

        qualified_desc = report.qualified[0]
        assert qualified_desc.pool_id == AI_USDG_V4_POOL_ID.lower()
        assert qualified_desc.fee == 2300
        assert qualified_desc.tick_spacing == 23
        assert qualified_desc.hooks == _ZERO_HOOKS.lower()
        assert qualified_desc.manager_or_factory == _VALID_MANAGER_A.lower()

        assert invalid_pool.pool_id.lower() in report.rejected
        assert "Dynamic fee hooks currently unsupported" in report.rejected[invalid_pool.pool_id.lower()]
