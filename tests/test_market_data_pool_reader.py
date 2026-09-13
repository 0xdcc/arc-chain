"""Tests for same-block multi-pool snapshot reading coordinator (arbitrage/market_data/pool_reader.py).

Verifies:
1. Multi-pool (Uniswap V3 + V4) normal reading and assembly into MarketSnapshot.
2. Strong error isolation: Single bad pool (revert / encoding error) does not contaminate normal pools.
3. Empty pool list boundary condition returns empty MarketSnapshot.
4. Block number consistency binding across MarketSnapshot and all PoolStateSnapshots.
5. Multicall2 and direct eth_call fallback pathways.
6. UnifiedPoolReader alias and convenience functions.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
from eth_abi import encode as abi_encode

from research.market_data.catalog import ROBINHOOD_CHAIN_ID
from research.market_data.pool_reader import (
    MULTICALL2_ADDRESS,
    STATE_VIEW_ADDRESS,
    SnapshotCoordinator,
    UnifiedPoolReader,
    decode_slot0,
    encode_pool_call,
    is_v3_pool,
    is_v4_pool,
    read_market_snapshot,
)
from research.market_data.types import MarketSnapshot, PoolIdentity


def _make_v3_pool(
    pool_address: str = "0x1111111111111111111111111111111111111111",
    token0: str = "0x4200000000000000000000000000000000000006",
    token1: str = "0x5fc5360d0400a0fd4f2af552add042d716f1d168",
    protocol: str = "uniswap_v3",
    fee_bps: float = 5.0,
    tick_spacing: int = 10,
) -> PoolIdentity:
    return PoolIdentity(
        chain_id=ROBINHOOD_CHAIN_ID,
        protocol=protocol,
        pool_id=pool_address,
        token0=token0,
        token1=token1,
        fee_bps=fee_bps,
        tick_spacing=tick_spacing,
    )


def _make_v4_pool(
    pool_id: str = "0x" + "22" * 32,
    token0: str = "0x4200000000000000000000000000000000000006",
    token1: str = "0x5fc5360d0400a0fd4f2af552add042d716f1d168",
    protocol: str = "uniswap_v4",
    fee_bps: float = 30.0,
    tick_spacing: int = 60,
) -> PoolIdentity:
    return PoolIdentity(
        chain_id=ROBINHOOD_CHAIN_ID,
        protocol=protocol,
        pool_id=pool_id,
        token0=token0,
        token1=token1,
        fee_bps=fee_bps,
        tick_spacing=tick_spacing,
    )


def _encode_v3_slot0(sqrt_price_x96: int, tick: int) -> bytes:
    """Encode V3 slot0 return tuple: (uint160, int24, uint16, uint16, uint16, uint8, bool)."""
    return abi_encode(
        ["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"],
        [sqrt_price_x96, tick, 0, 1, 1, 0, True],
    )


def _encode_v4_slot0(
    sqrt_price_x96: int,
    tick: int,
    protocol_fee: int = 0,
    lp_fee: int = 3000,
) -> bytes:
    """Encode V4 StateView.getSlot0 return tuple: (uint160, int24, uint24, uint24)."""
    return abi_encode(
        ["uint160", "int24", "uint24", "uint24"],
        [sqrt_price_x96, tick, protocol_fee, lp_fee],
    )


class TestEmptyPoolsBoundary:
    """Boundary test cases for empty pool collections."""

    def test_empty_pools_with_explicit_block_number(self) -> None:
        """Empty pool sequence with explicit block number returns valid empty MarketSnapshot."""
        coordinator = SnapshotCoordinator()
        snapshot = coordinator.read_market_snapshot(pools=[], block_number=57400000)

        assert isinstance(snapshot, MarketSnapshot)
        assert snapshot.chain_id == ROBINHOOD_CHAIN_ID
        assert snapshot.block_number == 57400000
        assert snapshot.pools == {}
        assert snapshot.captured_at > 0

    def test_empty_pools_with_rpc_inferred_block(self) -> None:
        """Empty pool sequence without block_number queries RPC for current block."""
        mock_rpc = MagicMock()
        mock_rpc.block_number.return_value = 57401234

        snapshot = read_market_snapshot(pools=[], rpc=mock_rpc)

        assert isinstance(snapshot, MarketSnapshot)
        assert snapshot.block_number == 57401234
        assert len(snapshot.pools) == 0


class TestSameBlockMultiPoolReading:
    """Tests reading multiple V3 and V4 pools in the same block."""

    def test_v3_and_v4_combined_multicall(self) -> None:
        """Verify normal Multicall2 reading of mixed V3 and V4 pools."""
        v3_p1 = _make_v3_pool("0x" + "11" * 20)
        v3_p2 = _make_v3_pool("0x" + "12" * 20)
        v4_p1 = _make_v4_pool("0x" + "21" * 32)
        v4_p2 = _make_v4_pool("0x" + "22" * 32)
        pools = [v3_p1, v3_p2, v4_p1, v4_p2]

        expected_block = 57408888
        q96 = 2**96

        sqrt_1 = int(q96 * 1.5)
        sqrt_2 = int(q96 * 2.0)
        sqrt_3 = int(q96 * 0.8)
        sqrt_4 = int(q96 * 3.2)

        # Mock Multicall2 response: call[0] is block_number, call[1..4] are pool slot0
        mock_items = [
            (True, expected_block.to_bytes(32, "big")),
            (True, _encode_v3_slot0(sqrt_1, -150)),
            (True, _encode_v3_slot0(sqrt_2, 350)),
            (True, _encode_v4_slot0(sqrt_3, -800)),
            (True, _encode_v4_slot0(sqrt_4, 1200)),
        ]
        raw_multicall = "0x" + abi_encode(["(bool,bytes)[]"], [mock_items]).hex()

        mock_rpc = MagicMock()
        mock_rpc.call.return_value = {"result": raw_multicall}

        coordinator = SnapshotCoordinator(rpc=mock_rpc)
        snapshot = coordinator.read_market_snapshot(pools)

        assert isinstance(snapshot, MarketSnapshot)
        assert snapshot.block_number == expected_block
        assert len(snapshot.pools) == 4

        # Verify each pool snapshot matches input and extracted state
        s1 = snapshot.pools[v3_p1.pool_id]
        assert s1.pool == v3_p1
        assert s1.block_number == expected_block
        assert s1.sqrt_price_x96 == sqrt_1
        assert s1.tick == -150

        s2 = snapshot.pools[v3_p2.pool_id]
        assert s2.pool == v3_p2
        assert s2.block_number == expected_block
        assert s2.sqrt_price_x96 == sqrt_2
        assert s2.tick == 350

        s3 = snapshot.pools[v4_p1.pool_id]
        assert s3.pool == v4_p1
        assert s3.block_number == expected_block
        assert s3.sqrt_price_x96 == sqrt_3
        assert s3.tick == -800

        s4 = snapshot.pools[v4_p2.pool_id]
        assert s4.pool == v4_p2
        assert s4.block_number == expected_block
        assert s4.sqrt_price_x96 == sqrt_4
        assert s4.tick == 1200

        # Verify Multicall2 RPC was called once
        mock_rpc.call.assert_called_once()
        call_args = mock_rpc.call.call_args[0]
        assert call_args[0] == "eth_call"
        assert call_args[1][0]["to"].lower() == MULTICALL2_ADDRESS.lower()

    def test_v3_and_v4_direct_call_pathway(self) -> None:
        """Verify normal direct eth_call reading of mixed V3 and V4 pools."""
        v3_pool = _make_v3_pool("0x" + "aa" * 20)
        v4_pool = _make_v4_pool("0x" + "bb" * 32)
        pools = [v3_pool, v4_pool]

        q96 = 2**96
        sqrt_v3 = int(q96 * 1.25)
        sqrt_v4 = int(q96 * 0.75)

        mock_rpc = MagicMock()
        mock_rpc.block_number.return_value = 57409999

        def mock_eth_call(method: str, params: list) -> dict:
            to_addr = params[0]["to"].lower()
            if to_addr == v3_pool.pool_id.lower():
                return {"result": "0x" + _encode_v3_slot0(sqrt_v3, 100).hex()}
            elif to_addr == STATE_VIEW_ADDRESS.lower():
                return {"result": "0x" + _encode_v4_slot0(sqrt_v4, -200).hex()}
            raise RuntimeError(f"Unexpected call target: {to_addr}")

        mock_rpc.call.side_effect = mock_eth_call

        coordinator = SnapshotCoordinator(rpc=mock_rpc, use_multicall=False)
        snapshot = coordinator.read_market_snapshot(pools)

        assert snapshot.block_number == 57409999
        assert len(snapshot.pools) == 2
        assert snapshot.pools[v3_pool.pool_id].sqrt_price_x96 == sqrt_v3
        assert snapshot.pools[v3_pool.pool_id].tick == 100
        assert snapshot.pools[v4_pool.pool_id].sqrt_price_x96 == sqrt_v4
        assert snapshot.pools[v4_pool.pool_id].tick == -200


class TestErrorIsolationAndDegradation:
    """Tests strong error isolation when individual pools revert or have bad data."""

    def test_single_bad_pool_revert_in_multicall_does_not_contaminate(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A reverting pool in Multicall produces degraded snapshot and does not affect others."""
        caplog.set_level(logging.WARNING)

        p1_good = _make_v3_pool("0x" + "01" * 20)
        p2_bad = _make_v3_pool("0x" + "02" * 20)  # Will revert
        p3_good = _make_v4_pool("0x" + "03" * 32)
        pools = [p1_good, p2_bad, p3_good]

        expected_block = 57405000
        q96 = 2**96
        sqrt_p1 = int(q96 * 10.0)
        sqrt_p3 = int(q96 * 20.0)

        mock_items = [
            (True, expected_block.to_bytes(32, "big")),
            (True, _encode_v3_slot0(sqrt_p1, 500)),
            (False, b""),  # Pool 2 execution reverted on chain
            (True, _encode_v4_slot0(sqrt_p3, -500)),
        ]
        raw_multicall = "0x" + abi_encode(["(bool,bytes)[]"], [mock_items]).hex()

        mock_rpc = MagicMock()
        mock_rpc.call.return_value = {"result": raw_multicall}

        coordinator = SnapshotCoordinator(rpc=mock_rpc)
        snapshot = coordinator.read_market_snapshot(pools)

        # Batch succeeds as a whole
        assert snapshot.block_number == expected_block
        assert len(snapshot.pools) == 3

        # Normal pools retain valid states
        assert snapshot.pools[p1_good.pool_id].sqrt_price_x96 == sqrt_p1
        assert snapshot.pools[p1_good.pool_id].tick == 500
        assert snapshot.pools[p3_good.pool_id].sqrt_price_x96 == sqrt_p3
        assert snapshot.pools[p3_good.pool_id].tick == -500

        # Bad pool produces degraded snapshot with None fields
        snap_bad = snapshot.pools[p2_bad.pool_id]
        assert snap_bad.sqrt_price_x96 is None
        assert snap_bad.liquidity is None
        assert snap_bad.tick is None
        assert snap_bad.block_number == expected_block
        assert snap_bad.pool == p2_bad

        # Warning was recorded
        assert any("reverted" in record.message for record in caplog.records)

    def test_single_bad_pool_revert_in_direct_call_does_not_contaminate(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A reverting pool in direct calls produces degraded snapshot and does not affect others."""
        caplog.set_level(logging.WARNING)

        p1_good = _make_v3_pool("0x" + "11" * 20)
        p2_bad = _make_v3_pool("0x" + "22" * 20)
        p3_good = _make_v4_pool("0x" + "33" * 32)
        pools = [p1_good, p2_bad, p3_good]

        q96 = 2**96
        sqrt_p1 = int(q96 * 100)
        sqrt_p3 = int(q96 * 300)

        mock_rpc = MagicMock()
        mock_rpc.block_number.return_value = 57407777

        def mock_eth_call(method: str, params: list) -> dict:
            to_addr = params[0]["to"].lower()
            if to_addr == p1_good.pool_id.lower():
                return {"result": "0x" + _encode_v3_slot0(sqrt_p1, 10).hex()}
            elif to_addr == p2_bad.pool_id.lower():
                raise RuntimeError("execution reverted: bad pool")
            elif to_addr == STATE_VIEW_ADDRESS.lower():
                return {"result": "0x" + _encode_v4_slot0(sqrt_p3, 30).hex()}
            raise RuntimeError(f"Unknown target {to_addr}")

        mock_rpc.call.side_effect = mock_eth_call

        coordinator = SnapshotCoordinator(rpc=mock_rpc, use_multicall=False)
        snapshot = coordinator.read_market_snapshot(pools)

        assert snapshot.block_number == 57407777
        assert snapshot.pools[p1_good.pool_id].sqrt_price_x96 == sqrt_p1
        assert snapshot.pools[p3_good.pool_id].sqrt_price_x96 == sqrt_p3

        # Bad pool degraded gracefully
        snap_bad = snapshot.pools[p2_bad.pool_id]
        assert snap_bad.sqrt_price_x96 is None
        assert snap_bad.liquidity is None
        assert snap_bad.tick is None
        assert snap_bad.block_number == 57407777

        # Warning logged
        assert any(p2_bad.pool_id in record.message for record in caplog.records)

    def test_malformed_pool_address_format_isolated(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A pool with an unrecognized format / invalid selector is isolated without crashing."""
        caplog.set_level(logging.WARNING)

        good_pool = _make_v3_pool("0x" + "11" * 20)

        # Mock an invalid pool object with unsupported address
        bad_pool = MagicMock(spec=PoolIdentity)
        bad_pool.pool_id = "0xdead"  # Not 42 or 66 chars
        bad_pool.protocol = "unknown_v99"
        bad_pool.chain_id = ROBINHOOD_CHAIN_ID

        expected_block = 57401111
        q96 = 2**96
        sqrt_good = int(q96 * 5.0)

        mock_items = [
            (True, expected_block.to_bytes(32, "big")),
            (True, _encode_v3_slot0(sqrt_good, 100)),
        ]
        raw_multicall = "0x" + abi_encode(["(bool,bytes)[]"], [mock_items]).hex()

        mock_rpc = MagicMock()
        mock_rpc.call.return_value = {"result": raw_multicall}

        coordinator = SnapshotCoordinator(rpc=mock_rpc)
        snapshot = coordinator.read_market_snapshot([good_pool, bad_pool])

        # Good pool must succeed
        assert snapshot.pools[good_pool.pool_id].sqrt_price_x96 == sqrt_good

        # Bad pool must degrade gracefully without aborting batch
        assert bad_pool.pool_id in snapshot.pools
        assert snapshot.pools[bad_pool.pool_id].sqrt_price_x96 is None

    def test_skip_failed_option_omits_reverted_pools(self) -> None:
        """When skip_failed=True, reverted pools are cleanly excluded from the snapshot dict."""
        p1 = _make_v3_pool("0x" + "11" * 20)
        p2_bad = _make_v3_pool("0x" + "22" * 20)

        expected_block = 57402222
        mock_items = [
            (True, expected_block.to_bytes(32, "big")),
            (True, _encode_v3_slot0(2**96, 0)),
            (False, b""),  # Pool 2 reverts
        ]
        raw_multicall = "0x" + abi_encode(["(bool,bytes)[]"], [mock_items]).hex()

        mock_rpc = MagicMock()
        mock_rpc.call.return_value = {"result": raw_multicall}

        snapshot = read_market_snapshot([p1, p2_bad], rpc=mock_rpc, skip_failed=True)

        assert len(snapshot.pools) == 1
        assert p1.pool_id in snapshot.pools
        assert p2_bad.pool_id not in snapshot.pools


class TestBlockNumberConsistencyBinding:
    """Tests atomic/consistent block number propagation across all pool snapshots."""

    def test_explicit_block_number_binding(self) -> None:
        """All pool snapshots bind to the caller's explicit block_number."""
        v3 = _make_v3_pool("0x" + "11" * 20)
        v4 = _make_v4_pool("0x" + "22" * 32)
        pools = [v3, v4]

        explicit_block = 57409999
        mock_items = [
            (True, (57400000).to_bytes(32, "big")),  # Return value in Multicall ignored when explicit
            (True, _encode_v3_slot0(2**96, 0)),
            (True, _encode_v4_slot0(2**96, 0)),
        ]
        raw_multicall = "0x" + abi_encode(["(bool,bytes)[]"], [mock_items]).hex()

        mock_rpc = MagicMock()
        mock_rpc.call.return_value = {"result": raw_multicall}

        snapshot = read_market_snapshot(pools, rpc=mock_rpc, block_number=explicit_block)

        assert snapshot.block_number == explicit_block
        assert snapshot.pools[v3.pool_id].block_number == explicit_block
        assert snapshot.pools[v4.pool_id].block_number == explicit_block

    def test_atomic_multicall_block_number_binding(self) -> None:
        """When block_number is None, all pools bind to call[0] atomic block number."""
        v3 = _make_v3_pool("0x" + "33" * 20)
        v4 = _make_v4_pool("0x" + "44" * 32)

        atomic_block = 57408765
        mock_items = [
            (True, atomic_block.to_bytes(32, "big")),
            (True, _encode_v3_slot0(2**96, 12)),
            (True, _encode_v4_slot0(2**96, -12)),
        ]
        raw_multicall = "0x" + abi_encode(["(bool,bytes)[]"], [mock_items]).hex()

        mock_rpc = MagicMock()
        mock_rpc.call.return_value = {"result": raw_multicall}

        snapshot = read_market_snapshot([v3, v4], rpc=mock_rpc, block_number=None)

        assert snapshot.block_number == atomic_block
        for snap in snapshot.pools.values():
            assert snap.block_number == atomic_block


class TestUnifiedPoolReaderInterface:
    """Tests interface compatibility and helper functions."""

    def test_unified_pool_reader_alias(self) -> None:
        """UnifiedPoolReader is an alias for SnapshotCoordinator."""
        assert UnifiedPoolReader is SnapshotCoordinator

        mock_rpc = MagicMock()
        mock_rpc.block_number.return_value = 100
        reader = UnifiedPoolReader(rpc=mock_rpc)
        snapshot = reader.read_market_snapshot([])
        assert snapshot.block_number == 100

    def test_class_level_call_dispatch(self) -> None:
        """SnapshotCoordinator.read_market_snapshot can be called directly as a class method."""
        mock_rpc = MagicMock()
        mock_rpc.block_number.return_value = 200
        snapshot = SnapshotCoordinator.read_market_snapshot([], rpc=mock_rpc)
        assert snapshot.block_number == 200

    def test_protocol_detection_helpers(self) -> None:
        """Verify is_v3_pool and is_v4_pool predicates."""
        v3 = _make_v3_pool("0x" + "11" * 20)
        v4 = _make_v4_pool("0x" + "22" * 32)

        assert is_v3_pool(v3) is True
        assert is_v4_pool(v3) is False

        assert is_v4_pool(v4) is True
        assert is_v3_pool(v4) is False

    def test_encode_pool_call_formats(self) -> None:
        """Verify encoding for both V3 and V4 pools."""
        v3 = _make_v3_pool("0x" + "11" * 20)
        target_v3, data_v3 = encode_pool_call(v3)
        assert target_v3.lower() == v3.pool_id.lower()
        assert data_v3.hex() == "3850c7bd"

        v4 = _make_v4_pool("0x" + "22" * 32)
        target_v4, data_v4 = encode_pool_call(v4)
        assert target_v4.lower() == STATE_VIEW_ADDRESS.lower()
        assert data_v4[:4].hex() == "c815641c"
        assert "0x" + data_v4[4:].hex() == v4.pool_id.lower()

    def test_decode_slot0_error_on_zero_price(self) -> None:
        """Uninitialized pool (sqrt_price_x96 == 0) strictly raises ValueError."""
        zero_data = _encode_v3_slot0(0, 0)
        with pytest.raises(ValueError, match="Invalid non-positive sqrt_price_x96"):
            decode_slot0(zero_data)
