"""Uniswap V3 Swap 事件日志纯离线解码与数学换算模块 (历史研究兼容).

【历史研究兼容性声明 / Historical Research Compatibility Notice】
本模块数据结构与数学计算移植自原回测套件与离线研究上游：
- 真实上游来源: /root/projects/crypto/v1-legacy/dex-sniper-engine/backtest/data/swap_decoder.py
  (SHA-256: bb5daf0a4e6e8631d122f5b5e2990749737d97d5ec2893a0435b9dffb9d95904)

从区块日志数据中解码出精确价格与成交数量，提供纯离线数值转换与轻量数据结构定义。
本模块字段使用 float（浮点数）如实保留历史研究回测算法语义，专供纯离线回测、
秒级价格切片与因果复盘使用。
严禁接入生产链上金融执行（execution）整数路径；不借任务改动精度算法与清洗策略。
零网络、零 RPC、零执行器、零 4663 池目录耦合。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SWAP_TOPIC: str = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"


@dataclass
class Tick:
    """单笔链上成交记录 (纯离线轻量数据载体)."""

    block: int
    ts: int  # 区块时间戳（秒）
    price: float  # token1 per token0
    amount0: int  # 有符号 int256
    amount1: int  # 有符号 int256
    tx_hash: str

    def to_list(self) -> list[Any]:
        """序列化为 JSON 缓存数组格式: [block, ts, price, amount0, amount1, tx_hash]."""
        return [self.block, self.ts, self.price, self.amount0, self.amount1, self.tx_hash]

    @classmethod
    def from_list(cls, data: list[Any]) -> Tick:
        """从 JSON 缓存数组反序列化."""
        return cls(
            block=int(data[0]),
            ts=int(data[1]),
            price=float(data[2]),
            amount0=int(data[3]),
            amount1=int(data[4]),
            tx_hash=str(data[5]),
        )


def to_int256(val: int) -> int:
    """将 uint256 补码转换为 Python 有符号整数 (支持负数与边界值)."""
    return val - (1 << 256) if val >= (1 << 255) else val


def price_from_sqrt(sqrt_price_x96: int) -> float:
    """根据 Uniswap V3 sqrtPriceX96 计算价格 (token1 per token0).

    公式: price_token1_per_token0 = (sqrtPriceX96 / 2**96) ** 2

    NOTE(Contract-Conflict):
    数学规范与金融安全公理要求价格 P > 0。
    在此当 sqrt_price_x96 <= 0 时返回 0.0，系为向下兼容既有哨兵测试与空窗口标记，
    仅供历史回测与离线研究使用，不宣称 0.0 为有效可交易价格。
    生产金融执行严格 fail-closed；下游查询层 (TickCache) 与消费层 (BacktestEngine)
    必须前置过滤 price > 0，严禁将 0.0 当作可交易报价撮合或核算。
    """
    if sqrt_price_x96 <= 0:
        return 0.0
    return float((sqrt_price_x96 / (1 << 96)) ** 2)


def decode_swap_log(log: dict[str, Any], block_ts: int) -> Tick | None:
    """从 Swap 事件原始 log dict 解码为 Tick 结构 (纯离线内存解析).

    事件签名: Swap(sender, recipient, amount0, amount1, sqrtPriceX96, liquidity, tick)
    data 布局: 5 个 32-byte word (160 字节 = 320 hex 字符)
    """
    topics = log.get("topics") or []
    if not topics:
        return None
    if str(topics[0]).lower() != SWAP_TOPIC.lower():
        return None

    raw_data = str(log.get("data") or "")
    if raw_data.startswith("0x") or raw_data.startswith("0X"):
        raw_data = raw_data[2:]

    if len(raw_data) < 320:
        return None

    try:
        w0 = int(raw_data[0:64], 16)
        w1 = int(raw_data[64:128], 16)
        w2 = int(raw_data[128:192], 16)

        amount0 = to_int256(w0)
        amount1 = to_int256(w1)
        sqrt_price_x96 = w2
        price = price_from_sqrt(sqrt_price_x96)

        raw_block = log.get("blockNumber", 0)
        if isinstance(raw_block, str) and (
            raw_block.startswith("0x") or raw_block.startswith("0X")
        ):
            block = int(raw_block, 16)
        else:
            block = int(raw_block or 0)

        tx_hash = str(log.get("transactionHash") or "")

        return Tick(
            block=block,
            ts=block_ts,
            price=price,
            amount0=amount0,
            amount1=amount1,
            tx_hash=tx_hash,
        )
    except Exception:
        return None
