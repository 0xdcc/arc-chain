"""三跳三角套利闭环检测引擎单元测试 (无网络依赖)."""

from __future__ import annotations

import pytest
from arbitrage.spread_monitor import PoolSpec, PriceQuote
from arbitrage.triangular import (
    DirectedEdge,
    SwapLeg,
    TokenGraph,
    TriangularArbAlert,
    bellman_ford_arbitrage,
    calculate_triangular_path,
    find_triangular_opportunities,
)
from arbitrage.v4_reader import V4PoolSpec


def make_v3_pool(suffix: str, label: str, fee_bps: float) -> PoolSpec:
    """构造合法 20 字节 V3 池规格."""
    addr = "0x" + suffix.rjust(40, "0")
    return PoolSpec(address=addr, label=label, fee_bps=fee_bps)


def make_v4_pool(suffix: str, label: str, fee_bps: float) -> V4PoolSpec:
    """构造合法 32 字节 V4 池规格."""
    addr = "0x" + suffix.rjust(64, "0")
    return V4PoolSpec(address=addr, label=label, fee_bps=fee_bps)


def make_quote(
    pool: PoolSpec | V4PoolSpec,
    base: str,
    quote: str,
    price: float,
    b_sym: str = "",
    q_sym: str = "",
) -> PriceQuote:
    """构造测试报价."""
    return PriceQuote(
        pool=pool,
        base=base,
        quote=quote,
        price=price,
        raw_price_t1_per_t0=price,
        base_symbol=b_sym or base,
        quote_symbol=q_sym or quote,
    )


class TestTriangularMultiplierFormula:
    """规格书公式乘数计算校验: R = (1 - f1) * (1 - f2) * (1 - f3) * P1 * P2 * P3."""

    def test_exact_formula_calculation(self) -> None:
        """验证三跳乘数计算与预期数学公式完全一致."""
        p1 = make_v3_pool("01", "USDG/WETH 0.01%", 1.0)
        p2 = make_v3_pool("02", "PONS/WETH 0.3%", 30.0)
        p3 = make_v3_pool("03", "PONS/USDG 0.3%", 30.0)

        e1 = DirectedEdge(
            from_token="USDG",
            to_token="WETH",
            pool=p1,
            rate=0.000403,
            fee_bps=1.0,
            effective_rate=0.000403 * (1 - 0.0001),
            weight=0.0,
            from_symbol="USDG",
            to_symbol="WETH",
        )
        e2 = DirectedEdge(
            from_token="WETH",
            to_token="PONS",
            pool=p2,
            rate=2800.0,
            fee_bps=30.0,
            effective_rate=2800.0 * (1 - 0.003),
            weight=0.0,
            from_symbol="WETH",
            to_symbol="PONS",
        )
        e3 = DirectedEdge(
            from_token="PONS",
            to_token="USDG",
            pool=p3,
            rate=0.95,
            fee_bps=30.0,
            effective_rate=0.95 * (1 - 0.003),
            weight=0.0,
            from_symbol="PONS",
            to_symbol="USDG",
        )

        alert = calculate_triangular_path(e1, e2, e3, slippage_buffer_pct=0.6)
        assert alert is not None

        expected_gross = 0.000403 * 2800.0 * 0.95
        expected_fee_mult = (1.0 - 0.0001) * (1.0 - 0.003) * (1.0 - 0.003)
        expected_r = expected_gross * expected_fee_mult

        assert alert.gross_multiplier == pytest.approx(expected_gross)
        assert alert.fee_multiplier == pytest.approx(expected_fee_mult)
        assert alert.expected_multiplier == pytest.approx(expected_r)
        assert alert.gross_profit_pct == pytest.approx((expected_gross - 1.0) * 100.0)
        assert alert.net_profit_pct == pytest.approx((expected_r - 1.0) * 100.0 - 0.6)


class TestFeeDeductionAndRejection:
    """手续费挤压与亏损拦截测试."""

    def test_rejects_triangle_eaten_by_fees(self) -> None:
        """毛价差存在 (+0.3%)，但双边/三边手续费合计 0.61% 导致亏损，不得报警."""
        p1 = make_v3_pool("01", "USDG/WETH 0.01%", 1.0)
        p2 = make_v3_pool("02", "PONS/WETH 0.3%", 30.0)
        p3 = make_v3_pool("03", "PONS/USDG 0.3%", 30.0)

        # 毛利: (1/2500) * 2500 * 1.003 = 1.003 (+0.3%)
        # 扣手续费乘数 ~ 0.9939 -> R ~ 0.9969 < 1.0
        q1 = make_quote(p1, "USDG", "WETH", 1.0 / 2500.0)
        q2 = make_quote(p2, "WETH", "PONS", 2500.0)
        q3 = make_quote(p3, "PONS", "USDG", 1.003)

        alerts = find_triangular_opportunities([q1, q2, q3], slippage_buffer_pct=0.6)
        assert alerts == []


class TestSlippageBufferInterception:
    """滑点缓冲拦截测试."""

    def test_rejects_profit_smaller_than_slippage_buffer(self) -> None:
        """预期乘数 R=1.003 (+0.3% 净手续费后利润)，但低于 0.6% 滑点缓冲时被拦截."""
        p1 = make_v3_pool("01", "USDG/WETH 0.01%", 1.0)
        p2 = make_v3_pool("02", "PONS/WETH 0.01%", 1.0)
        p3 = make_v3_pool("03", "PONS/USDG 0.01%", 1.0)

        # 总费率 0.03% (3 bps)
        # 毛利 +0.33%, 扣费后 R ~ 1.003 (+0.3%), 但滑点要求 0.6% -> net < 0
        q1 = make_quote(p1, "USDG", "WETH", 1.0 / 2500.0)
        q2 = make_quote(p2, "WETH", "PONS", 2500.0)
        q3 = make_quote(p3, "PONS", "USDG", 1.0033)

        alerts = find_triangular_opportunities(
            [q1, q2, q3],
            slippage_buffer_pct=0.6,
            min_profit_pct=0.0,
        )
        assert alerts == []


class TestProfitableTriangleDetection:
    """高额净利场景识别与告警组装."""

    def test_detects_profitable_triangle_with_v4_mix(self) -> None:
        """跨 V3 与 V4 池成功识别并组装套利机会."""
        p1 = make_v3_pool("01", "USDG/WETH 0.01%", 1.0)
        p2 = make_v4_pool("02", "PONS/WETH 0.3% (v4)", 30.0)
        p3 = make_v4_pool("03", "PONS/USDG 0.3% (v4)", 30.0)

        # 构造丰厚套利价差 (毛利 ~7%)
        q1 = make_quote(p1, "USDG", "WETH", 0.000403)
        q2 = make_quote(p2, "WETH", "PONS", 2800.0)
        q3 = make_quote(p3, "PONS", "USDG", 0.95)

        alerts = find_triangular_opportunities([q1, q2, q3], slippage_buffer_pct=0.6)
        assert len(alerts) >= 1
        a = alerts[0]
        assert a.expected_multiplier > 1.05
        assert a.net_profit_pct > 5.0
        assert len(a.legs) == 3
        # 验证包含 V4 池
        assert any(isinstance(leg.pool, V4PoolSpec) for leg in a.legs)

        # 验证字符串可读性
        s = str(a)
        assert "三角套利" in s
        assert "Leg 1" in s and "Leg 2" in s and "Leg 3" in s
        assert "USDG" in s and "WETH" in s and "PONS" in s


class TestBellmanFordArbitrage:
    """Bellman-Ford 负权环算法测试."""

    def test_bellman_ford_detects_negative_cycle(self) -> None:
        """验证基于 -ln 权重的 Bellman-Ford 正确找到套利环路."""
        p1 = make_v3_pool("01", "USDG/WETH 0.01%", 1.0)
        p2 = make_v3_pool("02", "PONS/WETH 0.3%", 30.0)
        p3 = make_v3_pool("03", "PONS/USDG 0.3%", 30.0)

        q1 = make_quote(p1, "USDG", "WETH", 0.000403)
        q2 = make_quote(p2, "WETH", "PONS", 2800.0)
        q3 = make_quote(p3, "PONS", "USDG", 0.95)

        g = TokenGraph()
        for q in [q1, q2, q3]:
            g.add_quote(q)

        alerts = bellman_ford_arbitrage(g, slippage_buffer_pct=0.6)
        assert len(alerts) >= 1
        assert alerts[0].expected_multiplier > 1.0

    def test_bellman_ford_no_cycle_when_fair_market(self) -> None:
        """公允无套利市场下不返回负环."""
        p1 = make_v3_pool("01", "A/B 0.3%", 30.0)
        p2 = make_v3_pool("02", "B/C 0.3%", 30.0)
        p3 = make_v3_pool("03", "C/A 0.3%", 30.0)

        # 完美对称闭环: 1 * 1 * 1 = 1.0, 扣费后 < 1.0
        q1 = make_quote(p1, "A", "B", 1.0)
        q2 = make_quote(p2, "B", "C", 1.0)
        q3 = make_quote(p3, "C", "A", 1.0)

        g = TokenGraph()
        for q in [q1, q2, q3]:
            g.add_quote(q)

        alerts = bellman_ford_arbitrage(g)
        assert alerts == []


class TestCycleDeduplicationAndFilter:
    """去重与始发币筛选."""

    def test_canonical_rotation_deduplication(self) -> None:
        """未指定始发币时，同一环路只应报告一次."""
        p1 = make_v3_pool("01", "USDG/WETH 0.01%", 1.0)
        p2 = make_v3_pool("02", "PONS/WETH 0.3%", 30.0)
        p3 = make_v3_pool("03", "PONS/USDG 0.3%", 30.0)

        q1 = make_quote(p1, "USDG", "WETH", 0.000403)
        q2 = make_quote(p2, "WETH", "PONS", 2800.0)
        q3 = make_quote(p3, "PONS", "USDG", 0.95)

        alerts = find_triangular_opportunities([q1, q2, q3], base_token=None)
        # 正反两向中仅正向盈利，因此去重后应只有 1 个
        assert len(alerts) == 1

    def test_base_token_filtering(self) -> None:
        """指定 base_token='USDG' 时，始发代币必须为 USDG."""
        p1 = make_v3_pool("01", "USDG/WETH 0.01%", 1.0)
        p2 = make_v3_pool("02", "PONS/WETH 0.3%", 30.0)
        p3 = make_v3_pool("03", "PONS/USDG 0.3%", 30.0)

        q1 = make_quote(p1, "USDG", "WETH", 0.000403)
        q2 = make_quote(p2, "WETH", "PONS", 2800.0)
        q3 = make_quote(p3, "PONS", "USDG", 0.95)

        alerts = find_triangular_opportunities([q1, q2, q3], base_token="USDG")
        assert len(alerts) == 1
        assert alerts[0].cycle[0] == "USDG"
        assert alerts[0].cycle[-1] == "USDG"


class TestEdgeCases:
    """边界异常保护."""

    def test_zero_or_negative_price_handled_safely(self) -> None:
        """零价格或负价格不引发除零或崩溃."""
        p1 = make_v3_pool("01", "A/B", 30.0)
        p2 = make_v3_pool("02", "B/C", 30.0)
        p3 = make_v3_pool("03", "C/A", 30.0)

        q1 = make_quote(p1, "A", "B", 0.0)
        q2 = make_quote(p2, "B", "C", -1.0)
        q3 = make_quote(p3, "C", "A", 1.0)

        alerts = find_triangular_opportunities([q1, q2, q3])
        assert alerts == []

    def test_multi_pool_best_rate_selection(self) -> None:
        """同一币对存在不同费率池时，自动选择有效汇率最高的池."""
        # A -> B 存在 1% 高费率池 和 0.01% 低费率池
        p_high_fee = make_v3_pool("01", "A/B 1%", 100.0)
        p_low_fee = make_v3_pool("02", "A/B 0.01%", 1.0)
        p_bc = make_v3_pool("03", "B/C 0.3%", 30.0)
        p_ca = make_v3_pool("04", "C/A 0.3%", 30.0)

        q_hf = make_quote(p_high_fee, "A", "B", 10.0)
        q_lf = make_quote(p_low_fee, "A", "B", 10.0)
        q_bc = make_quote(p_bc, "B", "C", 2.0)
        q_ca = make_quote(p_ca, "C", "A", 0.06)

        alerts = find_triangular_opportunities([q_hf, q_lf, q_bc, q_ca])
        assert len(alerts) >= 1
        # 首腿选取的池应当是低费率池 (0.01%)
        first_leg_pool = alerts[0].legs[0].pool
        assert first_leg_pool.fee_bps == 1.0


class TestZeroAddressDefense:
    """三角套利零地址防御测试."""

    def test_add_quote_rejects_zero_address(self) -> None:
        """add_quote 过滤包含零地址的 quote，不加入图."""
        g = TokenGraph()
        p = make_v3_pool("01", "ZERO/WETH 0.3%", 30.0)
        q = make_quote(
            p,
            "0x0000000000000000000000000000000000000000",
            "0x0bd7d308f8e1639fab988df18a8011f41eacad73",
            1.0,
        )
        g.add_quote(q)
        assert len(g.nodes) == 0
        assert len(g.adj) == 0

    def test_add_quote_rejects_invalid_hex_length(self) -> None:
        """add_quote 过滤 0x 开头但长度异常的 token."""
        g = TokenGraph()
        p = make_v3_pool("01", "SHORT/WETH 0.3%", 30.0)
        q = make_quote(p, "0x12d5ee79", "0x0bd7d308f8e1639fab988df18a8011f41eacad73", 1.0)
        g.add_quote(q)
        assert len(g.nodes) == 0

    def test_add_quote_rejects_pool_with_zero_address(self) -> None:
        """add_quote 过滤底层池 currency0 为零地址的 quote (如 V4 脏池)."""
        g = TokenGraph()
        dirty_v4 = V4PoolSpec(
            address="0x" + "aa" * 32,
            label="AI / WETH 1.004% (uniswap-v4)",
            fee_bps=100.4,
            token0="0x0000000000000000000000000000000000000000",
            token1="0x2E8c31162b855A2ffa90F6F8634643Ad6F111e18",
        )
        q = make_quote(
            dirty_v4,
            "0x2E8c31162b855A2ffa90F6F8634643Ad6F111e18",
            "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168",
            1.0,
        )
        g.add_quote(q)
        assert len(g.nodes) == 0

    def test_calculate_triangular_path_rejects_zero_address(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """calculate_triangular_path 包含零地址时返回 None 并记 [INVALID_PATH_TOKEN]."""
        caplog.set_level("WARNING")
        p1 = make_v3_pool("01", "USDG/WETH 0.01%", 1.0)
        p2 = make_v3_pool("02", "WETH/ZERO 0.3%", 30.0)
        p3 = make_v3_pool("03", "ZERO/USDG 0.3%", 30.0)

        zero_addr = "0x0000000000000000000000000000000000000000"
        weth_addr = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
        usdg_addr = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"

        e1 = DirectedEdge(
            from_token=usdg_addr,
            to_token=weth_addr,
            pool=p1,
            rate=0.0004,
            fee_bps=1.0,
            effective_rate=0.0004,
            weight=0.0,
        )
        e2 = DirectedEdge(
            from_token=weth_addr,
            to_token=zero_addr,
            pool=p2,
            rate=2500.0,
            fee_bps=30.0,
            effective_rate=2500.0,
            weight=0.0,
        )
        e3 = DirectedEdge(
            from_token=zero_addr,
            to_token=usdg_addr,
            pool=p3,
            rate=1.05,
            fee_bps=30.0,
            effective_rate=1.05,
            weight=0.0,
        )

        alert = calculate_triangular_path(e1, e2, e3)
        assert alert is None
        assert "[INVALID_PATH_TOKEN]" in caplog.text

    def test_triangular_graph_with_zero_token_generates_no_alert(self) -> None:
        """三角图含零地址 token 时不生成 alert."""
        p1 = make_v3_pool("01", "USDG/WETH 0.01%", 1.0)
        p2 = make_v3_pool("02", "WETH/ZERO 0.3%", 30.0)
        p3 = make_v3_pool("03", "ZERO/USDG 0.3%", 30.0)

        zero_addr = "0x0000000000000000000000000000000000000000"
        weth_addr = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
        usdg_addr = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"

        q1 = make_quote(p1, usdg_addr, weth_addr, 0.0004)
        q2 = make_quote(p2, weth_addr, zero_addr, 2500.0)
        q3 = make_quote(p3, zero_addr, usdg_addr, 1.05)

        alerts = find_triangular_opportunities([q1, q2, q3])
        assert len(alerts) == 0
