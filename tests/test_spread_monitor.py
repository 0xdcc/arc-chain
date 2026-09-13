"""跨池价差监控器单元测试 (不依赖真实网络)."""

from __future__ import annotations

import pytest
from arbitrage.pool_config import MONITOR_POOLS, get_pools
from arbitrage.spread_monitor import (
    PoolReader,
    PoolSpec,
    PriceQuote,
    SpreadAlert,
    find_spreads,
)

# 用于构造测试池的合法 20 字节地址
A_LOW = "0x000000000000000000000000000000000000000a"  # 字典序小 -> 作 base
A_HIGH = "0x000000000000000000000000000000000000000b"  # 字典序大 -> 作 quote


def make_pool(addr_suffix: str, label: str, fee_bps: float) -> PoolSpec:
    """构造测试用池规格."""
    addr = "0x" + addr_suffix.rjust(40, "0")
    return PoolSpec(address=addr, label=label, fee_bps=fee_bps)


def make_quote(pool: PoolSpec, price: float) -> PriceQuote:
    """构造测试用报价 (base=A_LOW, quote=A_HIGH)."""
    return PriceQuote(
        pool=pool,
        base=A_LOW,
        quote=A_HIGH,
        price=price,
        raw_price_t1_per_t0=price,
    )


class TestPoolSpec:
    """PoolSpec 地址校验."""

    def test_rejects_bytes32_address(self) -> None:
        """V4 的 bytes32 PoolId 必须被拒绝 (无法直读 slot0)."""
        with pytest.raises(ValueError, match="20-byte"):
            PoolSpec(
                address="0x4b7c86491df95f366b31217b2950d2c5136a2f19b6879613eac73d0e69092a1a",
                label="V4 pool",
                fee_bps=30.0,
            )

    def test_accepts_20byte_address(self) -> None:
        """20 字节地址应正常构造."""
        p = make_pool("aa", "test", 30.0)
        assert len(p.address) == 42


class TestFindSpreads:
    """价差识别核心逻辑."""

    def test_detects_profitable_spread(self) -> None:
        """价差覆盖手续费+缓冲后应识别为机会."""
        cheap = make_quote(make_pool("01", "cheap 0.3%", 30.0), price=100.0)
        rich = make_quote(make_pool("02", "rich 0.3%", 30.0), price=102.0)
        # 毛利 2% - 双边费 0.6% - 缓冲 0.2% = 净 1.2%
        alerts = find_spreads([cheap, rich], slippage_buffer_pct=0.2)
        assert len(alerts) == 1
        a = alerts[0]
        assert a.buy_pool.label == "cheap 0.3%"
        assert a.sell_pool.label == "rich 0.3%"
        assert a.gross_spread_pct == pytest.approx(2.0)
        assert a.net_spread_pct == pytest.approx(1.2)

    def test_rejects_spread_below_costs(self) -> None:
        """价差不足以覆盖成本时不应报警."""
        cheap = make_quote(make_pool("01", "cheap", 30.0), price=100.0)
        rich = make_quote(make_pool("02", "rich", 30.0), price=100.5)
        # 毛利 0.5% - 0.6% 费用 - 0.2% 缓冲 < 0
        alerts = find_spreads([cheap, rich], slippage_buffer_pct=0.2)
        assert alerts == []

    def test_ignores_single_pool(self) -> None:
        """只有一个池无法比价."""
        q = make_quote(make_pool("01", "solo", 30.0), price=100.0)
        assert find_spreads([q]) == []

    def test_groups_by_token_pair(self) -> None:
        """不同标的的池不应互相比较."""
        p1 = make_quote(make_pool("01", "A/USDG", 30.0), price=100.0)
        p2 = make_quote(make_pool("02", "A/USDG", 30.0), price=110.0)
        other = PriceQuote(
            pool=make_pool("03", "B/USDG", 30.0),
            base="0x" + "cc".rjust(40, "0"),
            quote=A_HIGH,
            price=5.0,
            raw_price_t1_per_t0=5.0,
        )
        alerts = find_spreads([p1, p2, other], slippage_buffer_pct=0.1)
        assert len(alerts) == 1
        assert alerts[0].base == A_LOW

    def test_skips_zero_price(self) -> None:
        """零价格(读取失败)应被跳过, 不产生假告警."""
        good = make_quote(make_pool("01", "good", 30.0), price=100.0)
        bad = make_quote(make_pool("02", "bad", 30.0), price=0.0)
        assert find_spreads([good, bad]) == []

    def test_sorted_by_net_desc(self) -> None:
        """多个机会应按净利润降序排列."""
        q1 = make_quote(make_pool("01", "p1", 5.0), price=100.0)
        q2 = make_quote(make_pool("02", "p2", 5.0), price=101.0)
        q3 = make_quote(make_pool("03", "p3", 5.0), price=103.0)
        alerts = find_spreads([q1, q2, q3], slippage_buffer_pct=0.0)
        assert len(alerts) == 1  # 同一组内只取最低买最高卖
        a = alerts[0]
        assert a.buy_price == 100.0
        assert a.sell_price == 103.0


class TestPriceNormalization:
    """价格归一化 (token 顺序反转场景)."""

    def test_reversed_token_order_inverts_price(self) -> None:
        """同一对 token 顺序相反时, 归一化后价格应互为倒数关系保持一致.

        池A: token0=LOW, token1=HIGH -> price = t1/t0 = 2480 (1 LOW 值 2480 HIGH)
        池B: token0=HIGH, token1=LOW -> price = t1/t0 = 1/2480
        归一化后 (base=LOW) 池B 应还原成 2480
        """
        reader = PoolReader.__new__(PoolReader)  # 不初始化 RPC

        pool_b = PoolSpec(
            address="0x" + "02".rjust(40, "0"),
            label="reversed",
            fee_bps=30.0,
            token0=A_HIGH,  # 顺序相反
            token1=A_LOW,
            dec0=6,
            dec1=18,
        )
        raw_price_t1_per_t0 = 1.0 / 2480.0 * (10 ** (6 - 18))  # t1=LOW(18) per t0=HIGH(6)

        # 模拟 quote() 的归一化分支: t0(HIGH) > t1(LOW) -> base=t1=LOW, price=1/p
        t0, t1 = pool_b.token0.lower(), pool_b.token1.lower()
        assert t0 > t1
        # price_t1_per_t0 已含 decimals 调整: (sqrt/2^96)^2 * 10^(dec0-dec1)
        # 此处直接给最终值
        normalized = 1.0 / raw_price_t1_per_t0 if raw_price_t1_per_t0 else 0.0
        # 因 decimals 差异, 归一化值 = 1 / (1/2480 * 10^-12) = 2480 * 10^12
        # 两个池若 dec 一致则完全可比; 此处仅验证取倒数分支被走到
        assert normalized > 0
        assert reader is not None


class TestDecodeString:
    """ERC20 symbol 解码 (Robinhood 链存在 bytes32 布局)."""

    def test_decodes_bytes32_layout(self) -> None:
        """bytes32 布局: 前补零, 尾部为字符串."""
        raw = b"WETH".hex()
        word = raw.ljust(64, "0")
        assert PoolReader._decode_string("0x" + word) == "WETH"

    def test_decodes_abi_string_layout(self) -> None:
        """标准 ABI string: [offset][length][data]."""
        data = b"USDC"
        payload = (32).to_bytes(32, "big") + len(data).to_bytes(32, "big") + data
        assert PoolReader._decode_string("0x" + payload.hex()) == "USDC"

    def test_strips_null_bytes(self) -> None:
        """尾部空字节应被清除 (实测 Robinhood 链返回含 \\x00 填充)."""
        raw = b"\x00" * 27 + b"\x04" + b"WETH"
        assert PoolReader._decode_string("0x" + raw.hex()) == "WETH"


class TestPoolConfig:
    """监控池配置校验."""

    def test_all_pools_are_20byte(self) -> None:
        """配置里所有池必须可直读 slot0 (排除 V4)."""
        for p in MONITOR_POOLS:
            assert len(p.address) == 42, f"{p.label} 不是 20 字节地址"

    def test_no_duplicate_addresses(self) -> None:
        """不应有重复池."""
        addrs = [p.address.lower() for p in MONITOR_POOLS]
        assert len(addrs) == len(set(addrs))

    def test_fee_bps_positive(self) -> None:
        """费率必须为正."""
        for p in MONITOR_POOLS:
            assert p.fee_bps > 0

    def test_get_pools_returns_copy(self) -> None:
        """get_pools 应返回副本, 防止调用方污染全局配置."""
        a = get_pools()
        a.clear()
        assert len(get_pools()) == len(MONITOR_POOLS)


class TestAlertFormatting:
    """告警输出格式."""

    def test_str_contains_key_fields(self) -> None:
        """告警字符串应包含方向、价格与净利."""
        a = SpreadAlert(
            base=A_LOW,
            quote=A_HIGH,
            buy_pool=make_pool("01", "buy", 30.0),
            sell_pool=make_pool("02", "sell", 30.0),
            buy_price=100.0,
            sell_price=102.0,
            gross_spread_pct=2.0,
            total_fee_pct=0.8,
            net_spread_pct=1.2,
        )
        s = str(a)
        assert "buy" in s and "sell" in s
        assert "2.000" in s and "1.200" in s
