"""Independent test suite verifying Uniswap V4 PoolId Keccak-256 integrity and batch admission.

Validates:
1. Exact EVM Keccak-256 derivation binding between 5-tuple PoolKey and 32-byte PoolId.
2. Production batch qualification (qualify_catalog) isolating tampered pool_ids in middle/end
   without contaminating eligible pool descriptors or crashing the batch pipeline.
3. Discovery engine (process_initialize) fail-closed rejection of tampered events before any
   state write or indexing occurs, guaranteeing zero state pollution.
4. EVM standard case-insensitive bytes32 semantics and rejection of malformed / short IDs.
5. Preservation of all orthogonal safety gates (Robinhood, deployments, dynamic fees, hooks, decimals).

Expected pool_id values are established via independent reference Keccak-256 calculations and
canonical fixed test vectors, strictly independent of the unit under test.
"""

from __future__ import annotations

import pytest
from eth_abi.abi import encode as independent_abi_encode
from eth_utils.crypto import keccak as independent_keccak

from arc_markets.decimals import DecimalsRegistry
from arc_markets.deployments import (
    ArcDeploymentRecord,
    ContractRole,
    DeploymentsRegistry,
)
from arc_markets.quote_catalog import PoolDescriptor, QualificationReport, QuoteCatalogBridge
from arc_markets.v4_discovery import (
    V4DiscoveredPool,
    V4MarketDiscoveryEngine,
)
from arc_markets.v4_events import (
    DYNAMIC_FEE_FLAG,
    V4InitializeEvent,
    V4PoolKey,
)
from arc_readiness.errors import (
    ArcMarketIneligibleError,
    ArcValidationError,
)

# ---------------------------------------------------------------------------
# Independent Reference Implementation & Canonical Vectors
# ---------------------------------------------------------------------------

def _independent_evm_keccak(c0: str, c1: str, fee: int, tick_spacing: int, hooks: str) -> str:
    """Independent reference EVM Keccak-256 calculation for standard Uniswap V4 PoolKey."""
    clean_c0 = c0.lower()
    clean_c1 = c1.lower()
    clean_hooks = hooks.lower()
    encoded = independent_abi_encode(
        ["address", "address", "uint24", "int24", "address"],
        [clean_c0, clean_c1, fee, tick_spacing, clean_hooks],
    )
    return "0x" + independent_keccak(encoded).hex()


AI_TOKEN = "0x2E8c31162b855A2ffa90F6F8634643Ad6F111e18"
USDG_TOKEN = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
PONS_TOKEN = "0x39dBED3a2bd333467115dE45665cC57F813C4571"
NATIVE_CURRENCY = "0x0000000000000000000000000000000000000000"
ZERO_HOOKS = "0x0000000000000000000000000000000000000000"
UNVERIFIED_HOOK = "0x4000000000000000000000000000000000000004"

VALID_MANAGER_A = "0x2222222222222222222222222222222222225042"
VALID_MANAGER_B = "0x3333333333333333333333333333333333335042"
UNAUTHORIZED_MANAGER = "0x9999999999999999999999999999999999999999"

# Verbatim canonical EVM Keccak-256 pool IDs
CANONICAL_ID_AI_USDG = "0x7aebd80541bfaaf23dbb6e99ce13d4d31c1a84c91414f971eadbff7db5f85995"
CANONICAL_ID_PONS_USDG = "0x4be9657ec9002e528f4f17a5c43edc525a07f888f7b180c2afbf75e096c4f38a"
CANONICAL_ID_NATIVE_USDG = "0x24107d152f14a76d292123265ae3f3c71f863fc2f4ef7ba49d64e78d28ea379e"


@pytest.fixture
def registries() -> tuple[DeploymentsRegistry, DecimalsRegistry]:
    """Build verified DeploymentsRegistry and DecimalsRegistry for qualification testing."""
    dep_reg = DeploymentsRegistry(target_chain_id=5042)
    dec_reg = DecimalsRegistry(chain_id=5042)

    # Register valid PoolManagers
    dep_reg.register(
        ArcDeploymentRecord(
            name="arc_v4_manager_a",
            role=ContractRole.MANAGER,
            address=VALID_MANAGER_A,
            chain_id=5042,
            abi_hash="bb" * 32,
            source_proof="genesis:v4_manager_a",
        )
    )
    dep_reg.register(
        ArcDeploymentRecord(
            name="arc_v4_manager_b",
            role=ContractRole.MANAGER,
            address=VALID_MANAGER_B,
            chain_id=5042,
            abi_hash="cc" * 32,
            source_proof="genesis:v4_manager_b",
        )
    )

    # Register token decimals
    dec_reg.register_decimals(token_address=AI_TOKEN, decimals=18, source_proof="proof:ai")
    dec_reg.register_decimals(token_address=USDG_TOKEN, decimals=6, source_proof="proof:usdg")
    dec_reg.register_decimals(token_address=PONS_TOKEN, decimals=18, source_proof="proof:pons")

    return dep_reg, dec_reg


@pytest.fixture
def catalog_bridge(registries: tuple[DeploymentsRegistry, DecimalsRegistry]) -> QuoteCatalogBridge:
    dep_reg, dec_reg = registries
    return QuoteCatalogBridge(deployments=dep_reg, decimals=dec_reg, chain_id=5042)


@pytest.fixture
def discovery_engine(registries: tuple[DeploymentsRegistry, DecimalsRegistry]) -> V4MarketDiscoveryEngine:
    dep_reg, _ = registries
    return V4MarketDiscoveryEngine(chain_id=5042, deployments_registry=dep_reg)


# ---------------------------------------------------------------------------
# Test Suite
# ---------------------------------------------------------------------------

class TestV4CatalogPoolIdIntegrity:
    """Test suite for V4 PoolId EVM Keccak-256 verification in production qualification & discovery."""

    def test_independent_reference_matches_canonical_vectors(self) -> None:
        """Verify the independent reference implementation aligns with canonical EVM Keccak hashes."""
        id_ai = _independent_evm_keccak(AI_TOKEN, USDG_TOKEN, 2300, 23, ZERO_HOOKS)
        assert id_ai == CANONICAL_ID_AI_USDG.lower()

        id_pons = _independent_evm_keccak(PONS_TOKEN, USDG_TOKEN, 3000, 60, ZERO_HOOKS)
        assert id_pons == CANONICAL_ID_PONS_USDG.lower()

        id_native = _independent_evm_keccak(NATIVE_CURRENCY, USDG_TOKEN, 100, 1, ZERO_HOOKS)
        assert id_native == CANONICAL_ID_NATIVE_USDG.lower()

    def test_qualify_catalog_batch_rejection_of_tampered_pool_ids(
        self, catalog_bridge: QuoteCatalogBridge
    ) -> None:
        """Verify qualify_catalog isolates tampered pool_ids in middle and end of batch without failure."""
        key_valid_0 = V4PoolKey(AI_TOKEN, USDG_TOKEN, 2300, 23, ZERO_HOOKS)
        key_valid_1 = V4PoolKey(PONS_TOKEN, USDG_TOKEN, 3000, 60, ZERO_HOOKS)
        key_valid_2 = V4PoolKey(NATIVE_CURRENCY, USDG_TOKEN, 100, 1, ZERO_HOOKS)

        # Genuine pool_ids derived independently
        real_id_0 = CANONICAL_ID_AI_USDG.lower()
        real_id_2 = CANONICAL_ID_NATIVE_USDG.lower()

        # Forged/tampered IDs: valid 5-tuple parameters but spoofed pool_ids
        tampered_id_middle = "0x" + "a1" * 32
        tampered_id_end = "0x" + "fe" * 32

        pool_0_valid = V4DiscoveredPool(
            pool_id=real_id_0,
            manager_address=VALID_MANAGER_A,
            v4_key=key_valid_0,
            chain_id=5042,
            created_at_block=1000,
        )
        pool_1_tampered_middle = V4DiscoveredPool(
            pool_id=tampered_id_middle,
            manager_address=VALID_MANAGER_A,
            v4_key=key_valid_1,
            chain_id=5042,
            created_at_block=1001,
        )
        pool_2_valid = V4DiscoveredPool(
            pool_id=real_id_2,
            manager_address=VALID_MANAGER_A,
            v4_key=key_valid_2,
            chain_id=5042,
            created_at_block=1002,
        )
        pool_3_tampered_end = V4DiscoveredPool(
            pool_id=tampered_id_end,
            manager_address=VALID_MANAGER_A,
            v4_key=key_valid_0,
            chain_id=5042,
            created_at_block=1003,
        )

        batch = [pool_0_valid, pool_1_tampered_middle, pool_2_valid, pool_3_tampered_end]

        # Execute production batch admission entry point
        report = catalog_bridge.qualify_catalog(v3_pools=[], v4_pools=batch)

        assert isinstance(report, QualificationReport)
        assert len(report.qualified) == 2
        assert len(report.rejected) == 2

        # 1. Legitimate pools admitted
        qualified_ids = {desc.pool_id for desc in report.qualified}
        assert real_id_0 in qualified_ids
        assert real_id_2 in qualified_ids

        # 2. Tampered pools isolated in rejected report with specific reason
        assert tampered_id_middle.lower() in report.rejected
        assert tampered_id_end.lower() in report.rejected
        assert "keccak derivation mismatch" in report.rejected[tampered_id_middle.lower()].lower()
        assert "keccak derivation mismatch" in report.rejected[tampered_id_end.lower()].lower()

    def test_qualify_v4_pool_keccak_mismatch_single(
        self, catalog_bridge: QuoteCatalogBridge
    ) -> None:
        """Verify qualify_v4_pool raises ArcMarketIneligibleError when pool_id differs from derived hash."""
        key = V4PoolKey(AI_TOKEN, USDG_TOKEN, 2300, 23, ZERO_HOOKS)
        tampered_id = "0x" + "99" * 32

        pool = V4DiscoveredPool(
            pool_id=tampered_id,
            manager_address=VALID_MANAGER_A,
            v4_key=key,
            chain_id=5042,
            created_at_block=1000,
        )

        with pytest.raises(
            ArcMarketIneligibleError, match="V4 pool_id keccak derivation mismatch"
        ):
            catalog_bridge.qualify_v4_pool(pool)

    def test_qualify_v4_pool_malformed_and_short_id_rejected(
        self, catalog_bridge: QuoteCatalogBridge
    ) -> None:
        """Verify qualify_v4_pool rejects short or malformed pool_id values."""
        key = V4PoolKey(AI_TOKEN, USDG_TOKEN, 2300, 23, ZERO_HOOKS)

        # Short ID (e.g. 20-byte address format instead of 32-byte hash)
        short_id = "0x" + "11" * 20
        pool_short = V4DiscoveredPool(
            pool_id=short_id,
            manager_address=VALID_MANAGER_A,
            v4_key=key,
            chain_id=5042,
            created_at_block=1000,
        )
        with pytest.raises(
            ArcMarketIneligibleError, match="Malformed V4 pool_id format"
        ):
            catalog_bridge.qualify_v4_pool(pool_short)

        # Corrupted non-hex string
        corrupted_id = "0x" + "zz" * 32
        pool_corrupt = V4DiscoveredPool(
            pool_id=corrupted_id,
            manager_address=VALID_MANAGER_A,
            v4_key=key,
            chain_id=5042,
            created_at_block=1000,
        )
        with pytest.raises(
            ArcMarketIneligibleError, match="Malformed V4 pool_id format"
        ):
            catalog_bridge.qualify_v4_pool(pool_corrupt)

        # Disallowed uppercase '0X' prefix (rigidly preserving 0x format boundary)
        illegal_prefix_id = "0X" + CANONICAL_ID_AI_USDG[2:]
        pool_illegal_prefix = V4DiscoveredPool(
            pool_id=illegal_prefix_id,
            manager_address=VALID_MANAGER_A,
            v4_key=key,
            chain_id=5042,
            created_at_block=1000,
        )
        with pytest.raises(
            ArcMarketIneligibleError, match="Malformed V4 pool_id format"
        ):
            catalog_bridge.qualify_v4_pool(pool_illegal_prefix)

    def test_qualify_v4_pool_case_insensitive_success(
        self, catalog_bridge: QuoteCatalogBridge
    ) -> None:
        """Verify qualify_v4_pool accepts uppercase hex payload with valid 0x prefix and normalizes to lowercase."""
        key = V4PoolKey(AI_TOKEN, USDG_TOKEN, 2300, 23, ZERO_HOOKS)
        upper_id = "0x" + CANONICAL_ID_AI_USDG[2:].upper()

        pool = V4DiscoveredPool(
            pool_id=upper_id,
            manager_address=VALID_MANAGER_A,
            v4_key=key,
            chain_id=5042,
            created_at_block=1000,
        )

        desc = catalog_bridge.qualify_v4_pool(pool)
        assert isinstance(desc, PoolDescriptor)
        assert desc.pool_id == CANONICAL_ID_AI_USDG.lower()
        assert desc.composite_id == f"uniswap_v4:{VALID_MANAGER_A.lower()}:{CANONICAL_ID_AI_USDG.lower()}"

    def test_process_initialize_keccak_mismatch_fails_closed_without_state_pollution(
        self, discovery_engine: V4MarketDiscoveryEngine
    ) -> None:
        """Verify process_initialize rejects mismatched event pool_id with zero state pollution."""
        tampered_id = "0x" + "bb" * 32
        event = V4InitializeEvent(
            pool_id=tampered_id,
            currency0=AI_TOKEN,
            currency1=USDG_TOKEN,
            fee=2300,
            tick_spacing=23,
            hooks=ZERO_HOOKS,
            manager_address=VALID_MANAGER_A,
            block_number=1000,
            tx_hash="0x" + "11" * 32,
        )

        with pytest.raises(
            ArcValidationError, match="V4 Initialize pool_id mismatch"
        ):
            discovery_engine.process_initialize(event)

        # Rigidly verify that NO partial state pollution occurred:
        assert discovery_engine.get_pool(VALID_MANAGER_A, tampered_id) is None
        assert discovery_engine.get_pool(VALID_MANAGER_A, CANONICAL_ID_AI_USDG) is None
        assert len(discovery_engine.list_all_pools()) == 0
        assert len(discovery_engine.list_pools_for_manager(VALID_MANAGER_A)) == 0

    def test_process_initialize_case_insensitive_success(
        self, discovery_engine: V4MarketDiscoveryEngine
    ) -> None:
        """Verify process_initialize accepts uppercase event pool_id and indexes correctly."""
        upper_id = "0x" + CANONICAL_ID_AI_USDG[2:].upper()
        event = V4InitializeEvent(
            pool_id=upper_id,
            currency0=AI_TOKEN,
            currency1=USDG_TOKEN,
            fee=2300,
            tick_spacing=23,
            hooks=ZERO_HOOKS,
            manager_address=VALID_MANAGER_A,
            block_number=1000,
            tx_hash="0x" + "22" * 32,
        )

        discovered = discovery_engine.process_initialize(event)
        assert isinstance(discovered, V4DiscoveredPool)
        assert discovered.pool_id == CANONICAL_ID_AI_USDG.lower()

        # Queryable by either lowercase or uppercase
        assert discovery_engine.get_pool(VALID_MANAGER_A, upper_id) is not None
        assert discovery_engine.get_pool(VALID_MANAGER_A, CANONICAL_ID_AI_USDG.lower()) is not None
        assert len(discovery_engine.list_all_pools()) == 1

        # Rigidly assert that illegal uppercase '0X' prefix is rejected at event ingress
        illegal_prefix_id = "0X" + CANONICAL_ID_AI_USDG[2:]
        with pytest.raises(
            ArcValidationError, match="pool_id must be a 66-char hex bytes32 string"
        ):
            V4InitializeEvent(
                pool_id=illegal_prefix_id,
                currency0=AI_TOKEN,
                currency1=USDG_TOKEN,
                fee=2300,
                tick_spacing=23,
                hooks=ZERO_HOOKS,
                manager_address=VALID_MANAGER_A,
                block_number=1000,
                tx_hash="0x" + "22" * 32,
            )

    def test_existing_qualification_gates_preserved(
        self, catalog_bridge: QuoteCatalogBridge
    ) -> None:
        """Verify orthogonal safety gates (Robinhood, deployments, dynamic fees, hooks) are preserved."""
        # 1. Robinhood market identifier rejected
        rh_key = V4PoolKey(AI_TOKEN, USDG_TOKEN, 2300, 23, ZERO_HOOKS)
        rh_pool = V4DiscoveredPool(
            pool_id=_independent_evm_keccak(AI_TOKEN, USDG_TOKEN, 2300, 23, ZERO_HOOKS),
            manager_address="0x" + "4663" * 10,
            v4_key=rh_key,
            chain_id=5042,
            created_at_block=1000,
        )
        with pytest.raises(ArcMarketIneligibleError, match="Foreign Robinhood identifier detected"):
            catalog_bridge.qualify_v4_pool(rh_pool)

        # 2. Unregistered manager rejected
        unreg_key = V4PoolKey(AI_TOKEN, USDG_TOKEN, 2300, 23, ZERO_HOOKS)
        unreg_pool = V4DiscoveredPool(
            pool_id=_independent_evm_keccak(AI_TOKEN, USDG_TOKEN, 2300, 23, ZERO_HOOKS),
            manager_address=UNAUTHORIZED_MANAGER,
            v4_key=unreg_key,
            chain_id=5042,
            created_at_block=1000,
        )
        with pytest.raises(ArcMarketIneligibleError, match="V4 PoolManager not registered or unverified"):
            catalog_bridge.qualify_v4_pool(unreg_pool)

        # 3. Dynamic fee rejected
        dyn_key = V4PoolKey(AI_TOKEN, USDG_TOKEN, DYNAMIC_FEE_FLAG | 2300, 23, ZERO_HOOKS)
        dyn_pool = V4DiscoveredPool(
            pool_id=_independent_evm_keccak(AI_TOKEN, USDG_TOKEN, DYNAMIC_FEE_FLAG | 2300, 23, ZERO_HOOKS),
            manager_address=VALID_MANAGER_A,
            v4_key=dyn_key,
            chain_id=5042,
            created_at_block=1000,
        )
        with pytest.raises(ArcMarketIneligibleError, match="Dynamic fee hooks currently unsupported"):
            catalog_bridge.qualify_v4_pool(dyn_pool)

        # 4. Unregistered hook contract rejected
        hook_key = V4PoolKey(AI_TOKEN, USDG_TOKEN, 2300, 23, UNVERIFIED_HOOK)
        hook_pool = V4DiscoveredPool(
            pool_id=_independent_evm_keccak(AI_TOKEN, USDG_TOKEN, 2300, 23, UNVERIFIED_HOOK),
            manager_address=VALID_MANAGER_A,
            v4_key=hook_key,
            chain_id=5042,
            created_at_block=1000,
        )
        with pytest.raises(ArcMarketIneligibleError, match="Unverified V4 hook contract"):
            catalog_bridge.qualify_v4_pool(hook_pool)
