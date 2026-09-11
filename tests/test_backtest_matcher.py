"""测试 FIFO 撮合器与部分平仓聚合 (matcher.py)."""

from backtest.data.ingesters.base import SwapRecord
from backtest.pipeline.matcher import FIFOMatcher


def _make_record(
    trader: str = "trader_1",
    token: str = "0xtok",
    side: str = "buy",
    ts: float = 1000.0,
    usd: float = 100.0,
    qty: float = 50.0,
    unsolicited: bool = False,
) -> SwapRecord:
    return SwapRecord(
        trader=trader,
        token=token,
        side=side,
        ts=ts,
        usd_amount=usd,
        token_amount=qty,
        unsolicited=unsolicited,
    )


def test_fifo_simple_one_to_one():
    """测试单个买入和单个卖出的完整配对."""
    matcher = FIFOMatcher(merge_partial=True)
    records = [
        _make_record(side="buy", ts=100.0, usd=100.0, qty=10.0),
        _make_record(side="sell", ts=200.0, usd=150.0, qty=10.0),
    ]
    closed, open_pos = matcher.match(records)
    assert len(closed) == 1
    assert len(open_pos) == 0

    p = closed[0]
    assert p.entry_ts == 100.0
    assert p.exit_ts == 200.0
    assert p.entry_usd == 100.0
    assert p.exit_usd == 150.0
    assert p.token_amount == 10.0
    assert p.hold_seconds == 100.0
    assert p.boss_return == 0.5


def test_fifo_partial_sell_with_open_position():
    """测试部分平仓: 买入 100 个，仅卖出 40 个，剩余 60 个记入未平仓."""
    matcher = FIFOMatcher(merge_partial=True)
    records = [
        _make_record(side="buy", ts=100.0, usd=1000.0, qty=100.0),
        _make_record(side="sell", ts=150.0, usd=600.0, qty=40.0),
    ]
    closed, open_pos = matcher.match(records)
    assert len(closed) == 1
    assert len(open_pos) == 1

    p = closed[0]
    assert p.token_amount == 40.0
    assert p.entry_usd == 400.0
    assert p.exit_usd == 600.0
    assert p.boss_return == 0.5

    op = open_pos[0]
    assert op.entry_ts == 100.0
    assert op.token_amount == 60.0
    assert op.entry_usd == 600.0


def test_fifo_merge_multiple_partial_sells():
    """测试防坑方法论修正 1: 同一 entry_ts 的多次部分平仓聚合为一笔完整对子."""
    # 买入 100 个 ($1000)，随后分两批卖出: 40 个 ($500) 与 60 个 ($900)
    matcher_merged = FIFOMatcher(merge_partial=True)
    records = [
        _make_record(side="buy", ts=100.0, usd=1000.0, qty=100.0),
        _make_record(side="sell", ts=200.0, usd=500.0, qty=40.0),
        _make_record(side="sell", ts=300.0, usd=900.0, qty=60.0),
    ]
    closed, open_pos = matcher_merged.match(records)
    # 合并后应只有 1 个完整平仓对子
    assert len(closed) == 1
    assert len(open_pos) == 0

    p = closed[0]
    assert p.entry_ts == 100.0
    assert p.exit_ts == 300.0  # 取最晚平仓时刻
    assert p.entry_usd == 1000.0
    assert p.exit_usd == 1400.0  # 500 + 900
    assert p.token_amount == 100.0
    assert p.hold_seconds == 200.0
    assert p.boss_return == 0.4

    # 对比未开启合并的情况: 会产生 2 个对子
    matcher_unmerged = FIFOMatcher(merge_partial=False)
    closed_raw, _ = matcher_unmerged.match(records)
    assert len(closed_raw) == 2


def test_fifo_one_sell_matches_multiple_buys():
    """测试单笔大额卖出同时匹配多笔历史小额买入."""
    matcher = FIFOMatcher(merge_partial=True)
    records = [
        _make_record(side="buy", ts=100.0, usd=100.0, qty=10.0),
        _make_record(side="buy", ts=150.0, usd=200.0, qty=20.0),
        _make_record(side="sell", ts=250.0, usd=450.0, qty=30.0),
    ]
    closed, open_pos = matcher.match(records)
    # 两次不同时刻的开仓，分别完成平仓
    assert len(closed) == 2
    assert len(open_pos) == 0
    assert closed[0].entry_ts == 100.0
    assert closed[0].token_amount == 10.0
    assert closed[1].entry_ts == 150.0
    assert closed[1].token_amount == 20.0


def test_fifo_trader_and_token_isolation():
    """测试不同交易员、不同代币之间互不串单撮合."""
    matcher = FIFOMatcher()
    records = [
        _make_record(trader="alice", token="token_a", side="buy", ts=10.0, usd=100.0, qty=10.0),
        _make_record(trader="bob", token="token_a", side="buy", ts=20.0, usd=200.0, qty=20.0),
        _make_record(trader="alice", token="token_b", side="buy", ts=30.0, usd=300.0, qty=30.0),
        _make_record(trader="alice", token="token_a", side="sell", ts=40.0, usd=150.0, qty=10.0),
    ]
    closed, open_pos = matcher.match(records)
    assert len(closed) == 1
    assert closed[0].trader == "alice"
    assert closed[0].token == "token_a"

    # bob 的 token_a 和 alice 的 token_b 仍在 open_positions 中
    assert len(open_pos) == 2
    open_traders = {op.trader for op in open_pos}
    assert open_traders == {"bob", "alice"}
