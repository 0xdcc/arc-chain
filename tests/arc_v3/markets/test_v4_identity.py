"""Tests for T15: Uniswap V4 Pool Identity, Multi-Pool indexing, and PoolId Guards."""

from __future__ import annotations

import pytest

from arc_markets.deployments import (
    ArcDeploymentRecord,
    ContractRole,
    DeploymentsRegistry,
)
from arc_markets.v4_discovery import (
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

_VALID_MANAGER = "0x1111111111111111111111111111111111115042"
_CURRENCY0 = "0x0000000000000000000000000000000000000000"  # Native currency
_CURRENCY1 = "0x2222222222222222222222222222222222222222"
_HOOKS_ADDR = "0x3333333333333333333333333333333333333333"
_ZERO_HOOKS = "0x0000000000000000000000000000000000000000"


class TestV4PoolIdentity:
    """Test suite for V4 pool identity invariants, PoolKey hashing, and PoolManager indexing."""

    @pytest.fixture
    def engine(self) -> V4MarketDiscoveryEngine:
        dep_reg = DeploymentsRegistry(target_chain_id=5042)
        rec = ArcDeploymentRecord(
            name="arc_v4_pool_manager",
            role=ContractRole.MANAGER,
            address=_VALID_MANAGER,
            chain_id=5042,
            abi_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            source_proof="genesis:v4_manager",
        )
        dep_reg.register(rec)
        return V4MarketDiscoveryEngine(chain_id=5042, deployments_registry=dep_reg)

    def test_v4_pool_key_and_pool_id_computation(self) -> None:
        key = V4PoolKey(
            currency0=_CURRENCY0,
            currency1=_CURRENCY1,
            fee=3000,
            tick_spacing=60,
            hooks=_ZERO_HOOKS,
        )
        pool_id = key.compute_pool_id()
        # Canonical EVM Keccak-256 derivation check (not just len/prefix)
        expected_keccak = "0x06339ed6a3555ccef52e5dd5be2a590cdeaeeb7bd1e0cecee96da150ac14dc58"
        assert pool_id == expected_keccak
        # Strictly verify divergence from erroneous NIST SHA3-256
        sha3_hash = "0x2782908cc9c8e7598adf4dd61af1ecabfe9271a6c8a6d6984f226837574946d4"
        assert pool_id != sha3_hash
        assert pool_id.startswith("0x")
        assert len(pool_id) == 66
        assert key.has_hooks is False
        assert key.is_dynamic_fee is False

    def test_v4_dynamic_fee_flag(self) -> None:
        key = V4PoolKey(
            currency0=_CURRENCY0,
            currency1=_CURRENCY1,
            fee=DYNAMIC_FEE_FLAG | 500,
            tick_spacing=60,
            hooks=_HOOKS_ADDR,
        )
        assert key.is_dynamic_fee is True
        assert key.has_hooks is True

    def test_currency_order_strictly_enforced(self) -> None:
        # Reversing currency0 and currency1 must fail:
        with pytest.raises(ArcValidationError, match="currency0.*must be strictly less than currency1"):
            V4PoolKey(
                currency0=_CURRENCY1,
                currency1=_CURRENCY0,
                fee=3000,
                tick_spacing=60,
                hooks=_ZERO_HOOKS,
            )

    def test_multi_pool_per_manager_discovery(self, engine: V4MarketDiscoveryEngine) -> None:
        key1 = V4PoolKey(_CURRENCY0, _CURRENCY1, 500, 10, _ZERO_HOOKS)
        id1 = key1.compute_pool_id()

        key2 = V4PoolKey(_CURRENCY0, _CURRENCY1, 3000, 60, _ZERO_HOOKS)
        id2 = key2.compute_pool_id()

        assert id1 != id2

        event1 = V4InitializeEvent(
            pool_id=id1,
            currency0=_CURRENCY0,
            currency1=_CURRENCY1,
            fee=500,
            tick_spacing=10,
            hooks=_ZERO_HOOKS,
            manager_address=_VALID_MANAGER,
            block_number=1000,
            tx_hash="0x" + "aa" * 32,
        )
        event2 = V4InitializeEvent(
            pool_id=id2,
            currency0=_CURRENCY0,
            currency1=_CURRENCY1,
            fee=3000,
            tick_spacing=60,
            hooks=_ZERO_HOOKS,
            manager_address=_VALID_MANAGER,
            block_number=1001,
            tx_hash="0x" + "bb" * 32,
        )

        p1 = engine.process_initialize(event1)
        p2 = engine.process_initialize(event2)

        # Both pools coexist independently on the SAME manager:
        assert p1.pool_id == id1
        assert p2.pool_id == id2
        assert len(engine.list_pools_for_manager(_VALID_MANAGER)) == 2
        assert engine.get_pool(_VALID_MANAGER, id1) is not None
        assert engine.get_pool(_VALID_MANAGER, id2) is not None

    def test_pool_id_cannot_be_treated_as_contract_address(self) -> None:
        key = V4PoolKey(_CURRENCY0, _CURRENCY1, 3000, 60, _ZERO_HOOKS)
        p_id = key.compute_pool_id()

        with pytest.raises(ArcValidationError, match="is a 32-byte PoolId, NOT a contract address"):
            V4MarketDiscoveryEngine.assert_not_contract_address(p_id)

    def test_unauthorized_manager_rejected(self, engine: V4MarketDiscoveryEngine) -> None:
        bogus_manager = "0x000000000000000000000000000000000000dead"
        key = V4PoolKey(_CURRENCY0, _CURRENCY1, 3000, 60, _ZERO_HOOKS)
        event = V4InitializeEvent(
            pool_id=key.compute_pool_id(),
            currency0=_CURRENCY0,
            currency1=_CURRENCY1,
            fee=3000,
            tick_spacing=60,
            hooks=_ZERO_HOOKS,
            manager_address=bogus_manager,
            block_number=1000,
            tx_hash="0x" + "aa" * 32,
        )
        with pytest.raises(ArcMarketIneligibleError, match="Unauthorized or unknown PoolManager"):
            engine.process_initialize(event)
