"""测试数据清洗器 (cleaner.py).

严格验证 6 大清洗规则:
1. 剔除单价 < $0.01 的跨链测试/手续费假仓
2. 成本 < $20 的记录剔除 (小额诱饵/退款)
3. 单笔收益 > 2000% 的对子剔除 (Relay 虚假对子)
4. 成交价 vs 市场价偏离 > 5 倍的剔除
5. fallback 假价格检测 (|R - (-0.017)| < 0.005) 剔除
6. 外部代买/空投转入打标 unsolicited (保留记录但不计入胜率统计)
"""

from backtest.pipeline.cleaner import Cleaner, CleanerConfig
from backtest.pipeline.matcher import ClosedPair


def _make_pair(
    entry_usd: float = 100.0,
    exit_usd: float = 150.0,
    token_amount: float = 1000.0,
    boss_entry_price: float = 0.0,
    boss_exit_price: float = 0.0,
    unsolicited: bool = False,
    hold_seconds: float = 3600.0,
    trader: str = "trader_a",
    token: str = "0xtoken",
) -> ClosedPair:
    return ClosedPair(
        trader=trader,
        token=token,
        entry_ts=1700000000.0,
        exit_ts=1700000000.0 + hold_seconds,
        entry_usd=entry_usd,
        exit_usd=exit_usd,
        token_amount=token_amount,
        hold_seconds=hold_seconds,
        unsolicited=unsolicited,
        boss_entry_price=boss_entry_price,
        boss_exit_price=boss_exit_price,
    )


def test_rule_1_low_unit_price():
    """规则 1: 单价 < $0.01 剔除."""
    # 当启用 min_unit_price=0.01 时
    cleaner = Cleaner(CleanerConfig(min_unit_price=0.01))

    # 单价 = 0.5 USD / 100 token = 0.005 < 0.01 -> 剔除
    p_low = _make_pair(entry_usd=0.5, token_amount=100.0)
    assert cleaner.is_low_unit_price(p_low) is True

    # 单价 = 20.0 USD / 100 token = 0.2 >= 0.01 -> 保留
    p_ok = _make_pair(entry_usd=20.0, token_amount=100.0)
    assert cleaner.is_low_unit_price(p_ok) is False

    kept, drops = cleaner.clean_pre_pricing([p_low, p_ok])
    assert len(kept) == 1
    assert drops["low_unit_price"] == 1


def test_rule_2_low_cost():
    """规则 2: 开仓成本 < $20 剔除."""
    cleaner = Cleaner(CleanerConfig(min_cost=20.0))

    # 成本 $15 < $20 -> 剔除
    p_cheap = _make_pair(entry_usd=15.0, exit_usd=30.0, token_amount=100.0)
    assert cleaner.is_low_cost(p_cheap) is True

    # 成本 $25 >= $20 -> 保留
    p_valid = _make_pair(entry_usd=25.0, exit_usd=50.0, token_amount=100.0)
    assert cleaner.is_low_cost(p_valid) is False

    kept, drops = cleaner.clean_pre_pricing([p_cheap, p_valid])
    assert len(kept) == 1
    assert drops["low_cost"] == 1


def test_rule_3_extreme_return():
    """规则 3: 单笔收益 > 2000% 剔除."""
    cleaner = Cleaner(CleanerConfig(max_return=20.0))

    # 收益 2500% (R = 25.0 > 20.0) -> 剔除
    p_extreme = _make_pair(entry_usd=100.0, exit_usd=2600.0, token_amount=100.0)
    assert cleaner.is_extreme_return(p_extreme) is True

    # 收益 50% (R = 0.5 <= 20.0) -> 保留
    p_normal = _make_pair(entry_usd=100.0, exit_usd=150.0, token_amount=100.0)
    assert cleaner.is_extreme_return(p_normal) is False

    kept, drops = cleaner.clean_pre_pricing([p_extreme, p_normal])
    assert len(kept) == 1
    assert drops["extreme_return"] == 1


def test_rule_4_price_deviation():
    """规则 4: 成交价 vs 市场价偏离 > 5 倍剔除."""
    cleaner = Cleaner(CleanerConfig(max_price_deviation=5.0))

    # 大V成交价 = 1.0 (100 / 100)
    pair = _make_pair(entry_usd=100.0, exit_usd=120.0, token_amount=100.0)

    # 市场价 6.0，偏离 6.0 / 1.0 = 6x > 5x -> 偏离
    assert cleaner.is_price_deviated(pair, market_entry_price=6.0) is True

    # 市场价 0.15，偏离 1.0 / 0.15 = 6.67x > 5x -> 偏离
    assert cleaner.is_price_deviated(pair, market_entry_price=0.15) is True

    # 市场价 1.2，偏离 1.2x <= 5x -> 正常
    assert cleaner.is_price_deviated(pair, market_entry_price=1.2) is False


def test_rule_5_fallback_fake_price():
    """规则 5: fallback 假价格检测 (|R - (-0.017)| < 0.005)."""
    cleaner = Cleaner(
        CleanerConfig(fallback_friction_target=-0.017, fallback_friction_tolerance=0.005)
    )

    # 纯摩擦假收益: R = -0.0169 (| -0.0169 - (-0.017) | = 0.0001 < 0.005) -> 假价格
    assert cleaner.is_fallback_fake_price(-0.0169) is True
    assert cleaner.is_fallback_fake_price(-0.020) is True  # 差 0.003 < 0.005

    # 真实市场行情波动的收益率 -> 正常保留
    assert cleaner.is_fallback_fake_price(0.15) is False
    assert cleaner.is_fallback_fake_price(-0.05) is False
    assert cleaner.is_fallback_fake_price(-0.01) is False

    pair1 = _make_pair(entry_usd=50.0)
    pair2 = _make_pair(entry_usd=60.0)
    items = [(pair1, -0.0172), (pair2, 0.25)]

    valid, dropped_count = cleaner.filter_fake_prices(items)
    assert len(valid) == 1
    assert valid[0][0] == pair2
    assert dropped_count == 1


def test_rule_6_unsolicited_tag():
    """规则 6: 外部代买/空投转入打标 unsolicited (保留记录但不计入胜率统计)."""
    cleaner = Cleaner(CleanerConfig(min_cost=20.0))

    p_normal = _make_pair(entry_usd=50.0, exit_usd=100.0, unsolicited=False)
    p_unsolicited = _make_pair(entry_usd=50.0, exit_usd=100.0, unsolicited=True)

    kept, drops = cleaner.clean_pre_pricing([p_normal, p_unsolicited])
    # 两者均保留在分析集中，但 drops 中统计到打标数量
    assert len(kept) == 2
    assert drops["unsolicited_flagged"] == 1
