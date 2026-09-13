"""基于先进先出 (FIFO) 原则的持仓撮合器 (历史研究兼容).

【历史研究兼容性声明 / Historical Research Compatibility Notice】
本模块撮合算法移植自原回测套件与离线研究上游：
- 上游来源: /root/projects/crypto/v2-modular/dex-sniper-engine-modular/backtest/pipeline/matcher.py
  (SHA-256: cc1204f5d239869a4dfd9d2d84ae7ddba124f776f42e89a57aa90b2192d07909)

实现基于先进先出 (FIFO) 原则的持仓撮合，支持同一开仓多次部分卖出的聚合，
输出 ClosedPair 列表与未平仓 OpenPosition 列表。
本模块内部计算使用 float 浮点数及 1e-12 浮点切片容差，如实保留历史研究回测语义；
严禁接入生产链上金融执行（execution）整数路径；不借本次移植任务修改精度算法或清洗策略。
零网络、零 RPC、零执行器依赖、零 4663 池目录耦合。
"""

from collections import defaultdict, deque
from typing import Any

from research.fifo.models import ClosedPair, OpenPosition, SwapRecord


class FIFOMatcher:
    """FIFO 队列配对器."""

    def __init__(self, merge_partial: bool = True) -> None:
        self.merge_partial = merge_partial

    def match(self, records: list[SwapRecord]) -> tuple[list[ClosedPair], list[OpenPosition]]:
        """按 (trader, token) 进行 FIFO 撮合.

        :param records: SwapRecord 列表
        :return: (已平仓对子列表, 未平仓持仓列表)
        """
        by_trader_token: dict[tuple[str, str], list[SwapRecord]] = defaultdict(list)
        for r in records:
            by_trader_token[(r.trader, r.token)].append(r)

        closed_pairs: list[ClosedPair] = []
        open_positions: list[OpenPosition] = []

        for (trader, tok), group in by_trader_token.items():
            # 按时间戳升序排，时间相同买入优先
            group.sort(key=lambda x: (x.ts, 0 if x.side == "buy" else 1))

            buy_queue: deque[dict[str, Any]] = deque()
            # 记录同一 entry_ts 对应的所有部分成交切片
            matched_by_entry: dict[float, list[dict[str, Any]]] = defaultdict(list)
            raw_slices: list[ClosedPair] = []

            for rec in group:
                if rec.side == "buy":
                    buy_queue.append(
                        {
                            "entry_ts": rec.ts,
                            "initial_qty": rec.token_amount,
                            "rem_qty": rec.token_amount,
                            "usd": rec.usd_amount,
                            "unsolicited": rec.unsolicited,
                            "raw": rec.raw,
                        }
                    )
                else:
                    sell_rem_qty = rec.token_amount
                    sell_total_usd = rec.usd_amount

                    while sell_rem_qty > 1e-12 and buy_queue:
                        lot = buy_queue[0]
                        matched_qty = min(sell_rem_qty, lot["rem_qty"])
                        frac_lot = (
                            matched_qty / lot["initial_qty"] if lot["initial_qty"] > 0 else 0.0
                        )
                        matched_entry_usd = frac_lot * lot["usd"]

                        frac_sell = matched_qty / rec.token_amount if rec.token_amount > 0 else 0.0
                        matched_exit_usd = frac_sell * sell_total_usd

                        is_unsolicited = lot.get("unsolicited", False) or rec.unsolicited

                        slice_info = {
                            "trader": trader,
                            "token": tok,
                            "entry_ts": lot["entry_ts"],
                            "exit_ts": rec.ts,
                            "entry_usd": matched_entry_usd,
                            "exit_usd": matched_exit_usd,
                            "token_amount": matched_qty,
                            "hold_seconds": max(0.0, rec.ts - lot["entry_ts"]),
                            "unsolicited": is_unsolicited,
                        }
                        raw_slices.append(
                            ClosedPair(
                                trader=trader,
                                token=tok,
                                entry_ts=lot["entry_ts"],
                                exit_ts=rec.ts,
                                entry_usd=matched_entry_usd,
                                exit_usd=matched_exit_usd,
                                token_amount=matched_qty,
                                hold_seconds=max(0.0, rec.ts - lot["entry_ts"]),
                                unsolicited=is_unsolicited,
                            )
                        )
                        matched_by_entry[lot["entry_ts"]].append(slice_info)

                        lot["rem_qty"] -= matched_qty
                        sell_rem_qty -= matched_qty

                        if lot["rem_qty"] <= 1e-12:
                            buy_queue.popleft()

            if self.merge_partial:
                for entry_ts, slices in matched_by_entry.items():
                    exit_ts = max(s["exit_ts"] for s in slices)
                    entry_usd = sum(s["entry_usd"] for s in slices)
                    exit_usd = sum(s["exit_usd"] for s in slices)
                    token_amount = sum(s["token_amount"] for s in slices)
                    unsolicited = any(s["unsolicited"] for s in slices)

                    closed_pairs.append(
                        ClosedPair(
                            trader=trader,
                            token=tok,
                            entry_ts=entry_ts,
                            exit_ts=exit_ts,
                            entry_usd=entry_usd,
                            exit_usd=exit_usd,
                            token_amount=token_amount,
                            hold_seconds=max(0.0, exit_ts - entry_ts),
                            unsolicited=unsolicited,
                        )
                    )
            else:
                closed_pairs.extend(raw_slices)

            # 统计剩余未平仓买单
            while buy_queue:
                lot = buy_queue.popleft()
                if lot["rem_qty"] > 1e-12:
                    rem_frac = (
                        lot["rem_qty"] / lot["initial_qty"] if lot["initial_qty"] > 0 else 0.0
                    )
                    open_positions.append(
                        OpenPosition(
                            trader=trader,
                            token=tok,
                            entry_ts=lot["entry_ts"],
                            token_amount=lot["rem_qty"],
                            entry_usd=rem_frac * lot["usd"],
                            unsolicited=lot.get("unsolicited", False),
                        )
                    )

        # 最终按 entry_ts 排序输出
        closed_pairs.sort(key=lambda p: (p.entry_ts, p.exit_ts))
        open_positions.sort(key=lambda p: p.entry_ts)
        return closed_pairs, open_positions
