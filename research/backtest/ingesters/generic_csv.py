"""通用 CSV 流水适配器 (历史研究兼容).

支持自定义列名映射表，将外部 CSV 记录转换为统一的 SwapRecord。
"""

import csv
import datetime
import io
from pathlib import Path
from typing import Any

from research.backtest.ingesters.base import BaseIngester, SwapRecord

DEFAULT_COLUMN_MAPPING: dict[str, str] = {
    "trader": "trader",
    "token": "token",
    "side": "side",
    "ts": "ts",
    "usd_amount": "usd_amount",
    "token_amount": "token_amount",
    "unsolicited": "unsolicited",
}


class GenericCSVIngester(BaseIngester):
    """通用 CSV 文件导入器."""

    def __init__(self, column_mapping: dict[str, str] | None = None) -> None:
        super().__init__()
        self.column_mapping = {**DEFAULT_COLUMN_MAPPING, **(column_mapping or {})}

    def ingest(self, source: Any) -> list[SwapRecord]:
        """解析 CSV 数据源.

        :param source: CSV 文件路径、Path、文件内容字符串、或可迭代行对象
        :return: 解析后的 SwapRecord 列表
        """
        rows: list[dict[str, str]] = []
        if isinstance(source, str | Path):
            path = Path(source)
            if path.exists() and path.is_file():
                with open(path, encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    rows = list(reader)
            else:
                # 可能是 CSV 文本内容
                reader = csv.DictReader(io.StringIO(str(source)))
                rows = list(reader)
        elif hasattr(source, "read"):
            reader = csv.DictReader(source)
            rows = list(reader)
        elif isinstance(source, list) and all(isinstance(x, dict) for x in source):
            rows = source
        else:
            raise ValueError(f"不支持的输入源类型: {type(source)}")

        self.total_records = len(rows)
        records: list[SwapRecord] = []
        cm = self.column_mapping

        for row in rows:
            try:
                trader = str(row.get(cm["trader"], "")).strip()
                token = str(row.get(cm["token"], "")).strip().lower()
                side_raw = str(row.get(cm["side"], "")).strip().lower()

                if side_raw in ("buy", "b", "in"):
                    side = "buy"
                elif side_raw in ("sell", "s", "out"):
                    side = "sell"
                else:
                    self.errors.append(f"无法识别的方向: {side_raw}")
                    continue

                ts_raw = row.get(cm["ts"], "")
                ts = self._parse_ts(ts_raw)
                usd_amount = float(row.get(cm["usd_amount"], 0.0) or 0.0)
                token_amount = float(row.get(cm["token_amount"], 0.0) or 0.0)

                unsolicited_raw = str(row.get(cm.get("unsolicited", "unsolicited"), "")).lower()
                unsolicited = unsolicited_raw in ("true", "1", "yes", "unsolicited")

                records.append(
                    SwapRecord(
                        trader=trader,
                        token=token,
                        side=side,
                        ts=ts,
                        usd_amount=usd_amount,
                        token_amount=token_amount,
                        raw=dict(row),
                        unsolicited=unsolicited,
                    )
                )
            except Exception as exc:
                self.errors.append(f"解析 CSV 行失败: {exc} | 原始: {row}")

        self.parsed_records = len(records)
        return records

    @staticmethod
    def _parse_ts(val: Any) -> float:
        if val is None:
            return 0.0
        if isinstance(val, int | float):
            return float(val)
        val_str = str(val).strip()
        try:
            return float(val_str)
        except ValueError:
            pass
        return datetime.datetime.fromisoformat(val_str.replace("Z", "+00:00")).timestamp()


__all__ = [
    "DEFAULT_COLUMN_MAPPING",
    "GenericCSVIngester",
]
