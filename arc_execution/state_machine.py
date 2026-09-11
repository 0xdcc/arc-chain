"""Arc Nonce Journal and Intent Persistence (T29)

Enforces:
- Single active in-flight transaction per wallet at any time
- Durable journaling: intent, tx_hash, nonce, and timestamp persisted before transmission
- Monotonic strictly-increasing nonce progression
- Recovery invariant: on crash recovery, inspect in-flight intent before any new dispatch
- Timeout does NOT mean transaction did not broadcast (prevents double-spend reissuing)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
import time
from typing import Any


class NonceJournalError(ValueError):
    """Raised when nonce journal invariants or concurrency rules are violated."""


class InFlightCollisionError(NonceJournalError):
    """Raised when attempting to issue an order while another is already in-flight."""


@dataclass(frozen=True, slots=True)
class JournaledIntent:
    """Immutable record of an authorized execution intent."""

    intent_id: str
    plan_id: str
    wallet_address: str
    target_router: str
    nonce: int
    amount_in_atoms: int
    tx_hash: str
    status: str  # "INTENT_RECORDED", "TRANSMITTED", "RECONCILED", "ABORTED"
    created_at_utc: float
    updated_at_utc: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> JournaledIntent:
        return cls(**d)


class NonceJournal:
    """Durable ledger tracking nonces and in-flight transactions per wallet."""

    def __init__(self, journal_path: str, wallet_address: str) -> None:
        self.journal_path = journal_path
        self.wallet_address = wallet_address.lower()
        self._intents: dict[str, JournaledIntent] = {}
        self._load_journal()

    def _load_journal(self) -> None:
        if not os.path.exists(self.journal_path):
            return
        with open(self.journal_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                intent = JournaledIntent.from_dict(d)
                if intent.wallet_address.lower() == self.wallet_address:
                    self._intents[intent.intent_id] = intent

    def _append_to_file(self, intent: JournaledIntent) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.journal_path)), exist_ok=True)
        with open(self.journal_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(intent.to_dict()) + "\n")

    def get_in_flight_intent(self) -> JournaledIntent | None:
        """Find any intent currently in-flight (TRANSMITTED or INTENT_RECORDED)."""
        for intent in self._intents.values():
            if intent.status in ("INTENT_RECORDED", "TRANSMITTED"):
                return intent
        return None

    def get_next_nonce(self) -> int:
        """Calculate next available nonce for this wallet."""
        if not self._intents:
            return 0
        return max(i.nonce for i in self._intents.values()) + 1

    def record_intent(
        self,
        intent_id: str,
        plan_id: str,
        target_router: str,
        amount_in_atoms: int,
        tx_hash: str,
        expected_nonce: int | None = None,
        now_utc: float | None = None,
    ) -> JournaledIntent:
        """Atomically reserve next nonce and persist intent before broadcast."""
        in_flight = self.get_in_flight_intent()
        if in_flight is not None:
            raise InFlightCollisionError(
                f"Cannot record new intent: intent {in_flight.intent_id} is already in-flight (nonce {in_flight.nonce}, hash {in_flight.tx_hash})"
            )

        current_time = now_utc if now_utc is not None else time.time()
        next_nonce = self.get_next_nonce()

        if expected_nonce is not None and expected_nonce != next_nonce:
            raise NonceJournalError(
                f"Nonce mismatch: expected {expected_nonce}, journal calculated next as {next_nonce}"
            )

        intent = JournaledIntent(
            intent_id=intent_id,
            plan_id=plan_id,
            wallet_address=self.wallet_address,
            target_router=target_router.lower(),
            nonce=next_nonce,
            amount_in_atoms=amount_in_atoms,
            tx_hash=tx_hash.lower(),
            status="INTENT_RECORDED",
            created_at_utc=current_time,
            updated_at_utc=current_time,
        )

        self._intents[intent_id] = intent
        self._append_to_file(intent)
        return intent

    def mark_transmitted(self, intent_id: str, now_utc: float | None = None) -> JournaledIntent:
        """Mark intent as transmitted to network."""
        if intent_id not in self._intents:
            raise NonceJournalError(f"Intent {intent_id} not found")

        cur = self._intents[intent_id]
        current_time = now_utc if now_utc is not None else time.time()
        updated = JournaledIntent(
            intent_id=cur.intent_id,
            plan_id=cur.plan_id,
            wallet_address=cur.wallet_address,
            target_router=cur.target_router,
            nonce=cur.nonce,
            amount_in_atoms=cur.amount_in_atoms,
            tx_hash=cur.tx_hash,
            status="TRANSMITTED",
            created_at_utc=cur.created_at_utc,
            updated_at_utc=current_time,
        )
        self._intents[intent_id] = updated
        self._append_to_file(updated)
        return updated

    def mark_reconciled(self, intent_id: str, now_utc: float | None = None) -> JournaledIntent:
        """Mark intent as reconciled (clearing the in-flight slot)."""
        if intent_id not in self._intents:
            raise NonceJournalError(f"Intent {intent_id} not found")

        cur = self._intents[intent_id]
        current_time = now_utc if now_utc is not None else time.time()
        updated = JournaledIntent(
            intent_id=cur.intent_id,
            plan_id=cur.plan_id,
            wallet_address=cur.wallet_address,
            target_router=cur.target_router,
            nonce=cur.nonce,
            amount_in_atoms=cur.amount_in_atoms,
            tx_hash=cur.tx_hash,
            status="RECONCILED",
            created_at_utc=cur.created_at_utc,
            updated_at_utc=current_time,
        )
        self._intents[intent_id] = updated
        self._append_to_file(updated)
        return updated
