"""Robinhood 链实时事件监听引擎 (Sequencer Feed 与 WS RPC 双通道).

功能:
1. 官方 Nitro Sequencer Feed (wss://feed.mainnet.chain.robinhood.com):
   接收 Nitro 序列消息包，毫秒级提取区块序列号、变动池子并触发针对性探测；
2. 标准 EVM WebSocket RPC (wss://robinhood-rpc.publicnode.com):
   通过 JSON-RPC eth_subscribe 订阅 newHeads 与 logs，捕获新块与池子 Swap 事件；
3. 双通道事件聚合与分发 (FeedEvent);
4. 自动指数退避断线重连 (Reconnect with backoff) 与线程安全启停管理。
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import websockets

from research.market_data.v4_reader import POOL_MANAGER_ADDRESS, SWAP_EVENT_TOPIC0

logger = logging.getLogger(__name__)

DEFAULT_SEQUENCER_FEED_URL = "wss://feed.mainnet.chain.robinhood.com"
DEFAULT_WS_RPC_URL = "wss://robinhood-rpc.publicnode.com"


@dataclass
class FeedEvent:
    """聚合事件模型."""

    source: str  # "sequencer_feed" | "ws_rpc"
    event_type: str  # "block" | "swap" | "sequence"
    block_number: int | None = None
    sequence_number: int | None = None
    touched_pools: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)
    raw_data: dict[str, Any] = field(default_factory=dict)


class FeedListener:
    """双通道事件监听器: 支持后台线程或协程运行，具备自动断线重连."""

    def __init__(
        self,
        feed_url: str | None = None,
        ws_rpc_url: str | None = None,
        on_event: Callable[[FeedEvent], None] | None = None,
        monitored_pools: Sequence[str] | None = None,
        reconnect_delay: float = 1.0,
        max_reconnect_delay: float = 30.0,
    ) -> None:
        self.feed_url = feed_url.strip() if feed_url and feed_url.strip() else None
        self.ws_rpc_url = ws_rpc_url.strip() if ws_rpc_url and ws_rpc_url.strip() else None
        self.callbacks: list[Callable[[FeedEvent], None]] = []
        if on_event is not None:
            self.callbacks.append(on_event)

        self.monitored_pools: set[str] = {p.lower() for p in (monitored_pools or []) if p}
        self.reconnect_delay = max(0.1, float(reconnect_delay))
        self.max_reconnect_delay = max(self.reconnect_delay, float(max_reconnect_delay))

        self._running = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._lock = threading.Lock()
        self._stop_requested = threading.Event()
        self._event_queue: queue.Queue[FeedEvent] = queue.Queue(maxsize=1024)
        self._callback_thread: threading.Thread | None = None
        self.dropped_events = 0
        self._rescan_required = threading.Event()
        self.last_seen: dict[str, float] = {}
        self.reconnects: dict[str, int] = {}
        self.consecutive_failures: dict[str, int] = {}
        self.terminal_error: str | None = None

    def _record_failure(self, source: str, exc: Exception) -> bool:
        """Trip after three consecutive receive/connect failures; do not retry forever."""
        count = self.consecutive_failures.get(source, 0) + 1
        self.consecutive_failures[source] = count
        self.reconnects[source] = self.reconnects.get(source, 0) + 1
        if count < 3:
            return False
        self.terminal_error = f"{source}: three consecutive failures ({type(exc).__name__})"
        self.enqueue_event(FeedEvent(source=source, event_type="failure"))
        self._stop_requested.set()
        self._running = False
        return True

    def enqueue_event(self, event: FeedEvent) -> None:
        """Network receive path only enqueues; overflow requests a full refresh."""
        if event.event_type != "failure":
            self.last_seen[event.source] = time.time()
        try:
            self._event_queue.put_nowait(event)
        except queue.Full:
            self.dropped_events += 1
            self._rescan_required.set()

    def _dispatch_worker(self) -> None:
        while self._is_active():
            try:
                event = self._event_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self.dispatch_event(event)
                if self._rescan_required.is_set():
                    self._rescan_required.clear()
                    self.dispatch_event(FeedEvent(source="queue_overflow", event_type="block"))
            finally:
                self._event_queue.task_done()

    def log_subscriptions(self) -> list[dict[str, Any]]:
        """Separate address pools from V4 PoolManager indexed pool IDs."""
        with self._lock:
            addresses = sorted(p for p in self.monitored_pools if len(p) == 42)
            identifiers = sorted(p for p in self.monitored_pools if len(p) == 66)
        filters: list[dict[str, Any]] = []
        if addresses:
            filters.append({"address": addresses})
        if identifiers:
            filters.append(
                {"address": POOL_MANAGER_ADDRESS, "topics": [SWAP_EVENT_TOPIC0, identifiers]}
            )
        return filters

    def register_callback(self, callback: Callable[[FeedEvent], None]) -> None:
        """注册事件监听回调函数."""
        with self._lock:
            if callback not in self.callbacks:
                self.callbacks.append(callback)

    def dispatch_event(self, event: FeedEvent) -> None:
        """广播分发事件至所有注册的回调."""
        with self._lock:
            cbs = list(self.callbacks)

        for cb in cbs:
            try:
                cb(event)
            except Exception as exc:  # noqa: BLE001
                logger.exception("FeedListener 回调执行异常: %s", exc)

    def on_event(self, event: FeedEvent) -> None:
        """默认事件处理入口 (兼容直接派生或赋值)."""
        self.dispatch_event(event)

    def set_monitored_pools(self, pools: Sequence[str]) -> None:
        """动态更新监控池地址列表."""
        with self._lock:
            self.monitored_pools = {p.lower() for p in pools if p}

    def parse_sequencer_message(self, data: str | bytes | dict[str, Any]) -> list[FeedEvent]:
        """解析 Nitro Sequencer Feed 消息."""
        if isinstance(data, bytes):
            data_str = data.decode("utf-8", errors="replace")
        elif isinstance(data, str):
            data_str = data
        else:
            data_str = json.dumps(data)

        try:
            payload = json.loads(data_str) if not isinstance(data, dict) else data
        except Exception:
            return []

        if not isinstance(payload, dict):
            return []

        # 检查是否命中了监控池地址
        touched: list[str] = []
        lower_str = data_str.lower()
        with self._lock:
            for pool in self.monitored_pools:
                if pool in lower_str:
                    touched.append(pool)

        events: list[FeedEvent] = []

        # 1. 批量 messages 数组包
        messages = payload.get("messages")
        if isinstance(messages, list) and messages:
            for item in messages:
                if isinstance(item, dict):
                    seq_num = item.get("sequenceNumber")
                    msg_body = item.get("message", {})
                    blk_num = None
                    if isinstance(msg_body, dict):
                        hdr = msg_body.get("header", {})
                        if isinstance(hdr, dict):
                            blk_num = hdr.get("blockNumber") or hdr.get("number")
                    events.append(
                        FeedEvent(
                            source="sequencer_feed",
                            event_type="sequence",
                            block_number=int(blk_num) if blk_num is not None else None,
                            sequence_number=int(seq_num) if seq_num is not None else None,
                            touched_pools=list(touched),
                            raw_data=item,
                        )
                    )
            return events

        # 2. 单条序列或区块消息
        seq_num = payload.get("sequenceNumber")
        blk_num = payload.get("block_number") or payload.get("blockNumber")
        e_type = "sequence" if seq_num is not None else "block"
        events.append(
            FeedEvent(
                source="sequencer_feed",
                event_type=e_type,
                block_number=int(blk_num) if blk_num is not None else None,
                sequence_number=int(seq_num) if seq_num is not None else None,
                touched_pools=list(touched),
                raw_data=payload,
            )
        )
        return events

    def parse_ws_rpc_message(self, data: str | bytes | dict[str, Any]) -> list[FeedEvent]:
        """解析标准 EVM WebSocket RPC 消息 (eth_subscribe 通知或响应)."""
        if isinstance(data, bytes):
            data_str = data.decode("utf-8", errors="replace")
        elif isinstance(data, str):
            data_str = data
        else:
            data_str = json.dumps(data)

        try:
            payload = json.loads(data_str) if not isinstance(data, dict) else data
        except Exception:
            return []

        if not isinstance(payload, dict):
            return []

        # 标准订阅通知 eth_subscription
        if payload.get("method") == "eth_subscription":
            params = payload.get("params", {})
            if isinstance(params, dict):
                res = params.get("result", {})
                if isinstance(res, dict):
                    # 1. newHeads 区块头事件
                    if "number" in res and ("hash" in res or "parentHash" in res):
                        raw_num = res["number"]
                        blk = (
                            int(raw_num, 16)
                            if isinstance(raw_num, str) and raw_num.startswith("0x")
                            else int(raw_num)
                        )
                        return [
                            FeedEvent(
                                source="ws_rpc",
                                event_type="block",
                                block_number=blk,
                                raw_data=res,
                            )
                        ]

                    # 2. logs 合约事件 (如 Swap)
                    if "address" in res and "topics" in res:
                        if res.get("removed", False):
                            return []
                        addr = str(res["address"]).lower()
                        touched = [addr]
                        if addr == POOL_MANAGER_ADDRESS.lower():
                            topics = res["topics"]
                            if (
                                len(topics) < 2
                                or str(topics[0]).lower() != SWAP_EVENT_TOPIC0
                                or len(str(topics[1])) != 66
                            ):
                                return []
                            pool_id = str(topics[1]).lower()
                            try:
                                int(pool_id[2:], 16)
                            except ValueError:
                                return []
                            with self._lock:
                                if pool_id not in self.monitored_pools:
                                    return []
                            touched = [pool_id]
                        raw_blk = res.get("blockNumber")
                        log_blk: int | None = None
                        if raw_blk is not None:
                            log_blk = (
                                int(raw_blk, 16)
                                if isinstance(raw_blk, str) and raw_blk.startswith("0x")
                                else int(raw_blk)
                            )
                        return [
                            FeedEvent(
                                source="ws_rpc",
                                event_type="swap",
                                block_number=log_blk,
                                touched_pools=touched,
                                raw_data=res,
                            )
                        ]

                    return [
                        FeedEvent(
                            source="ws_rpc",
                            event_type="block",
                            raw_data=res,
                        )
                    ]

        # 直接 RPC 结果 (如响应 blockNumber)
        if "result" in payload and isinstance(payload["result"], str):
            res_str = payload["result"]
            if res_str.startswith("0x"):
                try:
                    blk = int(res_str, 16)
                    return [
                        FeedEvent(
                            source="ws_rpc",
                            event_type="block",
                            block_number=blk,
                            raw_data=payload,
                        )
                    ]
                except ValueError:
                    pass

        return []

    async def _listen_sequencer_feed(self) -> None:
        """Sequencer Feed 长连接监听协程 (带指数退避)."""
        delay = self.reconnect_delay
        feed_url = self.feed_url
        if not feed_url:
            return

        while self._is_active():
            try:
                logger.info("Connecting to Sequencer Feed: %s", feed_url)
                async with websockets.connect(
                    feed_url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                ) as ws:
                    delay = self.reconnect_delay
                    logger.info("Sequencer Feed connected successfully: %s", feed_url)
                    while self._is_active():
                        msg = await ws.recv()
                        for event in self.parse_sequencer_message(msg):
                            self.consecutive_failures["sequencer_feed"] = 0
                            self.enqueue_event(event)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if not self._is_active():
                    break
                if self._record_failure("sequencer_feed", exc):
                    for task in self._tasks:
                        if task is not asyncio.current_task():
                            task.cancel()
                    break
                logger.warning(
                    "Sequencer Feed connection lost (%s). Reconnecting in %.1fs...",
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, self.max_reconnect_delay)

    async def _listen_ws_rpc(self) -> None:
        """WS RPC 长连接监听协程 (带指数退避与 eth_subscribe)."""
        delay = self.reconnect_delay
        rpc_url = self.ws_rpc_url
        if not rpc_url:
            return

        while self._is_active():
            try:
                logger.info("Connecting to WS RPC: %s", rpc_url)
                async with websockets.connect(
                    rpc_url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                ) as ws:
                    delay = self.reconnect_delay
                    logger.info("WS RPC connected successfully: %s", rpc_url)

                    # 订阅 newHeads
                    sub_heads = {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "eth_subscribe",
                        "params": ["newHeads"],
                    }
                    await ws.send(json.dumps(sub_heads))

                    # 若有监控池，订阅相关 logs
                    for sub_id, log_filter in enumerate(self.log_subscriptions(), start=2):
                        sub_logs = {
                            "jsonrpc": "2.0",
                            "id": sub_id,
                            "method": "eth_subscribe",
                            "params": ["logs", log_filter],
                        }
                        await ws.send(json.dumps(sub_logs))

                    while self._is_active():
                        msg = await ws.recv()
                        for event in self.parse_ws_rpc_message(msg):
                            self.consecutive_failures["ws_rpc"] = 0
                            self.enqueue_event(event)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if not self._is_active():
                    break
                if self._record_failure("ws_rpc", exc):
                    for task in self._tasks:
                        if task is not asyncio.current_task():
                            task.cancel()
                    break
                logger.warning(
                    "WS RPC connection lost (%s). Reconnecting in %.1fs...",
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, self.max_reconnect_delay)

    async def run_async(self) -> None:
        """异步运行主循环."""
        if self._stop_requested.is_set():
            return
        self._running = True
        self._callback_thread = threading.Thread(
            target=self._dispatch_worker, name="FeedCallbacks", daemon=True
        )
        self._callback_thread.start()
        tasks: list[asyncio.Task[None]] = []
        if self.feed_url:
            tasks.append(asyncio.create_task(self._listen_sequencer_feed()))
        if self.ws_rpc_url:
            tasks.append(asyncio.create_task(self._listen_ws_rpc()))

        self._tasks = tasks
        try:
            if not tasks:
                logger.warning("FeedListener 启动无有效 URL 配置")
                while self._is_active():
                    await asyncio.sleep(0.1)
            else:
                await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            if self._callback_thread:
                self._callback_thread.join(timeout=1)

    def _thread_worker(self) -> None:
        """后台守护线程工作体."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self.run_async())
        finally:
            self._loop.close()
            self._loop = None

    def start(self) -> None:
        """启动后台事件监听线程."""
        with self._lock:
            if self.terminal_error:
                raise RuntimeError("Feed listener is latched after repeated failures")
            if self._running or (self._thread and self._thread.is_alive()):
                return
            if self._callback_thread and self._callback_thread.is_alive():
                raise RuntimeError("Previous callback worker has not stopped")
            self._stop_requested.clear()
            self._running = True
            self._thread = threading.Thread(
                target=self._thread_worker,
                name="FeedListenerWorker",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        """安全停止后台监听线程与网络连接."""
        self._stop_requested.set()
        self._running = False
        with self._lock:
            loop = self._loop
            tasks = list(self._tasks)
            thread = self._thread

        if loop and loop.is_running():
            for t in tasks:
                loop.call_soon_threadsafe(t.cancel)

        if thread and thread.is_alive():
            thread.join(timeout=timeout)

        callback_thread = self._callback_thread
        if callback_thread and callback_thread.is_alive():
            callback_thread.join(timeout=timeout)
        if (thread and thread.is_alive()) or (callback_thread and callback_thread.is_alive()):
            raise TimeoutError("Feed listener worker did not stop; thread handles retained")

        with self._lock:
            self._thread = None
            self._loop = None
            self._tasks.clear()
            self._running = False

    def is_running(self) -> bool:
        """检查监听器是否处于运行状态."""
        return bool(self._running and self._thread and self._thread.is_alive())

    def _is_active(self) -> bool:
        """辅助状态检查 (防止 mypy 静态循环断言)."""
        return bool(self._running and not self._stop_requested.is_set())
