"""Process-safe offline intent journal. No signer or network execution capability."""

from __future__ import annotations

import fcntl
import json
import math
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any


class NonceJournalError(ValueError):
    """Invalid, corrupt or uncertain journal state."""


class InFlightCollisionError(NonceJournalError):
    """An unresolved intent already occupies the wallet."""


@dataclass(frozen=True, slots=True)
class JournaledIntent:
    intent_id: str
    plan_id: str
    wallet_address: str
    target_router: str
    nonce: int
    amount_in_atoms: int
    tx_hash: str
    status: str
    created_at_utc: float
    updated_at_utc: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JournaledIntent:
        return cls(**data)


class NonceJournal:
    """Serialize reload/check/append/fsync under one OS lock, across all instances.

    A separate journal path is required per chain. Initial nonce 0 is an offline
    default, not an assertion about a funded wallet's live pending nonce.
    """

    def __init__(self, journal_path: str, wallet_address: str) -> None:
        self.journal_path = journal_path
        self.wallet_address = wallet_address.lower()
        self._intents: dict[str, JournaledIntent] = {}
        self._failed = False
        with self._locked():
            pass

    @contextmanager
    def _locked(self) -> Iterator[None]:
        if self._failed:
            raise NonceJournalError("Uncertain journal write; reopen and reconcile before reuse")
        path = Path(self.journal_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_name(path.name + ".lock")
        if path.is_symlink() or lock_path.is_symlink():
            raise NonceJournalError("Journal and lock cannot be symlinks")
        with lock_path.open("a", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                self._load_journal()
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _load_journal(self) -> None:
        path = Path(self.journal_path)
        rebuilt: dict[str, JournaledIntent] = {}
        if path.exists():
            raw = path.read_bytes()
            if raw and not raw.endswith(b"\n"):
                raise NonceJournalError("Journal has a truncated tail; no append permitted")
            for line in raw.splitlines():
                if not line.strip():
                    continue
                try:
                    intent = JournaledIntent.from_dict(json.loads(line))
                    if (
                        type(intent.nonce) is not int
                        or intent.nonce < 0
                        or type(intent.amount_in_atoms) is not int
                        or intent.amount_in_atoms <= 0
                        or intent.status not in ("INTENT_RECORDED", "TRANSMITTED", "RECONCILED")
                    ):
                        raise ValueError("Invalid intent fields")
                except (ValueError, TypeError, KeyError) as exc:
                    raise NonceJournalError(f"Corrupt journal record: {exc}") from exc
                if intent.wallet_address.lower() != self.wallet_address:
                    continue
                old = rebuilt.get(intent.intent_id)
                if old is not None:
                    fields = ("plan_id", "nonce", "amount_in_atoms", "tx_hash", "target_router")
                    if any(getattr(old, f) != getattr(intent, f) for f in fields):
                        raise NonceJournalError("Intent identity changed within journal")
                    order = {"INTENT_RECORDED": 0, "TRANSMITTED": 1, "RECONCILED": 2}
                    if order[intent.status] < order[old.status]:
                        raise NonceJournalError("Journal status regressed")
                elif intent.status != "INTENT_RECORDED":
                    raise NonceJournalError("Journal transition has no initial intent")
                rebuilt[intent.intent_id] = intent
        nonces = [i.nonce for i in rebuilt.values()]
        if len(nonces) != len(set(nonces)):
            raise NonceJournalError("Duplicate wallet nonce in journal")
        if sum(i.status != "RECONCILED" for i in rebuilt.values()) > 1:
            raise NonceJournalError("Multiple unresolved intents in journal")
        self._intents = rebuilt

    def _append_to_file(self, intent: JournaledIntent) -> None:
        try:
            with open(self.journal_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(intent.to_dict(), allow_nan=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            # The new file name must be durable before any later sender may use it.
            fd = os.open(str(Path(self.journal_path).parent), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except (OSError, ValueError):
            self._failed = True
            raise
        self._intents[intent.intent_id] = intent

    def _in_flight(self) -> JournaledIntent | None:
        return next((i for i in self._intents.values() if i.status != "RECONCILED"), None)

    def get_in_flight_intent(self) -> JournaledIntent | None:
        with self._locked():
            return self._in_flight()

    def get_next_nonce(self) -> int:
        with self._locked():
            return max((i.nonce for i in self._intents.values()), default=-1) + 1

    @staticmethod
    def _time(now_utc: float | None) -> float:
        value = time.time() if now_utc is None else now_utc
        if isinstance(value, bool) or not math.isfinite(value) or value < 0:
            raise NonceJournalError("Invalid journal timestamp")
        return value

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
        with self._locked():
            if not intent_id or intent_id in self._intents:
                raise NonceJournalError("Empty or reused intent_id")
            active = self._in_flight()
            if active:
                raise InFlightCollisionError(f"Intent {active.intent_id} is already in-flight")
            if type(amount_in_atoms) is not int or amount_in_atoms <= 0:
                raise NonceJournalError("amount_in_atoms must be a positive integer")
            nonce = max((i.nonce for i in self._intents.values()), default=-1) + 1
            if expected_nonce is not None and (
                type(expected_nonce) is not int or expected_nonce != nonce
            ):
                raise NonceJournalError(
                    f"Nonce mismatch: expected {expected_nonce}, journal calculated {nonce}"
                )
            now = self._time(now_utc)
            intent = JournaledIntent(
                intent_id,
                plan_id,
                self.wallet_address,
                target_router.lower(),
                nonce,
                amount_in_atoms,
                tx_hash.lower(),
                "INTENT_RECORDED",
                now,
                now,
            )
            self._append_to_file(intent)
            return intent

    def _transition(self, intent_id: str, status: str, now_utc: float | None) -> JournaledIntent:
        with self._locked():
            if intent_id not in self._intents:
                raise NonceJournalError(f"Intent {intent_id} not found")
            cur = self._intents[intent_id]
            if cur.status == status:
                return cur
            if cur.status == "RECONCILED":
                raise NonceJournalError("Cannot reopen a reconciled intent")
            updated = replace(cur, status=status, updated_at_utc=self._time(now_utc))
            self._append_to_file(updated)
            return updated

    def mark_transmitted(self, intent_id: str, now_utc: float | None = None) -> JournaledIntent:
        return self._transition(intent_id, "TRANSMITTED", now_utc)

    def mark_reconciled(self, intent_id: str, now_utc: float | None = None) -> JournaledIntent:
        """Caller must first establish reconciliation; this journal never sends transactions."""
        return self._transition(intent_id, "RECONCILED", now_utc)
