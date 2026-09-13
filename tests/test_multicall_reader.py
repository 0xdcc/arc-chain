"""Multicall2 单次批量读池单元测试 (无真实网络依赖).

测试覆盖:
1. Multicall2 请求 calldata 编码与响应解码逻辑;
2. V3 池 (slot0) 与 V4 池 (StateView.getSlot0) 组合打包;
3. 单池 Revert 时 (success=False) 整体不崩盘，仅优雅过滤失败池;
4. call[0] getBlockNumber 原子块高解析与透传给 PriceQuote;
5. PoolReader 优雅降级机制 (Multicall 异常降级至 ThreadPoolExecutor).
"""

from __future__ import annotations

import math
from unittest.mock import MagicMock

import pytest
from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from web3 import Web3

from research.market_data.multicall import (
    GET_BLOCK_NUMBER_SELECTOR,
    MULTICALL2_ADDRESS,
    STATE_VIEW_ADDRESS,
    STATE_VIEW_GET_SLOT0_SELECTOR,
    TRY_AGGREGATE_SELECTOR,
    V3_SLOT0_SELECTOR,
    AnyPool,
    MulticallPoolReader,
    PoolSpec,
    PriceQuote,
    V4PoolSpec,
    decode_multicall_response,
    encode_multicall_calls,
    encode_pool_slot0_call,
)
from research.market_data.pool_reader import PoolReader


def _make_v3_pool(address_suffix: str, label: str, fee_bps: float = 30.0) -> PoolSpec:
    """构造测试用 V3 池 (20 字节标准 EVM 地址)."""
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
    """构造测试用 V4 池 (32 字节 bytes32 poolId)."""
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
    """由现货价反算 uint160 sqrtPriceX96."""
    # price = (sqrt / 2^96)^2 * 10^(dec0 - dec1)
    # sqrt = sqrt(price / 10^(dec0 - dec1)) * 2^96
    ratio = price / (10 ** (dec0 - dec1))
    sqrt = math.sqrt(ratio)
    return int(sqrt * (2**96))


class TestMulticallEncodingAndDecoding:
    """测试 Multicall2 calldata 构造与返回数据解码."""

    def test_constants_and_selectors(self) -> None:
        """验证合约地址与核心函数选择器."""
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
        # StateView.getSlot0(bytes32) -> 0xc815641c
        assert "0x" + w3.keccak(text="getSlot0(bytes32)")[:4].hex() == STATE_VIEW_GET_SLOT0_SELECTOR

    def test_encode_pool_slot0_call_v3(self) -> None:
        """测试 V3 池打包为直读 slot0()."""
        pool = _make_v3_pool("ab", label="V3 Pool")
        target, data = encode_pool_slot0_call(pool)

        assert target.lower() == pool.address.lower()
        assert data.hex() == "3850c7bd"

    def test_encode_pool_slot0_call_v4(self) -> None:
        """测试 V4 池打包为 StateView.getSlot0(poolId)."""
        pool = _make_v4_pool("cd", label="V4 Pool")
        target, data = encode_pool_slot0_call(pool)

        assert target.lower() == STATE_VIEW_ADDRESS.lower()
        # 4 字节 selector + 32 字节 poolId
        assert len(data) == 36
        assert data[:4].hex() == "c815641c"
        assert "0x" + data[4:].hex() == pool.address.lower()

    def test_encode_pool_slot0_call_invalid_address(self) -> None:
        """测试非法地址抛出 ValueError."""
        invalid_pool = MagicMock()
        invalid_pool.address = "0x1234"  # 既非 20 字节也非 32 字节
        with pytest.raises(ValueError, match="Unsupported pool address format"):
            encode_pool_slot0_call(invalid_pool)

    def test_encode_and_decode_multicall_roundtrip(self) -> None:
        """测试 tryAggregate 请求打包与响应解析回环."""
        calls = [
            (MULTICALL2_ADDRESS, bytes.fromhex("42cbb15c")),
            ("0x" + "1" * 40, bytes.fromhex("3850c7bd")),
        ]
        calldata = encode_multicall_calls(calls, require_success=False)
        assert calldata.startswith("0xbce38bd7")

        # 校验编码参数可正常被 abi.decode 解出
        decoded_args = abi_decode(["bool", "(address,bytes)[]"], bytes.fromhex(calldata[10:]))
        assert decoded_args[0] is False
        assert len(decoded_args[1]) == 2
        assert decoded_args[1][0][0].lower() == MULTICALL2_ADDRESS.lower()
        assert decoded_args[1][0][1] == bytes.fromhex("42cbb15c")

        # 模拟 Multicall2 响应 ((bool,bytes)[])
        mock_response_tuple = [
            (True, (57400100).to_bytes(32, "big")),
            (True, (123456789).to_bytes(32, "big")),
        ]
        raw_encoded = abi_encode(["(bool,bytes)[]"], [mock_response_tuple])
        decoded_resp = decode_multicall_response("0x" + raw_encoded.hex())

        assert len(decoded_resp) == 2
        assert decoded_resp[0][0] is True
        assert int.from_bytes(decoded_resp[0][1], "big") == 57400100
        assert decoded_resp[1][0] is True
        assert int.from_bytes(decoded_resp[1][1], "big") == 123456789


class TestMulticallBatchExecution:
    """测试 MulticallPoolReader 批量执行逻辑."""

    def test_combined_v3_and_v4_batch(self) -> None:
        """测试包含 V3 和 V4 池的组合批量读取与价格解析."""
        v3_1 = _make_v3_pool("01", label="WETH/USDG 0.01%")
        v3_2 = _make_v3_pool("02", label="PONS/USDG 0.3%")
        v4_1 = _make_v4_pool("03", label="AI/USDG 0.23%")
        v4_2 = _make_v4_pool("04", label="SPY/USDG 0.3%")

        pools: list[AnyPool] = [v3_1, v3_2, v4_1, v4_2]

        expected_block = 57406888
        price_v3_1 = 2500.0
        price_v3_2 = 1.25
        price_v4_1 = 0.50
        price_v4_2 = 550.0

        sqrt_v3_1 = _price_to_sqrt_price_x96(price_v3_1)
        sqrt_v3_2 = _price_to_sqrt_price_x96(price_v3_2)
        sqrt_v4_1 = _price_to_sqrt_price_x96(price_v4_1)
        sqrt_v4_2 = _price_to_sqrt_price_x96(price_v4_2)

        # 构造 Multicall2 响应:
        # call[0]: getBlockNumber()
        # call[1..4]: 各池 slot0 返回数据 (前 32 字节为 sqrtPriceX96)
        mock_items = [
            (True, expected_block.to_bytes(32, "big")),
            (True, sqrt_v3_1.to_bytes(32, "big") + b"\x00" * 192),
            (True, sqrt_v3_2.to_bytes(32, "big") + b"\x00" * 192),
            (True, sqrt_v4_1.to_bytes(32, "big") + b"\x00" * 96),
            (True, sqrt_v4_2.to_bytes(32, "big") + b"\x00" * 96),
        ]
        raw_result_hex = "0x" + abi_encode(["(bool,bytes)[]"], [mock_items]).hex()

        mock_rpc = MagicMock()
        mock_rpc.call.return_value = {"result": raw_result_hex}

        reader = MulticallPoolReader(rpc=mock_rpc)
        quotes = reader.batch_quote_multicall(pools)

        assert len(quotes) == 4
        # 验证单次 RPC 调用
        mock_rpc.call.assert_called_once()
        call_arg = mock_rpc.call.call_args[0]
        assert call_arg[0] == "eth_call"
        assert call_arg[1][0]["to"].lower() == MULTICALL2_ADDRESS.lower()

        # 验证所有报价的区块高度与 call[0] 完全一致 (原子对齐，0 块差)
        for q in quotes:
            assert q.block_number == expected_block

        # 验证价格换算精确度 (误差 < 0.01%)
        assert math.isclose(quotes[0].price, price_v3_1, rel_tol=1e-4)
        assert math.isclose(quotes[1].price, price_v3_2, rel_tol=1e-4)
        assert math.isclose(quotes[2].price, price_v4_1, rel_tol=1e-4)
        assert math.isclose(quotes[3].price, price_v4_2, rel_tol=1e-4)

    def test_partial_revert_graceful_filter(self) -> None:
        """测试个别池 Revert 时 (success=False) 整体不崩盘，仅过滤失败池."""
        p1 = _make_v3_pool("01", label="Pool 1")
        p2_bad = _make_v3_pool("02", label="Broken Pool (Reverted)")
        p3 = _make_v4_pool("03", label="Pool 3")

        pools: list[AnyPool] = [p1, p2_bad, p3]

        expected_block = 57407000
        sqrt_p1 = _price_to_sqrt_price_x96(100.0)
        sqrt_p3 = _price_to_sqrt_price_x96(200.0)

        mock_items = [
            (True, expected_block.to_bytes(32, "big")),
            (True, sqrt_p1.to_bytes(32, "big") + b"\x00" * 32),
            (False, b""),  # 模拟池 2 Revert 失败
            (True, abi_encode(["uint160", "int24", "uint24", "uint24"], [sqrt_p3, 0, 0, 0])),
        ]
        raw_result_hex = "0x" + abi_encode(["(bool,bytes)[]"], [mock_items]).hex()

        mock_rpc = MagicMock()
        mock_rpc.call.return_value = {"result": raw_result_hex}

        reader = MulticallPoolReader(rpc=mock_rpc)
        quotes = reader.batch_quote_multicall(pools)

        # 池 2 失败被过滤，剩余 2 个成功报价
        assert len(quotes) == 2
        labels = [q.pool.label for q in quotes]
        assert labels == ["Pool 1", "Pool 3"]
        assert quotes[0].block_number == expected_block
        assert quotes[1].block_number == expected_block

    def test_atomic_block_number_binding(self) -> None:
        """测试 block_number 从 call[0] 正确原子解析."""
        p1 = _make_v3_pool("01", label="P1")
        p2 = _make_v4_pool("02", label="P2")

        target_block = 98765432
        sqrt_val = _price_to_sqrt_price_x96(1.0)
        mock_items = [
            (True, target_block.to_bytes(32, "big")),
            (True, sqrt_val.to_bytes(32, "big") + b"\x00" * 32),
            (True, abi_encode(["uint160", "int24", "uint24", "uint24"], [sqrt_val, 0, 0, 0])),
        ]
        raw_hex = "0x" + abi_encode(["(bool,bytes)[]"], [mock_items]).hex()

        mock_rpc = MagicMock()
        mock_rpc.call.return_value = {"result": raw_hex}

        reader = MulticallPoolReader(rpc=mock_rpc)
        quotes = reader.batch_quote_multicall([p1, p2])

        assert len(quotes) == 2
        assert quotes[0].block_number == target_block
        assert quotes[1].block_number == target_block

    def test_empty_pool_list(self) -> None:
        """测试空池列表输入返回空列表且不发起网络请求."""
        mock_rpc = MagicMock()
        reader = MulticallPoolReader(rpc=mock_rpc)
        quotes = reader.batch_quote_multicall([])
        assert quotes == []
        mock_rpc.call.assert_not_called()

    def test_invalid_sqrt_price_filtered(self) -> None:
        """测试 sqrtPriceX96 <= 0 的异常返回值被过滤."""
        p = _make_v3_pool("01", label="Zero Price Pool")
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


class TestPoolReaderMulticallFallback:
    """测试 PoolReader.batch_quote 对 Multicall 的集成与优雅降级."""

    def test_pool_reader_prioritizes_multicall(self) -> None:
        """测试 PoolReader.batch_quote 默认优先走 Multicall2 读池."""
        p1 = _make_v3_pool("01", label="Pool A")
        p2 = _make_v3_pool("02", label="Pool B")
        pools: list[AnyPool] = [p1, p2]

        reader = PoolReader.__new__(PoolReader)
        reader.proxy_url = None
        mock_mc = MagicMock()
        q1 = PriceQuote(
            pool=p1,
            base=p1.token0,
            quote=p1.token1,
            price=10.0,
            raw_price_t1_per_t0=10.0,
            block_number=333,
        )
        q2 = PriceQuote(
            pool=p2,
            base=p2.token0,
            quote=p2.token1,
            price=20.0,
            raw_price_t1_per_t0=20.0,
            block_number=333,
        )
        mock_mc.batch_quote_multicall.return_value = [q1, q2]
        reader._multicall_reader = mock_mc
        mock_rpc = MagicMock()
        reader._rpc = mock_rpc

        quotes = reader.batch_quote(pools)
        assert len(quotes) == 2
        mock_mc.batch_quote_multicall.assert_called_once()
        assert quotes[0].block_number == 333

    def test_pool_reader_graceful_fallback_on_multicall_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """测试 Multicall 抛出不可抗力异常时，优雅降级为 ThreadPoolExecutor."""
        p1 = _make_v3_pool("01", label="Pool A")
        p2 = _make_v3_pool("02", label="Pool B")
        pools: list[AnyPool] = [p1, p2]

        reader = PoolReader.__new__(PoolReader)
        reader.proxy_url = None
        mock_mc = MagicMock()
        mock_mc.batch_quote_multicall.side_effect = RuntimeError("Multicall RPC execution reverted")
        reader._multicall_reader = mock_mc

        mock_rpc = MagicMock()
        mock_rpc.block_number.return_value = 55555
        mock_rpc.throttle = 0.0
        reader._rpc = mock_rpc

        # 模拟降级走 reader.quote
        def fallback_quote(pool: AnyPool, block_number: int | None = None) -> PriceQuote:
            p_spec = pool if isinstance(pool, PoolSpec) else pool
            return PriceQuote(
                pool=pool,
                base=p_spec.token0,
                quote=p_spec.token1,
                price=50.0,
                raw_price_t1_per_t0=50.0,
                block_number=block_number or 0,
            )

        monkeypatch.setattr(reader, "quote", fallback_quote)

        quotes = reader.batch_quote(pools)
        assert len(quotes) == 2
        assert all(q.block_number == 55555 for q in quotes)
