"""发射门槛与 QQ 门槛对齐及 4 个已验证 DEX (up-v3, giga-v3, ramses-v3, uniswap-v2) 单元测试."""

from __future__ import annotations

import math
from unittest.mock import MagicMock, patch

import pytest
from arbitrage.multicall_reader import (
    GET_RESERVES_SELECTOR,
    V3_SLOT0_SELECTOR,
    decode_quote_from_result,
    encode_pool_slot0_call,
)
from arbitrage.pool_scanner import (
    DEX_FACTORIES,
    EXCLUDED_DEXES,
    SUPPORTED_DEXES,
    V2_DEXES,
    V3_FORK_DEXES,
)
from arbitrage.spread_monitor import (
    PoolReader,
    PoolSpec,
    SpreadAlert,
)
from arbitrage.triangular import SwapLeg, TriangularArbAlert
from core.config import Config
from execution.weth_arbitrage_executor import (
    ArbitrageLeg,
    ArbitragePlan,
    WethArbitrageExecutor,
)
from monitors.daemons.arbitrage_daemon import ArbitrageDaemon
from web3 import Web3

WETH_ADDR = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
USDG_ADDR = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
PONS_ADDR = "0x10cc6bd38112cac182db90b6a71d8bb5939526ba"


def make_test_spread_alert(
    gross_spread_pct: float,
    total_fee_pct: float,
    net_spread_pct: float,
    max_capacity_usd: float = 1000.0,
) -> tuple[SpreadAlert, PoolSpec, PoolSpec]:
    """构造测试用两跳跨池价差告警."""
    pool_buy = PoolSpec(
        address="0x" + "1" * 40,
        label="USDG/WETH 0.05% (uniswap-v3)",
        fee_bps=5.0,
        token0=WETH_ADDR,
        token1=USDG_ADDR,
        dec0=18,
        dec1=6,
        dex="uniswap-v3",
    )
    pool_sell = PoolSpec(
        address="0x" + "2" * 40,
        label="USDG/WETH 0.3% (uniswap-v2)",
        fee_bps=30.0,
        token0=WETH_ADDR,
        token1=USDG_ADDR,
        dec0=18,
        dec1=6,
        dex="uniswap-v2",
    )
    alert = SpreadAlert(
        base="USDG",
        quote="WETH",
        buy_pool=pool_buy,
        sell_pool=pool_sell,
        buy_price=0.00040,
        sell_price=0.00042,
        gross_spread_pct=gross_spread_pct,
        total_fee_pct=total_fee_pct,
        net_spread_pct=net_spread_pct,
        max_capacity_usd=max_capacity_usd,
        optimal_size_usd=min(max_capacity_usd, 500.0),
        profit_at_500u=25.0,
    )
    return alert, pool_buy, pool_sell


def make_test_triangle_alert(
    gross_profit_pct: float,
    total_fee_pct: float,
    net_profit_pct: float,
) -> TriangularArbAlert:
    """构造测试用三跳闭环套利告警."""
    pool1 = PoolSpec(
        address="0x" + "1" * 40,
        label="USDG/WETH 0.05% (up-v3)",
        fee_bps=5.0,
        token0=WETH_ADDR,
        token1=USDG_ADDR,
        dec0=18,
        dec1=6,
        dex="up-v3",
    )
    pool2 = PoolSpec(
        address="0x" + "2" * 40,
        label="PONS/USDG 0.3% (giga-v3)",
        fee_bps=30.0,
        token0=USDG_ADDR,
        token1=PONS_ADDR,
        dec0=6,
        dec1=18,
        dex="giga-v3",
    )
    pool3 = PoolSpec(
        address="0x" + "3" * 40,
        label="PONS/WETH 0.3% (ramses-v3)",
        fee_bps=30.0,
        token0=WETH_ADDR,
        token1=PONS_ADDR,
        dec0=18,
        dec1=18,
        dex="ramses-v3",
    )
    legs = [
        SwapLeg(
            from_token=WETH_ADDR,
            to_token=USDG_ADDR,
            pool=pool1,
            rate=2500.0,
            fee_bps=5.0,
            effective_rate=2500.0 * 0.9995,
        ),
        SwapLeg(
            from_token=USDG_ADDR,
            to_token=PONS_ADDR,
            pool=pool2,
            rate=1.0,
            fee_bps=30.0,
            effective_rate=1.0 * 0.997,
        ),
        SwapLeg(
            from_token=PONS_ADDR,
            to_token=WETH_ADDR,
            pool=pool3,
            rate=0.00041,
            fee_bps=30.0,
            effective_rate=0.00041 * 0.997,
        ),
    ]
    return TriangularArbAlert(
        start_token=WETH_ADDR,
        cycle=(WETH_ADDR, USDG_ADDR, PONS_ADDR, WETH_ADDR),
        legs=legs,
        gross_multiplier=1.0 + gross_profit_pct / 100.0,
        fee_multiplier=1.0 - total_fee_pct / 100.0,
        expected_multiplier=1.0 + net_profit_pct / 100.0,
        slippage_buffer_pct=0.6,
        net_multiplier=1.0 + net_profit_pct / 100.0,
        gross_profit_pct=gross_profit_pct,
        net_profit_pct=net_profit_pct,
        total_fee_pct=total_fee_pct,
        bottleneck_tvl=1000.0,
        max_capacity_usd=1000.0,
        optimal_size_usd=500.0,
        max_profit_usd=20.0,
        profit_at_500u=20.0,
    )


# ==============================================================================
# 1. 发射门槛与 QQ 门槛一致性测试 (净利略负时既不开火也不推 QQ)
# ==============================================================================


class TestFireGateAndQQConsistency:
    """发射门槛与 QQ 推送门槛的一致性测试."""

    def test_spread_alert_slightly_negative_net_usd_no_fire_no_qq(self) -> None:
        """连击状态(scaled_up=True)下，净利润略负(如-$0.10)时，断言既不开火也不推 QQ."""
        daemon = ArbitrageDaemon(mode="spread", auto_execute=True)
        daemon.scaled_up = True  # 已完成探路，后续严格保本

        # 构造略微正的 spread 比率，但被 Gas 吞噬导致 expected_net_usd = -0.10
        alert, _, _ = make_test_spread_alert(
            gross_spread_pct=0.40, total_fee_pct=0.35, net_spread_pct=0.05
        )

        with (
            patch.object(daemon, "_estimate_instant_gas_cost", return_value=(1_000_000_000, 0.35)),
            patch(
                "monitors.daemons.arbitrage_daemon.estimate_realized_profit_usd",
                return_value=(0.25, 500.0, 0.0005),
            ),
            patch.object(daemon, "record_opportunity"),
            patch("monitors.daemons.arbitrage_daemon.send_qq_notification") as mock_qq,
            patch.object(daemon, "_try_auto_snipe_spread") as mock_snipe,
        ):
            # realized_profit_usd = 0.25, gas = 0.35 => expected_net_usd = -0.10
            daemon.handle_spread_alert(alert)

            # 严格验证：既不推 QQ，也不开发射
            mock_qq.assert_not_called()
            mock_snipe.assert_not_called()

    def test_spread_alert_positive_net_usd_fires_and_sends_qq(self) -> None:
        """连击状态(scaled_up=True)下，净利润为正(如+$0.65)时，断言既开火又推 QQ."""
        daemon = ArbitrageDaemon(mode="spread", auto_execute=True)
        daemon.scaled_up = True

        alert, _, _ = make_test_spread_alert(
            gross_spread_pct=1.0, total_fee_pct=0.35, net_spread_pct=0.65
        )

        with (
            patch.object(daemon, "_estimate_instant_gas_cost", return_value=(1_000_000_000, 0.35)),
            patch(
                "monitors.daemons.arbitrage_daemon.estimate_realized_profit_usd",
                return_value=(1.00, 500.0, 0.002),
            ),
            patch.object(daemon, "record_opportunity"),
            patch("monitors.daemons.arbitrage_daemon.send_qq_notification") as mock_qq,
            patch.object(daemon, "_try_auto_snipe_spread") as mock_snipe,
        ):
            # realized_profit_usd = 1.00, gas = 0.35 => expected_net_usd = +0.65
            daemon.handle_spread_alert(alert)

            mock_qq.assert_called_once()
            mock_snipe.assert_called_once()

    def test_triangle_alert_slightly_negative_net_usd_no_fire_no_qq(self) -> None:
        """三角套利连击状态(scaled_up=True)下，净利略负断言既不开火也不推 QQ."""
        daemon = ArbitrageDaemon(mode="triangle", auto_execute=True)
        daemon.scaled_up = True

        alert = make_test_triangle_alert(
            gross_profit_pct=0.40, total_fee_pct=0.35, net_profit_pct=0.05
        )

        with (
            patch.object(daemon, "_estimate_instant_gas_cost", return_value=(1_000_000_000, 0.40)),
            patch(
                "monitors.daemons.arbitrage_daemon.estimate_realized_profit_usd",
                return_value=(0.30, 500.0, 0.0006),
            ),
            patch.object(daemon, "record_opportunity"),
            patch("monitors.daemons.arbitrage_daemon.send_qq_notification") as mock_qq,
            patch.object(daemon, "_try_auto_snipe_triangle") as mock_snipe,
        ):
            # realized_profit_usd = 0.30, gas = 0.40 => expected_net_usd = -0.10
            daemon.handle_triangle_alert(alert)

            mock_qq.assert_not_called()
            mock_snipe.assert_not_called()

    def test_triangle_alert_positive_net_usd_fires_and_sends_qq(self) -> None:
        """三角套利连击状态(scaled_up=True)下，净利为正断言既开火又推 QQ."""
        daemon = ArbitrageDaemon(mode="triangle", auto_execute=True)
        daemon.scaled_up = True

        alert = make_test_triangle_alert(
            gross_profit_pct=1.0, total_fee_pct=0.35, net_profit_pct=0.65
        )

        with (
            patch.object(daemon, "_estimate_instant_gas_cost", return_value=(1_000_000_000, 0.35)),
            patch(
                "monitors.daemons.arbitrage_daemon.estimate_realized_profit_usd",
                return_value=(1.00, 500.0, 0.002),
            ),
            patch.object(daemon, "record_opportunity"),
            patch("monitors.daemons.arbitrage_daemon.send_qq_notification") as mock_qq,
            patch.object(daemon, "_try_auto_snipe_triangle") as mock_snipe,
        ):
            daemon.handle_triangle_alert(alert)

            mock_qq.assert_called_once()
            mock_snipe.assert_called_once()


# ==============================================================================
# 2. 首笔探路 -$1.00 固定容忍测试 ($5 与 $500 两档金额，均为 1 美元固定额度)
# ==============================================================================


class TestFirstProbeFixedOneDollarTolerance:
    """首笔探路 -$1.00 固定亏损容忍测试."""

    def test_threshold_function_direct(self) -> None:
        """直接测试 is_fire_threshold_met 的数学边界."""
        daemon = ArbitrageDaemon(mode="spread", auto_execute=True)

        # 首笔探路 (not scaled_up)
        daemon.scaled_up = False
        assert daemon.is_fire_threshold_met(0.50) is True
        assert daemon.is_fire_threshold_met(0.00) is True
        assert daemon.is_fire_threshold_met(-0.99) is True
        assert daemon.is_fire_threshold_met(-1.00) is True
        assert daemon.is_fire_threshold_met(-1.01) is False
        assert daemon.is_fire_threshold_met(-5.00) is False

        # 后续连击 (scaled_up)
        daemon.scaled_up = True
        assert daemon.is_fire_threshold_met(0.01) is True
        assert daemon.is_fire_threshold_met(0.00) is False
        assert daemon.is_fire_threshold_met(-0.01) is False
        assert daemon.is_fire_threshold_met(-1.00) is False

    @pytest.mark.parametrize("trade_size", [5.0, 500.0])
    def test_first_probe_allows_loss_up_to_one_dollar(
        self, trade_size: float, tmp_path, monkeypatch
    ) -> None:
        """在 $5 与 $500 两档金额下，预期亏损 -$0.90 (<= $1.00) 均允许发射探路."""
        from tests.receipt_fixture import make_receipt_fixture

        ctx = make_receipt_fixture(tmp_path, monkeypatch, base="WETH", triangle=False)
        daemon = ArbitrageDaemon(
            mode="spread", auto_execute=True, funds_runtime=ctx.runtime
        )
        daemon.scaled_up = False
        daemon.running = True

        alert, _, _ = make_test_spread_alert(
            gross_spread_pct=0.0,
            total_fee_pct=0.35,
            net_spread_pct=-0.35,
            max_capacity_usd=trade_size,
        )

        mock_executor = MagicMock()
        mock_executor.funds_runtime = ctx.runtime
        mock_executor.weth_address = WETH_ADDR
        mock_executor.guard.check_private_key_file.return_value.read_text.return_value = (
            "0x" + "f" * 64
        )
        mock_account = MagicMock()
        mock_account.address = "0x" + "e" * 40
        mock_executor.w3.eth.account.from_key.return_value = mock_account
        mock_executor.get_weth_balance.return_value = int(1e18)
        mock_executor.config.PRIVATE_KEY_PATH = "/tmp/mock.key"
        mock_executor.plan_from_spread_alert.return_value = MagicMock()
        mock_executor.execute.return_value = {"status": "DRY_RUN_SUCCESS"}
        daemon.executor = mock_executor

        with (
            patch.object(daemon, "_estimate_instant_gas_cost", return_value=(1_000_000_000, 0.40)),
            patch(
                "monitors.daemons.arbitrage_daemon.estimate_realized_profit_usd",
                return_value=(-0.50, trade_size, 0.0),
            ),
            patch("monitors.daemons.arbitrage_daemon.notify_chain_auditor") as mock_auditor,
        ):
            # expected_net_usd = -0.50 - 0.40 = -0.90 >= -1.00 => 探路允许
            daemon._try_auto_snipe_spread(alert)

            # 未发生 GAS_UNPROFITABLE_ABORT
            for call in mock_auditor.call_args_list:
                assert call[0][0].get("event_type") != "GAS_UNPROFITABLE_ABORT"
            mock_executor.plan_from_spread_alert.assert_called_once()
            mock_executor.execute.assert_called_once()

    @pytest.mark.parametrize("trade_size", [5.0, 500.0])
    def test_first_probe_aborts_when_loss_exceeds_one_dollar(
        self, trade_size: float, tmp_path, monkeypatch
    ) -> None:
        """在 $5 与 $500 两档金额下，预期亏损 -$1.15 (> $1.00) 均被严格拦截 (不随金额放大)."""
        from tests.receipt_fixture import make_receipt_fixture

        ctx = make_receipt_fixture(tmp_path, monkeypatch, base="WETH", triangle=False)

        daemon = ArbitrageDaemon(
            mode="spread",
            auto_execute=True,
            initial_amount_usd=trade_size,
            funds_runtime=ctx.runtime,
        )
        daemon.scaled_up = False
        daemon.running = True

        alert, _, _ = make_test_spread_alert(
            gross_spread_pct=0.0,
            total_fee_pct=0.35,
            net_spread_pct=-0.35,
            max_capacity_usd=trade_size,
        )

        mock_executor = MagicMock(wraps=ctx.executor)
        mock_executor.funds_runtime = ctx.runtime
        mock_executor.weth_address = WETH_ADDR
        mock_executor.guard = MagicMock(wraps=ctx.executor.guard)
        key_mock = MagicMock()
        key_mock.read_text.return_value = "0x" + "f" * 64
        mock_executor.guard.check_private_key_file.return_value = key_mock
        mock_account = MagicMock()
        mock_account.address = ctx.wallet
        mock_executor.w3.eth.account.from_key.return_value = mock_account
        mock_executor.get_weth_balance.return_value = int(1e18)
        mock_executor.config = MagicMock(wraps=ctx.executor.config)
        mock_executor.config.PRIVATE_KEY_PATH = "/tmp/mock.key"
        daemon.executor = mock_executor

        with (
            patch.object(daemon, "_estimate_instant_gas_cost", return_value=(1_000_000_000, 0.65)),
            patch(
                "monitors.daemons.arbitrage_daemon.estimate_realized_profit_usd",
                return_value=(-0.50, trade_size, 0.0),
            ),
            patch("monitors.daemons.arbitrage_daemon.notify_chain_auditor") as mock_auditor,
        ):
            # expected_net_usd = -0.50 - 0.65 = -1.15 < -1.00 => 拦截
            daemon._try_auto_snipe_spread(alert)

            mock_executor.execute.assert_not_called()
            assert mock_auditor.called
            event = mock_auditor.call_args[0][0]
            assert event["event_type"] == "GAS_UNPROFITABLE_ABORT"
            assert event["amount_usd"] == trade_size
            assert "未达到发射门槛" in event["reason"]


# ==============================================================================
# 3. V2 池 getReserves 价格换算与 V3 归一化对齐测试
# ==============================================================================


class TestV2PriceConversionAndV3Alignment:
    """Uniswap V2 getReserves 与 Uniswap V3 slot0 价格读取与归一化测试."""

    def test_v2_price_normalization_standard_order(self) -> None:
        """V2 池 token0 < token1: 验证 decimals 归一化计算.

        Token0: WETH (18 dec, 0x0bd7...)
        Token1: USDG (6 dec,  0x5fc5...)
        reserve0 = 2 WETH = 2 * 10^18
        reserve1 = 5000 USDG = 5000 * 10^6
        预期现货价格: 2500 USDG / WETH.
        """
        pool_v2 = PoolSpec(
            address="0x" + "a" * 40,
            label="USDG/WETH (uniswap-v2)",
            fee_bps=30.0,
            token0=WETH_ADDR,
            token1=USDG_ADDR,
            dec0=18,
            dec1=6,
            dex="uniswap-v2",
        )
        assert pool_v2.is_v2 is True

        reader = PoolReader(rpc=MagicMock())

        reserve0 = 2 * (10**18)
        reserve1 = 5000 * (10**6)
        block_ts = 1725800000

        # ABI 编码 getReserves: (uint112 reserve0, uint112 reserve1, uint32 blockTimestampLast)
        raw_reserves = (
            "0x"
            + reserve0.to_bytes(32, "big").hex()
            + reserve1.to_bytes(32, "big").hex()
            + block_ts.to_bytes(32, "big").hex()
        )
        with patch.object(reader, "_call", return_value=raw_reserves):
            quote = reader.quote(pool_v2, block_number=1000)

            assert quote.base == WETH_ADDR.lower()
            assert quote.quote == USDG_ADDR.lower()
            assert quote.price == pytest.approx(2500.0)
            assert quote.raw_price_t1_per_t0 == pytest.approx(2500.0)

    def test_v2_price_normalization_inverted_order(self) -> None:
        """V2 池 token0 > token1: 验证倒数换算后基准对对齐.

        Token0: USDG (6 dec, 0x5fc5...)
        Token1: WETH (18 dec, 0x0bd7...)
        reserve0 = 5000 USDG = 5000 * 10^6
        reserve1 = 2 WETH = 2 * 10^18
        由于 0x0bd7 < 0x5fc5，归一化后 base 必须为 WETH，quote 为 USDG，价格仍为 2500 USDG/WETH.
        """
        pool_v2 = PoolSpec(
            address="0x" + "b" * 40,
            label="WETH/USDG (uniswap-v2)",
            fee_bps=30.0,
            token0=USDG_ADDR,
            token1=WETH_ADDR,
            dec0=6,
            dec1=18,
            dex="uniswap-v2",
        )
        assert pool_v2.is_v2 is True

        reader = PoolReader(rpc=MagicMock())

        reserve0 = 5000 * (10**6)
        reserve1 = 2 * (10**18)
        block_ts = 1725800000

        raw_reserves = (
            "0x"
            + reserve0.to_bytes(32, "big").hex()
            + reserve1.to_bytes(32, "big").hex()
            + block_ts.to_bytes(32, "big").hex()
        )
        with patch.object(reader, "_call", return_value=raw_reserves):
            quote = reader.quote(pool_v2, block_number=1000)

            assert quote.base == WETH_ADDR.lower()
            assert quote.quote == USDG_ADDR.lower()
            assert quote.price == pytest.approx(2500.0)

    def test_v2_and_v3_price_quotes_alignment(self) -> None:
        """验证相同价格下，V2 getReserves 与 V3 slot0 解码后的 price 严格一致可比价."""
        pool_v2 = PoolSpec(
            address="0x" + "c" * 40,
            label="USDG/WETH (uniswap-v2)",
            fee_bps=30.0,
            token0=WETH_ADDR,
            token1=USDG_ADDR,
            dec0=18,
            dec1=6,
            dex="uniswap-v2",
        )
        pool_v3 = PoolSpec(
            address="0x" + "d" * 40,
            label="USDG/WETH (uniswap-v3)",
            fee_bps=5.0,
            token0=WETH_ADDR,
            token1=USDG_ADDR,
            dec0=18,
            dec1=6,
            dex="uniswap-v3",
        )

        target_price = 2473.50  # USDG per WETH

        # V2: reserve1 / reserve0 * 10^(18-6) = 2473.50
        r0 = 10 * (10**18)
        r1 = int(target_price * 10 * (10**6))
        v2_raw = (
            "0x"
            + r0.to_bytes(32, "big").hex()
            + r1.to_bytes(32, "big").hex()
            + (1725800000).to_bytes(32, "big").hex()
        )

        # V3: (sqrtPriceX96 / 2^96)^2 * 10^(18-6) = 2473.50
        p_raw = target_price * (10 ** (6 - 18))
        sqrt_price = math.sqrt(p_raw)
        sqrt_price_x96 = int(sqrt_price * (2**96))
        v3_raw = "0x" + sqrt_price_x96.to_bytes(32, "big").hex() + ("00" * 32 * 6)

        reader = PoolReader(rpc=MagicMock())

        with patch.object(reader, "_call", return_value=v2_raw):
            quote_v2 = reader.quote(pool_v2, block_number=1000)

        with patch.object(reader, "_call", return_value=v3_raw):
            quote_v3 = reader.quote(pool_v3, block_number=1000)

        assert quote_v2.base == quote_v3.base
        assert quote_v2.quote == quote_v3.quote
        assert quote_v2.price == pytest.approx(quote_v3.price, rel=1e-4)

    def test_multicall_reader_v2_selector_and_decoding(self) -> None:
        """验证 Multicall2 读池时正确路由 V2 getReserves 与 V3 slot0."""
        pool_v2 = PoolSpec(
            address="0x" + "a" * 40,
            label="USDG/WETH (uniswap-v2)",
            fee_bps=30.0,
            token0=WETH_ADDR,
            token1=USDG_ADDR,
            dec0=18,
            dec1=6,
            dex="uniswap-v2",
        )
        pool_v3 = PoolSpec(
            address="0x" + "b" * 40,
            label="USDG/WETH (uniswap-v3)",
            fee_bps=5.0,
            token0=WETH_ADDR,
            token1=USDG_ADDR,
            dec0=18,
            dec1=6,
            dex="uniswap-v3",
        )
        pool_up = PoolSpec(
            address="0x" + "c" * 40,
            label="USDG/WETH (up-v3)",
            fee_bps=5.0,
            token0=WETH_ADDR,
            token1=USDG_ADDR,
            dec0=18,
            dec1=6,
            dex="up-v3",
        )

        # 验证编码 selector
        target_v2, call_data_v2 = encode_pool_slot0_call(pool_v2)
        assert target_v2.lower() == pool_v2.address.lower()
        assert call_data_v2.hex() == GET_RESERVES_SELECTOR[2:]

        target_v3, call_data_v3 = encode_pool_slot0_call(pool_v3)
        assert target_v3.lower() == pool_v3.address.lower()
        assert call_data_v3.hex() == V3_SLOT0_SELECTOR[2:]

        _, call_data_up = encode_pool_slot0_call(pool_up)
        assert call_data_up.hex() == V3_SLOT0_SELECTOR[2:]

        # 验证 Multicall V2 解码
        r0 = 10 * (10**18)
        r1 = 25000 * (10**6)
        retdata = r0.to_bytes(32, "big") + r1.to_bytes(32, "big") + (123456).to_bytes(32, "big")

        quote = decode_quote_from_result(pool_v2, True, retdata, block_number=2000)
        assert quote is not None
        assert quote.price == pytest.approx(2500.0)


# ==============================================================================
# 4. 白名单与 4 个已验证 DEX 扫描器登记测试
# ==============================================================================


class TestVerifiedDexesRegistry:
    """验证 DEX 白名单及排除列表包含实测规格."""

    def test_dex_factories_registered(self) -> None:
        """检查 4 个已验证 DEX factory 是否准确配置."""
        assert DEX_FACTORIES["up-v3"] == "0x1ac9db4a2608ba45d6127b1737949b51bb54b7f3"
        assert DEX_FACTORIES["giga-v3"] == "0xece6ecd61177336ea6fb9b17937ac439d85ee20b"
        assert DEX_FACTORIES["ramses-v3"] == "0xe0c4ceb92d08ca985bb70fe0a22feb121a9854a8"
        assert DEX_FACTORIES["uniswap-v2"] == "0x8bceaa40b9acdfaedf85adf4ff01f5ad6517937f"

    def test_dex_categories(self) -> None:
        """检查 DEX 分类集合正确性."""
        assert {"uniswap-v3", "up-v3", "giga-v3", "ramses-v3"}.issubset(V3_FORK_DEXES)
        assert "uniswap-v2" in V2_DEXES
        assert {"up-v3", "giga-v3", "ramses-v3", "uniswap-v2"}.issubset(SUPPORTED_DEXES)

    def test_excluded_dexes_unsupported(self) -> None:
        """检查本轮明确排除的非标 DEX."""
        for dex in ["alandale-cl", "bankr", "orvex-v4", "ramses-dlmm", "pons-v2"]:
            assert dex in EXCLUDED_DEXES


# ==============================================================================
# 5. V2 与跨 DEX 执行器 Calldata 生成测试
# ==============================================================================


class TestV2ExecutionCalldata:
    """测试 Universal Router 对 V2 及混合 V2/V3 路径的指令编码."""

    @pytest.fixture
    def executor(self) -> WethArbitrageExecutor:
        cfg = Config()
        mock_w3 = MagicMock()
        mock_w3.is_connected.return_value = True
        mock_w3.eth.chain_id = 4663
        mock_w3.eth.get_block.return_value = {"baseFeePerGas": 1_000_000_000}
        mock_w3.eth.gas_price = 1_000_000_000
        mock_w3.from_wei = Web3.from_wei
        mock_w3.to_wei = Web3.to_wei
        mock_w3.to_checksum_address = Web3.to_checksum_address
        return WethArbitrageExecutor(config=cfg, w3=mock_w3)

    def test_all_v2_swap_calldata(self, executor: WethArbitrageExecutor) -> None:
        """全 V2 路径: WETH -> USDG -> WETH (uniswap-v2)."""
        legs = [
            ArbitrageLeg(
                from_token=WETH_ADDR,
                to_token=USDG_ADDR,
                pool_fee=3000,
                dex="uniswap-v2",
            ),
            ArbitrageLeg(
                from_token=USDG_ADDR,
                to_token=WETH_ADDR,
                pool_fee=3000,
                dex="uniswap-v2",
            ),
        ]
        plan = ArbitragePlan(
            legs=legs,
            amount_in_weth=10**17,
            expected_amount_out=int(10**17 * 1.01),
            amount_out_min=int(10**17 * 1.00),
            amount_usd=250.0,
            slippage_pct=1.0,
        )
        res = executor.build_swap_calldata(plan)

        assert res["all_v2"] is True
        assert res["all_v3"] is False
        assert res["decoded_function"] == "execute"
        assert res["calldata"].startswith("0x")
        assert len(res["decoded_params"]["commands"]) == 1  # 单个 V2_SWAP_EXACT_IN 指令完成全路径

    def test_mixed_v2_and_v3_hops_calldata(self, executor: WethArbitrageExecutor) -> None:
        """混合 V2 与 V3 路径: WETH -(v2)-> USDG -(v3)-> WETH."""
        legs = [
            ArbitrageLeg(
                from_token=WETH_ADDR,
                to_token=USDG_ADDR,
                pool_fee=3000,
                dex="uniswap-v2",
            ),
            ArbitrageLeg(
                from_token=USDG_ADDR,
                to_token=WETH_ADDR,
                pool_fee=500,
                dex="up-v3",
            ),
        ]
        plan = ArbitragePlan(
            legs=legs,
            amount_in_weth=10**17,
            expected_amount_out=int(10**17 * 1.01),
            amount_out_min=int(10**17 * 1.00),
            amount_usd=250.0,
            slippage_pct=1.0,
        )
        res = executor.build_swap_calldata(plan)

        assert res["all_v2"] is False
        assert res["all_v3"] is False
        assert res["decoded_function"] == "execute"
        assert len(res["decoded_params"]["commands"]) == 2  # 2 条依序执行的指令
        assert res["calldata"].startswith("0x")

    def test_plan_from_spread_alert_with_v2_pool(self, executor: WethArbitrageExecutor) -> None:
        """两跳跨池告警包含 V2 池时，正确提取 dex 标识并在 plan 中体现."""
        pool_buy = PoolSpec(
            address="0x" + "1" * 40,
            label="USDG/WETH 0.05% (uniswap-v3)",
            fee_bps=5.0,
            token0=WETH_ADDR,
            token1=USDG_ADDR,
            dec0=18,
            dec1=6,
            dex="uniswap-v3",
        )
        pool_sell = PoolSpec(
            address="0x" + "2" * 40,
            label="USDG/WETH (uniswap-v2)",
            fee_bps=30.0,
            token0=WETH_ADDR,
            token1=USDG_ADDR,
            dec0=18,
            dec1=6,
            dex="uniswap-v2",
        )
        alert = SpreadAlert(
            base="USDG",
            quote="WETH",
            buy_pool=pool_buy,
            sell_pool=pool_sell,
            buy_price=0.00040,
            sell_price=0.00042,
            gross_spread_pct=5.0,
            total_fee_pct=0.35,
            net_spread_pct=4.45,
            max_capacity_usd=1000.0,
            optimal_size_usd=500.0,
            profit_at_500u=22.25,
        )

        plan = executor.plan_from_spread_alert(alert=alert, amount_usd=250.0, slippage_pct=0.5)

        assert len(plan.legs) == 2
        assert plan.legs[0].dex == "uniswap-v3"
        assert plan.legs[1].dex == "uniswap-v2"
