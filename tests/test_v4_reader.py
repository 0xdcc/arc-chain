"""Uniswap V4 现货读取器与状态解码单元测试 (无外部网络依赖)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from research.market_data.v4_reader import (
    POOL_MANAGER_ADDRESS,
    POOLS_SLOT,
    V4PoolSpec,
    V4PoolState,
    V4Reader,
    ZeroSlippageError,
    calculate_pool_storage_slot,
    decode_slot0_data,
    decode_swap_log_data,
)

SAMPLE_POOL_ID = "0x4b7c86491df95f366b31217b2950d2c5136a2f19b6879613eac73d0e69092a1a"


class TestV4PoolSpec:
    """V4PoolSpec 地址及构造校验."""

    def test_accepts_valid_bytes32_address(self) -> None:
        """合法 66 字符 bytes32 PoolId 正常构造."""
        spec = V4PoolSpec(
            address=SAMPLE_POOL_ID,
            label="MEME/USDG 0.7%",
            fee_bps=70.0,
        )
        assert spec.address.lower() == SAMPLE_POOL_ID.lower()
        assert spec.fee_bps == 70.0

    def test_rejects_20byte_address(self) -> None:
        """20 字节 EVM 地址必须被拒绝 (V4 专用规格)."""
        with pytest.raises(ValueError, match="32-byte hex"):
            V4PoolSpec(
                address="0x52e65b17fb6e5ba00ed806f37afcd2daa50271ca",
                label="V3 pool",
                fee_bps=1.0,
            )

    def test_rejects_invalid_hex(self) -> None:
        """含非十六进制字符必须被拒绝."""
        bad_id = "0x" + "z" * 64
        with pytest.raises(ValueError, match="Invalid hex"):
            V4PoolSpec(address=bad_id, label="bad", fee_bps=10.0)


class TestStorageSlotCalculation:
    """PoolManager 状态存储槽位计算."""

    def test_calculate_pool_storage_slot(self) -> None:
        """验证 keccak256(poolId || uint256(6)) 槽位计算."""
        slot = calculate_pool_storage_slot(SAMPLE_POOL_ID, slot=POOLS_SLOT)
        assert slot.startswith("0x")
        assert len(slot) == 66
        # 多次计算具有幂等确定性
        assert slot == calculate_pool_storage_slot(SAMPLE_POOL_ID, slot=6)

    def test_rejects_invalid_length_pool_id(self) -> None:
        """长度不合法的 pool_id 抛出 ValueError."""
        with pytest.raises(ValueError, match="32 字节 hex"):
            calculate_pool_storage_slot("0x1234")


class TestDecodeSlot0Data:
    """Slot0 32 字节 packed 字段还原."""

    def test_decode_positive_tick_and_fees(self) -> None:
        """测试正 tick、正 lpFee 的打包数据解码."""
        sqrt_price = 23903843038634318466981
        tick = 12345
        protocol_fee = 4097000
        lp_fee = 7000

        packed = (lp_fee << 208) | (protocol_fee << 184) | (tick << 160) | sqrt_price
        hex_data = hex(packed)[2:].rjust(64, "0")

        state = decode_slot0_data(hex_data)
        assert state.sqrt_price_x96 == sqrt_price
        assert state.tick == tick
        assert state.protocol_fee == protocol_fee
        assert state.lp_fee == lp_fee

    def test_decode_negative_tick_twos_complement(self) -> None:
        """测试负 tick 的 24-bit 补码还原 (如 -300292)."""
        sqrt_price = 23903843038634318466981
        neg_tick = -300292
        tick_twos_complement = (1 << 24) + neg_tick
        protocol_fee = 100
        lp_fee = 3000

        packed = (
            (lp_fee << 208) | (protocol_fee << 184) | (tick_twos_complement << 160) | sqrt_price
        )
        hex_data = hex(packed)[2:].rjust(64, "0")

        state = decode_slot0_data(hex_data)
        assert state.sqrt_price_x96 == sqrt_price
        assert state.tick == neg_tick
        assert state.lp_fee == lp_fee

    def test_decode_empty_raises(self) -> None:
        """空字符串解析抛出异常."""
        with pytest.raises(ValueError):
            decode_slot0_data("")


class TestDecodeSwapLogData:
    """Swap 日志 6 个 32 字节 word 解码."""

    def test_decode_swap_log_data_signed_values(self) -> None:
        """测试 Swap 事件负 amount、负 tick 及流动性解码."""
        amount0 = -4120263227688965298663
        amount1 = 377048535
        sqrt_price = 24062482171339513408331
        liquidity = 15027744620836068939
        tick = -300159
        fee = 7993

        def _word_from_signed(val: int) -> str:
            if val < 0:
                val = (1 << 256) + val
            return hex(val)[2:].rjust(64, "0")

        data_hex = (
            _word_from_signed(amount0)
            + _word_from_signed(amount1)
            + hex(sqrt_price)[2:].rjust(64, "0")
            + hex(liquidity)[2:].rjust(64, "0")
            + _word_from_signed(tick)
            + hex(fee)[2:].rjust(64, "0")
        )

        decoded = decode_swap_log_data(data_hex)
        assert decoded["amount0"] == amount0
        assert decoded["amount1"] == amount1
        assert decoded["sqrt_price_x96"] == sqrt_price
        assert decoded["liquidity"] == liquidity
        assert decoded["tick"] == tick
        assert decoded["fee"] == fee

    def test_decode_short_data_raises(self) -> None:
        """不足 6 个 word 抛出异常."""
        with pytest.raises(ValueError, match="不足 6 个 word"):
            decode_swap_log_data("0x1234")


class TestV4PriceCalculation:
    """sqrtPriceX96 到代币单价换算."""

    def test_price_with_decimal_difference(self) -> None:
        """MEME(18) / USDG(6) 精度差 12 位场景换算."""
        state = V4PoolState(
            sqrt_price_x96=23903843038634318466981,
            tick=-300292,
            protocol_fee=0,
            lp_fee=7000,
        )
        price = state.sqrt_price_x96_to_price(dec0=18, dec1=6)
        assert 0.08 < price < 0.10

    def test_zero_sqrt_price_returns_zero(self) -> None:
        """异常零价格返回 0.0."""
        state = V4PoolState(sqrt_price_x96=0, tick=0, protocol_fee=0, lp_fee=0)
        assert state.sqrt_price_x96_to_price() == 0.0

    def test_v4_price_calculation_cross_validation_with_quoter_v2(self) -> None:
        """用真实链上 WETH/USDG V4 大池数值计算价格，并与 Uniswap V3 QuoterV2 实际报价交叉验证.

        真实链上数值:
        - V4 WETH/USDG 0.01% 池 (0x24107d152f14a76d292123265ae3f3c71f863fc2f4ef7ba49d64e78d28ea379e)
          sqrtPriceX96 = 3945274390255500519263000
          dec0 = 18 (WETH), dec1 = 6 (USDG)
        - 算出的现货价格 ~ 2479.6778 USDG / WETH
        - Uniswap V3 QuoterV2 (0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7)
          对 0.01 WETH 的链上实际产出: 24.735634 USDG (折合单价 2473.5634 USDG / WETH)
        断言: 两者差异必须严格小于 1.0%.
        """
        sqrt_price_x96 = 3945274390255500519263000
        state = V4PoolState(
            sqrt_price_x96=sqrt_price_x96,
            tick=-198162,
            protocol_fee=102425,
            lp_fee=100,
        )

        v4_calculated_price = state.sqrt_price_x96_to_price(dec0=18, dec1=6)
        quoter_v2_price = 2473.5634  # 0.01 WETH -> 24.735634 USDG

        diff_pct = abs(v4_calculated_price - quoter_v2_price) / quoter_v2_price * 100.0
        assert diff_pct < 1.0, (
            f"V4 price {v4_calculated_price} vs QuoterV2 {quoter_v2_price} diff={diff_pct:.4f}% >= 1.0%"
        )

    def test_v4_price_opposite_token_order_takes_reciprocal(self) -> None:
        """当 token0/token1 顺序与 base/quote 不一致时，断言价格正确取倒数 (1/price)."""
        weth = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
        usdg = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
        sqrt_price_x96 = 3945274390255500519263000
        state = V4PoolState(
            sqrt_price_x96=sqrt_price_x96,
            tick=-198162,
            protocol_fee=0,
            lp_fee=100,
        )

        # 正向: base=WETH, quote=USDG (与 token0/token1 顺序一致)
        price_fwd = state.sqrt_price_x96_to_price(
            dec0=18,
            dec1=6,
            base_token=weth,
            quote_token=usdg,
            token0=weth,
            token1=usdg,
        )
        assert 2400.0 < price_fwd < 2600.0

        # 反向: base=USDG, quote=WETH (与 token0/token1 顺序相反，正确取倒数)
        price_rev = state.sqrt_price_x96_to_price(
            dec0=18,
            dec1=6,
            base_token=usdg,
            quote_token=weth,
            token0=weth,
            token1=usdg,
        )
        assert price_rev == pytest.approx(1.0 / price_fwd)
        assert 0.0003 < price_rev < 0.0005

        # 通过 V4Reader.quote 校验反向取倒数
        mock_rpc = MagicMock()
        packed = (100 << 208) | ((-198162 & 0xFFFFFF) << 160) | sqrt_price_x96
        mock_rpc.call.return_value = {"result": "0x" + hex(packed)[2:].rjust(64, "0")}
        reader = V4Reader(rpc=mock_rpc)

        spec = V4PoolSpec(
            address=SAMPLE_POOL_ID,
            label="WETH / USDG 0.01%",
            fee_bps=1.0,
            token0=weth,
            token1=usdg,
            dec0=18,
            dec1=6,
        )

        # 默认规范归一化报价 (base=weth, quote=usdg 因为 weth < usdg)
        quote_default = reader.quote(spec)
        assert quote_default.price == pytest.approx(price_fwd)

        # 显式指定相反顺序 (base=usdg, quote=weth)
        quote_reversed = reader.quote(spec, base=usdg, quote=weth)
        assert quote_reversed.price == pytest.approx(1.0 / price_fwd)

        # 逆序 PoolSpec (token0=usdg, token1=weth 即 token0 > token1)
        spec_inv = V4PoolSpec(
            address=SAMPLE_POOL_ID,
            label="USDG / WETH 0.01%",
            fee_bps=1.0,
            token0=usdg,
            token1=weth,
            dec0=6,
            dec1=18,
        )
        # 逆序构造时，规范归一化 base 仍然为 weth (< usdg)，price 仍为 ~2479，绝不出现 10^24 级别错误
        quote_inv = reader.quote(spec_inv)
        assert quote_inv.base == weth.lower()
        assert quote_inv.quote == usdg.lower()
        assert quote_inv.price == pytest.approx(price_fwd)


class TestV4ReaderFlow:
    """V4Reader 各探查链路与 Quote 组装测试."""

    def test_quote_assembly(self) -> None:
        """验证 quote 方法成功返回标准 PriceQuote."""
        mock_rpc = MagicMock()
        # 模拟 extsload 返回有效 slot0 hex
        sqrt_price = 3950519125368201718652123  # WETH/USDG ~2486
        packed = (100 << 208) | ((-198135 & 0xFFFFFF) << 160) | sqrt_price
        mock_rpc.call.return_value = {"result": "0x" + hex(packed)[2:].rjust(64, "0")}

        reader = V4Reader(rpc=mock_rpc, pool_manager=POOL_MANAGER_ADDRESS)
        spec = V4PoolSpec(
            address=SAMPLE_POOL_ID,
            label="WETH/USDG 0.01%",
            fee_bps=1.0,
            token0="0x0bd7d308f8e1639fab988df18a8011f41eacad73",  # WETH (18)
            token1="0x5fc5360d0400a0fd4f2af552add042d716f1d168",  # USDG (6)
            dec0=18,
            dec1=6,
        )

        quote = reader.quote(spec)
        assert quote.pool == spec
        assert quote.base == spec.token0.lower()
        assert quote.quote == spec.token1.lower()
        assert quote.price > 2000.0

    def test_extsload_fallback_to_storage_at(self) -> None:
        """extsload 失败时自动降级到 eth_getStorageAt."""
        mock_rpc = MagicMock()
        sqrt_price = 23903843038634318466981
        packed_hex = "0x" + hex(sqrt_price)[2:].rjust(64, "0")

        def side_effect(method: str, params: list):
            if method == "eth_call":
                return {"result": "0x"}  # 模拟 revert/空
            if method == "eth_getStorageAt":
                return {"result": packed_hex}
            return {"result": "0x"}

        mock_rpc.call.side_effect = side_effect
        reader = V4Reader(rpc=mock_rpc)
        state = reader.read_pool_state(SAMPLE_POOL_ID)
        assert state.sqrt_price_x96 == sqrt_price

    def test_zero_sqrt_price_raises_guard(self) -> None:
        """非法零价格触发 ZeroSlippageError 保护."""
        mock_rpc = MagicMock()
        mock_rpc.call.return_value = {"result": "0x" + "0" * 64}
        reader = V4Reader(rpc=mock_rpc)
        spec = V4PoolSpec(address=SAMPLE_POOL_ID, label="bad", fee_bps=1.0)
        with pytest.raises(ZeroSlippageError):
            reader.quote(spec)
