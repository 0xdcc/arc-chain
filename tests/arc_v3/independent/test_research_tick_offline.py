"""离线 Tick 解码、缓存与回测引擎集成独立测试套件.

涵盖原 tests/test_tick_cache.py 前 6 项纯离线测试义务 (原断言机械保留)
及针对 0.0 哨兵值防污染、纯离线零 RPC 默认、显式注入约束的强化测试。
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from research.backtest.config import BacktestConfig
from research.backtest.engine import BacktestEngine
from research.backtest.reporter import MarkdownReporter
from research.backtest.swap_decoder import (
    SWAP_TOPIC,
    Tick,
    decode_swap_log,
    price_from_sqrt,
    to_int256,
)
from research.backtest.tick_cache import TickCache
from research.fifo import ClosedPair


def test_price_from_sqrt_math() -> None:
    """测试 sqrtPriceX96 -> price 价格换算数学正确性 (原测试 1 义务保留)."""
    q96 = 1 << 96

    # 1. 价格为 1.0 (sqrtPriceX96 = 2**96)
    assert pytest.approx(price_from_sqrt(q96), rel=1e-9) == 1.0

    # 2. 价格为 4.0 (sqrtPriceX96 = 2 * 2**96)
    assert pytest.approx(price_from_sqrt(2 * q96), rel=1e-9) == 4.0

    # 3. 价格为 0.25 (sqrtPriceX96 = 0.5 * 2**96)
    assert pytest.approx(price_from_sqrt(q96 // 2), rel=1e-9) == 0.25

    # 4. 非法或零值保护 (历史兼容哨兵值，不代表有效可交易价格)
    assert price_from_sqrt(0) == 0.0
    assert price_from_sqrt(-100) == 0.0

    # 5. 实测已知真实数据验证: PONS 池 sqrtPriceX96 对应约 3000 WETH/PONS
    sqrt_pons = 4353706221709919688002181270386
    p = price_from_sqrt(sqrt_pons)
    assert 2800.0 < p < 3200.0
    assert pytest.approx(1.0 / p, rel=0.05) == 0.00033


def test_signed_amount_parsing() -> None:
    """测试 int256 补码转有符号整数 (原测试 2 义务保留)."""
    # 零与正数
    assert to_int256(0) == 0
    assert to_int256(123456) == 123456

    # 常见有符号负数
    assert to_int256((1 << 256) - 1) == -1
    assert to_int256((1 << 256) - 500) == -500
    assert to_int256((1 << 256) - 10**18) == -(10**18)

    # 极值边界
    max_int256 = (1 << 255) - 1
    min_int256 = -(1 << 255)
    assert to_int256(max_int256) == max_int256
    assert to_int256(1 << 255) == min_int256


def test_decode_swap_log() -> None:
    """测试 Swap 事件完整日志解码 (原测试 3 义务保留)."""
    # 构造一条合法 Swap 日志
    # amount0 = 1000000 (正数), amount1 = -500000 (负数)
    # sqrtPriceX96 = 2**96 -> price = 1.0
    amt0_hex = (1000000).to_bytes(32, byteorder="big", signed=False).hex()
    amt1_hex = ((1 << 256) - 500000).to_bytes(32, byteorder="big", signed=False).hex()
    sqrt_hex = (1 << 96).to_bytes(32, byteorder="big", signed=False).hex()
    liq_hex = (1000).to_bytes(32, byteorder="big", signed=False).hex()
    tick_hex = (0).to_bytes(32, byteorder="big", signed=False).hex()

    valid_log = {
        "topics": [
            SWAP_TOPIC,
            "0x000000000000000000000000caf681a66d020601342297493863e78c959e5cb2",
            "0x000000000000000000000000217cadfa3230654e5ffe72ccb9dc2e0b998f2794",
        ],
        "data": "0x" + amt0_hex + amt1_hex + sqrt_hex + liq_hex + tick_hex,
        "blockNumber": "0x358fc53",  # 56163411
        "transactionHash": "0xabcdef1234567890",
    }

    tick = decode_swap_log(valid_log, block_ts=1788700000)
    assert tick is not None
    assert tick.block == 56163411
    assert tick.ts == 1788700000
    assert tick.amount0 == 1000000
    assert tick.amount1 == -500000
    assert pytest.approx(tick.price) == 1.0
    assert tick.tx_hash == "0xabcdef1234567890"

    # 测试异常与非法格式
    bad_topic_log = dict(valid_log, topics=["0x11111111111111111111111111111111"])
    assert decode_swap_log(bad_topic_log, 1788700000) is None

    bad_data_log = dict(valid_log, data="0x1234")
    assert decode_swap_log(bad_data_log, 1788700000) is None


def test_price_at_and_tolerance_boundaries(tmp_path: Path) -> None:
    """测试 price_at 容差匹配与无成交返回 None (原测试 4 义务保留)."""
    cache_dir = tmp_path / "tick_cache"
    cache_dir.mkdir()
    pool = "0xtestpool123"

    tc = TickCache(cache_dir=cache_dir, offline=True)

    # 1. 空缓存时查询返回 None 与 "no_trade_at_signal"
    p, meta = tc.price_at_with_meta(pool, 1700000100, tol_seconds=300)
    assert p is None
    assert meta == "no_trade_at_signal"
    assert tc.price_at(pool, 1700000100) is None

    # 2. 注入两笔确定时间点的 Tick
    t1 = Tick(
        block=100,
        ts=1700000100,
        price=10.0,
        amount0=100,
        amount1=-1000,
        tx_hash="0xtx1",
    )
    t2 = Tick(
        block=200,
        ts=1700000200,
        price=12.0,
        amount0=100,
        amount1=-1200,
        tx_hash="0xtx2",
    )
    tc.save_ticks(pool, [t1, t2])

    # 3. 精确命中 t1
    p, meta = tc.price_at_with_meta(pool, 1700000100, tol_seconds=300)
    assert p == 10.0
    assert meta == "exact"

    # 4. 在 t1 与 t2 之间 (1700000150) -> 取得 t1 (10.0)
    p, meta = tc.price_at_with_meta(pool, 1700000150, tol_seconds=300)
    assert p == 10.0
    assert meta == "exact"

    # 5. 在容差范围边缘 (ts - t2 <= 300) -> 取得 t2 (12.0)
    p, meta = tc.price_at_with_meta(pool, 1700000499, tol_seconds=300)
    assert p == 12.0
    assert meta == "exact"

    # 6. 超出容差范围 (ts - t2 > 300) -> 必须返回 None，绝不能 fallback 到旧价！
    p, meta = tc.price_at_with_meta(pool, 1700000600, tol_seconds=300)
    assert p is None
    assert meta == "no_trade_at_signal"
    assert tc.price_at(pool, 1700000600, tol_seconds=300) is None


def test_cache_persistence_roundtrip(tmp_path: Path) -> None:
    """测试 TickCache 落盘与磁盘读回数据一致性 (原测试 5 义务保留)."""
    cache_dir = tmp_path / "tick_cache"
    tc1 = TickCache(cache_dir=cache_dir, offline=True)
    pool = "0xroundtrip"

    ticks = [
        Tick(
            block=1,
            ts=1700000010,
            price=1.2345,
            amount0=-50,
            amount1=60,
            tx_hash="0xa1",
        ),
        Tick(
            block=2,
            ts=1700000020,
            price=2.3456,
            amount0=100,
            amount1=-230,
            tx_hash="0xa2",
        ),
    ]
    tc1.save_ticks(pool, ticks)

    # 用全新的实例重新从磁盘载入
    tc2 = TickCache(cache_dir=cache_dir, offline=True)
    loaded = tc2.load_ticks(pool)

    assert len(loaded) == 2
    assert loaded[0].block == 1
    assert loaded[0].ts == 1700000010
    assert pytest.approx(loaded[0].price) == 1.2345
    assert loaded[0].amount0 == -50
    assert loaded[0].amount1 == 60
    assert loaded[0].tx_hash == "0xa1"

    assert loaded[1].block == 2
    assert loaded[1].ts == 1700000020
    assert pytest.approx(loaded[1].price) == 2.3456
    assert loaded[1].amount0 == 100
    assert loaded[1].amount1 == -230
    assert loaded[1].tx_hash == "0xa2"


def test_engine_integration_use_tick_mode(tmp_path: Path) -> None:
    """测试回测引擎在 use_tick=True 时的行为与 no_trade_count 统计 (原测试 6 义务保留)."""
    cache_dir = tmp_path / "tick_cache"
    cache_dir.mkdir()
    pool = "0xpool_engine_test"

    # 仅提供开仓附近的 Tick，平仓时刻 1700000800 无成交 (制造 no_trade_at_signal)
    ticks = [
        Tick(
            block=10,
            ts=1700000060,
            price=1.0,
            amount0=100,
            amount1=-100,
            tx_hash="0xt1",
        ),
        Tick(
            block=11,
            ts=1700000064,
            price=1.5,
            amount0=100,
            amount1=-150,
            tx_hash="0xt2",
        ),
    ]
    tc = TickCache(cache_dir=cache_dir, offline=True)
    tc.save_ticks(pool, ticks)

    # 构造两个 ClosedPair: 一个可在容差内成交，一个因平仓太晚无法成交
    p_success = ClosedPair(
        trader="trader1",
        token="0xtok",
        entry_ts=1700000060.0,
        exit_ts=1700000062.0,
        entry_usd=100.0,
        exit_usd=110.0,
        token_amount=100.0,
        hold_seconds=2.0,
        pool=pool,
        pool_liquidity=500000.0,
    )
    p_no_trade = ClosedPair(
        trader="trader2",
        token="0xtok",
        entry_ts=1700000060.0,
        exit_ts=1700005000.0,  # 远超 tol_seconds
        entry_usd=100.0,
        exit_usd=110.0,
        token_amount=100.0,
        hold_seconds=4940.0,
        pool=pool,
        pool_liquidity=500000.0,
    )

    config = BacktestConfig(
        trade_amounts=[100.0],
        delays=[3],
        use_tick=True,
    )
    engine = BacktestEngine(config=config, tick_cache=tc)
    result = engine.run(pairs=[p_success, p_no_trade])

    st_cell = result.matrix_stats[(100.0, 3)]
    assert st_cell["n_pairs"] == 1
    assert st_cell["no_trade_count"] == 1
    assert result.no_trade_count == 1

    reporter = MarkdownReporter()
    report_md = reporter.generate(result)
    assert "本档为链上逐笔成交级精度" in report_md
    assert "no_trade_at_signal" in report_md


def test_price_sentinel_zero_and_negative_guarded(tmp_path: Path) -> None:
    """验证 price<=0 仅作为研究哨兵，所有查询绝对不将 0 当作可交易报价 (Fail-Closed)."""
    cache_dir = tmp_path / "tick_cache_sentinel"
    tc = TickCache(cache_dir=cache_dir, offline=True)
    pool = "0xsentinel_pool"

    # 1. 记录空窗口标记 (内部以 price=0.0 假 tick 占位)
    tc.mark_empty_window(pool, win_lo=100, win_hi=200)

    # 2. 查询点恰位于空窗口内，必须返回 None / no_trade_at_signal，绝不能返回 0.0
    p, meta = tc.price_at_with_meta(pool, ts=101.0, tol_seconds=10)
    assert p is None
    assert meta == "no_trade_at_signal"
    assert tc.price_at(pool, ts=101.0, tol_seconds=10) is None

    # 3. 显式注入负数价格 tick 验证防御
    t_negative = Tick(
        block=150,
        ts=150,
        price=-1.5,
        amount0=10,
        amount1=-10,
        tx_hash="0xnegative",
    )
    tc.merge_tick(pool, t_negative)
    p_neg, meta_neg = tc.price_at_with_meta(pool, ts=150.0, tol_seconds=10)
    assert p_neg is None
    assert meta_neg == "no_trade_at_signal"
    assert tc.price_at(pool, ts=150.0, tol_seconds=10) is None


def test_engine_consumption_guards_against_zero_price_sentinel(tmp_path: Path) -> None:
    """验证 BacktestEngine 消费 TickCache 时，遇到 0 价格哨兵时绝不撮合成交."""
    cache_dir = tmp_path / "tick_cache_engine_zero"
    tc = TickCache(cache_dir=cache_dir, offline=True)
    pool = "0xpool_zero_price"

    # 该池仅有 0 价格空窗口哨兵
    tc.mark_empty_window(pool, win_lo=1700000050, win_hi=1700000100)

    p_test = ClosedPair(
        trader="trader_zero",
        token="0xtok",
        entry_ts=1700000055.0,
        exit_ts=1700000060.0,
        entry_usd=100.0,
        exit_usd=110.0,
        token_amount=100.0,
        hold_seconds=5.0,
        pool=pool,
        pool_liquidity=500000.0,
    )

    config = BacktestConfig(
        trade_amounts=[100.0],
        delays=[0],
        use_tick=True,
    )
    engine = BacktestEngine(config=config, tick_cache=tc)
    result = engine.run(pairs=[p_test])

    st_cell = result.matrix_stats[(100.0, 0)]
    assert st_cell["n_pairs"] == 0
    assert st_cell["no_trade_count"] == 1
    assert result.no_trade_count == 1


def test_tick_cache_offline_defaults_and_explicit_rejection(tmp_path: Path) -> None:
    """验证纯离线默认配置 (rpc=None, offline=True) 及网络请求显式拒绝契约."""
    cache_dir = tmp_path / "tick_cache_offline"
    tc = TickCache(cache_dir=cache_dir)

    # 1. 默认无 RPC 且纯离线
    assert tc.rpc is None
    assert tc.offline is True

    # 2. 正常离线切片查询
    pool = "0xoffline_pool"
    t1 = Tick(block=1, ts=100, price=1.0, amount0=1, amount1=-1, tx_hash="0x1")
    t2 = Tick(block=2, ts=200, price=2.0, amount0=1, amount1=-1, tx_hash="0x2")
    tc.save_ticks(pool, [t1, t2])

    sliced = tc.fetch_pool_ticks(pool, ts_from=50, ts_to=150)
    assert len(sliced) == 1
    assert sliced[0].block == 1

    # 3. force_refetch 在离线模式下明确拒绝，绝不静默假装成功
    with pytest.raises(RuntimeError, match="force_refetch requires an active network RPC client"):
        tc.fetch_pool_ticks(pool, ts_from=50, ts_to=150, force_refetch=True)

    # 4. 非离线模式若未注入 RPC 客户端，明确拒绝
    tc_online_no_rpc = TickCache(cache_dir=cache_dir, offline=False, rpc=None)
    with pytest.raises(RuntimeError, match="online fetch requested but rpc client is None"):
        tc_online_no_rpc.fetch_pool_ticks(pool, ts_from=50, ts_to=150)


def test_tick_cache_path_boundary_defense(tmp_path: Path) -> None:
    """验证 pool 标识符的安全边界校验 (防路径穿越与非法字符)."""
    cache_dir = tmp_path / "tick_cache_sec"
    tc = TickCache(cache_dir=cache_dir, offline=True)

    with pytest.raises(ValueError, match="pool 标识符不能为空"):
        tc.load_ticks("")

    with pytest.raises(ValueError, match="pool 标识符禁止包含路径分隔符"):
        tc.load_ticks("pool/sub")

    with pytest.raises(ValueError, match="绝对路径|路径分隔符"):
        tc.load_ticks("/etc/passwd")

    with pytest.raises(ValueError, match="路径穿越|路径分隔符"):
        tc.load_ticks("../secret_pool")


def test_backtest_engine_requires_explicit_tick_cache_injection() -> None:
    """验证 BacktestEngine 在 use_tick=True 模式下必须显式注入 tick_cache."""
    config = BacktestConfig(
        trade_amounts=[100.0],
        delays=[3],
        use_tick=True,
    )
    engine = BacktestEngine(config=config, tick_cache=None)

    p = ClosedPair(
        trader="trader1",
        token="0xtok",
        entry_ts=1700000060.0,
        exit_ts=1700000062.0,
        entry_usd=100.0,
        exit_usd=110.0,
        token_amount=100.0,
        hold_seconds=2.0,
        pool="0xpool",
        pool_liquidity=500000.0,
    )
    with pytest.raises(RuntimeError, match="use_tick=True is blocked: TickCache is an optional"):
        engine.run(pairs=[p])


def test_non_finite_price_rejection_in_memory_and_persistence(tmp_path: Path) -> None:
    """测试内存中与持久化读回的非有限价格 (+inf, -inf, NaN) 均被 TickCache 拒绝并与正常有限正数对照."""
    cache_dir = tmp_path / "tick_cache_non_finite"
    tc1 = TickCache(cache_dir=cache_dir, offline=True)
    pool_bad = "0xpool_non_finite"

    # 1. 真实内存 Tick 构造 (+inf, -inf, NaN)
    t_inf = Tick(
        block=100,
        ts=1700000100,
        price=float("inf"),
        amount0=100,
        amount1=-100,
        tx_hash="0xtx_inf",
    )
    t_neginf = Tick(
        block=101,
        ts=1700000200,
        price=float("-inf"),
        amount0=100,
        amount1=-100,
        tx_hash="0xtx_neginf",
    )
    t_nan = Tick(
        block=102,
        ts=1700000300,
        price=float("nan"),
        amount0=100,
        amount1=-100,
        tx_hash="0xtx_nan",
    )
    tc1.save_ticks(pool_bad, [t_inf, t_neginf, t_nan])

    # 内存查询：+inf, -inf, NaN 一律返回 None 与 "no_trade_at_signal"，绝不放行 +inf
    p_inf, meta_inf = tc1.price_at_with_meta(pool_bad, ts=1700000100.0, tol_seconds=10)
    assert p_inf is None
    assert meta_inf == "no_trade_at_signal"
    assert tc1.price_at(pool_bad, ts=1700000100.0, tol_seconds=10) is None

    p_neginf, meta_neginf = tc1.price_at_with_meta(pool_bad, ts=1700000200.0, tol_seconds=10)
    assert p_neginf is None
    assert meta_neginf == "no_trade_at_signal"
    assert tc1.price_at(pool_bad, ts=1700000200.0, tol_seconds=10) is None

    p_nan, meta_nan = tc1.price_at_with_meta(pool_bad, ts=1700000300.0, tol_seconds=10)
    assert p_nan is None
    assert meta_nan == "no_trade_at_signal"
    assert tc1.price_at(pool_bad, ts=1700000300.0, tol_seconds=10) is None

    # 非有限时间戳入参防御
    p_ts_nan, meta_ts_nan = tc1.price_at_with_meta(pool_bad, ts=float("nan"), tol_seconds=10)
    assert p_ts_nan is None
    assert meta_ts_nan == "no_trade_at_signal"

    p_ts_inf, meta_ts_inf = tc1.price_at_with_meta(pool_bad, ts=float("inf"), tol_seconds=10)
    assert p_ts_inf is None
    assert meta_ts_inf == "no_trade_at_signal"

    # 2. 持久化读回：使用全新实例从磁盘 JSON 重新载入非有限值
    tc2 = TickCache(cache_dir=cache_dir, offline=True)
    reloaded_bad = tc2.load_ticks(pool_bad)
    assert len(reloaded_bad) == 3
    assert math.isinf(reloaded_bad[0].price) and reloaded_bad[0].price > 0
    assert math.isinf(reloaded_bad[1].price) and reloaded_bad[1].price < 0
    assert math.isnan(reloaded_bad[2].price)

    # 读回后查询依然被严格拒绝
    assert tc2.price_at_with_meta(pool_bad, ts=1700000100.0, tol_seconds=10) == (
        None,
        "no_trade_at_signal",
    )
    assert tc2.price_at(pool_bad, ts=1700000100.0, tol_seconds=10) is None
    assert tc2.price_at_with_meta(pool_bad, ts=1700000200.0, tol_seconds=10) == (
        None,
        "no_trade_at_signal",
    )
    assert tc2.price_at(pool_bad, ts=1700000200.0, tol_seconds=10) is None
    assert tc2.price_at_with_meta(pool_bad, ts=1700000300.0, tol_seconds=10) == (
        None,
        "no_trade_at_signal",
    )
    assert tc2.price_at(pool_bad, ts=1700000300.0, tol_seconds=10) is None

    # 3. 对照组：正常有限正数对照
    pool_good = "0xpool_finite_control"
    t_good1 = Tick(
        block=200,
        ts=1700000100,
        price=123.45,
        amount0=100,
        amount1=-12345,
        tx_hash="0xtx_good1",
    )
    t_good2 = Tick(
        block=201,
        ts=1700000200,
        price=125.50,
        amount0=100,
        amount1=-12550,
        tx_hash="0xtx_good2",
    )
    tc1.save_ticks(pool_good, [t_good1, t_good2])

    p_good1, meta_good1 = tc1.price_at_with_meta(pool_good, ts=1700000100.0, tol_seconds=10)
    assert p_good1 == 123.45
    assert meta_good1 == "exact"
    assert tc1.price_at(pool_good, ts=1700000100.0, tol_seconds=10) == 123.45

    reloaded_good = tc2.load_ticks(pool_good)
    assert len(reloaded_good) == 2
    assert math.isfinite(reloaded_good[0].price)
    assert pytest.approx(reloaded_good[0].price) == 123.45
    assert pytest.approx(reloaded_good[1].price) == 125.50
    assert tc2.price_at_with_meta(pool_good, ts=1700000200.0, tol_seconds=10) == (125.50, "exact")
    assert tc2.price_at(pool_good, ts=1700000200.0, tol_seconds=10) == 125.50


def test_engine_consumption_guards_against_non_finite_prices(tmp_path: Path) -> None:
    """验证 BacktestEngine 消费 TickCache 时，遇到非有限价格 (+inf, -inf, NaN) 绝不撮合成交并与正常有限价格对照."""
    cache_dir = tmp_path / "tick_cache_engine_non_finite"
    tc = TickCache(cache_dir=cache_dir, offline=True)

    pool_non_finite = "0xpool_non_finite_engine"
    pool_control = "0xpool_control_engine"

    # 1. 注入包含 +inf、-inf、NaN 的真实非有限值 Tick
    ticks_bad = [
        Tick(
            block=100,
            ts=1700000050,
            price=float("inf"),
            amount0=100,
            amount1=-100,
            tx_hash="0xtx_bad_inf",
        ),
        Tick(
            block=101,
            ts=1700000060,
            price=float("-inf"),
            amount0=100,
            amount1=-100,
            tx_hash="0xtx_bad_neginf",
        ),
        Tick(
            block=102,
            ts=1700000070,
            price=float("nan"),
            amount0=100,
            amount1=-100,
            tx_hash="0xtx_bad_nan",
        ),
    ]
    tc.save_ticks(pool_non_finite, ticks_bad)

    # 2. 注入对照组池：正常有限正数 Tick
    ticks_control = [
        Tick(
            block=200,
            ts=1700000050,
            price=2.0,
            amount0=100,
            amount1=-200,
            tx_hash="0xtx_ctrl_entry",
        ),
        Tick(
            block=201,
            ts=1700000060,
            price=2.2,
            amount0=100,
            amount1=-220,
            tx_hash="0xtx_ctrl_exit",
        ),
    ]
    tc.save_ticks(pool_control, ticks_control)

    # 3. 构造 2 笔非有限值反例交易对 + 1 笔正常有限正数对照对
    # 反例 1: 开仓命中 +inf, 平仓命中 -inf
    p_inf = ClosedPair(
        trader="trader_inf",
        token="0xtok_bad1",
        entry_ts=1700000050.0,
        exit_ts=1700000060.0,
        entry_usd=100.0,
        exit_usd=110.0,
        token_amount=50.0,
        hold_seconds=10.0,
        pool=pool_non_finite,
        pool_liquidity=500000.0,
    )
    # 反例 2: 开仓平仓均命中 NaN
    p_nan = ClosedPair(
        trader="trader_nan",
        token="0xtok_bad2",
        entry_ts=1700000070.0,
        exit_ts=1700000070.0,
        entry_usd=100.0,
        exit_usd=110.0,
        token_amount=50.0,
        hold_seconds=0.0,
        pool=pool_non_finite,
        pool_liquidity=500000.0,
    )
    # 对照组: 真实有限正数价格正常撮合
    p_valid = ClosedPair(
        trader="trader_valid",
        token="0xtok_good",
        entry_ts=1700000050.0,
        exit_ts=1700000060.0,
        entry_usd=100.0,
        exit_usd=110.0,
        token_amount=50.0,
        hold_seconds=10.0,
        pool=pool_control,
        pool_liquidity=500000.0,
    )

    config = BacktestConfig(
        trade_amounts=[100.0],
        delays=[0],
        use_tick=True,
    )
    # 查实际类/函数，不造 wrapper 模拟消费
    engine = BacktestEngine(config=config, tick_cache=tc)
    result = engine.run(pairs=[p_inf, p_nan, p_valid])

    st_cell = result.matrix_stats[(100.0, 0)]
    # 验证非有限值反例全部被拒 (单独打标计入 no_trade_count)，仅对照组 1 笔成功成交
    assert st_cell["n_pairs"] == 1
    assert st_cell["no_trade_count"] == 2
    assert result.no_trade_count == 2

    # 验证成功成交的唯一交易对为对照组 trader_valid，成交价严格为有限正数
    assert len(result.evaluated_pairs_baseline) == 1
    eval_p = result.evaluated_pairs_baseline[0]
    assert eval_p.pair.trader == "trader_valid"
    assert math.isfinite(eval_p.entry_price_follow) and eval_p.entry_price_follow == 2.0
    assert math.isfinite(eval_p.exit_price_follow) and eval_p.exit_price_follow == 2.2


def test_engine_consumption_guards_against_single_end_inf_prices(tmp_path: Path) -> None:
    """验证 BacktestEngine 消费 TickCache 时，单变量正无穷独立被拒为 no_trade_at_signal.

    避免两端异常互相掩盖 (entry+inf/exit有限正数，以及 entry有限正数/exit+inf 两交易对)，
    各真实 TickCache 注入 BacktestEngine，断言 no_trade 且无虚假收益，并与两端有限正数对照.
    """
    cache_dir = tmp_path / "tick_cache_single_end_inf"
    tc = TickCache(cache_dir=cache_dir, offline=True)

    pool_entry_inf = "0xpool_entry_inf_engine"
    pool_exit_inf = "0xpool_exit_inf_engine"
    pool_control = "0xpool_ctrl_engine"

    # 1. 单端开仓 +inf 池：entry 命中 +inf，exit 为正常有限价格 2.2
    # 时间戳间隔 1000s > tol_seconds(300s)，避免 entry 查询向前容差回退至 exit tick
    ticks_entry_inf = [
        Tick(
            block=100,
            ts=1700000000,
            price=float("inf"),
            amount0=100,
            amount1=-100,
            tx_hash="0xtx_entry_inf",
        ),
        Tick(
            block=101,
            ts=1700001000,
            price=2.2,
            amount0=100,
            amount1=-220,
            tx_hash="0xtx_entry_inf_exit",
        ),
    ]
    tc.save_ticks(pool_entry_inf, ticks_entry_inf)

    # 2. 单端平仓 +inf 池：entry 为正常有限价格 2.0，exit 命中 +inf
    ticks_exit_inf = [
        Tick(
            block=200,
            ts=1700000000,
            price=2.0,
            amount0=100,
            amount1=-200,
            tx_hash="0xtx_exit_inf_entry",
        ),
        Tick(
            block=201,
            ts=1700001000,
            price=float("inf"),
            amount0=100,
            amount1=-100,
            tx_hash="0xtx_exit_inf",
        ),
    ]
    tc.save_ticks(pool_exit_inf, ticks_exit_inf)

    # 3. 对照池：两端均为正常有限正数 (entry 2.0, exit 2.2)
    ticks_control = [
        Tick(
            block=300,
            ts=1700000000,
            price=2.0,
            amount0=100,
            amount1=-200,
            tx_hash="0xtx_ctrl_entry",
        ),
        Tick(
            block=301,
            ts=1700001000,
            price=2.2,
            amount0=100,
            amount1=-220,
            tx_hash="0xtx_ctrl_exit",
        ),
    ]
    tc.save_ticks(pool_control, ticks_control)

    # 4. 构造 2 笔单端反例交易对 + 1 笔正常两端有限对照对
    # 单变量反例 1: 开仓 +inf, 平仓 2.2 (有限正数)
    p_entry_inf = ClosedPair(
        trader="trader_entry_inf",
        token="0xtok_entry_inf",
        entry_ts=1700000000.0,
        exit_ts=1700001000.0,
        entry_usd=100.0,
        exit_usd=110.0,
        token_amount=50.0,
        hold_seconds=1000.0,
        pool=pool_entry_inf,
        pool_liquidity=500000.0,
    )

    # 单变量反例 2: 开仓 2.0 (有限正数), 平仓 +inf
    p_exit_inf = ClosedPair(
        trader="trader_exit_inf",
        token="0xtok_exit_inf",
        entry_ts=1700000000.0,
        exit_ts=1700001000.0,
        entry_usd=100.0,
        exit_usd=110.0,
        token_amount=50.0,
        hold_seconds=1000.0,
        pool=pool_exit_inf,
        pool_liquidity=500000.0,
    )

    # 对照组: 两端正常有限正数
    p_valid = ClosedPair(
        trader="trader_valid",
        token="0xtok_valid",
        entry_ts=1700000000.0,
        exit_ts=1700001000.0,
        entry_usd=100.0,
        exit_usd=110.0,
        token_amount=50.0,
        hold_seconds=1000.0,
        pool=pool_control,
        pool_liquidity=500000.0,
    )

    config = BacktestConfig(
        trade_amounts=[100.0],
        delays=[0],
        use_tick=True,
    )
    engine = BacktestEngine(config=config, tick_cache=tc)

    # 5. 各真实 TickCache 注入 BacktestEngine 独立消费断言
    # 5.1 单端开仓 +inf 独立注入：必须被 TickCache 拒绝为 no_trade_at_signal 并计入 no_trade_count
    res_entry = engine.run(pairs=[p_entry_inf])
    assert res_entry.no_trade_count == 1
    assert len(res_entry.evaluated_pairs_baseline) == 0

    # 5.2 单端平仓 +inf 独立注入：必须被 TickCache 拒绝为 no_trade_at_signal 并计入 no_trade_count
    res_exit = engine.run(pairs=[p_exit_inf])
    assert res_exit.no_trade_count == 1
    assert len(res_exit.evaluated_pairs_baseline) == 0

    # 5.3 对照组独立注入：正常撮合成交，无 no_trade
    res_valid = engine.run(pairs=[p_valid])
    assert res_valid.no_trade_count == 0
    assert len(res_valid.evaluated_pairs_baseline) == 1
    eval_v = res_valid.evaluated_pairs_baseline[0]
    assert eval_v.pair.trader == "trader_valid"
    assert math.isfinite(eval_v.entry_price_follow) and eval_v.entry_price_follow == 2.0
    assert math.isfinite(eval_v.exit_price_follow) and eval_v.exit_price_follow == 2.2
    assert math.isfinite(eval_v.follow_return)

    # 6. 批次混合消费断言：单端正无穷全部被拒绝，绝不产生虚假收益
    res_all = engine.run(pairs=[p_entry_inf, p_exit_inf, p_valid])
    st_cell = res_all.matrix_stats[(100.0, 0)]
    assert st_cell["n_pairs"] == 1
    assert st_cell["no_trade_count"] == 2
    assert res_all.no_trade_count == 2

    # 验证成交记录无虚假收益，唯一成交者为对照组
    assert len(res_all.evaluated_pairs_baseline) == 1
    eval_p = res_all.evaluated_pairs_baseline[0]
    assert eval_p.pair.trader == "trader_valid"
    assert math.isfinite(eval_p.entry_price_follow) and eval_p.entry_price_follow == 2.0
    assert math.isfinite(eval_p.exit_price_follow) and eval_p.exit_price_follow == 2.2
    assert math.isfinite(eval_p.follow_return)
    assert not any(
        ep.pair.trader in ("trader_entry_inf", "trader_exit_inf")
        for ep in res_all.evaluated_pairs_baseline
    )
