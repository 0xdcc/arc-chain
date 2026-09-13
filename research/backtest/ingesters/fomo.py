"""Fomo rh_swaps.json 适配器 (历史研究兼容).

根据 Fomo 跨链方向判定规则，将原始 JSON 记录解析为统一的 SwapRecord。
注意：历史 target_chain_id=4663 纯供离线研究与样本解析，严禁将 4663 / Robinhood 池注册至 Arc 活跃市场。
"""

import datetime
import json
from pathlib import Path
from typing import Any

from research.backtest.ingesters.base import BaseIngester, SwapRecord


class FomoIngester(BaseIngester):
    """Fomo 跨链 Swap 流水解析器."""

    def __init__(self, target_chain_id: int = 4663) -> None:
        super().__init__()
        self.target_chain_id = target_chain_id

    def ingest(self, source: Any) -> list[SwapRecord]:
        """解析 Fomo 数据源.

        :param source: JSON 文件路径、Path 对象、或已加载的 dict/list 数据
        :return: 解析后的 SwapRecord 列表
        """
        raw_items: list[dict[str, Any]]
        if isinstance(source, str | Path):
            with open(source, encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, list):
                    raw_items = loaded
                elif isinstance(loaded, dict) and "swaps" in loaded:
                    raw_items = loaded["swaps"]
                else:
                    raw_items = [loaded]
        elif isinstance(source, list):
            raw_items = source
        elif isinstance(source, dict):
            raw_items = source.get("swaps", [source])
        else:
            raise ValueError(f"不支持的输入源类型: {type(source)}")

        self.total_records = len(raw_items)
        records: list[SwapRecord] = []

        for item in raw_items:
            try:
                rec = self._parse_single(item)
                if rec is not None:
                    records.append(rec)
            except Exception as exc:
                self.errors.append(f"解析记录失败: {exc} | 原始: {item}")

        self.parsed_records = len(records)
        return records

    def _parse_single(self, item: dict[str, Any]) -> SwapRecord | None:
        in_net = item.get("inNetworkId")
        out_net = item.get("outNetworkId")
        target = self.target_chain_id

        # 1. 同链或非目标链跳过判定
        if in_net == target and out_net == target:
            self.skipped_same_chain += 1
            return None
        if in_net != target and out_net != target:
            self.skipped_other_chain += 1
            return None

        # 2. 方向判定
        if in_net != target and out_net == target:
            # 外链 -> 目标链: 买入
            side = "buy"
            token = str(item.get("outTokenAddress") or "").strip().lower()
            token_amount = float(item.get("outHumanAmount") or 0.0)
            usd_amount = float(item.get("humanUsdAmountIn") or 0.0)
        else:
            # 目标链 -> 外链: 卖出
            side = "sell"
            token = str(item.get("inTokenAddress") or "").strip().lower()
            token_amount = float(item.get("inHumanAmount") or 0.0)
            usd_amount = float(item.get("humanUsdAmountOut") or 0.0)

        if not token:
            self.errors.append(f"缺少代币地址: {item}")
            return None

        # 3. 时间戳解析 (ISO 8601 或 unix timestamp)
        created_at = item.get("createdAt")
        if isinstance(created_at, int | float):
            ts = float(created_at)
        elif isinstance(created_at, str):
            ts = datetime.datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
        else:
            ts = 0.0

        trader = str(item.get("trader") or item.get("userId") or "unknown")

        # 4. unsolicited (外部代买 / 空投 / 跨平台转入)
        unsolicited = (
            bool(item.get("isOffPlatform"))
            or bool(item.get("isCrossmint"))
            or bool(item.get("unsolicited"))
            or (usd_amount <= 0 and token_amount > 0)
        )

        return SwapRecord(
            trader=trader,
            token=token,
            side=side,
            ts=ts,
            usd_amount=usd_amount,
            token_amount=token_amount,
            raw=item,
            unsolicited=unsolicited,
        )


__all__ = [
    "FomoIngester",
]
