"""Tests for cycle deduplication, fail-closed validation, and profit anomaly protection."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
from arbitrage.spread_monitor import PoolSpec, PriceQuote
from arbitrage.triangular import (
    DirectedEdge,
    SwapLeg,
    TokenGraph,
    TriangularArbAlert,
    calculate_triangular_path,
    find_triangular_opportunities,
    normalize_cycle,
)
from execution.weth_arbitrage_executor import (
    CANONICAL_USDG_ADDRESS,
    CANONICAL_WETH_ADDRESS,
    WethArbitrageExecutor,
)
from monitors.daemons.arbitrage_daemon import ArbitrageDaemon
from web3 import Web3

WETH_ADDR = CANONICAL_WETH_ADDRESS
USDG_ADDR = CANONICAL_USDG_ADDRESS
PONS_ADDR = "0x39dbed3a2bd333467115de45665cc57f813c4571"
NET_ADDR = "0xCA9c78Dd337A67F6e0077F65F5E9218719d30eDf"


def make_test_pool(suffix: str, label: str, fee_bps: float) -> PoolSpec:
    addr = "0x" + suffix.rjust(40, "0")
    return PoolSpec(address=addr, label=label, fee_bps=fee_bps, tvl_usd=500_000.0)


@pytest.fixture
def mock_w3() -> MagicMock:
    w3 = MagicMock()
    w3.eth.chain_id = 4663
    w3.eth.gas_price = 1_000_000_000
    w3.eth.get_balance.return_value = 10**18
    w3.from_wei = Web3.from_wei
    w3.to_wei = Web3.to_wei
    w3.to_checksum_address = Web3.to_checksum_address
    return w3


@pytest.fixture
def executor(mock_w3: MagicMock) -> WethArbitrageExecutor:
    exec_inst = WethArbitrageExecutor(w3=mock_w3)
    exec_inst.get_weth_balance = MagicMock(return_value=10**18)
    exec_inst.get_eth_balance = MagicMock(return_value=10**18)
    exec_inst.get_weth_allowance = MagicMock(return_value=2**256 - 1)
    return exec_inst


class TestCycleNormalization:
    """1. 3 跳路径生成的 cycle 归一化后长度 = 4 且首尾相同."""

    def test_normalize_cycle_lengths_and_endpoints(self) -> None:
        """Verify cycle normalization ensures length = hops + 1 and starts/ends on origin."""
        # 3-hop closed
        c1 = ["WETH", "USDG", "PONS", "WETH"]
        norm1 = normalize_cycle(c1)
        assert len(norm1) == 4
        assert norm1[0] == norm1[-1] == "WETH"

        # 3-hop unclosed
        c2 = ["WETH", "USDG", "PONS"]
        norm2 = normalize_cycle(c2)
        assert len(norm2) == 4
        assert norm2[0] == norm2[-1] == "WETH"

        # Dirty cycle with duplicate tail
        c3 = ["WETH", "NET", "WETH", "WETH"]
        norm3 = normalize_cycle(c3)
        assert len(norm3) == 3
        assert norm3 == ["WETH", "NET", "WETH"]

    def test_calculate_triangular_path_generates_canonical_cycle(self) -> None:
        """Verify calculate_triangular_path produces cycle of length 4 with matching endpoints."""
        p1 = make_test_pool("01", "WETH/USDG 0.01%", 1.0)
        p2 = make_test_pool("02", "USDG/PONS 0.3%", 30.0)
        p3 = make_test_pool("03", "PONS/WETH 0.3%", 30.0)

        e1 = DirectedEdge(
            from_token=WETH_ADDR,
            to_token=USDG_ADDR,
            pool=p1,
            rate=2500.0,
            fee_bps=1.0,
            effective_rate=2500.0 * (1 - 0.0001),
            weight=0.0,
            from_symbol="WETH",
            to_symbol="USDG",
        )
        e2 = DirectedEdge(
            from_token=USDG_ADDR,
            to_token=PONS_ADDR,
            pool=p2,
            rate=5.0,
            fee_bps=30.0,
            effective_rate=5.0 * (1 - 0.003),
            weight=0.0,
            from_symbol="USDG",
            to_symbol="PONS",
        )
        e3 = DirectedEdge(
            from_token=PONS_ADDR,
            to_token=WETH_ADDR,
            pool=p3,
            rate=0.000085,
            fee_bps=30.0,
            effective_rate=0.000085 * (1 - 0.003),
            weight=0.0,
            from_symbol="PONS",
            to_symbol="WETH",
        )

        alert = calculate_triangular_path(e1, e2, e3)
        assert alert is not None
        assert len(alert.cycle) == 4
        assert alert.cycle[0] == alert.cycle[-1] == "WETH"
        assert alert.cycle == ("WETH", "USDG", "PONS", "WETH")


class TestDirtyCycleFailClosed:
    """2. executor 对 ['WETH','NET','WETH','WETH']（脏数据）不再崩溃，正确拒绝."""

    def test_dirty_cycle_duplicate_tail_fails_closed(
        self,
        executor: WethArbitrageExecutor,
    ) -> None:
        """Verify dirty data with duplicate nodes raises ValueError instead of crashing."""
        p1 = make_test_pool("01", "WETH/USDG 0.01%", 1.0)
        p2 = make_test_pool("02", "USDG/NET 0.3%", 30.0)
        p3 = make_test_pool("03", "NET/WETH 0.3%", 30.0)

        legs = [
            SwapLeg(
                from_token=WETH_ADDR,
                to_token=USDG_ADDR,
                pool=p1,
                rate=2500.0,
                fee_bps=1.0,
                effective_rate=2497.5,
                from_symbol="WETH",
                to_symbol="USDG",
            ),
            SwapLeg(
                from_token=USDG_ADDR,
                to_token=NET_ADDR,
                pool=p2,
                rate=100.0,
                fee_bps=30.0,
                effective_rate=99.7,
                from_symbol="USDG",
                to_symbol="NET",
            ),
            SwapLeg(
                from_token=NET_ADDR,
                to_token=WETH_ADDR,
                pool=p3,
                rate=0.0000041,
                fee_bps=30.0,
                effective_rate=0.00000408,
                from_symbol="NET",
                to_symbol="WETH",
            ),
        ]

        dirty_alert = TriangularArbAlert(
            start_token=WETH_ADDR,
            cycle=("WETH", "NET", "WETH", "WETH"),  # Dirty cycle!
            legs=legs,
            gross_multiplier=1.025,
            fee_multiplier=0.9939,
            expected_multiplier=1.018,
            slippage_buffer_pct=0.6,
            net_multiplier=1.012,
            gross_profit_pct=2.5,
            net_profit_pct=1.2,
            total_fee_pct=0.61,
            bottleneck_tvl=300_000.0,
            max_profit_usd=12.0,
            profit_at_500u=6.0,
        )

        with pytest.raises(ValueError) as exc_info:
            executor.plan_from_triangular_alert(dirty_alert, amount_usd=100.0)

        error_msg = str(exc_info.value)
        # 严禁抛出历史阻断级崩溃信息: "is not present in alert cycle"
        assert "is not present in alert cycle" not in error_msg
        # 明确拒绝由于节点重复或 legs 与 cycle 不匹配
        assert "does not match cycle hops" in error_msg or "duplicate" in error_msg

    def test_disconnected_legs_fail_closed(
        self,
        executor: WethArbitrageExecutor,
    ) -> None:
        """Verify legs that do not form a closed path are rejected."""
        p1 = make_test_pool("01", "WETH/USDG 0.01%", 1.0)
        p2 = make_test_pool("02", "NET/USDG 0.3%", 30.0)
        p3 = make_test_pool("03", "WETH/NET 0.3%", 30.0)

        # Disconnected legs from historical bug: Leg 1: WETH->USDG, Leg 2: NET->USDG, Leg 3: WETH->NET
        legs = [
            SwapLeg(
                from_token=WETH_ADDR,
                to_token=USDG_ADDR,
                pool=p1,
                rate=2500.0,
                fee_bps=1.0,
                effective_rate=2497.5,
            ),
            SwapLeg(
                from_token=NET_ADDR,
                to_token=USDG_ADDR,
                pool=p2,
                rate=0.0015,
                fee_bps=30.0,
                effective_rate=0.00149,
            ),
            SwapLeg(
                from_token=WETH_ADDR,
                to_token=NET_ADDR,
                pool=p3,
                rate=0.278,
                fee_bps=150.0,
                effective_rate=0.274,
            ),
        ]

        alert = TriangularArbAlert(
            start_token=WETH_ADDR,
            cycle=("WETH", "USDG", "NET", "WETH"),
            legs=legs,
            gross_multiplier=1.03,
            fee_multiplier=0.98,
            expected_multiplier=1.01,
            slippage_buffer_pct=0.6,
            net_multiplier=1.004,
            gross_profit_pct=3.0,
            net_profit_pct=0.4,
            total_fee_pct=2.0,
            bottleneck_tvl=300_000.0,
            max_profit_usd=8.0,
            profit_at_500u=4.0,
        )

        with pytest.raises(ValueError) as exc_info:
            executor.plan_from_triangular_alert(alert, amount_usd=100.0)
        assert "do not form a continuous closed path" in str(exc_info.value)


class TestProfitAnomalyProtection:
    """3. 构造 multiplier 连乘溢出场景，断言触发 PROFIT_ANOMALY 且 alert 被丢弃."""

    def test_triangular_engine_drops_profit_anomaly(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Verify calculate_triangular_path drops alerts with max_profit_usd > 1000."""
        p1 = make_test_pool("01", "WETH/USDG 0.01%", 1.0)
        p2 = make_test_pool("02", "USDG/SPACETIME 6.28%", 628.0)
        p3 = make_test_pool("03", "SPACETIME/WETH 89.99%", 8999.0)

        # 构造类似历史天文数字的异常汇率 (3.4e26)
        spacetime_addr = "0x" + "99" * 20
        e1 = DirectedEdge(
            from_token=WETH_ADDR,
            to_token=USDG_ADDR,
            pool=p1,
            rate=2500.0,
            fee_bps=1.0,
            effective_rate=2500.0,
            weight=0.0,
            from_symbol="WETH",
            to_symbol="USDG",
        )
        e2 = DirectedEdge(
            from_token=USDG_ADDR,
            to_token=spacetime_addr,
            pool=p2,
            rate=3.4e26,  # Multiplier overflow!
            fee_bps=628.0,
            effective_rate=3.4e26 * 0.9372,
            weight=0.0,
            from_symbol="USDG",
            to_symbol="SPACETIME",
        )
        e3 = DirectedEdge(
            from_token=spacetime_addr,
            to_token=WETH_ADDR,
            pool=p3,
            rate=9.1e-8,
            fee_bps=8999.0,
            effective_rate=9.1e-8 * 0.1,
            weight=0.0,
            from_symbol="SPACETIME",
            to_symbol="WETH",
        )

        with caplog.at_level(logging.ERROR):
            alert = calculate_triangular_path(e1, e2, e3)

        assert alert is None
        assert any("[PROFIT_ANOMALY]" in record.message for record in caplog.records)

    def test_executor_rejects_profit_anomaly_alert(
        self,
        executor: WethArbitrageExecutor,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Verify executor refuses to plan when max_profit_usd > 1000."""
        p1 = make_test_pool("01", "WETH/USDG 0.01%", 1.0)
        p2 = make_test_pool("02", "USDG/PONS 0.3%", 30.0)
        p3 = make_test_pool("03", "PONS/WETH 0.3%", 30.0)

        legs = [
            SwapLeg(
                from_token=WETH_ADDR,
                to_token=USDG_ADDR,
                pool=p1,
                rate=2500.0,
                fee_bps=1.0,
                effective_rate=2497.5,
            ),
            SwapLeg(
                from_token=USDG_ADDR,
                to_token=PONS_ADDR,
                pool=p2,
                rate=5.0,
                fee_bps=30.0,
                effective_rate=4.985,
            ),
            SwapLeg(
                from_token=PONS_ADDR,
                to_token=WETH_ADDR,
                pool=p3,
                rate=0.000085,
                fee_bps=30.0,
                effective_rate=0.000084,
            ),
        ]

        anomaly_alert = TriangularArbAlert(
            start_token=WETH_ADDR,
            cycle=("WETH", "USDG", "PONS", "WETH"),
            legs=legs,
            gross_multiplier=5.0,
            fee_multiplier=0.99,
            expected_multiplier=4.95,
            slippage_buffer_pct=0.6,
            net_multiplier=4.89,
            gross_profit_pct=400.0,
            net_profit_pct=389.0,
            total_fee_pct=1.0,
            max_profit_usd=25000.0,  # Huge profit anomaly!
            profit_at_500u=1945.0,
        )

        with caplog.at_level(logging.ERROR):
            with pytest.raises(ValueError) as exc_info:
                executor.plan_from_triangular_alert(anomaly_alert, amount_usd=100.0)

        assert "[PROFIT_ANOMALY]" in str(exc_info.value)
        assert any("[PROFIT_ANOMALY]" in record.message for record in caplog.records)

    def test_daemon_drops_profit_anomaly_alert(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Verify daemon handle_triangle_alert drops alerts with max_profit_usd > 1000."""
        daemon = ArbitrageDaemon()
        daemon.record_opportunity = MagicMock()
        daemon._try_auto_snipe_triangle = MagicMock()

        anomaly_alert = TriangularArbAlert(
            start_token=WETH_ADDR,
            cycle=("WETH", "USDG", "PONS", "WETH"),
            legs=[],
            gross_multiplier=10.0,
            fee_multiplier=0.99,
            expected_multiplier=9.9,
            slippage_buffer_pct=0.6,
            net_multiplier=9.3,
            gross_profit_pct=900.0,
            net_profit_pct=830.0,
            total_fee_pct=1.0,
            max_profit_usd=50000.0,  # Anomaly!
            profit_at_500u=4500.0,
        )

        with caplog.at_level(logging.ERROR):
            daemon.handle_triangle_alert(anomaly_alert)

        assert any("[PROFIT_ANOMALY]" in record.message for record in caplog.records)
        daemon.record_opportunity.assert_not_called()
        daemon._try_auto_snipe_triangle.assert_not_called()


class TestNormal3HopAlertParsing:
    """4. 正常 3 跳 alert → plan.legs == 3 且 legs[0].from_token == WETH."""

    def test_normal_3_hop_alert_parses_3_legs(
        self,
        executor: WethArbitrageExecutor,
    ) -> None:
        """Verify normal 3-hop alert produces 3 swap legs starting with WETH."""
        p1 = make_test_pool("01", "WETH/USDG 0.01%", 1.0)
        p2 = make_test_pool("02", "USDG/PONS 0.3%", 30.0)
        p3 = make_test_pool("03", "PONS/WETH 0.3%", 30.0)

        legs = [
            SwapLeg(
                from_token=WETH_ADDR,
                to_token=USDG_ADDR,
                pool=p1,
                rate=2500.0,
                fee_bps=1.0,
                effective_rate=2497.5,
                from_symbol="WETH",
                to_symbol="USDG",
            ),
            SwapLeg(
                from_token=USDG_ADDR,
                to_token=PONS_ADDR,
                pool=p2,
                rate=5.0,
                fee_bps=30.0,
                effective_rate=4.985,
                from_symbol="USDG",
                to_symbol="PONS",
            ),
            SwapLeg(
                from_token=PONS_ADDR,
                to_token=WETH_ADDR,
                pool=p3,
                rate=0.0000808,
                fee_bps=30.0,
                effective_rate=0.0000805,
                from_symbol="PONS",
                to_symbol="WETH",
            ),
        ]

        alert = TriangularArbAlert(
            start_token=WETH_ADDR,
            cycle=("WETH", "USDG", "PONS", "WETH"),
            legs=legs,
            gross_multiplier=1.01,
            fee_multiplier=0.9939,
            expected_multiplier=1.0038,
            slippage_buffer_pct=0.2,
            net_multiplier=1.0018,
            gross_profit_pct=1.0,
            net_profit_pct=0.18,
            total_fee_pct=0.61,
            bottleneck_tvl=500_000.0,
            max_profit_usd=25.0,
            profit_at_500u=12.0,
        )

        plan = executor.plan_from_triangular_alert(alert, amount_usd=100.0)
        assert len(plan.legs) == 3
        assert plan.legs[0].from_token == executor.weth_address
        assert plan.legs[1].from_token == Web3.to_checksum_address(USDG_ADDR)
        assert plan.legs[2].from_token == Web3.to_checksum_address(PONS_ADDR)
        assert plan.legs[-1].to_token == executor.weth_address
        assert plan.amount_usd == 100.0

    def test_normal_3_hop_rotated_alert_parses_3_legs(
        self,
        executor: WethArbitrageExecutor,
    ) -> None:
        """Verify 3-hop alert starting at non-WETH is rotated to start from WETH."""
        p1 = make_test_pool("01", "USDG/PONS 0.3%", 30.0)
        p2 = make_test_pool("02", "PONS/WETH 0.3%", 30.0)
        p3 = make_test_pool("03", "WETH/USDG 0.01%", 1.0)

        legs = [
            SwapLeg(
                from_token=USDG_ADDR,
                to_token=PONS_ADDR,
                pool=p1,
                rate=5.0,
                fee_bps=30.0,
                effective_rate=4.985,
                from_symbol="USDG",
                to_symbol="PONS",
            ),
            SwapLeg(
                from_token=PONS_ADDR,
                to_token=WETH_ADDR,
                pool=p2,
                rate=0.0000808,
                fee_bps=30.0,
                effective_rate=0.0000805,
                from_symbol="PONS",
                to_symbol="WETH",
            ),
            SwapLeg(
                from_token=WETH_ADDR,
                to_token=USDG_ADDR,
                pool=p3,
                rate=2500.0,
                fee_bps=1.0,
                effective_rate=2497.5,
                from_symbol="WETH",
                to_symbol="USDG",
            ),
        ]

        alert = TriangularArbAlert(
            start_token=USDG_ADDR,
            cycle=("USDG", "PONS", "WETH", "USDG"),
            legs=legs,
            gross_multiplier=1.01,
            fee_multiplier=0.9939,
            expected_multiplier=1.0038,
            slippage_buffer_pct=0.2,
            net_multiplier=1.0018,
            gross_profit_pct=1.0,
            net_profit_pct=0.18,
            total_fee_pct=0.61,
            bottleneck_tvl=500_000.0,
            max_profit_usd=25.0,
            profit_at_500u=12.0,
        )

        plan = executor.plan_from_triangular_alert(alert, amount_usd=100.0)
        assert len(plan.legs) == 3
        # First leg rotated to WETH
        assert plan.legs[0].from_token == executor.weth_address
        assert plan.legs[0].to_token == Web3.to_checksum_address(USDG_ADDR)
        assert plan.legs[-1].to_token == executor.weth_address
