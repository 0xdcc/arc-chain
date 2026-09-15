"""Injected read-only feed event coordination; no trading or implicit RPC."""
import logging
import threading
from typing import Any

from research.market_data.feed_listener import FeedEvent
from research.market_data.read_round import ReadRoundCoordinator
from research.market_data.token_policy import is_tax_token

logger = logging.getLogger(__name__)


class FeedCoordinator(ReadRoundCoordinator):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._wake_event = threading.Event()

    def on_feed_event(self, event: FeedEvent) -> None:
            """处理来自 Sequencer Feed 或 WS RPC 的实时事件 (毫秒级响应)."""
            logger.debug(
                "收到 Feed 事件 [%s:%s] Block=%s Seq=%s Touched=%s",
                event.source,
                event.event_type,
                event.block_number,
                event.sequence_number,
                event.touched_pools,
            )

            # 1. 若变动池涉及已知税代币，立即物理阻断并在日志中记录 [TAX_BLOCKED]
            for addr in event.touched_pools:
                if is_tax_token(addr):
                    logger.warning(
                        "[TAX_BLOCKED] Feed 事件捕获税收代币变动池 (%s)，物理丢弃不予探测",
                        addr,
                    )
                    return

            # 2. 若指定了 touched_pools，触发局部快速针对性探测 (Targeted Fast Probe)
            if event.touched_pools and self.running:
                self._probe_touched_pools(event.touched_pools)

            # 3. 若为新块或新序列事件，唤醒主循环进行即时报价刷新
            if event.event_type in ("block", "sequence") and self.running:
                self._wake_event.set()

    def _probe_touched_pools(self, touched_addresses: list[str]) -> None:
        if self.reader is None:
            raise ValueError("An injected read-only reader is required for feed probes")
        touched = {address.lower() for address in touched_addresses}
        eligible = []
        tokens_by_pool = {}
        for pool in self.pools:
            address = getattr(pool, "address", "").lower()
            tokens = {
                (getattr(pool, "token0", "") or getattr(pool, "currency0", "")).lower(),
                (getattr(pool, "token1", "") or getattr(pool, "currency1", "")).lower(),
            } - {""}
            if is_tax_token(address) or any(is_tax_token(token) for token in tokens):
                continue
            eligible.append(pool)
            tokens_by_pool[id(pool)] = tokens
        matched = [p for p in eligible if p.address.lower() in touched or tokens_by_pool[id(p)] & touched]
        adjacent = set().union(*(tokens_by_pool[id(p)] for p in matched)) if matched else set()
        selected = [p for p in eligible if p in matched or tokens_by_pool[id(p)] & adjacent]
        if selected:
            quotes = self.reader.batch_quote(selected, max_workers=self.default_max_workers)
            self.handle_spread_alert(quotes)
