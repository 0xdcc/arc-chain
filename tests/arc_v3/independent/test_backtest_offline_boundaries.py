"""回测离线边界与缓存路径隔离测试 (Offline Boundaries & Path Traversal Guards).

验证要求：
1. 真实 PriceCache 正常加载与价格检索；
2. 本地无数据严格返回 None / [] / (None, 0.0)，绝不伪造价格；
3. offline=False 明确拒绝抛出 ValueError；
4. 绝对路径、路径穿越 (..)、路径分隔符 (/) 严拒绝；
5. 目录外符号链接严拒绝；
6. 默认 fallback_cache_dir 为 None，绝不隐式读取宿主 /root/notes；
7. 显式用户指定缓存根允许正常使用，无全局路径白名单限制；
8. engine.use_tick=True 缺少 tick_cache 时明确抛出 RuntimeError 拒绝。
"""

import json
from pathlib import Path

import pytest

from research.backtest.config import BacktestConfig
from research.backtest.engine import BacktestEngine
from research.backtest.price_cache import PriceCache
from research.fifo import SwapRecord


def test_price_cache_normal_operation(tmp_path: Path) -> None:
    """验证真实 PriceCache 在合法目录与合法 pool 标识下的正常加载与价格查找."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    pool_matrix = "0xpool_matrix"
    candles_matrix = [
        [1700000000, 1.25],
        [1700000060, 1.30],
    ]
    with open(cache_dir / f"k_{pool_matrix}.json", "w", encoding="utf-8") as f:
        json.dump(candles_matrix, f)

    rh_pool = "0xrh_token_pool"
    candles_rh = [
        [1700000000, 2.50],
        [1700000060, 2.60],
    ]
    with open(cache_dir / f"{rh_pool}.json", "w", encoding="utf-8") as f:
        json.dump(candles_rh, f)

    pc = PriceCache(cache_dir=cache_dir)
    loaded = pc.load_candles(pool_matrix)
    assert len(loaded) == 2
    assert loaded[0] == (1700000000, 1.25)
    assert pc.price_at(pool_matrix, 1700000000) == 1.25
    assert pc.price_at(pool_matrix, 1700000060) == 1.30

    loaded_rh = pc.load_candles(rh_pool)
    assert len(loaded_rh) == 2
    assert pc.price_at(rh_pool, 1700000000) == 2.50


def test_price_cache_missing_price_returns_none(tmp_path: Path) -> None:
    """验证缺价或缺失池子时严格返回 None / [] / (None, 0.0)，绝不伪造行情."""
    empty_cache = tmp_path / "empty_cache"
    pc = PriceCache(cache_dir=empty_cache)

    assert pc.price_at("0xnonexistent", 1700000000) is None
    assert pc.load_candles("0xnonexistent") == []
    assert pc.find_best_pool("0xnonexistent") == (None, 0.0)


def test_price_cache_offline_false_rejected() -> None:
    """验证 offline=False 被物理阻断抛出 ValueError."""
    with pytest.raises(ValueError, match="PriceCache offline=False 已被严格禁止"):
        PriceCache(offline=False)


def test_price_cache_path_boundaries_and_traversal_rejected(tmp_path: Path) -> None:
    """验证绝对路径、路径穿越 (..)、目录分隔符 (/) 等路径越界行为被严格阻断."""
    pc = PriceCache(cache_dir=tmp_path / "cache")

    # 1. 绝对路径拒绝
    with pytest.raises(ValueError, match="绝对路径|路径分隔符"):
        pc.load_candles("/etc/passwd")
    with pytest.raises(ValueError, match="绝对路径|路径分隔符"):
        pc.price_at("/etc/passwd", 1700000000)
    with pytest.raises(ValueError, match="绝对路径|路径分隔符"):
        pc.load_candles("/root/notes/fomo-backtest/cache/pool")

    # 2. 相对路径穿越 .. 拒绝
    with pytest.raises(ValueError, match="路径穿越|路径分隔符"):
        pc.load_candles("../escaped_pool")
    with pytest.raises(ValueError, match="路径穿越|路径分隔符"):
        pc.price_at("../../secret_pool", 1700000000)
    with pytest.raises(ValueError, match="路径穿越|路径分隔符"):
        pc.load_candles("0xpool/../../../etc/passwd")

    # 3. 目录分隔符拒绝
    with pytest.raises(ValueError, match="路径分隔符"):
        pc.load_candles("sub_dir/pool_name")
    with pytest.raises(ValueError, match="路径分隔符"):
        pc.load_candles(r"sub_dir\pool_name")

    # 4. 空标识与非法空字符拒绝
    with pytest.raises(ValueError, match="不能为空"):
        pc.load_candles("")
    with pytest.raises(ValueError, match="不能为空"):
        pc.load_candles("   ")
    with pytest.raises(ValueError, match="空字符|路径分隔符"):
        pc.load_candles("pool" + chr(0) + "evil")


def test_price_cache_symlink_out_of_bounds_rejected(tmp_path: Path) -> None:
    """验证缓存目录下指向目录外的符号链接被严格识别并拒绝."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir(parents=True, exist_ok=True)

    outside_file = outside_dir / "evil_candles.json"
    with open(outside_file, "w", encoding="utf-8") as f:
        json.dump([[1700000000, 999.0]], f)

    symlink_file = cache_dir / "k_sym_outside.json"
    symlink_file.symlink_to(outside_file)

    pc = PriceCache(cache_dir=cache_dir)
    with pytest.raises(ValueError, match="拒绝越界符号链接"):
        pc.load_candles("sym_outside")
    with pytest.raises(ValueError, match="拒绝越界符号链接"):
        pc.price_at("sym_outside", 1700000000)

    # pools_raw.json 符号链接越界拒绝
    meta_cache = tmp_path / "meta_cache"
    meta_cache.mkdir(parents=True, exist_ok=True)
    sym_meta = meta_cache / "pools_raw.json"
    sym_meta.symlink_to(outside_file)

    with pytest.raises(ValueError, match="拒绝越界符号链接"):
        PriceCache(cache_dir=meta_cache)


def test_price_cache_default_does_not_access_host_notes(tmp_path: Path) -> None:
    """验证默认参数 fallback_cache_dir 为 None，绝不隐式访问宿主外部 /root/notes."""
    pc = PriceCache(cache_dir=tmp_path / "cache")
    assert pc.fallback_cache_dir is None

    # 即便宿主实际存在 /root/notes/fomo-backtest/cache，PriceCache 默认实例也绝不引用它
    host_notes = Path("/root/notes/fomo-backtest/cache")
    assert pc.fallback_cache_dir != host_notes
    assert pc.fallback_cache_dir is None


def test_price_cache_user_specified_cache_allowed(tmp_path: Path) -> None:
    """验证用户显式指定的合法缓存目录可正常工作，不设虚构的全局路径白名单限制."""
    custom_dir = tmp_path / "custom_user_specified_cache_root"
    custom_dir.mkdir(parents=True, exist_ok=True)

    pool = "0xuser_pool"
    with open(custom_dir / f"k_{pool}.json", "w", encoding="utf-8") as f:
        json.dump([[1700000000, 42.0]], f)

    pc = PriceCache(cache_dir=custom_dir)
    assert pc.price_at(pool, 1700000000) == 42.0


def test_engine_use_tick_without_cache_rejected() -> None:
    """验证 engine 在 use_tick=True 且无 tick_cache 时明确抛出 RuntimeError 拒绝."""
    cfg = BacktestConfig(use_tick=True)
    engine = BacktestEngine(config=cfg, tick_cache=None)
    records = [
        SwapRecord(
            trader="0xtrader1",
            token="0xtokena",
            side="buy",
            ts=1700000000.0,
            usd_amount=100.0,
            token_amount=100.0,
        )
    ]
    with pytest.raises(RuntimeError, match="use_tick=True is blocked"):
        engine.run(records=records)
