"""Tests for 2-hop spread price rationality, profit ceiling, and multiplier validation."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest
from arbitrage.spread_monitor import PoolSpec, PriceQuote, SpreadAlert, find_spreads
from execution.weth_arbitrage_executor import (
    CANONICAL_USDG_ADDRESS,
    CANONICAL_WETH_ADDRESS,
    ArbitrageDataError,
    WethArbitrageExecutor,
)
from monitors.daemons.arbitrage_daemon import ArbitrageDaemon
from web3 import Web3

USDG_ADDR = CANONICAL_USDG_ADDRESS
WETH_ADDR = CANONICAL_WETH_ADDRESS


def make_pool(addr_suffix: str, label: str, fee_bps: float, tvl_usd: float = 500_000.0) -> PoolSpec:
    """Helper to construct a test PoolSpec."""
    addr = "0x" + addr_suffix.rjust(40, "0")
    return PoolSpec(
        address=addr,
        label=label,
        fee_bps=fee_bps,
        token0=USDG_ADDR,
        token1=WETH_ADDR,
        tvl_usd=tvl_usd,
    )


def make_quote(pool: PoolSpec, price: float) -> PriceQuote:
    """Helper to construct a PriceQuote with base=USDG, quote=WETH."""
    return PriceQuote(
        pool=pool,
        base=USDG_ADDR,
        quote=WETH_ADDR,
        price=price,
        raw_price_t1_per_t0=price,
    )


@pytest.fixture
def mock_w3() -> MagicMock:
    """Fixture providing a mocked Web3 instance."""
    w3_mock = MagicMock()
    w3_mock.eth.chain_id = 4663
    w3_mock.eth.gas_price = 1_000_000_000
    w3_mock.eth.get_balance.return_value = 10**18
    w3_mock.from_wei = Web3.from_wei
    w3_mock.to_wei = Web3.to_wei
    w3_mock.to_checksum_address = Web3.to_checksum_address
    return w3_mock


@pytest.fixture
def executor(mock_w3: MagicMock) -> WethArbitrageExecutor:
    """Fixture providing a WethArbitrageExecutor with mocked balance checks."""
    exec_inst = WethArbitrageExecutor(w3=mock_w3)
    exec_inst.get_weth_balance = MagicMock(return_value=10**18)
    exec_inst.get_eth_balance = MagicMock(return_value=10**18)
    exec_inst.get_weth_allowance = MagicMock(return_value=2**256 - 1)
    return exec_inst


class TestSpreadPriceRationalityAndAnomaly:
    """1. find_spreads 价格合理性校验与 SPREAD_ANOMALY 日志."""

    def test_find_spreads_rejects_huge_price_ratio_1e30(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Construct sell/buy=1e30, assert no SpreadAlert generated and log contains SPREAD_ANOMALY."""
        pool_buy = make_pool("01", "pool_buy", 30.0)
        pool_sell = make_pool("02", "pool_sell", 30.0)

        quote_buy = make_quote(pool_buy, price=1.0)
        quote_sell = make_quote(pool_sell, price=1e30)

        with caplog.at_level(logging.ERROR):
            alerts = find_spreads([quote_buy, quote_sell], slippage_buffer_pct=0.2)

        assert alerts == []
        assert any("[SPREAD_ANOMALY]" in record.message for record in caplog.records)

    def test_find_spreads_rejects_moo_usdg_36pct_spread(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Construct sell/buy = 1.367 (36% gross spread, e.g. MOO/USDG buy@33.576 sell@45.881).

        Must be discarded by find_spreads and caplog must contain [SPREAD_ANOMALY].
        """
        pool_buy = make_pool("01", "MOO / USDG 3.94% (uniswap-v4)", 394.0)
        pool_sell = make_pool("02", "MOO / USDG 10% (uniswap-v4)", 1000.0)

        # 45.881253 / 33.576286 = 1.366478 (~1.367, 36.65% gross spread)
        quote_buy = make_quote(pool_buy, price=33.576286)
        quote_sell = make_quote(pool_sell, price=45.881253)

        with caplog.at_level(logging.ERROR):
            alerts = find_spreads([quote_buy, quote_sell], slippage_buffer_pct=0.2)

        assert alerts == []
        assert any("[SPREAD_ANOMALY]" in record.message for record in caplog.records)
        assert any(
            "33.576286" in record.message and "45.881253" in record.message
            for record in caplog.records
        )

    def test_find_spreads_rejects_ratio_exceeding_two(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Construct sell/buy ratio > 2.0, assert discarded with SPREAD_ANOMALY."""
        pool_buy = make_pool("01", "pool_buy", 30.0)
        pool_sell = make_pool("02", "pool_sell", 30.0)

        quote_buy = make_quote(pool_buy, price=100.0)
        quote_sell = make_quote(pool_sell, price=250.0)  # ratio = 2.5 > 2.0

        with caplog.at_level(logging.ERROR):
            alerts = find_spreads([quote_buy, quote_sell], slippage_buffer_pct=0.2)

        assert alerts == []
        assert any("[SPREAD_ANOMALY]" in record.message for record in caplog.records)

    def test_find_spreads_rejects_non_finite_price(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Construct non-finite price quote, assert filtered or rejected."""
        pool_buy = make_pool("01", "pool_buy", 30.0)
        pool_sell = make_pool("02", "pool_sell", 30.0)

        quote_buy = make_quote(pool_buy, price=100.0)
        quote_sell = make_quote(pool_sell, price=float("inf"))

        with caplog.at_level(logging.ERROR):
            alerts = find_spreads([quote_buy, quote_sell], slippage_buffer_pct=0.2)

        assert alerts == []

    def test_find_spreads_rejects_profit_anomaly_exceeding_1000(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Construct alert where max_profit_usd > 1000, assert PROFIT_ANOMALY logged and discarded."""
        # Under normal conditions max_profit <= 1000, but if TVL is astronomical or profit calculation overflows
        pool_buy = make_pool("01", "pool_buy", 1.0, tvl_usd=1e9)
        pool_sell = make_pool("02", "pool_sell", 1.0, tvl_usd=1e9)

        quote_buy = make_quote(pool_buy, price=100.0)
        quote_sell = make_quote(pool_sell, price=105.0)  # gross 5%, within ratio 1.05 < 1.10

        # At 500U with net ~90%, profit_500 ~ 450 USD.
        # But if we patch estimate_realized_profit_usd to return > 1000 profit
        with patch("arbitrage.spread_monitor.estimate_realized_profit_usd") as mock_est:
            mock_est.return_value = (1500.0, 500.0, 5000.0)
            with caplog.at_level(logging.ERROR):
                alerts = find_spreads([quote_buy, quote_sell], slippage_buffer_pct=0.2)

        assert alerts == []
        assert any("[PROFIT_ANOMALY]" in record.message for record in caplog.records)


class TestDaemonSpreadProfitAnomalyAndCeiling:
    """2. 守护进程对 max_profit_usd=1e20 拦截不推QQ不发射并记录 PROFIT_ANOMALY."""

    def test_handle_spread_alert_rejects_profit_anomaly_1e20(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Construct max_profit_usd=1e20, assert no QQ push, no firing, and log contains PROFIT_ANOMALY."""
        pool_buy = make_pool("01", "pool_buy", 30.0)
        pool_sell = make_pool("02", "pool_sell", 30.0)

        alert = SpreadAlert(
            base=USDG_ADDR,
            quote=WETH_ADDR,
            buy_pool=pool_buy,
            sell_pool=pool_sell,
            buy_price=0.00040,
            sell_price=0.00042,
            gross_spread_pct=5.0,
            total_fee_pct=0.6,
            net_spread_pct=4.4,
            max_capacity_usd=1000.0,
            optimal_size_usd=500.0,
            max_profit_usd=1e20,  # Astronomical profit anomaly!
            profit_at_500u=22.0,
        )

        daemon = ArbitrageDaemon(mode="spread", auto_execute=True)
        daemon.record_opportunity = MagicMock()
        daemon._try_auto_snipe_spread = MagicMock()

        with (
            patch("monitors.daemons.arbitrage_daemon.send_qq_notification") as mock_qq,
            caplog.at_level(logging.ERROR),
        ):
            daemon.handle_spread_alert(alert)

        mock_qq.assert_not_called()
        daemon._try_auto_snipe_spread.assert_not_called()
        daemon.record_opportunity.assert_not_called()
        assert any("[PROFIT_ANOMALY]" in record.message for record in caplog.records)

    def test_try_auto_snipe_spread_rejects_profit_anomaly_1e20(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Direct call to _try_auto_snipe_spread with max_profit_usd=1e20 must be safely intercepted."""
        pool_buy = make_pool("01", "pool_buy", 30.0)
        pool_sell = make_pool("02", "pool_sell", 30.0)

        alert = SpreadAlert(
            base=USDG_ADDR,
            quote=WETH_ADDR,
            buy_pool=pool_buy,
            sell_pool=pool_sell,
            buy_price=0.00040,
            sell_price=0.00042,
            gross_spread_pct=5.0,
            total_fee_pct=0.6,
            net_spread_pct=4.4,
            max_profit_usd=1e20,
        )

        daemon = ArbitrageDaemon(mode="spread", auto_execute=True)
        mock_executor = MagicMock()
        daemon.executor = mock_executor

        with caplog.at_level(logging.ERROR):
            daemon._try_auto_snipe_spread(alert)

        mock_executor.plan_from_spread_alert.assert_not_called()
        mock_executor.execute.assert_not_called()
        assert any("[PROFIT_ANOMALY]" in record.message for record in caplog.records)

    def test_handle_spread_alert_rejects_moo_usdg_36pct_anomaly(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Construct SpreadAlert with sell/buy=1.367 (gross 36.65%), assert daemon safely discards it."""
        pool_buy = make_pool("01", "MOO / USDG 3.94% (uniswap-v4)", 394.0)
        pool_sell = make_pool("02", "MOO / USDG 10% (uniswap-v4)", 1000.0)

        alert = SpreadAlert(
            base=USDG_ADDR,
            quote=WETH_ADDR,
            buy_pool=pool_buy,
            sell_pool=pool_sell,
            buy_price=33.576286,
            sell_price=45.881253,
            gross_spread_pct=36.648,
            total_fee_pct=13.94,
            net_spread_pct=22.508,
            max_profit_usd=111.31,
            profit_at_500u=111.31,
        )

        daemon = ArbitrageDaemon(mode="spread", auto_execute=True)
        daemon.record_opportunity = MagicMock()
        daemon._try_auto_snipe_spread = MagicMock()

        with (
            patch("monitors.daemons.arbitrage_daemon.send_qq_notification") as mock_qq,
            caplog.at_level(logging.ERROR),
        ):
            daemon.handle_spread_alert(alert)

        mock_qq.assert_not_called()
        daemon._try_auto_snipe_spread.assert_not_called()
        daemon.record_opportunity.assert_not_called()
        assert any("[SPREAD_ANOMALY]" in record.message for record in caplog.records)

    def test_handle_spread_alert_rejects_price_anomaly_ratio(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Construct alert with sell/buy=1e30, assert handle_spread_alert discards it."""
        pool_buy = make_pool("01", "pool_buy", 30.0)
        pool_sell = make_pool("02", "pool_sell", 30.0)

        alert = SpreadAlert(
            base=USDG_ADDR,
            quote=WETH_ADDR,
            buy_pool=pool_buy,
            sell_pool=pool_sell,
            buy_price=1.0,
            sell_price=1e30,
            gross_spread_pct=5.0,
            total_fee_pct=0.6,
            net_spread_pct=4.4,
            max_profit_usd=10.0,
        )

        daemon = ArbitrageDaemon(mode="spread", auto_execute=True)
        daemon.record_opportunity = MagicMock()
        daemon._try_auto_snipe_spread = MagicMock()

        with (
            patch("monitors.daemons.arbitrage_daemon.send_qq_notification") as mock_qq,
            caplog.at_level(logging.ERROR),
        ):
            daemon.handle_spread_alert(alert)

        mock_qq.assert_not_called()
        daemon._try_auto_snipe_spread.assert_not_called()
        daemon.record_opportunity.assert_not_called()
        assert any("[SPREAD_ANOMALY]" in record.message for record in caplog.records)


class TestMultiplierValidationAndDataError:
    """3. plan_two_hop 与 plan_from_spread_alert 校验 expected_multiplier 与 amount_in_weth."""

    def test_plan_two_hop_expected_multiplier_below_1_raises_arbitrage_data_error(
        self,
        executor: WethArbitrageExecutor,
    ) -> None:
        """Construct expected_multiplier=0.5 in plan_two_hop, assert ArbitrageDataError is raised."""
        with pytest.raises(ArbitrageDataError, match="expected_multiplier"):
            executor.plan_two_hop(
                token_b=USDG_ADDR,
                pool1_fee=5,
                pool2_fee=30,
                expected_multiplier=0.5,
            )

    def test_plan_two_hop_expected_multiplier_one_raises_arbitrage_data_error(
        self,
        executor: WethArbitrageExecutor,
    ) -> None:
        """Construct expected_multiplier=1.0 in plan_two_hop, assert ArbitrageDataError is raised."""
        with pytest.raises(ArbitrageDataError, match="expected_multiplier"):
            executor.plan_two_hop(
                token_b=USDG_ADDR,
                pool1_fee=5,
                pool2_fee=30,
                expected_multiplier=1.0,
            )

    def test_plan_two_hop_zero_amount_in_weth_raises_arbitrage_data_error(
        self,
        executor: WethArbitrageExecutor,
    ) -> None:
        """Construct amount_in_weth=0 in plan_two_hop, assert ArbitrageDataError is raised."""
        with pytest.raises(ArbitrageDataError, match="amount_in_weth"):
            executor.plan_two_hop(
                token_b=USDG_ADDR,
                pool1_fee=5,
                pool2_fee=30,
                amount_in_weth=0,
                expected_multiplier=1.05,
            )

    def test_plan_from_spread_alert_expected_multiplier_below_1_raises_arbitrage_data_error(
        self,
        executor: WethArbitrageExecutor,
    ) -> None:
        """Construct alert where expected_multiplier < 1.0, assert plan_from_spread_alert raises ArbitrageDataError."""
        pool_buy = make_pool("01", "pool_buy", 500.0)  # high fee 5%
        pool_sell = make_pool("02", "pool_sell", 500.0)  # high fee 5%

        alert = SpreadAlert(
            base=USDG_ADDR,
            quote=WETH_ADDR,
            buy_pool=pool_buy,
            sell_pool=pool_sell,
            buy_price=100.0,
            sell_price=101.0,  # gross mult = 1.01, after 10% fee mult ~ 0.909 < 1.0
            gross_spread_pct=1.0,
            total_fee_pct=10.0,
            net_spread_pct=-9.0,
            max_profit_usd=5.0,
            profit_at_500u=0.0,
        )

        with pytest.raises(ArbitrageDataError, match="expected_multiplier"):
            executor.plan_from_spread_alert(alert, amount_usd=100.0)

    def test_plan_from_spread_alert_zero_amount_raises_arbitrage_data_error(
        self,
        executor: WethArbitrageExecutor,
    ) -> None:
        """Construct amount_in_weth=0, assert plan_from_spread_alert raises ArbitrageDataError."""
        pool_buy = make_pool("01", "pool_buy", 5.0)
        pool_sell = make_pool("02", "pool_sell", 30.0)

        alert = SpreadAlert(
            base=USDG_ADDR,
            quote=WETH_ADDR,
            buy_pool=pool_buy,
            sell_pool=pool_sell,
            buy_price=0.00040,
            sell_price=0.00042,
            gross_spread_pct=5.0,
            total_fee_pct=0.35,
            net_spread_pct=4.65,
            max_profit_usd=20.0,
            profit_at_500u=15.0,
        )

        with pytest.raises(ArbitrageDataError, match="amount_in_weth"):
            executor.plan_from_spread_alert(alert, amount_in_weth=0)

    def test_plan_from_spread_alert_profit_anomaly_raises_arbitrage_data_error(
        self,
        executor: WethArbitrageExecutor,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Construct alert with max_profit_usd=50000.0, assert plan_from_spread_alert raises ArbitrageDataError."""
        pool_buy = make_pool("01", "pool_buy", 5.0)
        pool_sell = make_pool("02", "pool_sell", 30.0)

        alert = SpreadAlert(
            base=USDG_ADDR,
            quote=WETH_ADDR,
            buy_pool=pool_buy,
            sell_pool=pool_sell,
            buy_price=0.00040,
            sell_price=0.00042,
            gross_spread_pct=5.0,
            total_fee_pct=0.35,
            net_spread_pct=4.65,
            max_profit_usd=50000.0,  # Anomaly!
            profit_at_500u=2000.0,
        )

        with caplog.at_level(logging.ERROR):
            with pytest.raises(ArbitrageDataError, match="PROFIT_ANOMALY"):
                executor.plan_from_spread_alert(alert, amount_usd=100.0)

        assert any("[PROFIT_ANOMALY]" in record.message for record in caplog.records)


class TestNormalTwoHopExecution:
    """4. 正常两跳套利机会仍能正常生成、构建计划并发射."""

    def test_normal_one_percent_spread_generates_and_fires(self, tmp_path, monkeypatch) -> None:
        """Construct normal 1% spread opportunity (gross 1.0%), assert passes find_spreads and fires."""
        from tests.receipt_fixture import make_receipt_fixture

        ctx = make_receipt_fixture(tmp_path, monkeypatch, base="WETH", triangle=False)

        pool_buy = make_pool("01", "pool_buy", 1.0)
        pool_sell = make_pool("02", "pool_sell", 5.0)

        quote_buy = make_quote(pool_buy, price=100.0)
        quote_sell = make_quote(pool_sell, price=101.0)  # gross = 1.0%, ratio = 1.01

        alerts = find_spreads([quote_buy, quote_sell], slippage_buffer_pct=0.2)
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.gross_spread_pct == pytest.approx(1.0)
        assert alert.sell_price / alert.buy_price == pytest.approx(1.01)

        daemon = ArbitrageDaemon(
            mode="spread", auto_execute=True, one_shot_probe=True, funds_runtime=ctx.runtime
        )
        daemon.running = True

        mock_executor = MagicMock(wraps=ctx.executor)
        mock_executor.funds_runtime = ctx.runtime
        mock_executor.weth_address = WETH_ADDR
        mock_executor.guard = MagicMock(wraps=ctx.executor.guard)
        key_mock = MagicMock()
        key_mock.read_text.return_value = "0x" + "a" * 64
        mock_executor.guard.check_private_key_file.return_value = key_mock
        mock_account = MagicMock()
        mock_account.address = ctx.wallet
        mock_executor.w3.eth.account.from_key.return_value = mock_account
        mock_executor.get_weth_balance.return_value = 10**18
        mock_executor.config = MagicMock(wraps=ctx.executor.config)
        mock_executor.config.PRIVATE_KEY_PATH = "/tmp/mock.key"
        mock_executor.plan_from_spread_alert = MagicMock(return_value=ctx.plan)
        daemon.executor = mock_executor

        with (
            patch.object(daemon, "_estimate_instant_gas_cost", return_value=(1_000_000_000, 0.10)),
            patch("monitors.daemons.arbitrage_daemon.send_qq_notification"),
            patch("monitors.daemons.arbitrage_daemon.notify_chain_auditor"),
        ):
            daemon.handle_spread_alert(alert)

        assert daemon.successful_snipes == 1
        mock_executor.plan_from_spread_alert.assert_called_once()
        mock_executor.execute.assert_called_once()

    def test_normal_two_hop_spread_generates_alert(self) -> None:
        """Verify normal two-hop spread (gross 1.5%, fee 0.6%) generates valid SpreadAlert."""
        pool_buy = make_pool("01", "pool_buy", 30.0)
        pool_sell = make_pool("02", "pool_sell", 30.0)

        quote_buy = make_quote(pool_buy, price=100.0)
        quote_sell = make_quote(pool_sell, price=101.5)

        alerts = find_spreads([quote_buy, quote_sell], slippage_buffer_pct=0.2)
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.gross_spread_pct == pytest.approx(1.5)
        assert alert.total_fee_pct == pytest.approx(0.8)  # 0.6% fee + 0.2% buffer
        assert alert.net_spread_pct == pytest.approx(0.7)
        assert alert.max_profit_usd <= 1000.0

    def test_normal_two_hop_plan_success(
        self,
        executor: WethArbitrageExecutor,
    ) -> None:
        """Verify normal SpreadAlert converts to valid ArbitragePlan with positive amounts."""
        pool_buy = make_pool("01", "pool_buy", 5.0)
        pool_sell = make_pool("02", "pool_sell", 30.0)

        alert = SpreadAlert(
            base=USDG_ADDR,
            quote=WETH_ADDR,
            buy_pool=pool_buy,
            sell_pool=pool_sell,
            buy_price=0.00040,
            sell_price=0.00042,
            gross_spread_pct=5.0,
            total_fee_pct=0.35,
            net_spread_pct=4.65,
            max_profit_usd=20.0,
            profit_at_500u=15.0,
        )

        plan = executor.plan_from_spread_alert(alert, amount_usd=100.0, slippage_pct=0.5)
        assert plan.expected_multiplier > 1.0
        assert plan.expected_amount_out > 0
        assert plan.amount_out_min > 0
        assert plan.amount_in_weth > 0
        assert len(plan.legs) == 2
        assert plan.legs[0].from_token == executor.weth_address
        assert plan.legs[-1].to_token == executor.weth_address

    def test_normal_two_hop_daemon_fires_successfully(self, tmp_path, monkeypatch) -> None:
        """Verify daemon successfully processes and fires a normal profitable two-hop alert."""
        from tests.receipt_fixture import make_receipt_fixture

        ctx = make_receipt_fixture(tmp_path, monkeypatch, base="WETH", triangle=False)

        pool_buy = make_pool("01", "pool_buy", 5.0)
        pool_sell = make_pool("02", "pool_sell", 30.0)

        alert = SpreadAlert(
            base=USDG_ADDR,
            quote=WETH_ADDR,
            buy_pool=pool_buy,
            sell_pool=pool_sell,
            buy_price=0.00040,
            sell_price=0.00042,
            gross_spread_pct=5.0,
            total_fee_pct=0.35,
            net_spread_pct=4.65,
            max_capacity_usd=1000.0,
            optimal_size_usd=500.0,
            max_profit_usd=25.0,
            profit_at_500u=20.0,
        )

        daemon = ArbitrageDaemon(
            mode="spread", auto_execute=True, one_shot_probe=True, funds_runtime=ctx.runtime
        )
        daemon.running = True

        mock_executor = MagicMock(wraps=ctx.executor)
        mock_executor.funds_runtime = ctx.runtime
        mock_executor.weth_address = WETH_ADDR
        mock_executor.guard = MagicMock(wraps=ctx.executor.guard)
        key_mock = MagicMock()
        key_mock.read_text.return_value = "0x" + "a" * 64
        mock_executor.guard.check_private_key_file.return_value = key_mock
        mock_account = MagicMock()
        mock_account.address = ctx.wallet
        mock_executor.w3.eth.account.from_key.return_value = mock_account
        mock_executor.get_weth_balance.return_value = 10**18  # ~2470 USD
        mock_executor.config = MagicMock(wraps=ctx.executor.config)
        mock_executor.config.PRIVATE_KEY_PATH = "/tmp/mock.key"
        mock_executor.plan_from_spread_alert = MagicMock(return_value=ctx.plan)
        daemon.executor = mock_executor

        sent_msgs: list[str] = []
        with (
            patch.object(daemon, "_estimate_instant_gas_cost", return_value=(1_000_000_000, 0.20)),
            patch(
                "monitors.daemons.arbitrage_daemon.send_qq_notification",
                side_effect=lambda m, _: sent_msgs.append(m),
            ),
            patch("monitors.daemons.arbitrage_daemon.notify_chain_auditor"),
        ):
            daemon.handle_spread_alert(alert)

        assert daemon.successful_snipes == 1
        assert daemon.consecutive_snipe_fails == 0
        mock_executor.plan_from_spread_alert.assert_called_once()
        mock_executor.execute.assert_called_once()
        assert any("Robinhood 跨池套利机会" in msg for msg in sent_msgs)
        assert any("Robinhood 实盘跨池套利成功" in msg for msg in sent_msgs)
