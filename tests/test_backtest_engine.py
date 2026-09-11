"""测试回测引擎集成流程 (engine.py, ingesters, impact, reporter)."""

import json
from pathlib import Path

from backtest.config import BacktestConfig
from backtest.data.ingesters.fomo import FomoIngester
from backtest.data.ingesters.generic_csv import GenericCSVIngester
from backtest.data.price_cache import PriceCache
from backtest.engine import BacktestEngine
from backtest.pipeline.impact import FrictionModel
from backtest.pipeline.matcher import ClosedPair
from backtest.reporter import MarkdownReporter


def test_fomo_ingester_direction_rules():
    """测试 Fomo 跨链 Swap 方向判定与跳过规则."""
    ingester = FomoIngester(target_chain_id=4663)
    sample_swaps = [
        # 买入: 外链 -> 4663
        {
            "trader": "unipcs",
            "inNetworkId": 1,
            "outNetworkId": 4663,
            "inTokenAddress": "0xusdc",
            "outTokenAddress": "0xrh_token",
            "humanUsdAmountIn": 500.0,
            "humanUsdAmountOut": 490.0,
            "inHumanAmount": 500.0,
            "outHumanAmount": 10000.0,
            "createdAt": "2026-09-05T08:00:00.000Z",
        },
        # 卖出: 4663 -> 外链
        {
            "trader": "unipcs",
            "inNetworkId": 4663,
            "outNetworkId": 1,
            "inTokenAddress": "0xrh_token",
            "outTokenAddress": "0xusdc",
            "humanUsdAmountIn": 800.0,
            "humanUsdAmountOut": 800.0,
            "inHumanAmount": 10000.0,
            "outHumanAmount": 800.0,
            "createdAt": "2026-09-05T09:00:00.000Z",
        },
        # 同链兑换: 4663 -> 4663 (跳过)
        {
            "trader": "unipcs",
            "inNetworkId": 4663,
            "outNetworkId": 4663,
            "createdAt": "2026-09-05T09:30:00.000Z",
        },
        # 非目标链: 1 -> 56 (跳过)
        {
            "trader": "unipcs",
            "inNetworkId": 1,
            "outNetworkId": 56,
            "createdAt": "2026-09-05T10:00:00.000Z",
        },
    ]

    records = ingester.ingest(sample_swaps)
    assert len(records) == 2
    assert ingester.skipped_same_chain == 1
    assert ingester.skipped_other_chain == 1

    buy_rec = records[0]
    assert buy_rec.side == "buy"
    assert buy_rec.token == "0xrh_token"
    assert buy_rec.token_amount == 10000.0
    assert buy_rec.usd_amount == 500.0

    sell_rec = records[1]
    assert sell_rec.side == "sell"
    assert sell_rec.token == "0xrh_token"
    assert sell_rec.token_amount == 10000.0
    assert sell_rec.usd_amount == 800.0


def test_generic_csv_ingester():
    """测试通用 CSV 导入器与列名映射."""
    csv_content = """wallet,asset,direction,timestamp,value_usd,qty
trader_x,0xabc,buy,1700000100,200.0,50.0
trader_x,0xabc,sell,1700000500,300.0,50.0
"""
    mapping = {
        "trader": "wallet",
        "token": "asset",
        "side": "direction",
        "ts": "timestamp",
        "usd_amount": "value_usd",
        "token_amount": "qty",
    }
    ingester = GenericCSVIngester(column_mapping=mapping)
    records = ingester.ingest(csv_content)
    assert len(records) == 2
    assert records[0].trader == "trader_x"
    assert records[0].token == "0xabc"
    assert records[0].side == "buy"
    assert records[1].side == "sell"


def test_friction_model():
    """测试手续费与流动性冲击模型精算."""
    model = FrictionModel(fee_rate=0.006, impact_factor=0.5, low_liquidity_threshold=50000.0)

    # 1. 深度极佳 ($1,000,000 池)，单笔 $100，冲击仅 0.005%
    # net_entry = 1.0 * 1.006 * (1 + 100/1e6 * 0.5) = 1.00605
    # net_exit = 2.0 * 0.994 * (1 - 100/1e6 * 0.5) = 1.9879
    net_in, net_out, r = model.calculate_follow_return(
        entry_price=1.0,
        exit_price=2.0,
        trade_size_usd=100.0,
        pool_liquidity_usd=1_000_000.0,
    )
    assert net_in > 1.0
    assert net_out < 2.0
    assert 0.95 < r < 1.0

    # 2. 低流动性标记
    assert model.is_low_liquidity(20000.0) is True
    assert model.is_low_liquidity(100000.0) is False


def test_price_cache_local(tmp_path: Path):
    """测试行情本地缓存与 price_at 查找."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    pool_addr = "0xpool123"
    candles = [
        [1700000000, 1.0],
        [1700000060, 1.2],
        [1700000120, 1.5],
        [1700000180, 1.8],
    ]
    with open(cache_dir / f"k_{pool_addr}.json", "w") as f:
        json.dump(candles, f)

    pc = PriceCache(cache_dir=cache_dir, fallback_cache_dir=None, offline=True)
    # 精确命中
    assert pc.price_at(pool_addr, 1700000060) == 1.2
    # 在 5 根容差内取 timestamp <= ts 最近一根
    assert pc.price_at(pool_addr, 1700000075) == 1.2
    # 超出 5 根 (5 * 60s = 300s) -> None
    assert pc.price_at(pool_addr, 1700001000) is None


def test_matrix_backtest_engine_and_reporter(tmp_path: Path):
    """测试矩阵回测执行器与 Markdown 报告输出."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    pool = "0xpool_matrix"

    # 生成 1h 步长 1m 的 K 线
    candles = [[1700000000 + i * 60, 1.0 + i * 0.01] for i in range(100)]
    with open(cache_dir / f"k_{pool}.json", "w") as f:
        json.dump(candles, f)

    pc = PriceCache(cache_dir=cache_dir, fallback_cache_dir=None, offline=True)

    # 构造两个已平仓对子
    p1 = ClosedPair(
        trader="trader_good",
        token="0xtok1",
        entry_ts=1700000060.0,
        exit_ts=1700000600.0,
        entry_usd=100.0,
        exit_usd=150.0,
        token_amount=100.0,
        hold_seconds=540.0,
        pool=pool,
        pool_liquidity=200000.0,
    )
    p2 = ClosedPair(
        trader="trader_bad",
        token="0xtok1",
        entry_ts=1700000120.0,
        exit_ts=1700000300.0,
        entry_usd=200.0,
        exit_usd=100.0,
        token_amount=200.0,
        hold_seconds=180.0,
        pool=pool,
        pool_liquidity=30000.0,  # low liquidity
    )

    config = BacktestConfig(
        trade_amounts=[100.0, 300.0],
        delays=[3, 10],
        fee_rate=0.006,
        impact_factor=0.5,
        min_cost=20.0,
    )
    engine = BacktestEngine(config=config, price_cache=pc)
    result = engine.run(pairs=[p1, p2])

    assert result.matched_pairs_count == 2
    assert (100.0, 3) in result.matrix_stats

    st_cell = result.matrix_stats[(100.0, 3)]
    # 验证全部字段存在
    required_keys = [
        "n_pairs",
        "boss_median_return",
        "boss_win_rate",
        "follow_median_return",
        "follow_win_rate",
        "follow_mean_return",
        "return_decay",
        "median_hold_seconds",
        "max_single_loss",
        "missing_price_rate",
        "polluted_filtered",
    ]
    for k in required_keys:
        assert k in st_cell

    assert len(result.trader_groups) == 2
    assert len(result.duration_groups) == 5
    assert len(result.liquidity_groups) == 3

    reporter = MarkdownReporter()
    md = reporter.generate(result)
    assert "# 跨链智能合约跟单回测诊断报告" in md
    assert "事后回测，存在客观幸存者偏差" in md
    assert "金额 × 延迟综合表现" in md
    assert "@trader_good" in md


def test_trader_sample_size_protection(tmp_path: Path):
    """小样本交易员不应被下『可跟/别跟』结论（防 unipcs 13 对误判）."""
    from backtest.engine import MIN_TRADER_SAMPLE

    cache_dir = tmp_path / "cache2"
    cache_dir.mkdir()
    pool = "0xpool_sample"
    candles = [[1700000000 + i * 60, 1.0 + i * 0.01] for i in range(400)]
    with open(cache_dir / f"k_{pool}.json", "w") as f:
        json.dump(candles, f)

    pc = PriceCache(cache_dir=cache_dir, fallback_cache_dir=None, offline=True)

    pairs = []
    # 小样本交易员：仅 2 对（远低于 MIN_TRADER_SAMPLE）
    for i in range(2):
        pairs.append(
            ClosedPair(
                trader="tiny_sample_trader",
                token=f"0xtok{i}",
                entry_ts=1700000060.0 + i * 60,
                exit_ts=1700000600.0 + i * 60,
                entry_usd=100.0,
                exit_usd=90.0,
                token_amount=100.0,
                hold_seconds=540.0,
                pool=pool,
                pool_liquidity=900000.0,
            )
        )

    engine = BacktestEngine(price_cache=pc)
    result = engine.run(pairs=pairs)

    tiny = [g for g in result.trader_groups if g.name == "tiny_sample_trader"]
    assert tiny, "应产出 tiny_sample_trader 分组"
    assert tiny[0].n_pairs < MIN_TRADER_SAMPLE
    assert "样本不足" in tiny[0].recommendation
    assert "🏆" not in tiny[0].recommendation
    assert "❌" not in tiny[0].recommendation
