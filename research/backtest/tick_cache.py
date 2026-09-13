"""链上秒级 Tick 价格纯离线缓存与检索层 (历史研究兼容).

【历史研究兼容性声明 / Historical Research Compatibility Notice】
本模块数据结构与查询算法移植自原回测套件与离线研究上游：
- 真实上游来源: /root/projects/crypto/v1-legacy/dex-sniper-engine/backtest/data/tick_cache.py
  (SHA-256: 5087e26455a896cfbed7521561d761dd074fc552e330f32c43545d8bab8e1ec0)

从本地落盘缓存快速检索真实成交价与时间戳，
实现秒级精度与无价格即无成交 (no_trade_at_signal) 语义，根治假价格污染、0 值哨兵与非有限值误用。
零默认联网 RPC 依赖、零网络预热与 checkpoint 探针、零旧 Robinhood 导入。
"""

from __future__ import annotations

import bisect
import json
import math
import os
from pathlib import Path
from typing import Any

from research.backtest.swap_decoder import Tick


class TickCache:
    """链上逐笔 Swap Tick 本地纯离线缓存与二分查询执行器."""

    def __init__(
        self,
        cache_dir: str | Path = "research/backtest/data/tick_cache",
        rpc: Any | None = None,
        offline: bool = True,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        # 纯离线安全设计：默认不实例化任何 RPC 客户端，不预热网络或 checkpoints
        self.rpc = rpc
        self.offline = offline

        # 内存缓存: pool_lower -> list[Tick] (按 ts, block 升序)
        self._ticks: dict[str, list[Tick]] = {}

        # 区块时间戳索引
        self._block_ts_cache: dict[int, int] = {}
        self._load_block_ts_cache()

    def _load_block_ts_cache(self) -> None:
        """从磁盘缓存载入已知区块时间戳索引 (纯本地读取，零网络调用)."""
        ts_cache_path = self.cache_dir / "block_ts_cache.json"
        if ts_cache_path.exists():
            try:
                with open(ts_cache_path, encoding="utf-8") as f:
                    raw_cache = json.load(f)
                for k, v in raw_cache.items():
                    self._block_ts_cache[int(k)] = int(v)
            except Exception:
                pass

    def _save_block_ts_cache(self) -> None:
        """将内存已知区块时间戳索引持久化到磁盘."""
        if not hasattr(self, "_block_ts_cache") or not self._block_ts_cache:
            return
        ts_cache_path = self.cache_dir / "block_ts_cache.json"
        try:
            with open(ts_cache_path, "w", encoding="utf-8") as f:
                json.dump({str(k): v for k, v in self._block_ts_cache.items()}, f)
        except Exception:
            pass

    def _pool_file(self, pool: str) -> Path:
        """安全校验并返回指定池子的本地 JSON 缓存文件路径."""
        if not isinstance(pool, str):
            raise ValueError(f"pool 标识符必须为字符串，收到: {type(pool).__name__}")
        cleaned = pool.strip().lower()
        if not cleaned:
            raise ValueError("pool 标识符不能为空")

        if "/" in cleaned or "\\" in cleaned or "\x00" in cleaned:
            raise ValueError(f"pool 标识符禁止包含路径分隔符或空字符: {pool!r}")
        if os.sep in cleaned or (os.altsep and os.altsep in cleaned):
            raise ValueError(f"pool 标识符禁止包含路径分隔符: {pool!r}")

        path_obj = Path(cleaned)
        if path_obj.is_absolute():
            raise ValueError(f"pool 标识符禁止为绝对路径: {pool!r}")
        if ".." in cleaned or ".." in path_obj.parts:
            raise ValueError(f"pool 标识符禁止包含路径穿越片段 '..': {pool!r}")
        if len(path_obj.parts) != 1:
            raise ValueError(f"pool 标识符必须为单个文件名标识，禁止目录层级: {pool!r}")

        return self.cache_dir / f"{cleaned}.json"

    def load_ticks(self, pool: str) -> list[Tick]:
        """载入指定池子的全部已缓存 Tick，优先内存缓存 (纯离线读取)."""
        pool_lower = pool.strip().lower()
        if pool_lower in self._ticks:
            return self._ticks[pool_lower]

        path = self._pool_file(pool_lower)
        if not path.exists():
            return []

        try:
            with open(path, encoding="utf-8") as f:
                raw_data = json.load(f)
            ticks = [Tick.from_list(item) for item in raw_data]
            ticks.sort(key=lambda t: (t.ts, t.block))
            self._ticks[pool_lower] = ticks
            return ticks
        except Exception:
            return []

    def save_ticks(self, pool: str, ticks: list[Tick]) -> None:
        """将池子 Tick 列表持久化落盘并同步至内存缓存."""
        pool_lower = pool.strip().lower()
        self._ticks[pool_lower] = ticks
        path = self._pool_file(pool_lower)
        data = [t.to_list() for t in ticks]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def merge_tick(self, pool: str, tick: Tick) -> None:
        """合并单个 tick 到池子缓存并落盘 (去重键: block, tx_hash, amount0, amount1)."""
        pool_lower = pool.strip().lower()
        existing = self.load_ticks(pool_lower)
        key = (tick.block, tick.tx_hash, tick.amount0, tick.amount1)
        for t in existing:
            if (t.block, t.tx_hash, t.amount0, t.amount1) == key:
                return
        merged = sorted(existing + [tick], key=lambda t: (t.ts, t.block))
        self.save_ticks(pool_lower, merged)

    def mark_empty_window(self, pool: str, win_lo: int, win_hi: int) -> None:
        """记录空窗口标记（以 1 wei 假 tick 实现——price=0 会被查询侧过滤）."""
        marker = Tick(
            block=win_lo,
            ts=win_lo + 1,
            price=0.0,
            amount0=0,
            amount1=0,
            tx_hash=f"empty_{win_lo}_{win_hi}",
        )
        self.merge_tick(pool, marker)

    def fetch_pool_ticks(
        self,
        pool: str,
        ts_from: int,
        ts_to: int,
        force_refetch: bool = False,
        max_span: int = 50000,
    ) -> list[Tick]:
        """抓取指定池子在 [ts_from, ts_to] 时间窗口内的 Swap Tick.

        纯离线契约保护：
        1. 当 force_refetch=True 时，若处于离线模式或 rpc=None，明确抛出 RuntimeError；
        2. 当处于非离线模式 (offline=False) 但 rpc=None 时，明确抛出 RuntimeError；
        3. 绝不实现网络查询伪方法，绝不默默忽略 fetch 请求假装成功。
        4. 在合法纯离线模式下，返回本地已缓存切片。
        """
        if force_refetch and (self.offline or self.rpc is None):
            raise RuntimeError(
                "TickCache: force_refetch requires an active network RPC client, "
                "but TickCache is configured offline or rpc is None."
            )
        if not self.offline and self.rpc is None:
            raise RuntimeError(
                "TickCache: online fetch requested but rpc client is None. "
                "Pure offline mode does not allow network queries."
            )

        pool_lower = pool.strip().lower()
        existing = self.load_ticks(pool_lower)
        return [t for t in existing if ts_from <= t.ts <= ts_to]

    def price_at(self, pool: str, ts: float, tol_seconds: int = 300) -> float | None:
        """根据池子地址与时间戳获取最近成交价，容差内无成交返回 None."""
        price, _ = self.price_at_with_meta(pool, ts, tol_seconds=tol_seconds)
        return price

    def price_at_with_meta(
        self, pool: str, ts: float, tol_seconds: int = 300
    ) -> tuple[float | None, str]:
        """获取最近真实成交价并附带状态标记.

        返回:
            (price, "exact") - 容差内有真实成交 (严格保证 math.isfinite 且 price > 0)
            (None, "no_trade_at_signal") - 容差内无成交 (根治 fallback 假价格污染、0 值哨兵与非有限值)
        """
        if not pool or ts <= 0 or not math.isfinite(ts):
            return None, "no_trade_at_signal"

        pool_lower = pool.strip().lower()
        ticks = self.load_ticks(pool_lower)
        if not ticks:
            return None, "no_trade_at_signal"

        ts_keys = [t.ts for t in ticks]
        idx = bisect.bisect_right(ts_keys, int(ts))

        # 优先选择发生于 ts 之前且最接近的成交 (ts - candidate.ts <= tol_seconds)
        if idx > 0:
            candidate = ticks[idx - 1]
            if (
                (ts - candidate.ts) <= tol_seconds
                and math.isfinite(candidate.price)
                and candidate.price > 0
            ):
                return candidate.price, "exact"

        # 若 <= ts 无成交，检查稍微滞后的最近一笔成交 (> ts)
        if idx < len(ticks):
            candidate = ticks[idx]
            if (
                (candidate.ts - ts) <= tol_seconds
                and math.isfinite(candidate.price)
                and candidate.price > 0
            ):
                return candidate.price, "exact"

        return None, "no_trade_at_signal"
