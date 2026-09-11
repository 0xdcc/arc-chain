"""Arc Notification & Mock Delivery Dispatcher (T39)

Provides safe, isolated notification channels for Arc opportunities:
- Default fake/mock notifier (zero production QQ / webhook side effects)
- Idempotent deduplication by opportunity_id and block_hash
- Strict gating: only strictly positive net profit (> 0) can trigger notifications
- Hard freeze on real push without explicit separate channel authorization
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class DeliveryStatus(StrEnum):
    QUEUED = "QUEUED"
    DELIVERED_FAKE = "DELIVERED_FAKE"
    SUPPRESSED_DUPLICATE = "SUPPRESSED_DUPLICATE"
    SUPPRESSED_UNPROFITABLE = "SUPPRESSED_UNPROFITABLE"
    BLOCKED_NO_AUTH = "BLOCKED_NO_AUTH"


@dataclass(frozen=True)
class OpportunityNotification:
    """Immutable payload for opportunity alerts."""

    notification_id: str
    opportunity_id: str
    chain_id: int
    cycle_summary: str
    expected_profit_atoms: int
    net_profit_usd: float
    is_estimated: bool
    as_of_block: int
    created_at: float

    def __post_init__(self) -> None:
        if self.chain_id != 5042 and self.chain_id != 5042002:
            raise ValueError(f"Invalid chain_id: {self.chain_id}")
        if not self.is_estimated:
            raise ValueError("Pre-execution notifications must explicitly set is_estimated=True")
        if not self.notification_id:
            raise ValueError("notification_id cannot be empty")


@dataclass
class DeliveryReceipt:
    """Proof of notification processing."""

    notification_id: str
    status: DeliveryStatus
    channel: str
    idempotent_key: str
    delivered_at: float
    raw_payload_preview: str


class FakeNotificationDispatcher:
    """Safe dispatcher that records delivery receipts without external side effects."""

    def __init__(self, allow_real_send: bool = False, auth_token: str | None = None) -> None:
        self.allow_real_send = allow_real_send
        self.auth_token = auth_token
        self._sent_keys: set[str] = set()
        self._receipts: list[DeliveryReceipt] = []

        if self.allow_real_send and not self.auth_token:
            raise PermissionError("Real notification broadcast requires verified separate channel authorization")

    def dispatch(self, notif: OpportunityNotification) -> DeliveryReceipt:
        """Process opportunity notification through safety gates."""
        # 1. Profitability Gate: Must have positive expected profit
        if notif.net_profit_usd <= 0 or notif.expected_profit_atoms <= 0:
            receipt = DeliveryReceipt(
                notification_id=notif.notification_id,
                status=DeliveryStatus.SUPPRESSED_UNPROFITABLE,
                channel="mock_filter",
                idempotent_key="",
                delivered_at=time.time(),
                raw_payload_preview="Suppressed: net profit <= 0",
            )
            self._receipts.append(receipt)
            return receipt

        # 2. Idempotency Gate
        idempotent_key = hashlib.sha256(
            f"{notif.chain_id}:{notif.opportunity_id}:{notif.as_of_block}".encode()
        ).hexdigest()

        if idempotent_key in self._sent_keys:
            receipt = DeliveryReceipt(
                notification_id=notif.notification_id,
                status=DeliveryStatus.SUPPRESSED_DUPLICATE,
                channel="idempotency_gate",
                idempotent_key=idempotent_key,
                delivered_at=time.time(),
                raw_payload_preview="Suppressed: duplicate notification for same block/opportunity",
            )
            self._receipts.append(receipt)
            return receipt

        # 3. Real Send Physical Lock
        if self.allow_real_send:
            # Under T39 non-goals, real network push is strictly blocked
            raise PermissionError("Real QQ/Webhook broadcast is forbidden under T39 scope")

        # 4. Safe Mock Delivery
        self._sent_keys.add(idempotent_key)
        receipt = DeliveryReceipt(
            notification_id=notif.notification_id,
            status=DeliveryStatus.DELIVERED_FAKE,
            channel="mock_qq",
            idempotent_key=idempotent_key,
            delivered_at=time.time(),
            raw_payload_preview=f"[FAKE_QQ] {notif.cycle_summary} | Net: ~${notif.net_profit_usd:.2f} (ESTIMATED)",
        )
        self._receipts.append(receipt)
        return receipt

    @property
    def total_delivered(self) -> int:
        return sum(1 for r in self._receipts if r.status == DeliveryStatus.DELIVERED_FAKE)

    @property
    def receipts(self) -> tuple[DeliveryReceipt, ...]:
        return tuple(self._receipts)
