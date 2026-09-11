"""Feed event worker with bounded queue and per-pool debounce map.

Receives high-frequency PoolManager and Swap events, coalesces multiple updates
for the same pool into the latest state, and exposes backpressure observability.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)


def _extract_pool_id(event: Any) -> str:
    """Extract pool identifier string from event object or mapping."""
    if isinstance(event, dict):
        for key in ("pool_id", "pool", "poolId", "address", "pool_address"):
            val = event.get(key)
            if val is not None and str(val).strip():
                return str(val).strip().lower()
        return str(id(event))

    for attr in ("pool_id", "pool", "poolId", "address", "pool_address"):
        if hasattr(event, attr):
            val = getattr(event, attr)
            if val is not None and str(val).strip():
                return str(val).strip().lower()

    return str(id(event))


class FeedEventWorker:
    """Thread-safe event worker providing debounce coalescing and bounded buffering.

    Maintains:
    1. Bounded Queue: limits buffer memory footprint under intense load.
    2. Debounce Map: merges multiple successive events for the same pool into the latest one.
    """

    def __init__(
        self,
        max_queue_size: int = 1000,
        busy_threshold: int | None = None,
        overflow_policy: str = "drop_oldest",
    ) -> None:
        if max_queue_size <= 0:
            raise ValueError(f"max_queue_size must be positive, got {max_queue_size}")

        self._max_queue_size = max_queue_size
        self._busy_threshold = (
            busy_threshold if busy_threshold is not None else max(1, int(max_queue_size * 0.8))
        )
        self._overflow_policy = overflow_policy

        self._lock = threading.Lock()
        self._debounce_map: OrderedDict[str, Any] = OrderedDict()

        # Metrics for observability
        self._total_received: int = 0
        self._total_debounced: int = 0
        self._total_dropped: int = 0

    def enqueue(self, event: Any) -> bool:
        """Enqueue event immediately without waiting for downstream consumers.

        Coalesces updates for the same pool so that only the latest state is retained.
        Returns True immediately.
        """
        pool_id = _extract_pool_id(event)

        with self._lock:
            self._total_received += 1
            if pool_id in self._debounce_map:
                # Debounce: retain the latest event state for this pool
                self._debounce_map[pool_id] = event
                self._total_debounced += 1
                return True

            # If bounded queue is full, handle overflow
            if len(self._debounce_map) >= self._max_queue_size:
                if self._overflow_policy == "drop_oldest":
                    self._debounce_map.popitem(last=False)
                    self._total_dropped += 1
                else:
                    self._total_dropped += 1
                    return False

            self._debounce_map[pool_id] = event
            return True

    # Aliases for callback integration
    push = enqueue
    on_event = enqueue

    def queue_size(self) -> int:
        """Return the current number of pending debounced pool events."""
        with self._lock:
            return len(self._debounce_map)

    def is_busy(self) -> bool:
        """Check if backpressure threshold has been reached."""
        with self._lock:
            return len(self._debounce_map) >= self._busy_threshold

    def pop_events(self, max_count: int | None = None) -> list[Any]:
        """Pop up to max_count events from the queue atomically.

        If max_count is None, pops and returns all currently queued events.
        """
        with self._lock:
            if max_count is None or max_count >= len(self._debounce_map):
                events = list(self._debounce_map.values())
                self._debounce_map.clear()
                return events

            count = max(0, max_count)
            events = []
            for _ in range(count):
                if not self._debounce_map:
                    break
                _, item = self._debounce_map.popitem(last=False)
                events.append(item)
            return events

    def clear(self) -> None:
        """Clear all events and reset the queue."""
        with self._lock:
            self._debounce_map.clear()

    def get_metrics(self) -> dict[str, Any]:
        """Return observability metrics for backpressure tracking."""
        with self._lock:
            return {
                "queue_size": len(self._debounce_map),
                "max_queue_size": self._max_queue_size,
                "busy_threshold": self._busy_threshold,
                "is_busy": len(self._debounce_map) >= self._busy_threshold,
                "total_received": self._total_received,
                "total_debounced": self._total_debounced,
                "total_dropped": self._total_dropped,
            }
