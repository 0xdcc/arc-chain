"""离线行情数据本地缓存层 (纯离线历史研究兼容).

仅支持纯本地文件缓存 (OFFLINE) 模式，完全剥离 urllib 等网络抓取路径。
当 offline=False 时明确拒绝（非静默抛错），当本地无数据时保持原语义返回 (None, 0.0) 或 None，绝不伪造虚假价格。
"""

import bisect
import json
import os
from pathlib import Path
from typing import Any


class PriceCache:
    """代币池信息与 1m/5m K线价格本地离线缓存."""

    def __init__(
        self,
        cache_dir: str | Path = "backtest/data/cache",
        fallback_cache_dir: str | Path | None = None,
        offline: bool = True,
        proxy: str = "http://127.0.0.1:7890",
        network: str = "robinhood",
        rate_limit_interval: float = 0.45,
    ) -> None:
        if not offline:
            raise ValueError(
                "PriceCache offline=False 已被严格禁止：本工程运行于纯离线研究环境，禁止发起网络行情请求"
            )
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.fallback_cache_dir = Path(fallback_cache_dir) if fallback_cache_dir else None
        self.offline = True
        self.proxy = proxy
        self.network = network
        self.rate_limit_interval = rate_limit_interval

        # 运行时状态
        self._last_request_time: float = 0.0
        self.errors: list[str] = []

        # 内存缓存: pool -> sorted list of (timestamp, close_price)
        self._candles: dict[str, list[tuple[int, float]]] = {}
        # pool -> bar_step (in seconds)
        self._steps: dict[str, int] = {}
        # token -> (best_pool_address, reserve_in_usd)
        self._pool_meta: dict[str, tuple[str, float]] = {}

        # 加载本地池子元数据 (若存在 pools_raw.json)
        self._load_pools_meta()

    @staticmethod
    def _validate_pool_identifier(pool: str) -> str:
        """严格校验 pool 标识符为单文件名标识，严禁绝对路径、路径穿越与目录分隔符."""
        if not isinstance(pool, str):
            raise ValueError(f"pool 标识符必须为字符串，收到: {type(pool).__name__}")
        cleaned = pool.strip()
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

        return cleaned.lower()

    def _load_pools_meta(self) -> None:
        """从本地 cache_dir 或 fallback_cache_dir 读取 pools_raw.json 建立映射."""
        for cdir in (self.cache_dir, self.fallback_cache_dir):
            if cdir is None:
                continue
            path = cdir / "pools_raw.json"
            if path.exists() or path.is_symlink():
                resolved = path.resolve()
                if not resolved.is_relative_to(cdir.resolve()):
                    raise ValueError(f"拒绝越界符号链接: {path} -> {resolved}")
                if path.exists():
                    try:
                        with open(path, encoding="utf-8") as f:
                            raw: dict[str, Any] = json.load(f)
                        for tok, pools in raw.items():
                            tok_lower = tok.lower()
                            if not pools:
                                continue
                            best = max(
                                pools,
                                key=lambda x: float(
                                    (x.get("attributes") or {}).get("reserve_in_usd") or 0.0
                                ),
                            )
                            best_attr = best.get("attributes") or {}
                            p_addr = str(best_attr.get("address") or "").lower()
                            p_liq = float(best_attr.get("reserve_in_usd") or 0.0)
                            if p_addr:
                                self._pool_meta[tok_lower] = (p_addr, p_liq)
                        break
                    except Exception as exc:
                        self.errors.append(f"加载 pools_raw.json 失败: {exc}")

    def find_best_pool(self, token: str) -> tuple[str | None, float]:
        """查询代币流动性最大的池子 (地址, reserve_in_usd).

        纯离线语义：仅查询本地缓存元数据；本地无记录返回 (None, 0.0)，绝不伪造虚假池信息。
        """
        tok_lower = token.strip().lower()
        if tok_lower in self._pool_meta:
            return self._pool_meta[tok_lower]

        if not self.offline:
            raise ValueError("PriceCache offline=False 已被禁用，无法发起远程网络查询")

        return None, 0.0

    def load_candles(self, pool: str) -> list[tuple[int, float]]:
        """从缓存或本地文件加载指定池子的 K 线数据."""
        pool_lower = self._validate_pool_identifier(pool)
        if pool_lower in self._candles:
            return self._candles[pool_lower]

        candidate_dirs: list[tuple[Path, Path]] = [
            (self.cache_dir / f"k_{pool_lower}.json", self.cache_dir),
            (self.cache_dir / f"{pool_lower}.json", self.cache_dir),
        ]
        if self.fallback_cache_dir:
            candidate_dirs.extend(
                [
                    (self.fallback_cache_dir / f"{pool_lower}.json", self.fallback_cache_dir),
                    (self.fallback_cache_dir / f"k_{pool_lower}.json", self.fallback_cache_dir),
                ]
            )

        for p, cdir in candidate_dirs:
            if p.exists() or p.is_symlink():
                resolved = p.resolve()
                if not resolved.is_relative_to(cdir.resolve()):
                    raise ValueError(f"拒绝越界符号链接: {p} -> {resolved}")
                if p.exists():
                    try:
                        with open(p, encoding="utf-8") as f:
                            raw = json.load(f)
                        candles = sorted([(int(t), float(c)) for t, c in raw])
                        self._candles[pool_lower] = candles
                        self._steps[pool_lower] = self._detect_step(candles)
                        return candles
                    except Exception as exc:
                        self.errors.append(f"读取池子缓存失败 {p}: {exc}")

        self._candles[pool_lower] = []
        return []

    def fetch_pool_candles(
        self,
        pool: str,
        min_target_ts: float | None = None,
        max_target_ts: float | None = None,
    ) -> list[tuple[int, float]]:
        """离线模式仅加载本地缓存，网络抓取已被彻底剔除."""
        if not self.offline:
            raise ValueError("PriceCache offline=False 已被禁用，网络拉取被物理封印")
        return self.load_candles(pool)

    def price_at(self, pool: str, ts: float, tol_bars: int = 5) -> float | None:
        """根据池子地址与时间戳获取收盘价.

        规则: 取 timestamp <= ts 最近一根的 close；上下 tol_bars 根内无有效值返回 None。
        纯离线语义：无有效数据返回 None，绝不伪造价格。
        """
        if not pool or ts <= 0:
            return None

        pool_lower = self._validate_pool_identifier(pool)
        candles = self.load_candles(pool_lower)
        if not candles:
            return None

        step = self._steps.get(pool_lower, 60)
        tol_seconds = tol_bars * step

        idx = bisect.bisect_right(candles, (int(ts), float("inf")))
        # idx - 1 为 timestamp <= ts 且最接近的 K 线
        if idx > 0:
            t, c = candles[idx - 1]
            if ts - t <= tol_seconds and c > 0:
                return c

        # 若 <= ts 无有效值，检查稍微滞后的最近 K 线 (> ts)
        if idx < len(candles):
            t, c = candles[idx]
            if t - ts <= tol_seconds and c > 0:
                return c

        return None

    @staticmethod
    def _detect_step(candles: list[tuple[int, float]]) -> int:
        """检测 K 线步长 (秒)，默认 60 秒."""
        if len(candles) < 2:
            return 60
        diffs = [
            candles[i + 1][0] - candles[i][0]
            for i in range(min(30, len(candles) - 1))
            if candles[i + 1][0] > candles[i][0]
        ]
        if not diffs:
            return 60
        min_diff = min(diffs)
        return min_diff if min_diff in (60, 300, 900, 3600) else 60


__all__ = [
    "PriceCache",
]
