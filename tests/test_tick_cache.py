"""单元与集成测试: 秒级 Tick 价格层 (TickCache / SwapDecoder / RobinhoodRpc).

覆盖:
1. price_from_sqrt 数学正确性
2. 有符号 amount0/amount1 补码转换
3. decode_swap_log 日志解码
4. price_at 容差范围与无成交返回 None (防假价格污染)
5. 缓存落盘与读回一致性
6. 回测引擎 use_tick=True 模式集成与 no_trade_count 统计
7. 真实 Robinhood RPC 抓取 0xed50bdeea8adc232f159486192a4157281d722ff 最近 20,000 区块验证
"""

import json
from pathlib import Path

import pytest

from backtest.config import BacktestConfig
from backtest.data.rpc_client import RobinhoodRpc
from backtest.data.swap_decoder import (
    SWAP_TOPIC,
    Tick,
    decode_swap_log,
    price_from_sqrt,
    to_int256,
)
from backtest.data.tick_cache import TickCache
from backtest.engine import BacktestEngine
from backtest.pipeline.matcher import ClosedPair
from backtest.reporter import MarkdownReporter


def test_price_from_sqrt_math():
    """测试 sqrtPriceX96 -> price 价格换算数学正确性."""
    q96 = 1 << 96

    # 1. 价格为 1.0 (sqrtPriceX96 = 2**96)
    assert pytest.approx(price_from_sqrt(q96), rel=1e-9) == 1.0

    # 2. 价格为 4.0 (sqrtPriceX96 = 2 * 2**96)
    assert pytest.approx(price_from_sqrt(2 * q96), rel=1e-9) == 4.0

    # 3. 价格为 0.25 (sqrtPriceX96 = 0.5 * 2**96)
    assert pytest.approx(price_from_sqrt(q96 // 2), rel=1e-9) == 0.25

    # 4. 非法或零值保护
    assert price_from_sqrt(0) == 0.0
    assert price_from_sqrt(-100) == 0.0

    # 5. 实测已知真实数据验证: PONS 池 sqrtPriceX96 对应约 3000 WETH/PONS
    sqrt_pons = 4353706221709919688002181270386
    p = price_from_sqrt(sqrt_pons)
    assert 2800.0 < p < 3200.0
    assert pytest.approx(1.0 / p, rel=0.05) == 0.00033


def test_signed_amount_parsing():
    """测试 int256 补码转有符号整数 (支持负数与边界值)."""
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


def test_decode_swap_log():
    """测试 Swap 事件完整日志解码."""
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


def test_price_at_and_tolerance_boundaries(tmp_path: Path):
    """测试 price_at 容差匹配与无成交返回 None (关键防污染验证)."""
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


def test_cache_persistence_roundtrip(tmp_path: Path):
    """测试 TickCache 落盘与磁盘读回数据一致性."""
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


def test_engine_integration_use_tick_mode(tmp_path: Path):
    """测试回测引擎在 use_tick=True 时的行为与 no_trade_count 统计."""
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


def test_live_robinhood_decode_pons_pool():
    """验收标准 1: 实测 Robinhood RPC 解码 0xed50bdeea8adc232f159486192a4157281d722ff 最近 20000 区块."""
    rpc = RobinhoodRpc()
    curr_block = rpc.block_number()
    assert curr_block > 50_000_000

    from_block = curr_block - 20000
    pool = "0xed50bdeea8adc232f159486192a4157281d722ff"

    logs = rpc.get_logs(
        address=pool,
        topics=[SWAP_TOPIC],
        from_block=from_block,
        to_block=curr_block,
    )
    assert len(logs) >= 100, f"Expected >= 100 logs, got {len(logs)}"

    # 验证最新几条日志的解码
    sample_logs = logs[-5:]
    blocks = [int(str(sample_log["blockNumber"]), 16) for sample_log in sample_logs]
    ts_map = rpc.block_timestamps(blocks)

    ticks: list[Tick] = []
    for sample_log in sample_logs:
        b = int(str(sample_log["blockNumber"]), 16)
        ts = ts_map[b]
        tick = decode_swap_log(sample_log, ts)
        assert tick is not None
        ticks.append(tick)

    last_tick = ticks[-1]
    # 末笔价格在 2975 ± 100 区间，或倒数落在 0.00033 ± 0.00002
    p = last_tick.price
    # 价格断言用宽窗口（币价本身波动，2026-09-07 实测 3338）：只验证解码数量级正确
    is_weth_per_pons = 2500.0 <= p <= 5000.0
    is_pons_per_weth = 0.0002 <= (1.0 / p) <= 0.0004
    assert is_weth_per_pons or is_pons_per_weth, f"Unexpected price {p}"

    # 所有 Tick 的 ts 单调不减
    for i in range(len(ticks) - 1):
        assert ticks[i].ts <= ticks[i + 1].ts


def test_rpc_adaptive_bisection_on_limit_overflow():
    """测试 get_logs 收到 -32000 错误或条数达 10000 时自动对半二分递归重试与合并去重."""
    from backtest.data.rpc_client import RpcError

    rpc = RobinhoodRpc()
    call_records: list[tuple[int, int]] = []

    def mock_call(method: str, params: list | None = None):
        assert method == "eth_getLogs"
        assert params is not None and len(params) > 0
        p = params[0]
        fb = int(str(p["fromBlock"]), 16)
        tb = int(str(p["toBlock"]), 16)
        call_records.append((fb, tb))

        # 1. 模拟全量区间 [1000, 2000] 触发节点 -32000 溢出上限异常
        if fb == 1000 and tb == 2000:
            raise RpcError(
                code=-32000,
                message="logs matched by query exceeds limit of 10000",
                raw={"code": -32000, "message": "logs matched by query exceeds limit of 10000"},
            )

        # 2. 模拟左半段 [1000, 1500] 成功返回日志，且包含一条重复数据测试去重
        if fb == 1000 and tb == 1500:
            return {
                "result": [
                    {"blockNumber": "0x3e8", "logIndex": "0x0", "transactionHash": "0x111"},
                    {"blockNumber": "0x3e9", "logIndex": "0x1", "transactionHash": "0x222"},
                ]
            }

        # 3. 模拟右半段 [1501, 2000] 返回条数达到 10000，触发二分切分
        if fb == 1501 and tb == 2000:
            return {
                "result": [
                    {"blockNumber": "0x5dc", "logIndex": f"0x{i}", "transactionHash": f"0x{i}"}
                    for i in range(10000)
                ]
            }

        # 4. 模拟右半段被进一步切分后的子区间:
        # [1501, 1750]
        if fb == 1501 and tb == 1750:
            return {
                "result": [
                    {"blockNumber": "0x5dc", "logIndex": "0x1", "transactionHash": "0x333"},
                ]
            }
        # [1751, 2000] (含一条与 0x111 相同的 blockNumber+logIndex 重复日志)
        if fb == 1751 and tb == 2000:
            return {
                "result": [
                    {"blockNumber": "0x3e8", "logIndex": "0x0", "transactionHash": "0x111"},  # 重复
                    {"blockNumber": "0x6a0", "logIndex": "0x0", "transactionHash": "0x444"},
                ]
            }

        return {"result": []}

    rpc.call = mock_call

    logs = rpc.get_logs(
        address="0x1234567890123456789012345678901234567890",
        topics=[SWAP_TOPIC],
        from_block=1000,
        to_block=2000,
        max_span=5000,
        min_span=50,
    )

    # 验证确实发生了对半二分递归切分
    assert (1000, 2000) in call_records
    assert (1000, 1500) in call_records
    assert (1501, 2000) in call_records
    assert (1501, 1750) in call_records
    assert (1751, 2000) in call_records

    # 验证最终合并去重后的结果正确性: 4 条唯一记录 (0x111, 0x222, 0x333, 0x444)
    assert len(logs) == 4
    tx_hashes = [item["transactionHash"] for item in logs]
    assert tx_hashes == ["0x111", "0x222", "0x333", "0x444"]


def test_overflow_semantics_timeout_not_overflow():
    """超时绝不能被当作 overflow（否则触发二分递归爆炸卡死）."""
    from backtest.data.rpc_client import RobinhoodRpc, RpcError

    rpc = RobinhoodRpc()
    # overflow：-32000 / exceeds limit
    assert rpc._is_log_overflow_error(RpcError(code=-32000, message="logs exceeds limit of 10000"))
    assert rpc._is_log_overflow_error(RuntimeError("query exceeds limit of 10000"))
    # 非 overflow：超时 / 429 / 普通网络错误
    assert not rpc._is_log_overflow_error(RuntimeError("Log query timed out: ReadTimeout"))
    assert not rpc._is_log_overflow_error(RuntimeError("HTTP 429 Too Many Requests"))
    assert not rpc._is_log_overflow_error(RuntimeError("connection reset by peer"))


def test_get_logs_skips_bytes32_poolid():
    """66字符 bytes32 PoolId 不能作为 getLogs address，必须返回空而不是炸 RPC."""
    from backtest.data.rpc_client import RobinhoodRpc

    rpc = RobinhoodRpc()
    b32 = "0x" + "a" * 64  # 66 字符
    assert len(b32) == 66
    out = rpc.get_logs(
        address=b32,
        topics=["0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"],
        from_block=1,
        to_block=10,
    )
    assert out == []
