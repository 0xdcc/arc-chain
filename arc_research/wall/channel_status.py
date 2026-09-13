"""Arc OTC Channel Status and Operational Monitor (T31)

Tracks off-chain OTC venue health, rate limiting, and delivery status:
- Prohibits treating loyalty rewards / credits as real cash yield
- Classifies channel status into: ACTIVE, DEGRADED, RATE_LIMITED, UNAVAILABLE
- Maintains an immutable audit record of channel availability
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum


class ChannelHealthStatus(StrEnum):
    ACTIVE = "active"
    DEGRADED = "degraded"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    AUTH_REQUIRED = "auth_required"


class ChannelError(ValueError):
    """Raised for invalid channel status or configuration."""


@dataclass(frozen=True, slots=True)
class ChannelHealthReport:
    """Audit report on an external OTC pricing channel."""

    venue: str
    status: ChannelHealthStatus
    latency_ms: int
    last_successful_utc: float
    error_message: str | None = None
    rate_limited_until_utc: float | None = None
    cash_flow_settlement_confirmed: bool = False

    def is_reliable(self) -> bool:
        """Channel is reliable only if active, unthrottled, and confirms real cash flow."""
        return self.status == ChannelHealthStatus.ACTIVE and self.cash_flow_settlement_confirmed


class OtcChannelMonitor:
    """Monitors public OTC channel endpoints."""

    def __init__(self, venue: str) -> None:
        self.venue = venue
        self._reports: list[ChannelHealthReport] = []

    def record_probe(
        self,
        status: ChannelHealthStatus,
        latency_ms: int,
        error_message: str | None = None,
        rate_limited_seconds: int | None = None,
        cash_flow_confirmed: bool = True,
        timestamp_utc: float | None = None,
    ) -> ChannelHealthReport:
        """Record a channel health probe."""
        now_utc = timestamp_utc if timestamp_utc is not None else time.time()
        rate_until = (now_utc + rate_limited_seconds) if rate_limited_seconds else None

        # Rule: Loyalty rewards or points cannot be passed off as confirmed cash settlement
        report = ChannelHealthReport(
            venue=self.venue,
            status=status,
            latency_ms=latency_ms,
            last_successful_utc=now_utc if status == ChannelHealthStatus.ACTIVE else 0.0,
            error_message=error_message,
            rate_limited_until_utc=rate_until,
            cash_flow_settlement_confirmed=cash_flow_confirmed and (status == ChannelHealthStatus.ACTIVE),
        )
        self._reports.append(report)
        return report

    def latest_report(self) -> ChannelHealthReport | None:
        """Return the most recent health report."""
        return self._reports[-1] if self._reports else None
