"""Uniswap V3 / V4 链上直接费率读取与真实契约强校验独立单元测试.

对照原 tests/test_pool_fee_verification.py 直接 fee 测试与上游 v2 arbitrage/pool_scanner.py:
1. 完整保留原 TestDirectFeeReaders 直接断言:
   - test_read_v3_pool_fee_success: 16 进制 32 字节返回值 (0x64 = 100 ppm -> 1.0 bps)
   - test_read_v3_pool_fee_raises_on_revert: RPC 失败时抛出 RuntimeError
   - test_read_v3_pool_fee_accepts_numeric_zero: 完整 ABI 零值 (0 ppm -> 0.0 bps) 合法返回
2. 真实契约与协议规范强校验:
   - V3 uint24 ppm -> bps (100 ppm -> 1.0 bps, 3000 ppm -> 30.0 bps, 10000 ppm -> 100.0 bps)
   - V4 StateView.getSlot0 128 字节 ABI 完整解析 (uint160, int24, uint24, uint24)
   - 异常、截断 (< 128 字节)、空数据 ("0x") 严禁使用默认值兜底 (fail-closed)
   - 动态费 (DYNAMIC_FEE_FLAG 0x800000) 严禁默认合法, 必须抛出 ValueError
   - 未验证 DEX 分支 (如 ramses-v3) 严格抛出 ValueError
   - 注入只读 handler, 严禁隐式构造 HTTP 连接 (rpc=None 抛出 ValueError)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from eth_abi.abi import encode as abi_encode

from research.market_data.fee_verification import (
    DYNAMIC_FEE_FLAG,
    STATE_VIEW_ADDRESS,
    STATE_VIEW_GET_SLOT0_SELECTOR,
    V3_FEE_SELECTOR,
    decode_stateview_slot0_fee,
    decode_v3_fee_data,
    read_v3_pool_fee,
    read_v4_pool_fee,
)


def _make_mock_rpc(
    v3_fee_return: str | int | None = None, raise_exc: Exception | None = None
) -> MagicMock:
    """构造用于模拟 JSON-RPC eth_call 的 Mock 客户端 (保留原测试辅助契约)."""
    mock_rpc = MagicMock()
    if raise_exc is not None:
        mock_rpc.call.side_effect = raise_exc
    elif v3_fee_return is not None:
        if isinstance(v3_fee_return, int):
            hex_val = "0x" + hex(v3_fee_return)[2:].zfill(64)
            mock_rpc.call.return_value = {"result": hex_val}
        else:
            mock_rpc.call.return_value = {"result": v3_fee_return}
    else:
        mock_rpc.call.return_value = {"result": "0x"}
    return mock_rpc


class TestDirectFeeReadersOriginalContract:
    """底层 read_v3_pool_fee 与 read_v4_pool_fee 原测试契约 100% 保留."""

    def test_read_v3_pool_fee_success(self) -> None:
        """read_v3_pool_fee 成功解析 16 进制 32 字节返回值 (原断言保留)."""
        mock_rpc = MagicMock()
        mock_rpc.call.return_value = {
            "result": "0x0000000000000000000000000000000000000000000000000000000000000064"
        }
        fee = read_v3_pool_fee("0x1111111111111111111111111111111111111111", rpc=mock_rpc)
        assert fee == pytest.approx(1.0)

    def test_read_v3_pool_fee_raises_on_revert(self) -> None:
        """read_v3_pool_fee 在 RPC 失败时抛出 RuntimeError (原断言保留)."""
        mock_rpc = MagicMock()
        mock_rpc.call.side_effect = RuntimeError("RPC call failed: execution reverted")
        with pytest.raises(RuntimeError):
            read_v3_pool_fee("0x1111111111111111111111111111111111111111", rpc=mock_rpc)

    def test_read_v3_pool_fee_accepts_numeric_zero(self) -> None:
        """Complete ABI zero differs from empty RPC data; no live zero-tier claim (原断言保留)."""
        mock_rpc = _make_mock_rpc(v3_fee_return=0)
        fee = read_v3_pool_fee("0x" + "1" * 40, rpc=mock_rpc)
        assert fee == 0.0


class TestV3FeeProtocolVerification:
    """Uniswap V3 真实协议选择器、单位转换与防护边界."""

    def test_v3_fee_selector_contract(self) -> None:
        """fee() 选择器必须为 0xddca3f43."""
        assert V3_FEE_SELECTOR == "0xddca3f43"

    def test_v3_fee_ppm_to_bps_conversion(self) -> None:
        """uint24 ppm 正确转换为 bps: 500 ppm -> 5.0 bps, 3000 ppm -> 30.0 bps, 10000 ppm -> 100.0 bps."""
        assert decode_v3_fee_data(500) == pytest.approx(5.0)
        assert decode_v3_fee_data(3000) == pytest.approx(30.0)
        assert decode_v3_fee_data(10000) == pytest.approx(100.0)

    def test_v3_fee_empty_result_raises_fail_closed(self) -> None:
        """fee() 返回 0x 或空串时 fail-closed 抛出 RuntimeError, 杜绝 30.0 兜底."""
        mock_rpc = MagicMock()
        mock_rpc.call.return_value = {"result": "0x"}
        with pytest.raises(RuntimeError, match="empty eth_call result"):
            read_v3_pool_fee("0x" + "1" * 40, rpc=mock_rpc)

    def test_v3_fee_out_of_range_raises(self) -> None:
        """uint24 超出 1,000,000 ppm (100%) 或负数时抛出 ValueError."""
        with pytest.raises(ValueError, match="Invalid fee value"):
            decode_v3_fee_data(1_000_001)

    def test_v3_fee_unverified_dex_raises(self) -> None:
        """未验证的分支 DEX (如 ramses-v3) 严格抛出 ValueError, 杜绝继承主线单位."""
        mock_rpc = MagicMock()
        with pytest.raises(ValueError, match="adapter unverified"):
            read_v3_pool_fee("0x" + "1" * 40, rpc=mock_rpc, dex="ramses-v3")
        mock_rpc.call.assert_not_called()

    def test_v3_fee_invalid_address_raises(self) -> None:
        """非法池地址格式严格抛出 ValueError."""
        mock_rpc = MagicMock()
        with pytest.raises(ValueError, match="20-byte hex"):
            read_v3_pool_fee("0xinvalid", rpc=mock_rpc)

    def test_v3_fee_no_http_construction(self) -> None:
        """未注入 rpc 时严禁构造 HTTP 客户端, 必须立即抛出 ValueError."""
        with pytest.raises(ValueError, match="ReadOnlyRpcTransport must be explicitly injected"):
            read_v3_pool_fee("0x" + "1" * 40, rpc=None)


class TestV4StateViewFeeVerification:
    """Uniswap V4 StateView.getSlot0 真实 128 字节解析与防护门禁."""

    def test_state_view_constants(self) -> None:
        """StateView 地址与 getSlot0(bytes32) 选择器常量校验."""
        assert STATE_VIEW_ADDRESS == "0xf3334192d15450cdd385c8b70e03f9a6bd9e673b"
        assert STATE_VIEW_GET_SLOT0_SELECTOR == "0xc815641c"
        assert DYNAMIC_FEE_FLAG == 0x800000

    def test_read_v4_pool_fee_success(self) -> None:
        """StateView 返回 128 字节 (sqrtPriceX96, tick, protocolFee, lpFee) 解析出 70.0 bps."""
        mock_rpc = MagicMock()
        # lpFee 设为 7000 (70.0 bps = 0.70%)
        encoded = abi_encode(["uint160", "int24", "uint24", "uint24"], [2**96, 0, 0, 7000])
        assert len(encoded) == 128
        mock_rpc.call.return_value = {"result": "0x" + encoded.hex()}

        v4_pool_id = "0x" + "aa" * 32
        fee = read_v4_pool_fee(v4_pool_id, rpc=mock_rpc)
        assert fee == pytest.approx(70.0)

        # 校验 RPC 调用契约
        mock_rpc.call.assert_called_once()
        args = mock_rpc.call.call_args[0]
        assert args[0] == "eth_call"
        assert args[1][0]["to"].lower() == STATE_VIEW_ADDRESS.lower()
        assert args[1][0]["data"] == "0xc815641c" + "aa" * 32

    def test_read_v4_pool_fee_standard_30bps(self) -> None:
        """StateView lpFee=3000 (0.30%) 解析出 30.0 bps."""
        encoded = abi_encode(["uint160", "int24", "uint24", "uint24"], [2**96, -100, 10, 3000])
        fee = decode_stateview_slot0_fee(encoded)
        assert fee == pytest.approx(30.0)

    def test_read_v4_pool_fee_zero_fee(self) -> None:
        """StateView lpFee=0 解析出 0.0 bps."""
        encoded = abi_encode(["uint160", "int24", "uint24", "uint24"], [2**96, 0, 0, 0])
        fee = decode_stateview_slot0_fee(encoded)
        assert fee == 0.0

    def test_read_v4_pool_fee_revert_raises_fail_closed(self) -> None:
        """StateView 调用 Revert 时抛出 RuntimeError, 绝不兜底 30.0."""
        mock_rpc = MagicMock()
        mock_rpc.call.side_effect = RuntimeError("StateView call reverted: 0x")
        v4_pool_id = "0x" + "bb" * 32
        with pytest.raises(RuntimeError, match="StateView call reverted"):
            read_v4_pool_fee(v4_pool_id, rpc=mock_rpc)

    def test_read_v4_pool_fee_empty_raises(self) -> None:
        """StateView 返回 0x 时抛出 RuntimeError."""
        mock_rpc = MagicMock()
        mock_rpc.call.return_value = {"result": "0x"}
        v4_pool_id = "0x" + "bb" * 32
        with pytest.raises(RuntimeError, match="empty eth_call result"):
            read_v4_pool_fee(v4_pool_id, rpc=mock_rpc)

    def test_read_v4_pool_fee_truncated_length_raises(self) -> None:
        """StateView 返回非 128 字节截断数据时抛出 ValueError."""
        # 截断为 64 字节
        short_bytes = b"\x00" * 64
        with pytest.raises(ValueError, match="must be exactly 128 bytes"):
            decode_stateview_slot0_fee(short_bytes)

    def test_read_v4_pool_fee_dynamic_fee_flag_raises(self) -> None:
        """Uniswap V4 动态费率池 (Bit 23 0x800000 置位) 严禁默认合法, 必须抛出 ValueError."""
        # 设置动态费标志位
        dynamic_lp_fee = DYNAMIC_FEE_FLAG | 3000
        encoded = abi_encode(
            ["uint160", "int24", "uint24", "uint24"],
            [2**96, 0, 0, dynamic_lp_fee],
        )
        with pytest.raises(ValueError, match="Dynamic fee pool"):
            decode_stateview_slot0_fee(encoded)

    def test_read_v4_pool_fee_invalid_pool_id_raises(self) -> None:
        """非 32 字节 poolId (例如 20 字节 EVM 地址) 抛出 ValueError."""
        mock_rpc = MagicMock()
        with pytest.raises(ValueError, match="32-byte hex"):
            read_v4_pool_fee("0x1111111111111111111111111111111111111111", rpc=mock_rpc)

    def test_read_v4_pool_fee_no_http_construction(self) -> None:
        """未注入 rpc 时严禁隐式构造 HTTP 连接, 抛出 ValueError."""
        with pytest.raises(ValueError, match="ReadOnlyRpcTransport must be explicitly injected"):
            read_v4_pool_fee("0x" + "aa" * 32, rpc=None)


class TestInjectedRpcHandlerFlexibility:
    """测试注入只读 RPC 处理器对不同调用风格的兼容性 (函数或类)."""

    def test_callable_handler_injection(self) -> None:
        """支持无类封装的直接 callable handler (method, params)."""
        encoded = abi_encode(["uint160", "int24", "uint24", "uint24"], [2**96, 0, 0, 500])

        def simple_handler(method: str, params: list[Any]) -> dict[str, str]:
            assert method == "eth_call"
            return {"result": "0x" + encoded.hex()}

        fee = read_v4_pool_fee("0x" + "cc" * 32, rpc=simple_handler)
        assert fee == pytest.approx(5.0)
