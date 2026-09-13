"""测试 arbitrage_daemon 的告警去重、状态记录及通知触发逻辑."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from arbitrage.spread_monitor import PoolSpec, PriceQuote, SpreadAlert
from arbitrage.triangular import SwapLeg, TriangularArbAlert
from execution.weth_arbitrage_executor import ArbitrageDataError
from monitors.daemons.arbitrage_daemon import (
    AlertDeduplicator,
    ArbitrageDaemon,
    notify_chain_auditor,
)


def test_deduplicator_cooldown() -> None:
    """测试套利告警冷却与防刷逻辑."""
    dedup = AlertDeduplicator(cooldown_seconds=10.0, profit_jump_pct=0.5)

    # 1. 首次发现放行
    assert dedup.should_notify_opportunity("key1", 0.5) is True

    # 2. 冷却期内且净利未显著跃升，拦截
    assert dedup.should_notify_opportunity("key1", 0.6) is False

    # 3. 净利出现明显跃升 (>= +0.5%)，放行
    assert dedup.should_notify_opportunity("key1", 1.1) is True

    # 4. 不同币对独立判定
    assert dedup.should_notify_opportunity("key2", 0.3) is True


def test_deduplicator_error_cooldown() -> None:
    """测试错误通知冷却."""
    dedup = AlertDeduplicator()

    # 首次报错放行
    assert dedup.should_notify_error("rpc_down", cooldown=100.0) is True

    # 冷却内拦截
    assert dedup.should_notify_error("rpc_down", cooldown=100.0) is False

    # 其它错误类型放行
    assert dedup.should_notify_error("pool_empty", cooldown=100.0) is True


def test_daemon_status_and_record(tmp_path: Path) -> None:
    """测试守护进程状态持久化与流水账本写入."""
    status_file = tmp_path / "status.json"
    opp_file = tmp_path / "opp.jsonl"

    daemon = ArbitrageDaemon(mode="all", min_tvl=100000.0)

    with (
        patch("monitors.daemons.arbitrage_daemon.STATUS_FILE", status_file),
        patch("monitors.daemons.arbitrage_daemon.OPPORTUNITIES_FILE", opp_file),
    ):
        daemon.update_status("running")
        assert status_file.exists()
        status_data = json.loads(status_file.read_text(encoding="utf-8"))
        assert status_data["status"] == "running"

        daemon.record_opportunity(
            opp_type="spread",
            summary="test opportunity",
            gross_pct=1.5,
            net_pct=1.0,
            details={"foo": "bar"},
        )
        assert opp_file.exists()
        lines = opp_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        opp_data = json.loads(lines[0])
        assert opp_data["type"] == "spread"
        assert opp_data["net_profit_pct"] == 1.0


def test_daemon_scan_round_trigger() -> None:
    """测试单轮扫描命中套利时的处理链路."""
    daemon = ArbitrageDaemon(mode="spread", min_tvl=100000.0)

    p1 = PoolSpec(
        address="0x1111111111111111111111111111111111111111",
        label="TokenA/TokenB 0.05%",
        fee_bps=5.0,
        token0="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        token1="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        dec0=18,
        dec1=18,
    )
    p2 = PoolSpec(
        address="0x2222222222222222222222222222222222222222",
        label="TokenA/TokenB 0.3%",
        fee_bps=30.0,
        token0="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        token1="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        dec0=18,
        dec1=18,
    )

    daemon.pools = [p1, p2]
    daemon.running = True

    # 模拟报价: p1 价格 100, p2 价格 105 (存在明显价差)
    q1 = PriceQuote(
        pool=p1,
        base=p1.token0,
        quote=p1.token1,
        price=100.0,
        raw_price_t1_per_t0=100.0,
        base_symbol="TokenA",
        quote_symbol="TokenB",
    )
    q2 = PriceQuote(
        pool=p2,
        base=p2.token0,
        quote=p2.token1,
        price=105.0,
        raw_price_t1_per_t0=105.0,
        base_symbol="TokenA",
        quote_symbol="TokenB",
    )

    daemon.reader = MagicMock()
    daemon.reader.batch_quote.return_value = [q1, q2]
    daemon.reader.quote.side_effect = [q1, q2]

    with (
        patch.object(daemon, "handle_spread_alert") as mock_spread_alert,
        patch.object(daemon, "update_status"),
    ):
        daemon.scan_round()
        assert mock_spread_alert.called


def make_mock_triangle_setup() -> tuple[TriangularArbAlert, PoolSpec, PoolSpec, PoolSpec]:
    """构造三跳套利告警与对应的三个测试流动性池规格."""
    pool_one = PoolSpec(
        address="0x" + "1" * 40,
        label="WETH/USDG 0.05%",
        fee_bps=5.0,
        token0="0x" + "a" * 40,
        token1="0x" + "b" * 40,
        dec0=18,
        dec1=18,
    )
    pool_two = PoolSpec(
        address="0x" + "2" * 40,
        label="USDG/PONS 0.05%",
        fee_bps=5.0,
        token0="0x" + "b" * 40,
        token1="0x" + "c" * 40,
        dec0=18,
        dec1=18,
    )
    pool_three = PoolSpec(
        address="0x" + "3" * 40,
        label="PONS/WETH 0.05%",
        fee_bps=5.0,
        token0="0x" + "c" * 40,
        token1="0x" + "a" * 40,
        dec0=18,
        dec1=18,
    )
    legs = [
        SwapLeg(
            from_token=pool_one.token0,
            to_token=pool_one.token1,
            pool=pool_one,
            rate=2500.0,
            fee_bps=5.0,
            effective_rate=2498.75,
            from_symbol="WETH",
            to_symbol="USDG",
        ),
        SwapLeg(
            from_token=pool_two.token0,
            to_token=pool_two.token1,
            pool=pool_two,
            rate=1.0,
            fee_bps=5.0,
            effective_rate=0.9995,
            from_symbol="USDG",
            to_symbol="PONS",
        ),
        SwapLeg(
            from_token=pool_three.token0,
            to_token=pool_three.token1,
            pool=pool_three,
            rate=0.00041,
            fee_bps=5.0,
            effective_rate=0.0004098,
            from_symbol="PONS",
            to_symbol="WETH",
        ),
    ]
    alert = TriangularArbAlert(
        start_token=pool_one.token0,
        cycle=("WETH", "USDG", "PONS", "WETH"),
        legs=legs,
        gross_multiplier=1.025,
        fee_multiplier=0.9985,
        expected_multiplier=1.0234,
        slippage_buffer_pct=0.6,
        net_multiplier=1.0174,
        gross_profit_pct=2.5,
        net_profit_pct=1.74,
        total_fee_pct=0.15,
        max_capacity_usd=1000.0,
        optimal_size_usd=500.0,
        max_profit_usd=8.7,
        profit_at_500u=8.7,
    )
    return alert, pool_one, pool_two, pool_three


def test_burst_chomp_multi_rounds_success(tmp_path, monkeypatch) -> None:
    """测试连续区块紧咬连击循环正常多次成交、战报推送与收工汇总汇报."""
    from tests.receipt_fixture import make_receipt_fixture

    ctx = make_receipt_fixture(tmp_path, monkeypatch, base="WETH", triangle=True)
    ctx.executor.broadcast_swap(
        tx={}, wallet_address=ctx.wallet, base_symbol="WETH", base_token=ctx.token, plan=ctx.plan
    )

    alert, pool_one, pool_two, pool_three = make_mock_triangle_setup()
    daemon = ArbitrageDaemon(
        mode="triangle", auto_execute=True, max_burst_rounds=2, funds_runtime=ctx.runtime
    )
    daemon.running = True

    quote_one = PriceQuote(
        pool=pool_one,
        base=pool_one.token0,
        quote=pool_one.token1,
        price=2500.0,
        raw_price_t1_per_t0=2500.0,
    )
    quote_two = PriceQuote(
        pool=pool_two,
        base=pool_two.token0,
        quote=pool_two.token1,
        price=1.0,
        raw_price_t1_per_t0=1.0,
    )
    quote_three = PriceQuote(
        pool=pool_three,
        base=pool_three.token0,
        quote=pool_three.token1,
        price=0.00041,
        raw_price_t1_per_t0=0.00041,
    )
    daemon.reader = MagicMock()
    daemon.reader.quote.side_effect = [quote_one, quote_two, quote_three] * 5

    mock_executor = MagicMock(wraps=ctx.executor)
    mock_executor.funds_runtime = ctx.runtime
    mock_executor.plan_from_triangular_alert.return_value = ctx.plan
    mock_executor.get_weth_balance.return_value = int(1e18)
    daemon.executor = mock_executor

    sent_notifications: list[str] = []

    def mock_send(message: str, target: str) -> bool:
        sent_notifications.append(message)
        return True

    with (
        patch("monitors.daemons.arbitrage_daemon.send_qq_notification", side_effect=mock_send),
        patch.object(daemon, "record_opportunity"),
    ):
        burst_rounds = daemon._run_burst_chomp(
            ta=alert,
            initial_profit_eth=0.001,
            initial_profit_usd=2.47,
            initial_snipes=1,
            wallet_addr="0x" + "e" * 40,
            weth_price_est=2470.0,
        )

        assert burst_rounds == 2
        assert mock_executor.execute.call_count == 2
        assert daemon.successful_snipes == 2

        assert any("连击吞噬 #2 成功" in item for item in sent_notifications)
        assert any("连击吞噬 #3 成功" in item for item in sent_notifications)
        assert any("紧咬吞噬战役收工汇总" in item for item in sent_notifications)
        summary_text = [item for item in sent_notifications if "紧咬吞噬战役收工汇总" in item][0]
        assert "连续连击次数: 3 次" in summary_text
        assert "已达到最大连击轮数 (2)" in summary_text


def test_burst_chomp_exit_on_spread_flattened() -> None:
    """测试当价差推平至门槛以下时安全停止连击."""
    alert, pool_one, pool_two, pool_three = make_mock_triangle_setup()
    daemon = ArbitrageDaemon(mode="triangle", auto_execute=True, min_profit_pct=0.2)
    daemon.running = True

    quote_one = PriceQuote(
        pool=pool_one,
        base=pool_one.token0,
        quote=pool_one.token1,
        price=2500.0,
        raw_price_t1_per_t0=2500.0,
    )
    quote_two = PriceQuote(
        pool=pool_two,
        base=pool_two.token0,
        quote=pool_two.token1,
        price=1.0,
        raw_price_t1_per_t0=1.0,
    )
    quote_three = PriceQuote(
        pool=pool_three,
        base=pool_three.token0,
        quote=pool_three.token1,
        price=0.000398,
        raw_price_t1_per_t0=0.000398,
    )
    daemon.reader = MagicMock()
    daemon.reader.quote.side_effect = [quote_one, quote_two, quote_three]

    mock_executor = MagicMock()
    daemon.executor = mock_executor

    sent_notifications: list[str] = []

    def record_notification(msg: str, target: str) -> bool:
        sent_notifications.append(msg)
        return True

    with patch(
        "monitors.daemons.arbitrage_daemon.send_qq_notification",
        side_effect=record_notification,
    ):
        burst_rounds = daemon._run_burst_chomp(ta=alert)
        assert burst_rounds == 0
        mock_executor.execute.assert_not_called()
        assert len(sent_notifications) == 0


def test_burst_chomp_exit_on_profit_eaten_by_gas() -> None:
    """测试当预期净利润低于瞬时 Gas 成本时安全退出."""
    alert, pool_one, pool_two, pool_three = make_mock_triangle_setup()
    daemon = ArbitrageDaemon(mode="triangle", auto_execute=True, min_profit_pct=0.01)
    daemon.running = True

    quote_one = PriceQuote(
        pool=pool_one,
        base=pool_one.token0,
        quote=pool_one.token1,
        price=2500.0,
        raw_price_t1_per_t0=2500.0,
    )
    quote_two = PriceQuote(
        pool=pool_two,
        base=pool_two.token0,
        quote=pool_two.token1,
        price=1.0,
        raw_price_t1_per_t0=1.0,
    )
    quote_three = PriceQuote(
        pool=pool_three,
        base=pool_three.token0,
        quote=pool_three.token1,
        price=0.00041,
        raw_price_t1_per_t0=0.00041,
    )
    daemon.reader = MagicMock()
    daemon.reader.quote.side_effect = [quote_one, quote_two, quote_three]

    mock_executor = MagicMock()
    mock_executor.get_weth_balance.return_value = int(1e18)
    daemon.executor = mock_executor

    with (
        patch.object(daemon, "_estimate_instant_gas_cost", return_value=(500_000_000_000, 50.0)),
        patch("monitors.daemons.arbitrage_daemon.send_qq_notification"),
    ):
        burst_rounds = daemon._run_burst_chomp(ta=alert)
        assert burst_rounds == 0
        mock_executor.execute.assert_not_called()


def test_burst_chomp_exit_on_atomic_revert(tmp_path, monkeypatch) -> None:
    """测试节点仿真 Revert 零损耗退出拦截."""
    from tests.receipt_fixture import make_receipt_fixture

    ctx = make_receipt_fixture(tmp_path, monkeypatch, base="WETH", triangle=True)
    ctx.executor.broadcast_swap(
        tx={}, wallet_address=ctx.wallet, base_symbol="WETH", base_token=ctx.token, plan=ctx.plan
    )

    alert, pool_one, pool_two, pool_three = make_mock_triangle_setup()
    daemon = ArbitrageDaemon(
        mode="triangle", auto_execute=True, max_burst_rounds=5, funds_runtime=ctx.runtime
    )
    daemon.running = True

    quote_one = PriceQuote(
        pool=pool_one,
        base=pool_one.token0,
        quote=pool_one.token1,
        price=2500.0,
        raw_price_t1_per_t0=2500.0,
    )
    quote_two = PriceQuote(
        pool=pool_two,
        base=pool_two.token0,
        quote=pool_two.token1,
        price=1.0,
        raw_price_t1_per_t0=1.0,
    )
    quote_three = PriceQuote(
        pool=pool_three,
        base=pool_three.token0,
        quote=pool_three.token1,
        price=0.00041,
        raw_price_t1_per_t0=0.00041,
    )
    daemon.reader = MagicMock()
    daemon.reader.quote.side_effect = [quote_one, quote_two, quote_three]

    mock_executor = MagicMock(wraps=ctx.executor)
    mock_executor.funds_runtime = ctx.runtime
    mock_executor.plan_from_triangular_alert.return_value = ctx.plan
    mock_executor.get_weth_balance.return_value = int(1e18)

    orig_request = ctx.request

    def revert_request(method, params):
        if (
            method == "eth_call"
            and params
            and params[0].get("to", "").lower() == ctx.profile["router"].lower()
        ):
            return {"error": {"code": 3, "data": "0x"}}
        return orig_request(method, params)

    ctx.runtime.verifier.request = revert_request
    daemon.executor = mock_executor

    with patch("monitors.daemons.arbitrage_daemon.send_qq_notification"):
        burst_rounds = daemon._run_burst_chomp(ta=alert)
        assert burst_rounds == 0
        assert mock_executor.execute.call_count == 1


def test_try_auto_snipe_triangle_enters_burst_chomp(tmp_path, monkeypatch) -> None:
    """测试首笔套利成功广播后直接进入连续紧咬吞噬模式."""
    from tests.receipt_fixture import make_receipt_fixture

    ctx = make_receipt_fixture(tmp_path, monkeypatch, base="WETH", gain=10**15, triangle=True)

    alert, _, _, _ = make_mock_triangle_setup()
    daemon = ArbitrageDaemon(mode="triangle", auto_execute=True, funds_runtime=ctx.runtime)
    daemon.running = True

    mock_executor = MagicMock(wraps=ctx.executor)
    mock_executor.funds_runtime = ctx.runtime
    mock_executor.weth_address = ctx.token
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
    mock_executor.plan_from_triangular_alert.return_value = ctx.plan
    daemon.executor = mock_executor

    with (
        patch("monitors.daemons.arbitrage_daemon.send_qq_notification"),
        patch.object(daemon, "_run_burst_chomp") as mock_burst,
    ):
        daemon._try_auto_snipe_triangle(alert)
        assert mock_burst.called
        assert mock_burst.call_args[1]["initial_snipes"] == 1
        assert mock_burst.call_args[1]["initial_profit_eth"] == 0.001


def test_notify_chain_auditor_records_event(tmp_path: Path) -> None:
    """测试 notify_chain_auditor 将事件持久化写入 arbitrage_events.jsonl."""
    test_events_file = tmp_path / "arbitrage_events.jsonl"
    event_payload = {
        "event_type": "TEST_EVENT",
        "time": "2026-09-08 08:30:00",
        "amount_usd": 100.0,
    }

    with patch("monitors.daemons.arbitrage_daemon.EVENTS_FILE", test_events_file):
        notify_chain_auditor(event_payload)

        assert test_events_file.exists()
        content = test_events_file.read_text(encoding="utf-8")
        assert "TEST_EVENT" in content


def make_mock_spread_setup() -> tuple[SpreadAlert, PoolSpec, PoolSpec]:
    """构造两跳跨池价差告警与对应的两个测试流动性池规格."""
    pool_buy = PoolSpec(
        address="0x" + "1" * 40,
        label="USDG/WETH 0.05%",
        fee_bps=5.0,
        token0="0x" + "a" * 40,
        token1="0x" + "b" * 40,
        dec0=18,
        dec1=18,
    )
    pool_sell = PoolSpec(
        address="0x" + "2" * 40,
        label="USDG/WETH 0.3%",
        fee_bps=30.0,
        token0="0x" + "a" * 40,
        token1="0x" + "b" * 40,
        dec0=18,
        dec1=18,
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
    return alert, pool_buy, pool_sell


def test_spread_alert_qq_notification_filtered_by_gas() -> None:
    """测试当预期净利被 Gas 成本吞噬时 (expected_net_usd <= 0)，QQ 推送被拦截."""
    alert, _, _ = make_mock_spread_setup()
    alert.net_spread_pct = 0.001
    alert.gross_spread_pct = alert.total_fee_pct + 0.001

    daemon = ArbitrageDaemon(mode="spread", auto_execute=False)

    with (
        patch.object(daemon, "_estimate_instant_gas_cost", return_value=(1_000_000_000, 0.20)),
        patch.object(daemon, "record_opportunity"),
        patch("monitors.daemons.arbitrage_daemon.send_qq_notification") as mock_send,
    ):
        daemon.handle_spread_alert(alert)
        mock_send.assert_not_called()


def test_spread_alert_qq_notification_sent_when_net_profitable() -> None:
    """测试当扣除 Gas 后净赚 (expected_net_usd > 0) 时，QQ 成功推送并展示 Gas 与净赚细节."""
    alert, _, _ = make_mock_spread_setup()
    alert.net_spread_pct = 2.0
    alert.gross_spread_pct = alert.total_fee_pct + 2.0

    daemon = ArbitrageDaemon(mode="spread", auto_execute=False)
    sent_msgs: list[str] = []

    def mock_send(msg: str, target: str) -> bool:
        sent_msgs.append(msg)
        return True

    with (
        patch.object(daemon, "_estimate_instant_gas_cost", return_value=(1_000_000_000, 0.20)),
        patch.object(daemon, "record_opportunity"),
        patch("monitors.daemons.arbitrage_daemon.send_qq_notification", side_effect=mock_send),
    ):
        daemon.handle_spread_alert(alert)
        assert len(sent_msgs) == 1
        msg = sent_msgs[0]
        assert "Robinhood 跨池套利机会" in msg
        assert "Gas 成本: -$0.20 USD" in msg
        assert "预计真实净到手: +" in msg


def test_triangle_alert_qq_notification_filtered_by_gas() -> None:
    """测试三角套利当预期利润低于实时 Gas 成本时，QQ 推送被拦截."""
    alert, _, _, _ = make_mock_triangle_setup()
    alert.net_profit_pct = 0.001
    alert.gross_profit_pct = alert.total_fee_pct + 0.001

    daemon = ArbitrageDaemon(mode="triangle", auto_execute=False)

    with (
        patch.object(daemon, "_estimate_instant_gas_cost", return_value=(1_000_000_000, 0.35)),
        patch.object(daemon, "record_opportunity"),
        patch("monitors.daemons.arbitrage_daemon.send_qq_notification") as mock_send,
    ):
        daemon.handle_triangle_alert(alert)
        mock_send.assert_not_called()


def test_triangle_alert_qq_notification_sent_when_net_profitable() -> None:
    """测试三角套利当扣除 Gas 净赚时，QQ 成功推送并包含 Gas 与净赚信息."""
    alert, _, _, _ = make_mock_triangle_setup()
    alert.net_profit_pct = 1.0
    alert.gross_profit_pct = alert.total_fee_pct + 1.0

    daemon = ArbitrageDaemon(mode="triangle", auto_execute=False)
    sent_msgs: list[str] = []

    def mock_send(msg: str, target: str) -> bool:
        sent_msgs.append(msg)
        return True

    with (
        patch.object(daemon, "_estimate_instant_gas_cost", return_value=(1_000_000_000, 0.35)),
        patch.object(daemon, "record_opportunity"),
        patch("monitors.daemons.arbitrage_daemon.send_qq_notification", side_effect=mock_send),
    ):
        daemon.handle_triangle_alert(alert)
        assert len(sent_msgs) == 1
        msg = sent_msgs[0]
        assert "Robinhood 三角套利闭环" in msg
        assert "Gas 成本: -$0.35 USD" in msg
        assert "预计真实净到手: +$3.65 USD" in msg


def test_try_auto_snipe_spread_success_and_one_shot_probe(tmp_path, monkeypatch) -> None:
    """测试两跳跨池实盘自动发射成功后触发 --one-shot-probe 挂起停机."""
    from tests.receipt_fixture import make_receipt_fixture

    ctx = make_receipt_fixture(tmp_path, monkeypatch, base="WETH", triangle=False)
    alert, _, _ = make_mock_spread_setup()
    daemon = ArbitrageDaemon(
        mode="spread", auto_execute=True, one_shot_probe=True, funds_runtime=ctx.runtime
    )
    daemon.running = True
    monkeypatch.setattr(
        daemon,
        "_execute_admitted_plan",
        lambda p: daemon.executor.execute(plan=p) if daemon.executor else {},
    )

    mock_executor = MagicMock()
    mock_executor.weth_address = "0x" + "b" * 40
    mock_executor.guard.check_private_key_file.return_value.read_text.return_value = "0x" + "f" * 64
    mock_account = MagicMock()
    mock_account.address = "0x" + "e" * 40
    mock_executor.w3.eth.account.from_key.return_value = mock_account
    mock_executor.get_weth_balance.return_value = int(1e18)
    mock_executor.config.PRIVATE_KEY_PATH = "/tmp/mock.key"
    mock_executor.plan_from_spread_alert.return_value = MagicMock()
    mock_executor.execute.return_value = {
        "status": "LIVE_EXECUTION_SUCCESS",
        "broadcast": {
            "tx_hash": "0xspreadtx123",
            "real_profit_eth": 0.0015,
            "block_number": 77777,
        },
    }
    daemon.executor = mock_executor

    sent_msgs: list[str] = []

    def mock_send(msg: str, target: str) -> bool:
        sent_msgs.append(msg)
        return True

    with (
        patch.object(daemon, "_estimate_instant_gas_cost", return_value=(1_000_000_000, 0.20)),
        patch("monitors.daemons.arbitrage_daemon.send_qq_notification", side_effect=mock_send),
        patch("monitors.daemons.arbitrage_daemon.notify_chain_auditor") as mock_auditor,
    ):
        daemon._try_auto_snipe_spread(alert)

        assert daemon.successful_snipes == 1
        assert not daemon.running
        mock_executor.plan_from_spread_alert.assert_called_once()
        mock_executor.execute.assert_called_once()
        assert any("Robinhood 实盘跨池套利成功" in m for m in sent_msgs)
        assert mock_auditor.called
        event = mock_auditor.call_args[0][0]
        assert event["event_type"] == "LIVE_EXECUTION_SUCCESS"
        assert event["tx_hash"] == "0xspreadtx123"


def test_try_auto_snipe_spread_gas_unprofitable_abort() -> None:
    """测试两跳跨池当亏损超出容忍上限时中止发射并记录审计日志."""
    alert, _, _ = make_mock_spread_setup()
    alert.net_spread_pct = -0.4
    alert.gross_spread_pct = 0.0
    daemon = ArbitrageDaemon(mode="spread", auto_execute=True)
    daemon.running = True

    mock_executor = MagicMock()
    mock_executor.weth_address = "0x" + "b" * 40
    mock_executor.guard.check_private_key_file.return_value.read_text.return_value = "0x" + "f" * 64
    mock_account = MagicMock()
    mock_account.address = "0x" + "e" * 40
    mock_executor.w3.eth.account.from_key.return_value = mock_account
    mock_executor.get_weth_balance.return_value = int(1e18)
    mock_executor.config.PRIVATE_KEY_PATH = "/tmp/mock.key"
    daemon.executor = mock_executor

    with (
        patch.object(daemon, "_estimate_instant_gas_cost", return_value=(5_000_000_000, 2.0)),
        patch("monitors.daemons.arbitrage_daemon.notify_chain_auditor") as mock_auditor,
    ):
        daemon._try_auto_snipe_spread(alert)

        mock_executor.execute.assert_not_called()
        assert mock_auditor.called
        event = mock_auditor.call_args[0][0]
        assert event["event_type"] == "GAS_UNPROFITABLE_ABORT"


def test_try_auto_snipe_spread_simulation_revert(tmp_path, monkeypatch) -> None:
    """测试节点仿真 Revert 零损耗拦截: consecutive_snipe_fails 不累加，且触发 SIMULATION_REVERTED 审计."""
    from tests.receipt_fixture import make_receipt_fixture

    ctx = make_receipt_fixture(tmp_path, monkeypatch, base="WETH", triangle=False)

    alert, _, _ = make_mock_spread_setup()
    daemon = ArbitrageDaemon(mode="spread", auto_execute=True, funds_runtime=ctx.runtime)
    daemon.running = True

    mock_executor = MagicMock(wraps=ctx.executor)
    mock_executor.funds_runtime = ctx.runtime
    mock_executor.weth_address = ctx.token
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
    mock_executor.plan_from_spread_alert.return_value = ctx.plan

    orig_request = ctx.request

    def revert_request(method, params):
        if (
            method == "eth_call"
            and params
            and params[0].get("to", "").lower() == ctx.profile["router"].lower()
        ):
            return {"error": {"code": 3, "data": "0x"}}
        return orig_request(method, params)

    ctx.runtime.verifier.request = revert_request
    daemon.executor = mock_executor

    with patch("monitors.daemons.arbitrage_daemon.notify_chain_auditor") as mock_auditor:
        daemon._try_auto_snipe_spread(alert)

        assert mock_auditor.called
        event = mock_auditor.call_args[0][0]
        assert event["event_type"] == "SIMULATION_REVERTED"
        assert daemon.consecutive_snipe_fails == 0


def test_try_auto_snipe_spread_circuit_breaker(tmp_path, monkeypatch) -> None:
    """测试连续发射失败达到上限时触发紧急熔断停机与 QQ 告警."""
    from tests.receipt_fixture import make_receipt_fixture

    ctx = make_receipt_fixture(tmp_path, monkeypatch, base="WETH", triangle=False)
    alert, _, _ = make_mock_spread_setup()
    daemon = ArbitrageDaemon(
        mode="spread",
        auto_execute=True,
        consecutive_fails_limit=2,
        funds_runtime=ctx.runtime,
    )
    daemon.running = True

    mock_executor = MagicMock()
    mock_executor.funds_runtime = ctx.runtime
    mock_executor.weth_address = "0x" + "b" * 40
    mock_executor.guard.check_private_key_file.return_value.read_text.return_value = "0x" + "f" * 64
    mock_account = MagicMock()
    mock_account.address = "0x" + "e" * 40
    mock_executor.w3.eth.account.from_key.return_value = mock_account
    mock_executor.get_weth_balance.return_value = int(1e18)
    mock_executor.config.PRIVATE_KEY_PATH = "/tmp/mock.key"
    mock_executor.plan_from_spread_alert.return_value = MagicMock()
    mock_executor.execute.side_effect = RuntimeError("RPC connection closed abruptly")
    daemon.executor = mock_executor

    sent_msgs: list[str] = []

    def mock_send(msg: str, target: str) -> bool:
        sent_msgs.append(msg)
        return True

    with patch("monitors.daemons.arbitrage_daemon.send_qq_notification", side_effect=mock_send):
        daemon._try_auto_snipe_spread(alert)
        assert daemon.consecutive_snipe_fails == 1
        assert bool(daemon.running)

        daemon._try_auto_snipe_spread(alert)
        assert daemon.consecutive_snipe_fails == 2
        assert not daemon.running
        assert any("紧急熔断停机" in m for m in sent_msgs)


def test_try_auto_snipe_spread_enters_burst_chomp(tmp_path, monkeypatch) -> None:
    """测试首笔两跳套利成功广播后直接进入连续紧咬吞噬模式."""
    from tests.receipt_fixture import make_receipt_fixture

    ctx = make_receipt_fixture(tmp_path, monkeypatch, base="WETH", gain=10**15, triangle=False)

    alert, _, _ = make_mock_spread_setup()
    daemon = ArbitrageDaemon(
        mode="spread", auto_execute=True, one_shot_probe=False, funds_runtime=ctx.runtime
    )
    daemon.running = True

    mock_executor = MagicMock(wraps=ctx.executor)
    mock_executor.funds_runtime = ctx.runtime
    mock_executor.weth_address = ctx.token
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
    mock_executor.plan_from_spread_alert.return_value = ctx.plan
    daemon.executor = mock_executor

    with (
        patch("monitors.daemons.arbitrage_daemon.send_qq_notification"),
        patch.object(daemon, "_run_burst_chomp_spread") as mock_burst,
    ):
        daemon._try_auto_snipe_spread(alert)
        assert mock_burst.called
        assert mock_burst.call_args[1]["initial_snipes"] == 1
        assert mock_burst.call_args[1]["initial_profit_eth"] == 0.001


def test_burst_chomp_spread_multi_rounds_success(tmp_path, monkeypatch) -> None:
    """测试两跳跨池紧咬连击循环多次成交、战报推送与收工汇总汇报."""
    from tests.receipt_fixture import make_receipt_fixture

    ctx = make_receipt_fixture(tmp_path, monkeypatch, base="WETH", triangle=False)
    ctx.executor.broadcast_swap(
        tx={}, wallet_address=ctx.wallet, base_symbol="WETH", base_token=ctx.token, plan=ctx.plan
    )

    alert, pool_buy, pool_sell = make_mock_spread_setup()
    daemon = ArbitrageDaemon(
        mode="spread", auto_execute=True, max_burst_rounds=2, funds_runtime=ctx.runtime
    )
    daemon.running = True

    quote_buy = PriceQuote(
        pool=pool_buy,
        base=alert.base,
        quote=alert.quote,
        price=0.00040,
        raw_price_t1_per_t0=0.00040,
    )
    quote_sell = PriceQuote(
        pool=pool_sell,
        base=alert.base,
        quote=alert.quote,
        price=0.00042,
        raw_price_t1_per_t0=0.00042,
    )
    daemon.reader = MagicMock()
    daemon.reader.quote.side_effect = [quote_buy, quote_sell] * 5

    mock_executor = MagicMock(wraps=ctx.executor)
    mock_executor.funds_runtime = ctx.runtime
    mock_executor.plan_from_spread_alert.return_value = ctx.plan
    mock_executor.get_weth_balance.return_value = int(1e18)
    daemon.executor = mock_executor

    sent_notifications: list[str] = []

    def mock_send(message: str, target: str) -> bool:
        sent_notifications.append(message)
        return True

    with (
        patch("monitors.daemons.arbitrage_daemon.send_qq_notification", side_effect=mock_send),
        patch.object(daemon, "record_opportunity"),
    ):
        burst_rounds = daemon._run_burst_chomp_spread(
            sa=alert,
            initial_profit_eth=0.001,
            initial_profit_usd=2.47,
            initial_snipes=1,
            wallet_addr="0x" + "e" * 40,
            weth_price_est=2470.0,
        )

        assert burst_rounds == 2
        assert mock_executor.execute.call_count == 2
        assert daemon.successful_snipes == 2

        assert any("跨池连击吞噬 #2 成功" in item for item in sent_notifications)
        assert any("跨池连击吞噬 #3 成功" in item for item in sent_notifications)
        assert any("跨池紧咬吞噬战役收工汇总" in item for item in sent_notifications)


def test_data_error_does_not_trigger_circuit_breaker() -> None:
    """测试连续 5 次数据/校验错误后 consecutive_snipe_fails 仍为 0 且守护进程未停机."""
    alert, _, _, _ = make_mock_triangle_setup()
    daemon = ArbitrageDaemon(mode="triangle", auto_execute=True, consecutive_fails_limit=3)
    daemon.running = True

    mock_executor = MagicMock()
    mock_executor.weth_address = "0x" + "b" * 40
    mock_executor.guard.check_private_key_file.return_value.read_text.return_value = "0x" + "f" * 64
    mock_account = MagicMock()
    mock_account.address = "0x" + "e" * 40
    mock_executor.w3.eth.account.from_key.return_value = mock_account
    mock_executor.get_weth_balance.return_value = int(1e18)
    mock_executor.config.PRIVATE_KEY_PATH = "/tmp/mock.key"
    mock_executor.plan_from_triangular_alert.side_effect = ArbitrageDataError(
        "[INVALID_TOKEN] 路径含零地址: 0x0000000000000000000000000000000000000000"
    )
    daemon.executor = mock_executor

    for _ in range(5):
        daemon._try_auto_snipe_triangle(alert)

    assert daemon.consecutive_snipe_fails == 0
    assert daemon.data_error_count == 5
    assert daemon.running is True


def test_spread_data_error_does_not_trigger_circuit_breaker() -> None:
    """测试两跳跨池连续 5 次数据错误后 consecutive_snipe_fails 仍为 0 且守护进程未停机."""
    alert, _, _ = make_mock_spread_setup()
    daemon = ArbitrageDaemon(mode="spread", auto_execute=True, consecutive_fails_limit=3)
    daemon.running = True

    mock_executor = MagicMock()
    mock_executor.weth_address = "0x" + "b" * 40
    mock_executor.guard.check_private_key_file.return_value.read_text.return_value = "0x" + "f" * 64
    mock_account = MagicMock()
    mock_account.address = "0x" + "e" * 40
    mock_executor.w3.eth.account.from_key.return_value = mock_account
    mock_executor.get_weth_balance.return_value = int(1e18)
    mock_executor.config.PRIVATE_KEY_PATH = "/tmp/mock.key"
    mock_executor.plan_from_spread_alert.side_effect = ValueError(
        "WETH (0x0bd7...) is not present in alert legs"
    )
    daemon.executor = mock_executor

    for _ in range(5):
        daemon._try_auto_snipe_spread(alert)

    assert daemon.consecutive_snipe_fails == 0
    assert daemon.data_error_count == 5
    assert daemon.running is True


def test_try_auto_snipe_triangle_on_chain_failure_circuit_breaker(tmp_path, monkeypatch) -> None:
    """测试三跳套利链上失败达到阈值仍会触发熔断停机 (保持原行为)."""
    from tests.receipt_fixture import make_receipt_fixture

    ctx = make_receipt_fixture(tmp_path, monkeypatch, base="WETH", triangle=True)
    alert, _, _, _ = make_mock_triangle_setup()
    daemon = ArbitrageDaemon(
        mode="triangle",
        auto_execute=True,
        consecutive_fails_limit=2,
        funds_runtime=ctx.runtime,
    )
    daemon.running = True

    mock_executor = MagicMock()
    mock_executor.funds_runtime = ctx.runtime
    mock_executor.weth_address = "0x" + "b" * 40
    mock_executor.guard.check_private_key_file.return_value.read_text.return_value = "0x" + "f" * 64
    mock_account = MagicMock()
    mock_account.address = "0x" + "e" * 40
    mock_executor.w3.eth.account.from_key.return_value = mock_account
    mock_executor.get_weth_balance.return_value = int(1e18)
    mock_executor.config.PRIVATE_KEY_PATH = "/tmp/mock.key"
    mock_executor.plan_from_triangular_alert.return_value = MagicMock()
    mock_executor.execute.side_effect = RuntimeError("Broadcast failed: nonce too low")
    daemon.executor = mock_executor

    sent_msgs: list[str] = []

    def mock_send(msg: str, target: str) -> bool:
        sent_msgs.append(msg)
        return True

    with patch("monitors.daemons.arbitrage_daemon.send_qq_notification", side_effect=mock_send):
        daemon._try_auto_snipe_triangle(alert)
        assert daemon.consecutive_snipe_fails == 1
        assert bool(daemon.running)

        daemon._try_auto_snipe_triangle(alert)
        assert daemon.consecutive_snipe_fails == 2
        assert not daemon.running
        assert any("紧急熔断停机" in m for m in sent_msgs)


def test_try_auto_snipe_spread_concurrent_lock(tmp_path, monkeypatch) -> None:
    """Keep two serialized NORMAL calls; all admission/receipt/ledger checks run."""
    from tests.receipt_fixture import make_receipt_fixture

    ctx = make_receipt_fixture(tmp_path, monkeypatch, base="WETH")
    ctx.executor.broadcast_swap(
        tx={}, wallet_address=ctx.wallet, base_symbol="WETH", base_token=ctx.token, plan=ctx.plan
    )
    assert ctx.ledger.status(4663, ctx.wallet, "WETH")["mode"] == "NORMAL"
    ctx.w3.eth.account.sign_transaction.reset_mock()
    ctx.w3.eth.send_raw_transaction.reset_mock()
    alert, _, _ = make_mock_spread_setup()
    daemon = ArbitrageDaemon(
        mode="spread", auto_execute=False, one_shot_probe=False, funds_runtime=ctx.runtime
    )
    daemon.running = daemon.auto_execute = True
    mock_executor = ctx.executor
    daemon.executor = mock_executor
    mock_executor.plan_from_spread_alert = MagicMock(return_value=ctx.plan)
    real_execute = mock_executor.execute
    active_executions = 0
    max_concurrent_executions = 0
    lock = threading.Lock()

    def slow_execute(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal active_executions, max_concurrent_executions
        with lock:
            active_executions += 1
            max_concurrent_executions = max(max_concurrent_executions, active_executions)
        try:
            time.sleep(0.05)
            return real_execute(*args, **kwargs)
        finally:
            with lock:
                active_executions -= 1

    mock_executor.execute = MagicMock(side_effect=slow_execute)
    with (
        patch.object(daemon, "_estimate_instant_gas_cost", return_value=(1_000_000_000, 0.20)),
        patch("monitors.daemons.arbitrage_daemon.send_qq_notification"),
        patch("monitors.daemons.arbitrage_daemon.notify_chain_auditor"),
        patch.object(daemon, "_run_burst_chomp_spread"),
    ):
        t1 = threading.Thread(target=daemon._try_auto_snipe_spread, args=(alert,))
        t2 = threading.Thread(target=daemon._try_auto_snipe_spread, args=(alert,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

    assert max_concurrent_executions == 1, "互斥锁未生效，两个线程同时进入了开火执行流程"
    assert mock_executor.execute.call_count == 2
    assert ctx.w3.eth.send_raw_transaction.call_count == 2
    assert ctx.w3.eth.account.sign_transaction.call_count == 2
    assert ctx.ledger.status(4663, ctx.wallet, "WETH")["mode"] == "NORMAL"


@pytest.fixture
def _low_gas_reserve_fixture(tmp_path, monkeypatch):
    from tests.receipt_fixture import make_receipt_fixture

    ctx = make_receipt_fixture(tmp_path, monkeypatch, base="WETH", triangle=False)
    yield ctx
    assert ctx.w3.eth.account.sign_transaction.call_count == 1
    assert ctx.w3.eth.send_raw_transaction.call_count == 1


def test_try_auto_snipe_spread_low_gas_reserve_intercept(_low_gas_reserve_fixture) -> None:
    """测试当原生 ETH 余额低于 0.003 ETH 时，自动开火被拦截并触发 low_gas_reserve 去重告警."""
    ctx = _low_gas_reserve_fixture
    alert, _, _ = make_mock_spread_setup()
    daemon = ArbitrageDaemon(mode="spread", auto_execute=True, funds_runtime=ctx.runtime)
    daemon.running = True

    mock_executor = MagicMock(wraps=ctx.executor)
    mock_executor.funds_runtime = ctx.runtime
    mock_executor.weth_address = ctx.token
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
    mock_executor.plan_from_spread_alert.return_value = ctx.plan

    # 原生 ETH 余额设为 0.002 ETH (< 0.003 ETH)
    mock_executor.get_eth_balance.return_value = int(0.002 * 1e18)
    daemon.executor = mock_executor

    sent_msgs: list[str] = []

    def mock_send(msg: str, target: str) -> bool:
        sent_msgs.append(msg)
        return True

    with (
        patch.object(daemon, "_estimate_instant_gas_cost", return_value=(1_000_000_000, 0.20)),
        patch("monitors.daemons.arbitrage_daemon.send_qq_notification", side_effect=mock_send),
    ):
        # 第一次触发开火，应拦截并发送低水位告警
        daemon._try_auto_snipe_spread(alert)
        assert mock_executor.execute.call_count == 0, "低水位下不应执行开火"
        assert len(sent_msgs) == 1
        assert "低水位告警" in sent_msgs[0]

        # 第二次触发开火，应拦截但因去重机制不再重复发告警
        daemon._try_auto_snipe_spread(alert)
        assert mock_executor.execute.call_count == 0
        assert len(sent_msgs) == 1, "低水位告警未被去重机制拦截"

        # 余额补充到 0.005 ETH (>= 0.003 ETH) 后重新开火
        mock_executor.get_eth_balance.return_value = int(0.005 * 1e18)
        with patch.object(daemon, "_run_burst_chomp_spread"):
            daemon._try_auto_snipe_spread(alert)
        assert mock_executor.execute.call_count == 1, "恢复正常水位后应放行开火"


def test_try_auto_snipe_triangle_low_gas_reserve_intercept() -> None:
    """测试三跳闭环在原生 ETH 余额低于 0.003 ETH 时被拦截并去重告警."""
    alert, _, _, _ = make_mock_triangle_setup()
    daemon = ArbitrageDaemon(mode="triangle", auto_execute=True)
    daemon.running = True

    mock_executor = MagicMock()
    mock_executor.weth_address = alert.start_token
    mock_executor.guard.check_private_key_file.return_value.read_text.return_value = "0x" + "f" * 64
    mock_account = MagicMock()
    mock_account.address = "0x" + "e" * 40
    mock_executor.w3.eth.account.from_key.return_value = mock_account
    mock_executor.get_weth_balance.return_value = int(1e18)
    mock_executor.config.PRIVATE_KEY_PATH = "/tmp/mock.key"
    mock_executor.plan_from_triangular_alert.return_value = MagicMock()
    mock_executor.get_eth_balance.return_value = int(0.001 * 1e18)
    daemon.executor = mock_executor

    sent_msgs: list[str] = []

    def mock_send(msg: str, target: str) -> bool:
        sent_msgs.append(msg)
        return True

    with (
        patch.object(daemon, "_estimate_instant_gas_cost", return_value=(1_000_000_000, 0.20)),
        patch("monitors.daemons.arbitrage_daemon.send_qq_notification", side_effect=mock_send),
    ):
        daemon._try_auto_snipe_triangle(alert)
        assert mock_executor.execute.call_count == 0
        assert len(sent_msgs) == 1
        assert "低水位告警" in sent_msgs[0]

        daemon._try_auto_snipe_triangle(alert)
        assert mock_executor.execute.call_count == 0
        assert len(sent_msgs) == 1


def test_probe_touched_pools_debounce() -> None:
    """测试 _probe_touched_pools 去抖生效：0.3 秒内重复传入相同池子，第二次不触发 reader.quote."""
    daemon = ArbitrageDaemon(mode="spread", auto_execute=False)
    daemon.running = True

    pool_addr = "0x1111111111111111111111111111111111111111"
    mock_pool = MagicMock()
    mock_pool.address = pool_addr
    mock_pool.token0 = "0x2222222222222222222222222222222222222222"
    mock_pool.token1 = "0x3333333333333333333333333333333333333333"
    mock_pool.label = "MockDebouncePool"
    daemon.pools = [mock_pool]

    mock_quote = MagicMock()
    mock_quote.price = 100.0
    with patch.object(daemon.reader, "quote", return_value=mock_quote) as mock_quote_fn:
        # 首次探测：应该触发 reader.quote
        daemon._probe_touched_pools([pool_addr])
        assert mock_quote_fn.call_count == 1

        # 0.3 秒内立即重复传入相同池子：去抖生效，不触发 reader.quote
        daemon._probe_touched_pools([pool_addr])
        assert mock_quote_fn.call_count == 1, "去抖未生效，重复触发了 reader.quote"

        # 模拟经过 0.35 秒后再次探测：去抖窗口已过，应再次触发 reader.quote
        daemon._pool_last_probe_time[pool_addr.lower()] -= 0.35
        daemon._probe_touched_pools([pool_addr])
        assert mock_quote_fn.call_count == 2


def make_mock_usdg_triangle_setup() -> tuple[TriangularArbAlert, PoolSpec, PoolSpec, PoolSpec]:
    """构造 USDG 本位闭环三跳套利告警与规格."""
    usdg_addr = "0x" + "a" * 40
    tok1_addr = "0x" + "b" * 40
    tok2_addr = "0x" + "c" * 40

    p1 = PoolSpec(
        address="0x" + "1" * 40,
        label="USDG/PONS 0.05%",
        fee_bps=5.0,
        token0=usdg_addr,
        token1=tok1_addr,
        dec0=6,
        dec1=18,
    )
    p2 = PoolSpec(
        address="0x" + "2" * 40,
        label="PONS/TOKEN_C 0.05%",
        fee_bps=5.0,
        token0=tok1_addr,
        token1=tok2_addr,
        dec0=18,
        dec1=18,
    )
    p3 = PoolSpec(
        address="0x" + "3" * 40,
        label="TOKEN_C/USDG 0.05%",
        fee_bps=5.0,
        token0=tok2_addr,
        token1=usdg_addr,
        dec0=18,
        dec1=6,
    )

    legs = [
        SwapLeg(
            from_token=usdg_addr,
            to_token=tok1_addr,
            pool=p1,
            rate=2.0,
            fee_bps=5.0,
            effective_rate=1.999,
            from_symbol="USDG",
            to_symbol="PONS",
        ),
        SwapLeg(
            from_token=tok1_addr,
            to_token=tok2_addr,
            pool=p2,
            rate=0.5,
            fee_bps=5.0,
            effective_rate=0.49975,
            from_symbol="PONS",
            to_symbol="TOKEN_C",
        ),
        SwapLeg(
            from_token=tok2_addr,
            to_token=usdg_addr,
            pool=p3,
            rate=1.05,
            fee_bps=5.0,
            effective_rate=1.049475,
            from_symbol="TOKEN_C",
            to_symbol="USDG",
        ),
    ]
    alert = TriangularArbAlert(
        start_token=usdg_addr,
        cycle=("USDG", "PONS", tok2_addr, "USDG"),
        legs=legs,
        gross_multiplier=1.05,
        fee_multiplier=0.9985,
        expected_multiplier=1.05 * 0.9985,
        slippage_buffer_pct=0.5,
        net_multiplier=1.045,
        gross_profit_pct=5.0,
        net_profit_pct=4.85,
        total_fee_pct=0.15,
        optimal_size_usd=100.0,
        max_capacity_usd=1000.0,
        max_profit_usd=4.85,
        profit_at_500u=24.25,
        bottleneck_tvl=50000.0,
    )
    return alert, p1, p2, p3


def test_usdg_auto_snipe_triangle_probe_and_scale_up(tmp_path, monkeypatch) -> None:
    """测试 USDG 三跳套利自动识别、探路者 10 USDG 初始额度及盈利后 scaled_up 置 True."""
    from tests.receipt_fixture import make_receipt_fixture

    ctx = make_receipt_fixture(tmp_path, monkeypatch, base="USDG", triangle=True)

    alert, _, _, _ = make_mock_usdg_triangle_setup()
    daemon = ArbitrageDaemon(
        mode="triangle", auto_execute=True, initial_amount_usd=10.0, funds_runtime=ctx.runtime
    )
    daemon.running = True
    daemon.scaled_up = False

    mock_executor = MagicMock(wraps=ctx.executor)
    mock_executor.funds_runtime = ctx.runtime
    mock_executor.weth_address = ctx.profile["transfer_tokens"][1]
    mock_executor.usdg_address = ctx.token
    mock_executor.guard = MagicMock(wraps=ctx.executor.guard)
    key_mock = MagicMock()
    key_mock.read_text.return_value = "0x" + "1" * 64
    mock_executor.guard.check_private_key_file.return_value = key_mock
    mock_account = MagicMock()
    mock_account.address = ctx.wallet
    mock_executor.w3.eth.account.from_key.return_value = mock_account
    mock_executor.get_usdg_balance.return_value = int(100 * 1e6)  # 100 USDG
    mock_executor.get_eth_balance.return_value = int(0.01 * 1e18)
    mock_executor.config = MagicMock(wraps=ctx.executor.config)
    mock_executor.config.PRIVATE_KEY_PATH = "/tmp/mock.key"
    mock_executor.plan_from_triangular_alert = MagicMock(return_value=ctx.plan)
    daemon.executor = mock_executor

    with (
        patch.object(daemon, "_estimate_instant_gas_cost", return_value=(1_000_000_000, 0.10)),
        patch.object(daemon, "_run_burst_chomp") as mock_burst,
        patch("monitors.daemons.arbitrage_daemon.send_qq_notification"),
    ):
        daemon._try_auto_snipe_triangle(alert)

    # 验证初始下单金额为 10.0 USDG (首单探路)，且指定 base_token='USDG'
    assert mock_executor.plan_from_triangular_alert.call_count == 1
    call_kwargs = mock_executor.plan_from_triangular_alert.call_args.kwargs
    assert call_kwargs["base_token"] == "USDG"
    assert call_kwargs["amount_usd"] == 10.0

    # 验证盈利后成功解锁 scaled_up，触发后续连击
    assert daemon.scaled_up is True
    assert daemon.successful_snipes == 1
    assert daemon.consecutive_snipe_fails == 0
    assert daemon.running is True
    assert mock_burst.called


def test_usdg_pathfinder_loss_tolerated_does_not_halt_daemon(tmp_path, monkeypatch) -> None:
    """测试 USDG 探路者首单亏损 <= 1.0 USD 时记录 PATHFINDER_LOSS_TOLERATED 且不触发停机熔断."""
    from tests.receipt_fixture import make_receipt_fixture

    ctx = make_receipt_fixture(tmp_path, monkeypatch, base="USDG", triangle=True)
    alert, _, _, _ = make_mock_usdg_triangle_setup()
    daemon = ArbitrageDaemon(
        mode="triangle", auto_execute=True, one_shot_probe=False, funds_runtime=ctx.runtime
    )
    daemon.running = True
    daemon.scaled_up = False
    monkeypatch.setattr(
        daemon,
        "_execute_admitted_plan",
        lambda p: daemon.executor.execute(plan=p) if daemon.executor else {},
    )

    mock_executor = MagicMock()
    mock_executor.weth_address = "0x" + "f" * 40
    mock_executor.usdg_address = alert.start_token
    mock_executor.guard.check_private_key_file.return_value.read_text.return_value = "0x" + "1" * 64
    mock_account = MagicMock()
    mock_account.address = "0x" + "9" * 40
    mock_executor.w3.eth.account.from_key.return_value = mock_account
    mock_executor.get_usdg_balance.return_value = int(100 * 1e6)
    mock_executor.get_eth_balance.return_value = int(0.01 * 1e18)
    mock_executor.config.PRIVATE_KEY_PATH = "/tmp/mock.key"
    mock_executor.plan_from_triangular_alert.return_value = MagicMock()
    mock_executor.execute.return_value = {
        "status": "LIVE_EXECUTION_SUCCESS",
        "broadcast": {
            "tx_hash": "0xloss_tolerated",
            "real_profit_usdg": -0.50,
            "real_profit_usd": -0.50,
            "block_number": 9999,
        },
    }
    daemon.executor = mock_executor

    events_recorded: list[dict[str, Any]] = []
    with (
        patch.object(daemon, "_estimate_instant_gas_cost", return_value=(1_000_000_000, 0.10)),
        patch(
            "monitors.daemons.arbitrage_daemon.notify_chain_auditor",
            side_effect=events_recorded.append,
        ),
        patch("monitors.daemons.arbitrage_daemon.send_qq_notification"),
    ):
        daemon._try_auto_snipe_triangle(alert)

    # 验证容忍亏损：未熔断停机，失败计数器未累加
    assert daemon.running is True
    assert daemon.consecutive_snipe_fails == 0
    assert not daemon.scaled_up

    # 验证审计事件记录为 PATHFINDER_LOSS_TOLERATED
    assert any(e.get("event_type") == "PATHFINDER_LOSS_TOLERATED" for e in events_recorded)


def test_usdg_pathfinder_excessive_loss_triggers_emergency_breaker(tmp_path, monkeypatch) -> None:
    """测试 USDG 探路者首单亏损 > 1.0 USD 时触发紧急熔断停机 (EMERGENCY_BREAKER_TRIGGERED)."""
    from tests.receipt_fixture import make_receipt_fixture

    ctx = make_receipt_fixture(tmp_path, monkeypatch, base="USDG", triangle=True)
    alert, _, _, _ = make_mock_usdg_triangle_setup()
    daemon = ArbitrageDaemon(
        mode="triangle", auto_execute=True, one_shot_probe=False, funds_runtime=ctx.runtime
    )
    daemon.running = True
    daemon.scaled_up = False
    monkeypatch.setattr(
        daemon,
        "_execute_admitted_plan",
        lambda p: daemon.executor.execute(plan=p) if daemon.executor else {},
    )

    mock_executor = MagicMock()
    mock_executor.weth_address = "0x" + "f" * 40
    mock_executor.usdg_address = alert.start_token
    mock_executor.guard.check_private_key_file.return_value.read_text.return_value = "0x" + "1" * 64
    mock_account = MagicMock()
    mock_account.address = "0x" + "9" * 40
    mock_executor.w3.eth.account.from_key.return_value = mock_account
    mock_executor.get_usdg_balance.return_value = int(100 * 1e6)
    mock_executor.get_eth_balance.return_value = int(0.01 * 1e18)
    mock_executor.config.PRIVATE_KEY_PATH = "/tmp/mock.key"
    mock_executor.plan_from_triangular_alert.return_value = MagicMock()
    mock_executor.execute.return_value = {
        "status": "LIVE_EXECUTION_SUCCESS",
        "broadcast": {
            "tx_hash": "0xloss_excessive",
            "real_profit_usdg": -1.50,
            "real_profit_usd": -1.50,
            "block_number": 9999,
        },
    }
    daemon.executor = mock_executor

    events_recorded: list[dict[str, Any]] = []
    sent_msgs: list[str] = []
    with (
        patch.object(daemon, "_estimate_instant_gas_cost", return_value=(1_000_000_000, 0.10)),
        patch(
            "monitors.daemons.arbitrage_daemon.notify_chain_auditor",
            side_effect=events_recorded.append,
        ),
        patch(
            "monitors.daemons.arbitrage_daemon.send_qq_notification",
            side_effect=lambda msg, t: sent_msgs.append(msg),
        ),
    ):
        daemon._try_auto_snipe_triangle(alert)

    # 验证超限熔断停机
    assert daemon.running is False
    assert daemon.consecutive_snipe_fails == 1
    assert any(e.get("event_type") == "EMERGENCY_BREAKER_TRIGGERED" for e in events_recorded)
    assert any("紧急熔断停机" in msg for msg in sent_msgs)


def test_usdg_auto_snipe_spread_probe_and_scale_up(tmp_path, monkeypatch) -> None:
    """测试 USDG 两跳跨池套利自动识别、探路首单及盈利解锁."""
    from tests.receipt_fixture import make_receipt_fixture

    ctx = make_receipt_fixture(tmp_path, monkeypatch, base="USDG", triangle=False)

    p1 = PoolSpec(
        address="0x" + "1" * 40,
        label="USDG/PONS 0.05%",
        fee_bps=5.0,
        token0=ctx.token,
        token1=ctx.profile["transfer_tokens"][1],
    )
    p2 = PoolSpec(
        address="0x" + "2" * 40,
        label="USDG/PONS 0.3%",
        fee_bps=30.0,
        token0=ctx.token,
        token1=ctx.profile["transfer_tokens"][1],
    )

    alert = SpreadAlert(
        base=ctx.token,
        quote=ctx.profile["transfer_tokens"][1],
        buy_pool=p1,
        sell_pool=p2,
        buy_price=2.0,
        sell_price=2.1,
        gross_spread_pct=5.0,
        total_fee_pct=0.35,
        net_spread_pct=4.65,
        optimal_size_usd=100.0,
        max_capacity_usd=1000.0,
        max_profit_usd=4.65,
        profit_at_500u=23.25,
        bottleneck_tvl=50000.0,
    )
    alert.base_token = "USDG"

    daemon = ArbitrageDaemon(
        mode="spread", auto_execute=True, initial_amount_usd=10.0, funds_runtime=ctx.runtime
    )
    daemon.running = True
    daemon.scaled_up = False

    mock_executor = MagicMock(wraps=ctx.executor)
    mock_executor.funds_runtime = ctx.runtime
    mock_executor.weth_address = ctx.profile["transfer_tokens"][1]
    mock_executor.usdg_address = ctx.token
    mock_executor.guard = MagicMock(wraps=ctx.executor.guard)
    key_mock = MagicMock()
    key_mock.read_text.return_value = "0x" + "1" * 64
    mock_executor.guard.check_private_key_file.return_value = key_mock
    mock_account = MagicMock()
    mock_account.address = ctx.wallet
    mock_executor.w3.eth.account.from_key.return_value = mock_account
    mock_executor.get_usdg_balance.return_value = int(100 * 1e6)  # 100 USDG
    mock_executor.get_eth_balance.return_value = int(0.01 * 1e18)
    mock_executor.config = MagicMock(wraps=ctx.executor.config)
    mock_executor.config.PRIVATE_KEY_PATH = "/tmp/mock.key"
    mock_executor.plan_from_spread_alert = MagicMock(return_value=ctx.plan)
    daemon.executor = mock_executor

    with (
        patch.object(daemon, "_estimate_instant_gas_cost", return_value=(1_000_000_000, 0.10)),
        patch.object(daemon, "_run_burst_chomp_spread") as mock_burst,
        patch("monitors.daemons.arbitrage_daemon.send_qq_notification"),
    ):
        daemon._try_auto_snipe_spread(alert)

    assert mock_executor.plan_from_spread_alert.call_count == 1
    call_kwargs = mock_executor.plan_from_spread_alert.call_args.kwargs
    assert call_kwargs["base_token"] == "USDG"
    assert call_kwargs["amount_usd"] == 10.0
    assert daemon.scaled_up is True
    assert daemon.running is True
    assert mock_burst.called


@pytest.fixture(autouse=True)
def _candidate_public_preflight_inputs(request, tmp_path, monkeypatch):
    """Five named pre-send tests get explicit real runtime inputs; assertions unchanged."""
    names = {
        "test_spread_data_error_does_not_trigger_circuit_breaker",
        "test_data_error_does_not_trigger_circuit_breaker",
        "test_try_auto_snipe_spread_gas_unprofitable_abort",
        "test_try_auto_snipe_spread_low_gas_reserve_intercept",
        "test_try_auto_snipe_triangle_low_gas_reserve_intercept",
    }
    if request.node.name not in names:
        yield
        return
    if request.node.name == "test_try_auto_snipe_spread_low_gas_reserve_intercept":
        # Scoped to explicit test-local fixture for recovered send
        yield
        return
    from tests.receipt_fixture import make_receipt_fixture

    ctx = make_receipt_fixture(tmp_path, monkeypatch, base="WETH")
    original_init = ArbitrageDaemon.__init__

    def initialized(self, *args, **kwargs):
        kwargs["funds_runtime"] = ctx.runtime
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(ArbitrageDaemon, "__init__", initialized)
    yield
    ctx.w3.eth.account.sign_transaction.assert_not_called()
    ctx.w3.eth.send_raw_transaction.assert_not_called()
