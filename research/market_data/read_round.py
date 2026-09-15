"""Single-round market data reading and dependency-injected orchestration.

Provides deterministic single-round quote collection, status reporting, and spread alert handling
without persistent daemon threads, background polling, or network side effects.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from research.market_data.multicall import AnyPool, PriceQuote

logger = logging.getLogger(__name__)

__all__ = [
    "ArbitrageRoundCoordinator",
    "ReadRoundCoordinator",
    "RoundResult",
    "RoundStatus",
    "execute_read_round",
]


@dataclass(slots=True)
class RoundStatus:
    """Execution metrics and status emitted after a single read round."""

    round_id: int
    timestamp: float
    pool_count: int
    quotes_count: int
    duration_sec: float
    success: bool
    latest_block: int | None = None
    error: str | None = None


@dataclass(slots=True)
class RoundResult:
    """Result artifact of a single-round read execution."""

    status: RoundStatus
    quotes: list[PriceQuote] = field(default_factory=list)
    alerts: list[Any] = field(default_factory=list)


class ReadRoundCoordinator:
    """Deterministic single-round quote reading coordinator.

    Strictly offline-safe:
    - Zero background threads / daemon processes.
    - Zero continuous while loops / socket polling.
    - Full dependency injection for reader, pools, and alert/status callbacks.
    """

    def __init__(
        self,
        mode: str = "spread",
        min_tvl: float = 0.0,
        reader: Any = None,
        on_spread_alert: Callable[[list[PriceQuote]], Any] | None = None,
        on_status_update: Callable[[RoundStatus], Any] | None = None,
        on_error: Callable[[Exception], Any] | None = None,
        default_max_workers: int = 15,
    ) -> None:
        self.mode = mode
        self.min_tvl = min_tvl
        self.reader = reader
        self.pools: list[AnyPool] = []
        self.running: bool = False
        self.default_max_workers = default_max_workers
        self._round_counter: int = 0
        self._last_status: RoundStatus | None = None
        self._on_spread_alert = on_spread_alert
        self._on_status_update = on_status_update
        self._on_error = on_error

    def handle_spread_alert(self, quotes: list[PriceQuote]) -> list[PriceQuote]:
        """Process price quotes and invoke spread alert handler if injected."""
        if self._on_spread_alert is not None:
            self._on_spread_alert(quotes)
        return quotes

    def update_status(self, status: RoundStatus | None = None) -> RoundStatus:
        """Update and record status metadata, dispatching to listener if injected."""
        if status is None:
            status = self._last_status or RoundStatus(
                round_id=self._round_counter,
                timestamp=time.time(),
                pool_count=len(self.pools),
                quotes_count=0,
                duration_sec=0.0,
                success=True,
            )
        self._last_status = status
        if self._on_status_update is not None:
            self._on_status_update(status)
        return status

    def scan_round(self, max_workers: int | None = None) -> RoundResult:
        """Execute a single synchronous round of concurrent pool quoting."""
        self._round_counter += 1
        workers = max_workers if max_workers is not None else self.default_max_workers
        start_t = time.time()

        if self.reader is None:
            err = ValueError("Reader dependency must be configured before calling scan_round")
            if self._on_error is not None:
                self._on_error(err)
            status = RoundStatus(
                round_id=self._round_counter,
                timestamp=start_t,
                pool_count=len(self.pools),
                quotes_count=0,
                duration_sec=0.0,
                success=False,
                error=str(err),
            )
            self.update_status(status)
            return RoundResult(status=status, quotes=[], alerts=[])

        try:
            quotes = self.reader.batch_quote(self.pools, max_workers=workers)
            duration = time.time() - start_t
            latest_block = max((q.block_number for q in quotes), default=None) if quotes else None
            alerts = self.handle_spread_alert(quotes)
            status = RoundStatus(
                round_id=self._round_counter,
                timestamp=start_t,
                pool_count=len(self.pools),
                quotes_count=len(quotes),
                duration_sec=duration,
                success=True,
                latest_block=latest_block,
            )
            self.update_status(status)
            return RoundResult(
                status=status,
                quotes=quotes,
                alerts=alerts if isinstance(alerts, list) else [],
            )
        except Exception as exc:
            duration = time.time() - start_t
            if self._on_error is not None:
                self._on_error(exc)
            status = RoundStatus(
                round_id=self._round_counter,
                timestamp=start_t,
                pool_count=len(self.pools),
                quotes_count=0,
                duration_sec=duration,
                success=False,
                error=str(exc),
            )
            self.update_status(status)
            return RoundResult(status=status, quotes=[], alerts=[])


ArbitrageRoundCoordinator = ReadRoundCoordinator


def execute_read_round(
    reader: Any,
    pools: Sequence[AnyPool],
    max_workers: int = 15,
    on_spread_alert: Callable[[list[PriceQuote]], Any] | None = None,
    on_status_update: Callable[[RoundStatus], Any] | None = None,
    on_error: Callable[[Exception], Any] | None = None,
) -> RoundResult:
    """Functional entrypoint for executing a single dependency-injected read round."""
    coordinator = ReadRoundCoordinator(
        reader=reader,
        on_spread_alert=on_spread_alert,
        on_status_update=on_status_update,
        on_error=on_error,
        default_max_workers=max_workers,
    )
    coordinator.pools = list(pools)
    return coordinator.scan_round(max_workers=max_workers)
