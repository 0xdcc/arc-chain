"""Multicall2 Contract & Read-Only Dispatch Core Tests (Arc v3 independent).

Migrated and adapted from original tests/test_multicall_reader.py.
Covers:
1. Multicall2 request calldata encoding & response decoding (tryAggregate);
2. Combined V3 slot0 and V4 StateView.getSlot0 batch dispatch;
3. Single-item failure isolation (revert in one pool does not fail the batch);
4. Atomic block number binding from call[0] getBlockNumber();
5. Decoupled read-only transport injection contract;
6. Rigorous V4 128-byte StateView retdata validation (both default & provenance modes);
7. Error boundaries: malformed hex/ABI, call[0] revert, return count mismatch;
8. Real provenance verification: block header validation, liquidity query, reorg detection;
9. Type safety: verified AnyPool union without Any;
10. Explicit reservation of PoolReader.batch_quote fallback and PoolIdentity seam for M3.
"""

from __future__ import annotations

import inspect
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_args
from unittest.mock import MagicMock

import pytest
from eth_abi.abi import encode as abi_encode
from web3 import Web3

# Ensure candidate src is available in sys.path
_CANDIDATE_SRC = Path(__file__).resolve().parents[3] / "src"
if _CANDIDATE_SRC.is_dir() and str(_CANDIDATE_SRC) not in sys.path:
    sys.path.insert(0, str(_CANDIDATE_SRC))

from research.market_data.multicall import (  # noqa: E402
    GET_BLOCK_NUMBER_SELECTOR,
    GET_RESERVES_SELECTOR,
    MULTICALL2_ADDRESS,
    STATE_VIEW_ADDRESS,
    STATE_VIEW_GET_SLOT0_SELECTOR,
    TRY_AGGREGATE_SELECTOR,
    V3_SLOT0_SELECTOR,
    AnyPool,
    MulticallPoolReader,
    PoolSpec,
    PriceQuote,
    ReadOnlyRpcTransport,
    V4PoolSpec,
    adapt_pool_identity,
    decode_multicall_response,
    decode_quote_from_result,
    encode_multicall_calls,
    encode_pool_slot0_call,
)


def _make_v3_pool(address_suffix: str, label: str, fee_bps: float = 30.0) -> PoolSpec:
    """Construct a test V3 pool (20-byte standard EVM address)."""
    addr = "0x" + address_suffix.rjust(40, "0")
    return PoolSpec(
        address=addr,
        label=label,
        fee_bps=fee_bps,
        token0="0x" + "1" * 40,
        token1="0x" + "2" * 40,
        dec0=18,
        dec1=18,
    )


def _make_v4_pool(pool_id_suffix: str, label: str, fee_bps: float = 30.0) -> V4PoolSpec:
    """Construct a test V4 pool (32-byte bytes32 poolId)."""
    pool_id = "0x" + pool_id_suffix.rjust(64, "0")
    return V4PoolSpec(
        address=pool_id,
        label=label,
        fee_bps=fee_bps,
        token0="0x" + "1" * 40,
        token1="0x" + "2" * 40,
        dec0=18,
        dec1=18,
    )


def _price_to_sqrt_price_x96(price: float, dec0: int = 18, dec1: int = 18) -> int:
    """Calculate uint160 sqrtPriceX96 from spot price."""
    ratio = price / (10 ** (dec0 - dec1))
    sqrt = math.sqrt(ratio)
    return int(sqrt * (2**96))


class TestMulticallEncodingAndDecoding:
    """Test Multicall2 calldata construction and return data decoding."""

    def test_constants_and_selectors(self) -> None:
        """Verify contract addresses and core function selectors."""
        w3 = Web3()
        assert MULTICALL2_ADDRESS == "0x2cAC2D899eCC914d704FeaAE33ac1bF36277DaD1"
        assert STATE_VIEW_ADDRESS == "0xf3334192d15450cdd385c8b70e03f9a6bd9e673b"

        # tryAggregate(bool,(address,bytes)[]) -> 0xbce38bd7
        assert (
            "0x" + w3.keccak(text="tryAggregate(bool,(address,bytes)[])")[:4].hex()
            == TRY_AGGREGATE_SELECTOR
        )
        # getBlockNumber() -> 0x42cbb15c
        assert "0x" + w3.keccak(text="getBlockNumber()")[:4].hex() == GET_BLOCK_NUMBER_SELECTOR
        # slot0() -> 0x3850c7bd
        assert "0x" + w3.keccak(text="slot0()")[:4].hex() == V3_SLOT0_SELECTOR
        # getSlot0(bytes32) -> 0xc815641c
        assert "0x" + w3.keccak(text="getSlot0(bytes32)")[:4].hex() == STATE_VIEW_GET_SLOT0_SELECTOR
        # getReserves() -> 0x0902f1ac
        assert "0x" + w3.keccak(text="getReserves()")[:4].hex() == GET_RESERVES_SELECTOR

    def test_encode_pool_slot0_call_v3(self) -> None:
        """Test Uniswap V3 slot0 call encoding."""
        pool = _make_v3_pool("ab", label="V3 Pool")
        target, calldata = encode_pool_slot0_call(pool)
        assert target == Web3.to_checksum_address(pool.address)
        assert calldata == bytes.fromhex("3850c7bd")

    def test_encode_pool_slot0_call_v4(self) -> None:
        """Test Uniswap V4 StateView.getSlot0 call encoding."""
        pool = _make_v4_pool("cd", label="V4 Pool")
        target, calldata = encode_pool_slot0_call(pool)
        assert target == Web3.to_checksum_address(STATE_VIEW_ADDRESS)
        expected_calldata = bytes.fromhex("c815641c") + bytes.fromhex(pool.address[2:])
        assert calldata == expected_calldata
        assert len(calldata) == 4 + 32  # selector(4) + bytes32 poolId(32)

    def test_encode_pool_slot0_call_invalid_address(self) -> None:
        """Non-20-byte and non-32-byte pool address must raise ValueError."""
        mock_pool = MagicMock()
        mock_pool.address = "0xdead"  # Invalid length
        with pytest.raises(ValueError, match="Unsupported pool address") as exc_info:
            encode_pool_slot0_call(mock_pool)
        assert "Unsupported pool address" in str(exc_info.value)

    def test_encode_and_decode_multicall_roundtrip(self) -> None:
        """Test full Multicall calldata construction and return decoding roundtrip."""
        addr1 = "0x" + "1" * 40
        addr2 = "0x" + "2" * 40
        calls = [(addr1, b"\x01\x02"), (addr2, b"\x03\x04")]

        calldata = encode_multicall_calls(calls, require_success=False)
        assert calldata.startswith(TRY_AGGREGATE_SELECTOR)

        # Mock Multicall2 return: ((bool, bytes)[])
        mock_decoded_items = [
            (True, b"\xaa\xbb"),
            (False, b""),
        ]
        encoded_return = abi_encode(["(bool,bytes)[]"], [mock_decoded_items])
        raw_hex = "0x" + encoded_return.hex()

        result = decode_multicall_response(raw_hex)
        assert len(result) == 2
        assert result[0] == (True, b"\xaa\xbb")
        assert result[1] == (False, b"")

    def test_decode_quote_from_result_directly(self) -> None:
        """Test direct single-pool result decoding and error isolation."""
        pool = _make_v3_pool("ab", label="Direct Pool")
        sqrt_val = _price_to_sqrt_price_x96(2500.0)
        retdata = sqrt_val.to_bytes(32, "big") + b"\x00" * 192

        quote = decode_quote_from_result(
            pool=pool,
            success=True,
            retdata=retdata,
            block_number=54321,
        )
        assert quote is not None
        assert isinstance(quote, PriceQuote)
        assert math.isclose(quote.price, 2500.0, rel_tol=1e-4)
        assert quote.block_number == 54321

        # Reverted call yields None gracefully
        revert_quote = decode_quote_from_result(
            pool=pool,
            success=False,
            retdata=b"",
            block_number=54321,
        )
        assert revert_quote is None

    def test_v4_slot0_128_bytes_enforced_in_direct_decoding(self) -> None:
        """Verify V4 StateView retdata requires full 128 bytes; 32 bytes yields None."""
        p4 = _make_v4_pool("04", label="V4 Pool")
        sqrt_val = _price_to_sqrt_price_x96(100.0)

        # Truncated 32-byte retdata -> returns None
        truncated_retdata = sqrt_val.to_bytes(32, "big")
        quote_truncated = decode_quote_from_result(
            pool=p4,
            success=True,
            retdata=truncated_retdata,
            block_number=100,
        )
        assert quote_truncated is None

        # Full 128-byte retdata (sqrtPriceX96, tick, protocolFee, lpFee) -> succeeds
        full_128_retdata = (
            sqrt_val.to_bytes(32, "big")
            + (100).to_bytes(32, "big")
            + (0).to_bytes(32, "big")
            + (3000).to_bytes(32, "big")
        )
        quote_valid = decode_quote_from_result(
            pool=p4,
            success=True,
            retdata=full_128_retdata,
            block_number=100,
        )
        assert quote_valid is not None
        assert math.isclose(quote_valid.price, 100.0, rel_tol=1e-4)
        assert quote_valid.raw_fee == 3000
        assert quote_valid.fee_denominator == 1_000_000


class TestMulticallBatchExecution:
    """Test batch quote execution via Multicall2 tryAggregate."""

    def test_combined_v3_and_v4_batch(self) -> None:
        """Test mixed V3 and V4 batch execution in a single RPC call with atomic block height."""
        p1 = _make_v3_pool("01", label="Pool 1")
        p2 = _make_v3_pool("02", label="Pool 2")
        p3 = _make_v4_pool("03", label="Pool 3")
        p4 = _make_v4_pool("04", label="Pool 4")
        pools: list[AnyPool] = [p1, p2, p3, p4]

        expected_block = 123456
        price_v3_1 = 3000.0
        price_v3_2 = 0.0005
        price_v4_1 = 2000.0
        price_v4_2 = 1.0

        sqrt_v3_1 = _price_to_sqrt_price_x96(price_v3_1)
        sqrt_v3_2 = _price_to_sqrt_price_x96(price_v3_2)
        sqrt_v4_1 = _price_to_sqrt_price_x96(price_v4_1)
        sqrt_v4_2 = _price_to_sqrt_price_x96(price_v4_2)

        mock_items = [
            (True, expected_block.to_bytes(32, "big")),
            (True, sqrt_v3_1.to_bytes(32, "big") + b"\x00" * 192),
            (True, sqrt_v3_2.to_bytes(32, "big") + b"\x00" * 192),
            (True, sqrt_v4_1.to_bytes(32, "big") + b"\x00" * 96),  # 128 bytes
            (True, sqrt_v4_2.to_bytes(32, "big") + b"\x00" * 96),  # 128 bytes
        ]
        raw_result_hex = "0x" + abi_encode(["(bool,bytes)[]"], [mock_items]).hex()

        mock_rpc = MagicMock()
        mock_rpc.call.return_value = {"result": raw_result_hex}

        reader = MulticallPoolReader(rpc=mock_rpc)
        quotes = reader.batch_quote_multicall(pools)

        assert len(quotes) == 4
        # Verify single RPC call
        mock_rpc.call.assert_called_once()
        call_arg = mock_rpc.call.call_args[0]
        assert call_arg[0] == "eth_call"
        assert call_arg[1][0]["to"].lower() == MULTICALL2_ADDRESS.lower()

        # Verify all quotes have atomic block height aligned to call[0]
        for q in quotes:
            assert isinstance(q, PriceQuote)
            assert q.block_number == expected_block

        # Verify price precision within 0.01%
        assert math.isclose(quotes[0].price, price_v3_1, rel_tol=1e-4)
        assert math.isclose(quotes[1].price, price_v3_2, rel_tol=1e-4)
        assert math.isclose(quotes[2].price, price_v4_1, rel_tol=1e-4)
        assert math.isclose(quotes[3].price, price_v4_2, rel_tol=1e-4)

    def test_partial_revert_graceful_filter(self) -> None:
        """Single pool revert (success=False) does not break batch; reverted pool is filtered."""
        p1 = _make_v3_pool("01", label="Pool 1")
        p2 = _make_v3_pool("02", label="Pool 2 Revert")
        p3 = _make_v4_pool("03", label="Pool 3")
        pools: list[AnyPool] = [p1, p2, p3]

        expected_block = 88888
        price_v3_1 = 3000.0
        price_v4_3 = 1500.0

        sqrt_v3_1 = _price_to_sqrt_price_x96(price_v3_1)
        sqrt_v4_3 = _price_to_sqrt_price_x96(price_v4_3)

        # call[0]: getBlockNumber
        # call[1]: Pool 1 success
        # call[2]: Pool 2 revert (success=False, retdata=b"")
        # call[3]: Pool 3 success (128 bytes for V4)
        mock_items = [
            (True, expected_block.to_bytes(32, "big")),
            (True, sqrt_v3_1.to_bytes(32, "big") + b"\x00" * 192),
            (False, b""),  # Pool 2 reverts
            (True, sqrt_v4_3.to_bytes(32, "big") + b"\x00" * 96),
        ]
        raw_result_hex = "0x" + abi_encode(["(bool,bytes)[]"], [mock_items]).hex()

        mock_rpc = MagicMock()
        mock_rpc.call.return_value = {"result": raw_result_hex}

        reader = MulticallPoolReader(rpc=mock_rpc)
        quotes = reader.batch_quote_multicall(pools)

        # Batch succeeds with 2 valid quotes, reverted pool is safely omitted
        assert len(quotes) == 2
        assert quotes[0].pool == p1
        assert quotes[1].pool == p3

    def test_atomic_block_number_binding(self) -> None:
        """Call[0] block number is atomically passed to all PriceQuotes."""
        pools: list[AnyPool] = [_make_v3_pool("01", label="Pool 1")]

        expected_block = 999999
        mock_items = [
            (True, expected_block.to_bytes(32, "big")),
            (True, (2**96).to_bytes(32, "big") + b"\x00" * 192),
        ]
        raw_result_hex = "0x" + abi_encode(["(bool,bytes)[]"], [mock_items]).hex()

        mock_rpc = MagicMock()
        mock_rpc.call.return_value = {"result": raw_result_hex}

        reader = MulticallPoolReader(rpc=mock_rpc)
        quotes = reader.batch_quote_multicall(pools)

        assert len(quotes) == 1
        assert quotes[0].block_number == expected_block

    def test_empty_pool_list(self) -> None:
        """Empty pool list returns empty list immediately without RPC call."""
        mock_rpc = MagicMock()
        reader = MulticallPoolReader(rpc=mock_rpc)
        assert reader.batch_quote_multicall([]) == []
        mock_rpc.call.assert_not_called()

    def test_invalid_sqrt_price_filtered(self) -> None:
        """Pool with sqrtPriceX96 <= 0 is safely filtered out."""
        p = _make_v3_pool("01", label="Inactive Pool")
        mock_items = [
            (True, (100).to_bytes(32, "big")),
            (True, (0).to_bytes(32, "big")),  # sqrtPriceX96 == 0
        ]
        raw_hex = "0x" + abi_encode(["(bool,bytes)[]"], [mock_items]).hex()
        mock_rpc = MagicMock()
        mock_rpc.call.return_value = {"result": raw_hex}

        reader = MulticallPoolReader(rpc=mock_rpc)
        quotes = reader.batch_quote_multicall([p])
        assert len(quotes) == 0

    def test_v4_truncated_retdata_rejected_in_default_mode(self) -> None:
        """V4 StateView retdata must be 128 bytes; 32-byte truncated data is rejected in default mode."""
        p4 = _make_v4_pool("04", label="V4 Pool")
        # 32-byte retdata (only sqrtPriceX96, missing tick and fee words)
        mock_items = [
            (True, (50000).to_bytes(32, "big")),
            (True, (2**96).to_bytes(32, "big")),  # Truncated 32 bytes
        ]
        raw_hex = "0x" + abi_encode(["(bool,bytes)[]"], [mock_items]).hex()

        mock_rpc = MagicMock()
        mock_rpc.call.return_value = {"result": raw_hex}
        reader = MulticallPoolReader(rpc=mock_rpc)

        # Must reject truncated retdata and produce 0 quotes
        quotes = reader.batch_quote_multicall([p4], with_provenance=False)
        assert len(quotes) == 0


class TestMulticallTransportContract:
    """Test read-only transport injection contract and error boundaries."""

    def test_transport_injection_and_missing_rpc_rejection(self) -> None:
        """MulticallPoolReader rejects execution when read-only RPC transport is not injected."""
        reader = MulticallPoolReader(rpc=None)
        p = _make_v3_pool("01", label="Pool 1")

        with pytest.raises(RuntimeError, match="Read-only RPC transport must be injected") as exc_info:
            reader.batch_quote_multicall([p])
        assert "Read-only RPC transport must be injected" in str(exc_info.value)

    def test_read_only_protocol_compliance(self) -> None:
        """Injected transport implementing ReadOnlyRpcTransport protocol is called correctly."""

        class MockReadOnlyTransport:
            def __init__(self, raw_hex: str) -> None:
                self.raw_hex = raw_hex
                self.calls: list[tuple[str, list[Any]]] = []

            def call(
                self,
                method: str,
                params: list[Any] | tuple[Any, ...] | None = None,
                block_identifier: str | None = None,
            ) -> dict[str, Any]:
                self.calls.append((method, list(params or [])))
                return {"result": self.raw_hex}

        expected_block = 777
        mock_items = [
            (True, expected_block.to_bytes(32, "big")),
            (True, (2**96).to_bytes(32, "big") + b"\x00" * 192),
        ]
        raw_hex = "0x" + abi_encode(["(bool,bytes)[]"], [mock_items]).hex()

        transport = MockReadOnlyTransport(raw_hex)
        assert isinstance(transport, ReadOnlyRpcTransport)

        reader = MulticallPoolReader(rpc=transport)
        p = _make_v3_pool("01", label="Pool 1")
        quotes = reader.batch_quote_multicall([p])

        assert len(quotes) == 1
        assert len(transport.calls) == 1
        assert transport.calls[0][0] == "eth_call"
        assert quotes[0].block_number == expected_block

    def test_read_only_transport_method_whitelist_enforced(self) -> None:
        """Verify transport only receives read-only methods ('eth_call', 'eth_getBlockByNumber')."""
        called_methods: list[str] = []

        class StrictReadOnlyTransport:
            def call(
                self,
                method: str,
                params: list[Any] | tuple[Any, ...] | None = None,
                block_identifier: str | None = None,
            ) -> dict[str, Any]:
                called_methods.append(method)
                if method == "eth_getBlockByNumber":
                    return {
                        "result": {
                            "hash": "0x" + "c" * 64,
                            "number": hex(500),
                            "timestamp": hex(1700000000),
                        }
                    }
                elif method == "eth_call":
                    mock_items = [
                        (True, (500).to_bytes(32, "big")),
                        (True, (2**96).to_bytes(32, "big") + b"\x00" * 192),
                        (True, (1000).to_bytes(32, "big")),
                    ]
                    return {"result": "0x" + abi_encode(["(bool,bytes)[]"], [mock_items]).hex()}
                else:
                    raise PermissionError(f"Attempted write or unauthorized RPC method: {method}")

        transport = StrictReadOnlyTransport()
        reader = MulticallPoolReader(rpc=transport)
        quotes = reader.batch_quote_multicall([_make_v3_pool("01", "P1")], with_provenance=True)
        assert len(quotes) == 1

        # Assert all invoked methods are strictly in read-only set
        assert set(called_methods).issubset({"eth_call", "eth_getBlockByNumber"})
        assert "eth_sendRawTransaction" not in called_methods
        assert "eth_sendTransaction" not in called_methods


class TestMulticallErrorBoundariesAndFalsificationControls:
    """Rigorous error boundary controls evaluating Multicall2 fail-closed semantics."""

    def test_malformed_multicall_abi_fails_closed(self) -> None:
        """Malformed hex or corrupt ABI in RPC response raises explicit error and yields 0 quotes."""
        # Truncated ABI dynamic array: points to offset 32 but has no array length/elements
        corrupt_hex = "0x" + "00" * 31 + "20"
        with pytest.raises(ValueError, match="Malformed ABI in multicall response") as exc_info1:
            decode_multicall_response(corrupt_hex)
        assert "Malformed ABI" in str(exc_info1.value)

        mock_rpc = MagicMock()
        mock_rpc.call.return_value = {"result": corrupt_hex}
        reader = MulticallPoolReader(rpc=mock_rpc)

        # MulticallPoolReader converts to explicit RuntimeError and fails closed
        with pytest.raises(RuntimeError, match="Multicall response decoding failed") as exc_info2:
            reader.batch_quote_multicall([_make_v3_pool("01", "P1")])
        assert "Multicall response decoding failed" in str(exc_info2.value)

    def test_call_zero_revert_fails_closed(self) -> None:
        """When call[0] getBlockNumber reverts, batch_quote_multicall fails closed."""
        p = _make_v3_pool("01", "P1")
        mock_items = [
            (False, b""),  # blockNumber reverted
            (True, (2**96).to_bytes(32, "big") + b"\x00" * 192),
        ]
        raw_hex = "0x" + abi_encode(["(bool,bytes)[]"], [mock_items]).hex()

        mock_rpc = MagicMock()
        mock_rpc.call.return_value = {"result": raw_hex}
        reader = MulticallPoolReader(rpc=mock_rpc)

        with pytest.raises(RuntimeError, match="Multicall block identity unavailable") as exc_info:
            reader.batch_quote_multicall([p])
        assert "Multicall block identity unavailable" in str(exc_info.value)

    def test_return_count_mismatch_raises_runtime_error(self) -> None:
        """Multicall returning fewer or more items than calls raises RuntimeError."""
        mock_items = [(True, (12345).to_bytes(32, "big"))]  # Only 1 item for 2 calls
        raw_hex = "0x" + abi_encode(["(bool,bytes)[]"], [mock_items]).hex()

        mock_rpc = MagicMock()
        mock_rpc.call.return_value = {"result": raw_hex}
        reader = MulticallPoolReader(rpc=mock_rpc)

        with pytest.raises(RuntimeError, match="Multicall return count mismatch") as exc_info:
            reader.batch_quote_multicall([_make_v3_pool("01", "P1")])
        assert "Multicall return count mismatch" in str(exc_info.value)


class TestMulticallProvenanceVerification:
    """Rigorous verification of quote provenance: block header, reorgs, and liquidity."""

    def test_batch_quote_with_provenance_success(self) -> None:
        """Full provenance verification with block header, liquidity, and V4 fees."""
        v3 = _make_v3_pool("01", label="V3 Pool")
        v4 = _make_v4_pool("02", label="V4 Pool")
        block_height = 2000
        block_hash = "0x" + "a" * 64
        block_ts = 1710000000

        sqrt_v3 = _price_to_sqrt_price_x96(2000.0)
        sqrt_v4 = _price_to_sqrt_price_x96(1.0)
        v4_retdata = (
            sqrt_v4.to_bytes(32, "big")
            + (0).to_bytes(32, "big")
            + (0).to_bytes(32, "big")
            + (2500).to_bytes(32, "big")  # lpFee = 2500
        )

        mock_rpc = MagicMock()
        mock_rpc.call.side_effect = [
            # 1. eth_getBlockByNumber("latest", False)
            {
                "result": {
                    "hash": block_hash,
                    "number": hex(block_height),
                    "timestamp": hex(block_ts),
                }
            },
            # 2. eth_call multicall
            {
                "result": "0x"
                + abi_encode(
                    ["(bool,bytes)[]"],
                    [
                        [
                            (True, block_height.to_bytes(32, "big")),
                            (True, sqrt_v3.to_bytes(32, "big") + b"\x00" * 192),
                            (True, v4_retdata),
                            (True, (5000000).to_bytes(32, "big")),  # V3 liquidity
                            (True, (10000000).to_bytes(32, "big")),  # V4 liquidity
                        ]
                    ],
                ).hex()
            },
            # 3. eth_getBlockByNumber(block_height, False) for reorg verification
            {
                "result": {
                    "hash": block_hash,
                    "number": hex(block_height),
                    "timestamp": hex(block_ts),
                }
            },
        ]

        reader = MulticallPoolReader(rpc=mock_rpc)
        quotes = reader.batch_quote_multicall([v3, v4], with_provenance=True)

        assert len(quotes) == 2
        q_v3, q_v4 = quotes[0], quotes[1]

        # Verify provenance fields
        assert q_v3.block_hash == block_hash
        assert q_v3.block_timestamp == block_ts
        assert q_v3.active_liquidity == 5000000

        assert q_v4.block_hash == block_hash
        assert q_v4.block_timestamp == block_ts
        assert q_v4.active_liquidity == 10000000
        assert q_v4.raw_fee == 2500
        assert q_v4.fee_denominator == 1_000_000

    def test_batch_quote_with_provenance_block_mismatch(self) -> None:
        """Call[0] block height disagreeing with latest block header raises RuntimeError."""
        p = _make_v3_pool("01", "P1")
        mock_rpc = MagicMock()
        mock_rpc.call.side_effect = [
            # Header block 1000
            {
                "result": {
                    "hash": "0x" + "a" * 64,
                    "number": hex(1000),
                    "timestamp": hex(1700000000),
                }
            },
            # Multicall returns block 1001 (mismatch)
            {
                "result": "0x"
                + abi_encode(
                    ["(bool,bytes)[]"],
                    [
                        [
                            (True, (1001).to_bytes(32, "big")),
                            (True, (2**96).to_bytes(32, "big") + b"\x00" * 192),
                            (True, (1000).to_bytes(32, "big")),
                        ]
                    ],
                ).hex()
            },
        ]

        reader = MulticallPoolReader(rpc=mock_rpc)
        with pytest.raises(RuntimeError, match="disagrees with requested block hash") as exc_info:
            reader.batch_quote_multicall([p], with_provenance=True)
        assert "disagrees with requested block hash" in str(exc_info.value)

    def test_batch_quote_with_provenance_block_reorg(self) -> None:
        """Header recheck hash mismatch (chain reorganization) raises RuntimeError."""
        p = _make_v3_pool("01", "P1")
        mock_rpc = MagicMock()
        mock_rpc.call.side_effect = [
            # Initial header
            {
                "result": {
                    "hash": "0x" + "a" * 64,
                    "number": hex(1000),
                    "timestamp": hex(1700000000),
                }
            },
            # Multicall
            {
                "result": "0x"
                + abi_encode(
                    ["(bool,bytes)[]"],
                    [
                        [
                            (True, (1000).to_bytes(32, "big")),
                            (True, (2**96).to_bytes(32, "big") + b"\x00" * 192),
                            (True, (1000).to_bytes(32, "big")),
                        ]
                    ],
                ).hex()
            },
            # Recheck header has different hash (reorg occurred)
            {
                "result": {
                    "hash": "0x" + "b" * 64,
                    "number": hex(1000),
                    "timestamp": hex(1700000000),
                }
            },
        ]

        reader = MulticallPoolReader(rpc=mock_rpc)
        with pytest.raises(RuntimeError, match="Quote block reorganized") as exc_info:
            reader.batch_quote_multicall([p], with_provenance=True)
        assert "Quote block reorganized" in str(exc_info.value)


class TestPoolReaderContractObligation:
    """Verify contracts required by subsequent PoolReader (reserved for M3)."""

    def test_batch_quote_multicall_api_signature_for_m3_pool_reader(self) -> None:
        """Verify MulticallPoolReader.batch_quote_multicall signature matches PoolReader requirements."""
        sig = inspect.signature(MulticallPoolReader.batch_quote_multicall)
        params = list(sig.parameters.keys())

        # Expected: self, pools, *, with_provenance=False
        assert "pools" in params
        assert "with_provenance" in params
        assert sig.parameters["with_provenance"].default is False
        assert sig.parameters["with_provenance"].kind == inspect.Parameter.KEYWORD_ONLY

    def test_anypool_type_safety_union_verified(self) -> None:
        """Verify AnyPool is a verifiable Union[PoolSpec, V4PoolSpec] without Any."""
        assert Any not in get_args(AnyPool)
        assert PoolSpec in get_args(AnyPool)
        assert V4PoolSpec in get_args(AnyPool)

        # PriceQuote.pool must be typed as AnyPool
        assert PriceQuote.__annotations__["pool"] in ("AnyPool", AnyPool)

        # MulticallPoolReader.batch_quote_multicall parameter annotation
        sig = inspect.signature(MulticallPoolReader.batch_quote_multicall)
        pools_ann = str(sig.parameters["pools"].annotation)
        assert "AnyPool" in pools_ann


@dataclass(frozen=True)
class _MockPoolIdentity:
    """Mock representing M1 PoolIdentity without business import dependencies."""

    chain_id: int
    protocol: str
    pool_id: str
    token0: Any
    token1: Any
    fee_bps: float = 30.0
    tick_spacing: int = 60
    hooks: str | None = None
    factory: str | None = None
    verified_source: str = "on_chain"


@dataclass(frozen=True)
class _MockTokenIdentity:
    """Mock representing M1 TokenIdentity without business import dependencies."""

    chain_id: int
    address: str
    decimals: int
    symbol: str


class TestPoolIdentityDecimalsAdaptationAndFalsification:
    """Tests for M2 decimals fix: strict resolution, 6/18, 18/6, 18/18, and rejection of 18 default."""

    def test_adapt_pool_spec_and_v4_pool_spec_passthrough(self) -> None:
        """Existing PoolSpec and V4PoolSpec explicit values are preserved intact."""
        v3 = _make_v3_pool("1", "V3/Test")
        v4 = _make_v4_pool("2", "V4/Test")
        assert adapt_pool_identity(v3) is v3
        assert adapt_pool_identity(v4) is v4

    def test_adapt_pool_identity_6_18_decimals_and_price_magnitude_falsification(self) -> None:
        """Verify 6/18 pair (e.g. USDC/WETH) resolves correctly and falsifies naive 18/18 default."""
        t0_addr = "0x" + "a" * 40
        t1_addr = "0x" + "b" * 40
        pool = _MockPoolIdentity(
            chain_id=5042,
            protocol="uniswap_v3",
            pool_id="0x" + "1" * 40,
            token0=t0_addr,
            token1=t1_addr,
        )
        catalog = {
            (5042, t0_addr): 6,
            (5042, t1_addr): 18,
        }
        spec = adapt_pool_identity(pool, token_catalog=catalog)
        assert isinstance(spec, PoolSpec)
        assert spec.dec0 == 6
        assert spec.dec1 == 18

        # SqrtPriceX96 = 2^96 corresponds to 1:1 atom ratio
        retdata = (2**96).to_bytes(32, "big")
        quote = decode_quote_from_result(spec, True, retdata, 100)
        assert quote is not None
        # Raw price is 1.0; with dec0=6 and dec1=18, price = 1.0 * 10^(6 - 18) = 1e-12
        assert abs(quote.price - 1e-12) < 1e-20
        # Falsification: If default 18 was used, price would be 1.0 (difference is 10^12)
        naive_default_price = 1.0
        assert abs(quote.price - naive_default_price) > 0.999

    def test_adapt_pool_identity_18_6_decimals_and_price_magnitude_falsification(self) -> None:
        """Verify 18/6 pair (e.g. WETH/USDC) resolves correctly and falsifies naive 18/18 default."""
        t0_addr = "0x" + "c" * 40
        t1_addr = "0x" + "d" * 40
        pool = _MockPoolIdentity(
            chain_id=5042,
            protocol="uniswap_v3",
            pool_id="0x" + "2" * 40,
            token0=t0_addr,
            token1=t1_addr,
        )
        catalog = {
            (5042, t0_addr): 18,
            (5042, t1_addr): 6,
        }
        spec = adapt_pool_identity(pool, token_catalog=catalog)
        assert isinstance(spec, PoolSpec)
        assert spec.dec0 == 18
        assert spec.dec1 == 6

        retdata = (2**96).to_bytes(32, "big")
        quote = decode_quote_from_result(spec, True, retdata, 100)
        assert quote is not None
        # With dec0=18 and dec1=6, price = 1.0 * 10^(18 - 6) = 1e12
        assert abs(quote.price - 1e12) < 1e-3
        naive_default_price = 1.0
        assert abs(quote.price - naive_default_price) > 1e11

    def test_adapt_pool_identity_18_18_decimals(self) -> None:
        """Verify 18/18 pair (e.g. WETH/DAI) resolves correctly."""
        t0_addr = "0x" + "e" * 40
        t1_addr = "0x" + "f" * 40
        pool = _MockPoolIdentity(
            chain_id=5042,
            protocol="uniswap_v3",
            pool_id="0x" + "3" * 40,
            token0=t0_addr,
            token1=t1_addr,
        )
        catalog = {
            (5042, t0_addr): 18,
            (5042, t1_addr): 18,
        }
        spec = adapt_pool_identity(pool, token_catalog=catalog)
        assert isinstance(spec, PoolSpec)
        assert spec.dec0 == 18
        assert spec.dec1 == 18

        retdata = (2**96).to_bytes(32, "big")
        quote = decode_quote_from_result(spec, True, retdata, 100)
        assert quote is not None
        assert abs(quote.price - 1.0) < 1e-9

    def test_adapt_pool_identity_missing_decimals_strictly_prohibits_18_default(self) -> None:
        """Pool with missing decimals raises ValueError and strictly refuses 18 default."""
        pool = _MockPoolIdentity(
            chain_id=5042,
            protocol="uniswap_v3",
            pool_id="0x" + "4" * 40,
            token0="0x" + "a" * 40,
            token1="0x" + "b" * 40,
        )
        with pytest.raises(ValueError, match="defaulting to 18 is strictly forbidden"):
            adapt_pool_identity(pool)

    def test_adapt_pool_identity_chain_5042_rejects_4663_catalog(self) -> None:
        """Chain 5042 pools strictly reject Robinhood (4663) catalog entries and never default."""
        t0_addr = "0x" + "a" * 40
        t1_addr = "0x" + "b" * 40
        pool_5042 = _MockPoolIdentity(
            chain_id=5042,
            protocol="uniswap_v3",
            pool_id="0x" + "5" * 40,
            token0=t0_addr,
            token1=t1_addr,
        )
        # 1. Injected catalog entry with wrong chain_id=4663
        catalog_4663 = {
            t0_addr: _MockTokenIdentity(chain_id=4663, address=t0_addr, decimals=6, symbol="USDC"),
            t1_addr: _MockTokenIdentity(chain_id=4663, address=t1_addr, decimals=18, symbol="WETH"),
        }
        with pytest.raises(ValueError, match="disagrees with pool chain_id 5042"):
            adapt_pool_identity(pool_5042, token_catalog=catalog_4663)

        # 2. Without catalog, chain 5042 does NOT fall back to 4663 built-in catalog
        with pytest.raises(ValueError, match="defaulting to 18 is strictly forbidden"):
            adapt_pool_identity(pool_5042)

    def test_adapt_pool_identity_with_token_identity_duck_types(self) -> None:
        """Pool containing TokenIdentity objects with .decimals resolves successfully."""
        t0 = _MockTokenIdentity(chain_id=5042, address="0x" + "a" * 40, decimals=6, symbol="USDC")
        t1 = _MockTokenIdentity(chain_id=5042, address="0x" + "b" * 40, decimals=18, symbol="WETH")
        pool = _MockPoolIdentity(
            chain_id=5042,
            protocol="uniswap_v3",
            pool_id="0x" + "6" * 40,
            token0=t0,
            token1=t1,
        )
        spec = adapt_pool_identity(pool)
        assert spec.dec0 == 6
        assert spec.dec1 == 18

    def test_adapt_v4_pool_identity_66_bytes(self) -> None:
        """Pool with 66-character bytes32 poolId adapts to V4PoolSpec with correct decimals."""
        pool_id_66 = "0x" + "7" * 64
        t0_addr = "0x" + "a" * 40
        t1_addr = "0x" + "b" * 40
        pool = _MockPoolIdentity(
            chain_id=5042,
            protocol="uniswap_v4",
            pool_id=pool_id_66,
            token0=t0_addr,
            token1=t1_addr,
        )
        catalog = {
            (5042, t0_addr): 6,
            (5042, t1_addr): 18,
        }
        spec = adapt_pool_identity(pool, token_catalog=catalog)
        assert isinstance(spec, V4PoolSpec)
        assert spec.dec0 == 6
        assert spec.dec1 == 18
        assert spec.address == pool_id_66

    def test_batch_quote_multicall_excludes_unresolvable_decimals_pool(self) -> None:
        """MulticallPoolReader.batch_quote_multicall gracefully excludes pools with unresolvable decimals."""
        resolvable = _make_v3_pool("1", "V3/Valid")
        unresolvable = _MockPoolIdentity(
            chain_id=5042,
            protocol="uniswap_v3",
            pool_id="0x" + "8" * 40,
            token0="0x" + "a" * 40,
            token1="0x" + "b" * 40,
        )

        mock_rpc = MagicMock()
        retdata = (2**96).to_bytes(32, "big")
        mock_response = [
            (True, (12345).to_bytes(32, "big")),
            (True, retdata),
        ]
        mock_rpc.call.return_value = {
            "result": "0x" + abi_encode(["(bool,bytes)[]"], [mock_response]).hex()
        }

        bad_pools: list[Any] = [resolvable, unresolvable]
        reader = MulticallPoolReader(rpc=mock_rpc)
        quotes = reader.batch_quote_multicall(bad_pools)
        assert len(quotes) == 1
        assert quotes[0].pool.address == resolvable.address
