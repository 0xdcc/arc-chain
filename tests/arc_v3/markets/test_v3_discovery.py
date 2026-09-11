"""Tests for T14: V3 Market Discovery, Normalized Event Decoding, and Lifecycle Tracking."""

from __future__ import annotations

import pytest

from arc_markets.decimals import DecimalsRegistry
from arc_markets.deployments import (
    ArcDeploymentRecord,
    ContractRole,
    DeploymentsRegistry,
)
from arc_markets.v3_discovery import (
    V3DiscoveredPool,
    V3MarketDiscoveryEngine,
)
from arc_markets.v3_events import (
    TOPIC0_V3_INITIALIZE,
    TOPIC0_V3_MINT,
    TOPIC0_V3_POOL_CREATED,
    TOPIC0_V3_SWAP,
    V3InitializeEvent,
    V3MintEvent,
    V3PoolCreatedEvent,
    V3SwapEvent,
    decode_v3_initialize,
    decode_v3_mint,
    decode_v3_pool_created,
    decode_v3_swap,
)
from arc_readiness.errors import (
    ArcMarketIneligibleError,
    ArcValidationError,
)

_VALID_FACTORY = "0x0000000000000000000000000000000000005042"
_TOKEN0 = "0x0000000000000000000000000000000000000001"
_TOKEN1 = "0x0000000000000000000000000000000000000002"
_POOL_ADDR = "0x00000000000000000000000000000000000000aa"


class TestV3DiscoveryLifecycle:
    """Test suite for T14 V3 event decoding and discovery engine lifecycle."""

    @pytest.fixture
    def engine(self) -> V3MarketDiscoveryEngine:
        dep_reg = DeploymentsRegistry(target_chain_id=5042)
        rec = ArcDeploymentRecord(
            name="arc_v3_factory",
            role=ContractRole.FACTORY,
            address=_VALID_FACTORY,
            chain_id=5042,
            abi_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            source_proof="genesis:factory",
        )
        dep_reg.register(rec)

        dec_reg = DecimalsRegistry(chain_id=5042)
        # Token0 has 0 decimals (boundary: zero decimals is completely legal!)
        dec_reg.register_decimals(_TOKEN0, 0, "verified:token0")
        dec_reg.register_decimals(_TOKEN1, 6, "verified:token1")

        return V3MarketDiscoveryEngine(
            chain_id=5042,
            deployments_registry=dep_reg,
            decimals_registry=dec_reg,
        )

    def test_minimum_filter_topics(self, engine: V3MarketDiscoveryEngine) -> None:
        filters = engine.get_minimum_filter_topics()
        assert "topics" in filters
        topic_list = filters["topics"][0]
        assert TOPIC0_V3_POOL_CREATED in topic_list
        assert TOPIC0_V3_INITIALIZE in topic_list
        assert TOPIC0_V3_MINT in topic_list
        assert TOPIC0_V3_SWAP in topic_list

    def test_full_pool_lifecycle_progression(self, engine: V3MarketDiscoveryEngine) -> None:
        # 1. PoolCreated
        created_event = V3PoolCreatedEvent(
            factory_address=_VALID_FACTORY,
            token0=_TOKEN0,
            token1=_TOKEN1,
            fee=3000,
            tick_spacing=60,
            pool_address=_POOL_ADDR,
            block_number=1000,
            block_hash="0x" + "aa" * 32,
            tx_hash="0x" + "bb" * 32,
            log_index=1,
        )
        pool = engine.process_pool_created(created_event)
        assert pool.pool_address == _POOL_ADDR
        assert pool.fee_model.raw_value == 3000
        assert pool.is_initialized is False
        assert pool.liquidity == 0
        assert pool.is_quoteable is False
        assert pool.asset0.token_key is not None
        assert pool.asset0.token_key.address == _TOKEN0
        assert engine.decimals.get_decimals(_TOKEN0) == 0  # 0 decimals preserved!

        # 2. Initialize
        init_event = V3InitializeEvent(
            pool_address=_POOL_ADDR,
            sqrt_price_x96=79228162514264337593543950336,  # 1.0
            tick=0,
            block_number=1001,
            tx_hash="0x" + "cc" * 32,
        )
        pool = engine.process_initialize(init_event)
        assert pool.is_initialized is True
        assert pool.sqrt_price_x96 == 79228162514264337593543950336
        assert pool.current_tick == 0
        assert pool.is_quoteable is False  # Liquidity still 0!

        # 3. Mint
        mint_event = V3MintEvent(
            pool_address=_POOL_ADDR,
            sender="0x" + "11" * 20,
            owner="0x" + "22" * 20,
            tick_lower=-600,
            tick_upper=600,
            amount=5000000,
            amount0=1000,
            amount1=1000,
            block_number=1002,
        )
        pool = engine.process_mint(mint_event)
        assert pool.liquidity == 5000000
        assert pool.liquidity_available is True
        assert pool.is_quoteable is True
        assert len(engine.list_quoteable_pools()) == 1

        # 4. Swap
        swap_event = V3SwapEvent(
            pool_address=_POOL_ADDR,
            sender="0x" + "33" * 20,
            recipient="0x" + "44" * 20,
            amount0=100,
            amount1=-99,
            sqrt_price_x96=79228162514264337593543950000,
            liquidity=5000000,
            tick=-1,
            block_number=1003,
        )
        pool = engine.process_swap(swap_event)
        assert pool.current_tick == -1
        assert pool.is_quoteable is True

    def test_unauthorized_factory_rejected(self, engine: V3MarketDiscoveryEngine) -> None:
        bogus_factory = "0x000000000000000000000000000000000000dead"
        event = V3PoolCreatedEvent(
            factory_address=bogus_factory,
            token0=_TOKEN0,
            token1=_TOKEN1,
            fee=3000,
            tick_spacing=60,
            pool_address=_POOL_ADDR,
            block_number=1000,
            block_hash="0x" + "aa" * 32,
            tx_hash="0x" + "bb" * 32,
            log_index=1,
        )
        with pytest.raises(ArcMarketIneligibleError, match="Unauthorized or unknown factory"):
            engine.process_pool_created(event)

    def test_log_decoding_pool_created(self) -> None:
        raw_log = {
            "address": _VALID_FACTORY,
            "topics": [
                TOPIC0_V3_POOL_CREATED,
                "0x0000000000000000000000000000000000000000000000000000000000000001",
                "0x0000000000000000000000000000000000000000000000000000000000000002",
                hex(3000),
            ],
            "data": "0x" + "00" * 31 + "3c" + "00" * 12 + "00000000000000000000000000000000000000aa",
            "blockNumber": hex(100),
            "blockHash": "0x" + "11" * 32,
            "transactionHash": "0x" + "22" * 32,
            "logIndex": hex(0),
        }
        decoded = decode_v3_pool_created(raw_log, verified_factory=_VALID_FACTORY)
        assert decoded.factory_address == _VALID_FACTORY
        assert decoded.token0 == _TOKEN0
        assert decoded.token1 == _TOKEN1
        assert decoded.fee == 3000
        assert decoded.tick_spacing == 60
        assert decoded.pool_address == _POOL_ADDR

    def test_log_decoding_rejects_fake_topic(self) -> None:
        fake_log = {
            "address": _VALID_FACTORY,
            "topics": ["0x" + "ff" * 32],
            "data": "0x",
        }
        with pytest.raises(ArcValidationError, match="Invalid Topic0"):
            decode_v3_pool_created(fake_log)
