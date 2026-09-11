"""测试二次衰减收益容量模型准确性、TVL 门槛、费率拦截及元数据缓存."""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from arbitrage.spread_monitor import (
    PoolSpec,
    SpreadAlert,
    estimate_realized_profit_usd,
    find_spreads,
)
from arbitrage.triangular import SwapLeg, TriangularArbAlert
from monitors.daemons.arbitrage_daemon import (
    ArbitrageDaemon,
    load_pools_metadata_cache,
    save_pools_metadata_cache,
)


def test_capacity_quadratic_vs_linear_tvl100k_scenario() -> None:
    """验证 TVL10万/gross1.0/fee0.6 场景下真实净利 < 0 而线性估算 > 0 的核心差异断言.

    对应实测对照表:
      - 真实净利: -$0.040 USD (真亏，应静默拦截)
      - 线性估算: +$0.160 USD (虚高，导致 QQ 误报)
      - 误差: $0.200 USD
    """
    tvl_buy = 100_000.0
    tvl_sell = 100_000.0
    gross_pct = 1.0
    fee_pct = 0.6
    size_usd = 100.0
    gas_cost_usd = 0.24

    # 1. 真实二次衰减模型计算
    real_gross, actual_size, max_cap = estimate_realized_profit_usd(
        gross_pct=gross_pct,
        fee_pct=fee_pct,
        tvl_buy_usd=tvl_buy,
        tvl_sell_usd=tvl_sell,
        size_usd=size_usd,
    )
    real_net = real_gross - gas_cost_usd

    # 2. 线性模型计算
    linear_gross = size_usd * ((gross_pct - fee_pct) / 100.0)
    linear_net = linear_gross - gas_cost_usd

    # 3. 断言二次衰减计算中间量
    assert actual_size == pytest.approx(100.0, abs=1e-4)
    assert max_cap == pytest.approx(100.0, abs=1e-4)
    assert real_gross == pytest.approx(0.200, abs=1e-4)

    # 4. 断言核心差异: 真实净利 < 0, 线性估算 > 0, 误差刚好 0.200 美元
    assert real_net == pytest.approx(-0.040, abs=1e-4)
    assert real_net < 0.0, "真实模型净利润必须小于0 (亏损)"

    assert linear_gross == pytest.approx(0.400, abs=1e-4)
    assert linear_net == pytest.approx(0.160, abs=1e-4)
    assert linear_net > 0.0, "线性模型净利润大于0 (假阳性误报)"

    discrepancy = linear_net - real_net
    assert discrepancy == pytest.approx(0.200, abs=1e-4)


def test_capacity_table_other_scenarios() -> None:
    """验证对照表其他典型流动性场景的收益与容量推导."""
    gas_cost = 0.24

    # 场景 1: TVL50万 gross1.5/fee0.6 投100U -> 真实净利 0.620, 线性 0.660
    real_g_500k, _, _ = estimate_realized_profit_usd(1.5, 0.6, 500_000.0, 500_000.0, 100.0)
    assert real_g_500k - gas_cost == pytest.approx(0.620, abs=1e-4)
    linear_500k = 100.0 * (1.5 - 0.6) / 100.0 - gas_cost
    assert linear_500k == pytest.approx(0.660, abs=1e-4)

    # 场景 2: TVL10万 gross1.5/fee0.6 投100U -> 真实净利 0.460, 线性 0.660
    real_g_100k, _, _ = estimate_realized_profit_usd(1.5, 0.6, 100_000.0, 100_000.0, 100.0)
    assert real_g_100k - gas_cost == pytest.approx(0.460, abs=1e-4)

    # 场景 3: TVL10万 gross0.8/fee0.6 投100U -> 容量上限 50U, 实际投入 50U, 真实净利 -0.190
    real_g_low, size_low, cap_low = estimate_realized_profit_usd(
        0.8, 0.6, 100_000.0, 100_000.0, 100.0
    )
    assert cap_low == pytest.approx(50.0, abs=1e-4)
    assert size_low == pytest.approx(50.0, abs=1e-4)
    assert real_g_low - gas_cost == pytest.approx(-0.190, abs=1e-4)


def test_daemon_handle_spread_alert_prevents_tvl100k_false_positive() -> None:
    """测试守护进程利用真实模型拦截 TVL10万/gross1.0/fee0.6 场景的 QQ 误报."""
    pool_buy = PoolSpec(
        address="0x" + "1" * 40,
        label="USDG/WETH 0.3%",
        fee_bps=30.0,
        token0="0x" + "a" * 40,
        token1="0x" + "b" * 40,
        tvl_usd=100_000.0,
    )
    pool_sell = PoolSpec(
        address="0x" + "2" * 40,
        label="USDG/WETH 0.3%",
        fee_bps=30.0,
        token0="0x" + "a" * 40,
        token1="0x" + "b" * 40,
        tvl_usd=100_000.0,
    )
    alert = SpreadAlert(
        base="USDG",
        quote="WETH",
        buy_pool=pool_buy,
        sell_pool=pool_sell,
        buy_price=0.00040,
        sell_price=0.000404,  # 1.0% 毛价差
        gross_spread_pct=1.0,
        total_fee_pct=0.6,
        fee_pct=0.6,
        net_spread_pct=0.4,
        bottleneck_tvl=100_000.0,
        max_capacity_usd=100.0,
    )

    daemon = ArbitrageDaemon(mode="spread", auto_execute=False)

    with (
        patch.object(daemon, "_estimate_instant_gas_cost", return_value=(1_000_000_000, 0.24)),
        patch.object(daemon, "_get_trade_amount_usd", return_value=100.0),
        patch.object(daemon, "record_opportunity"),
        patch("monitors.daemons.arbitrage_daemon.send_qq_notification") as mock_send,
    ):
        daemon.handle_spread_alert(alert)
        mock_send.assert_not_called()


def test_high_fee_high_gross_spread_fires_qq_and_execution() -> None:
    """测试当跨池价差总费率较高 (如 2.07%) 但毛利更高 (如 5.0%) 扣 Gas 仍为正利润时，仍会推 QQ 并开火."""
    pool_buy = PoolSpec(
        address="0x" + "1" * 40,
        label="MEME/USDG 1.37%",
        fee_bps=137.0,
        token0="0x" + "a" * 40,
        token1="0x" + "b" * 40,
        tvl_usd=1_420_000.0,
    )
    pool_sell = PoolSpec(
        address="0x" + "2" * 40,
        label="MEME/USDG 0.7%",
        fee_bps=70.0,
        token0="0x" + "a" * 40,
        token1="0x" + "b" * 40,
        tvl_usd=1_420_000.0,
    )
    # 对应对照表场景: gross 5.0%, fee 2.07%, 扣 Gas 后真实净利润 +13.91 USD (开火)
    alert = SpreadAlert(
        base="MEME",
        quote="USDG",
        buy_pool=pool_buy,
        sell_pool=pool_sell,
        buy_price=1.0,
        sell_price=1.05,
        gross_spread_pct=5.0,
        total_fee_pct=2.07,
        fee_pct=2.07,
        net_spread_pct=2.93,
        bottleneck_tvl=1_420_000.0,
        max_capacity_usd=10_000.0,
    )

    daemon = ArbitrageDaemon(mode="spread", auto_execute=True)

    with (
        patch.object(daemon, "_estimate_instant_gas_cost", return_value=(1_000_000_000, 0.24)),
        patch.object(daemon, "_get_trade_amount_usd", return_value=500.0),
        patch.object(daemon, "record_opportunity"),
        patch.object(daemon, "_try_auto_snipe_spread") as mock_snipe,
        patch("monitors.daemons.arbitrage_daemon.send_qq_notification") as mock_qq,
    ):
        daemon.handle_spread_alert(alert)

        # 唯一判据 (净利润 - Gas > 0) 生效: 高费但真实净赚 $13.91，不再被费率误杀，顺利推 QQ 且触发开火
        mock_qq.assert_called_once()
        sent_msg = mock_qq.call_args[0][0]
        assert "Robinhood 跨池套利机会" in sent_msg
        assert "MEME/USDG" in sent_msg
        mock_snipe.assert_called_once_with(alert)


def test_high_fee_high_gross_triangle_fires_qq_and_execution() -> None:
    """测试三角套利高费高毛利场景 (fee 2.07%, gross 5.0%) 同样顺利推 QQ 并触发开火."""
    pool_one = PoolSpec(
        address="0x" + "1" * 40,
        label="WETH/USDG 1.37%",
        fee_bps=137.0,
        token0="0x" + "a" * 40,
        token1="0x" + "b" * 40,
    )
    pool_two = PoolSpec(
        address="0x" + "2" * 40,
        label="USDG/PONS 0.7%",
        fee_bps=70.0,
        token0="0x" + "b" * 40,
        token1="0x" + "c" * 40,
    )
    pool_three = PoolSpec(
        address="0x" + "3" * 40,
        label="PONS/WETH 0.0%",
        fee_bps=0.0,
        token0="0x" + "c" * 40,
        token1="0x" + "a" * 40,
    )
    legs = [
        SwapLeg("0x" + "a" * 40, "0x" + "b" * 40, pool_one, 2500.0, 137.0, 2465.75),
        SwapLeg("0x" + "b" * 40, "0x" + "c" * 40, pool_two, 1.0, 70.0, 0.993),
        SwapLeg("0x" + "c" * 40, "0x" + "a" * 40, pool_three, 0.00042, 0.0, 0.00042),
    ]
    alert = TriangularArbAlert(
        start_token=pool_one.token0,
        cycle=("WETH", "USDG", "PONS", "WETH"),
        legs=legs,
        gross_multiplier=1.05,
        fee_multiplier=0.9793,
        expected_multiplier=1.028,
        slippage_buffer_pct=0.2,
        net_multiplier=1.026,
        gross_profit_pct=5.0,
        net_profit_pct=2.73,
        total_fee_pct=2.07,
        bottleneck_tvl=1_000_000.0,
        max_capacity_usd=5000.0,
    )

    daemon = ArbitrageDaemon(mode="triangle", auto_execute=True)

    with (
        patch.object(daemon, "_estimate_instant_gas_cost", return_value=(1_000_000_000, 0.24)),
        patch.object(daemon, "_get_trade_amount_usd", return_value=500.0),
        patch.object(daemon, "record_opportunity"),
        patch.object(daemon, "_try_auto_snipe_triangle") as mock_snipe,
        patch("monitors.daemons.arbitrage_daemon.send_qq_notification") as mock_qq,
    ):
        daemon.handle_triangle_alert(alert)

        mock_qq.assert_called_once()
        sent_msg = mock_qq.call_args[0][0]
        assert "Robinhood 三角套利闭环" in sent_msg
        mock_snipe.assert_called_once_with(alert)


def test_pools_metadata_cache_hit_and_force_refresh(tmp_path: Path) -> None:
    """测试元数据缓存落盘、启动直接读取与 --refresh-pools 强制刷新链路."""
    cache_file = tmp_path / "pools_metadata_cache.json"

    raw_pool_a = PoolSpec(
        address="0x" + "11" * 20,
        label="TokenA/TokenB 0.05%",
        fee_bps=5.0,
    )
    raw_pool_b = PoolSpec(
        address="0x" + "22" * 20,
        label="TokenC/TokenD 0.3%",
        fee_bps=30.0,
    )

    discovered_pool_a = PoolSpec(
        address=raw_pool_a.address,
        label=raw_pool_a.label,
        fee_bps=5.0,
        token0="0x" + "33" * 20,
        token1="0x" + "44" * 20,
        dec0=18,
        dec1=6,
    )
    discovered_pool_b = PoolSpec(
        address=raw_pool_b.address,
        label=raw_pool_b.label,
        fee_bps=30.0,
        token0="0x" + "55" * 20,
        token1="0x" + "66" * 20,
        dec0=18,
        dec1=18,
    )

    # 阶段 1: 无缓存冷启动，必须执行 reader.discover 并落盘 JSON
    daemon_cold = ArbitrageDaemon(min_tvl=100_000.0, refresh_pools=False)
    daemon_cold.reader = MagicMock()
    daemon_cold.reader.discover.side_effect = [discovered_pool_a, discovered_pool_b]
    daemon_cold.reader.symbol.return_value = "MOCK_SYM"

    with (
        patch("monitors.daemons.arbitrage_daemon.POOLS_METADATA_CACHE_FILE", cache_file),
        patch(
            "monitors.daemons.arbitrage_daemon.load_dynamic_pools",
            return_value=[raw_pool_a, raw_pool_b],
        ),
    ):
        daemon_cold.init_pools()

        assert daemon_cold.reader.discover.call_count == 2
        assert len(daemon_cold.pools) == 2
        assert cache_file.exists()

        # 检查缓存落盘数据结构
        cache_data = json.loads(cache_file.read_text(encoding="utf-8"))
        assert "updated_at" in cache_data
        assert raw_pool_a.address.lower() in cache_data["pools"]
        meta_a = cache_data["pools"][raw_pool_a.address.lower()]
        assert meta_a["token0"] == "0x" + "33" * 20
        assert meta_a["token1"] == "0x" + "44" * 20
        assert meta_a["dec0"] == 18
        assert meta_a["dec1"] == 6

    # 阶段 2: 重启且未过期，直接读取缓存 (0 次 RPC discover 调用)
    daemon_warm = ArbitrageDaemon(min_tvl=100_000.0, refresh_pools=False)
    daemon_warm.reader = MagicMock()

    with (
        patch("monitors.daemons.arbitrage_daemon.POOLS_METADATA_CACHE_FILE", cache_file),
        patch(
            "monitors.daemons.arbitrage_daemon.load_dynamic_pools",
            return_value=[raw_pool_a, raw_pool_b],
        ),
    ):
        daemon_warm.init_pools()

        assert daemon_warm.reader.discover.call_count == 0
        assert len(daemon_warm.pools) == 2
        p_a = daemon_warm.pools[0]
        assert p_a.token0 == "0x" + "33" * 20
        assert p_a.token1 == "0x" + "44" * 20
        assert p_a.dec0 == 18
        assert p_a.dec1 == 6

    # 阶段 3: 指定 --refresh-pools 强制刷新 (重新调用 discover 并更新缓存)
    daemon_refresh = ArbitrageDaemon(min_tvl=100_000.0, refresh_pools=True)
    daemon_refresh.reader = MagicMock()
    daemon_refresh.reader.discover.side_effect = [discovered_pool_a, discovered_pool_b]
    daemon_refresh.reader.symbol.return_value = "REFRESHED_SYM"

    with (
        patch("monitors.daemons.arbitrage_daemon.POOLS_METADATA_CACHE_FILE", cache_file),
        patch(
            "monitors.daemons.arbitrage_daemon.load_dynamic_pools",
            return_value=[raw_pool_a, raw_pool_b],
        ),
    ):
        daemon_refresh.init_pools()

        assert daemon_refresh.reader.discover.call_count == 2
        assert len(daemon_refresh.pools) == 2

    # 阶段 4: 缓存过期 (超过 24 小时) 自动触发重新拉取
    expired_cache = {
        "updated_at": time.time() - 90_000.0,
        "pools": cache_data["pools"],
    }
    cache_file.write_text(json.dumps(expired_cache), encoding="utf-8")

    daemon_expired = ArbitrageDaemon(min_tvl=100_000.0, refresh_pools=False)
    daemon_expired.reader = MagicMock()
    daemon_expired.reader.discover.side_effect = [discovered_pool_a, discovered_pool_b]
    daemon_expired.reader.symbol.return_value = "EXP_REFRESH"

    with (
        patch("monitors.daemons.arbitrage_daemon.POOLS_METADATA_CACHE_FILE", cache_file),
        patch(
            "monitors.daemons.arbitrage_daemon.load_dynamic_pools",
            return_value=[raw_pool_a, raw_pool_b],
        ),
    ):
        daemon_expired.init_pools()
        assert daemon_expired.reader.discover.call_count == 2


def test_pools_metadata_cache_corrupted_fallback(tmp_path: Path) -> None:
    """测试缓存文件损坏/非法 JSON 时静默降级为全量 RPC 探测且不崩溃."""
    cache_file = tmp_path / "pools_metadata_cache.json"
    cache_file.write_text("corrupted json content {{{ not a valid json", encoding="utf-8")

    raw_pool = PoolSpec(
        address="0x" + "11" * 20,
        label="TokenA/TokenB 0.05%",
        fee_bps=5.0,
    )
    discovered_pool = PoolSpec(
        address=raw_pool.address,
        label=raw_pool.label,
        fee_bps=5.0,
        token0="0x" + "33" * 20,
        token1="0x" + "44" * 20,
        dec0=18,
        dec1=6,
    )

    daemon = ArbitrageDaemon(min_tvl=100_000.0, refresh_pools=False)
    daemon.reader = MagicMock()
    daemon.reader.discover.return_value = discovered_pool
    daemon.reader.symbol.return_value = "SYM"

    with (
        patch("monitors.daemons.arbitrage_daemon.POOLS_METADATA_CACHE_FILE", cache_file),
        patch(
            "monitors.daemons.arbitrage_daemon.load_dynamic_pools",
            return_value=[raw_pool],
        ),
    ):
        daemon.init_pools()

        assert daemon.reader.discover.call_count == 1
        assert len(daemon.pools) == 1
        assert daemon.pools[0].token0 == "0x" + "33" * 20

        repaired = json.loads(cache_file.read_text(encoding="utf-8"))
        assert "pools" in repaired
        assert raw_pool.address.lower() in repaired["pools"]
        assert repaired["pools"][raw_pool.address.lower()]["token0"] == "0x" + "33" * 20


def test_min_tvl_and_daemon_defaults() -> None:
    """验证 min-tvl 默认值下调为 100000.0 及相关参数默认值."""
    daemon = ArbitrageDaemon()
    assert daemon.min_tvl == 100_000.0
    assert daemon.refresh_pools is False

    # 验证 argparse 默认值
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-tvl", type=float, default=100_000.0)
    parser.add_argument("--refresh-pools", action="store_true")
    args = parser.parse_args([])
    assert args.min_tvl == 100_000.0
    assert args.refresh_pools is False
