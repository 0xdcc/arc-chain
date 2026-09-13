"""Arc Execution Financial Circuit Breaker (T30)

Enforces:
- Stop-not-auto-repair: halts automated trading upon consecutive reverts or loss thresholds
- Consecutive revert guard: 3 consecutive on-chain reverts trips the breaker to OPEN
- Daily cumulative loss ceiling: total loss exceeding budget trips the breaker to OPEN
- Durable persistence: process restart preserves accumulated daily losses (never resets to 0)
- Invariant: execution dispatch must call check_can_execute() before reserving funds or nonces
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from enum import StrEnum

from arc_execution.reconcile import ReconciliationCategory, ReconciliationReport


class CircuitBreakerState(StrEnum):
    CLOSED = "closed"        # Healthy, executions permitted
    OPEN = "open"            # Tripped, all executions blocked
    HALF_OPEN = "half_open"  # Controlled single probe permitted


class CircuitBreakerOpenError(RuntimeError):
    """Raised when an execution dispatch is attempted while the circuit breaker is OPEN."""


@dataclass(frozen=True, slots=True)
class CircuitBreakerSnapshot:
    """Serializable snapshot of circuit breaker status."""

    state: CircuitBreakerState
    consecutive_reverts: int
    cumulative_loss_atoms: int
    max_consecutive_reverts: int
    max_daily_loss_atoms: int
    trip_reason: str | None
    last_tripped_at_utc: float | None
    last_updated_at_utc: float


class FinancialCircuitBreaker:
    """Monitors settlement reconciliation outcomes and halts execution on adverse conditions."""

    def __init__(
        self,
        max_consecutive_reverts: int = 3,
        max_daily_loss_atoms: int = 50_000_000,  # 50 USD hard loss limit (6 decimals)
        state_file_path: str | None = None,
    ) -> None:
        self.max_consecutive_reverts = max_consecutive_reverts
        self.max_daily_loss_atoms = max_daily_loss_atoms
        self.state_file_path = state_file_path

        self.state = CircuitBreakerState.CLOSED
        self.consecutive_reverts = 0
        self.cumulative_loss_atoms = 0
        self.trip_reason: str | None = None
        self.last_tripped_at_utc: float | None = None
        self.last_updated_at_utc: float = time.time()

        if self.state_file_path and os.path.exists(self.state_file_path):
            self.load_from_disk(self.state_file_path)

    def check_can_execute(self) -> None:
        """Check if trading is permitted. Raises CircuitBreakerOpenError if OPEN."""
        if self.state == CircuitBreakerState.OPEN:
            raise CircuitBreakerOpenError(
                f"Trading halted: Financial circuit breaker is OPEN. Reason: {self.trip_reason}"
            )

    def record_reconciliation(self, report: ReconciliationReport, now_utc: float | None = None) -> None:
        """Process a reconciliation report and update circuit state."""
        current_time = now_utc if now_utc is not None else time.time()
        self.last_updated_at_utc = current_time

        # 1. On-chain Revert: count consecutive revert and add gas loss
        if report.category == ReconciliationCategory.REVERTED:
            self.consecutive_reverts += 1
            if report.realized_net_atoms < 0:
                self.cumulative_loss_atoms += abs(report.realized_net_atoms)

        # 2. Indeterminate: treat as failure
        elif report.category == ReconciliationCategory.INDETERMINATE:
            self.consecutive_reverts += 1

        # 3. Success
        elif report.category == ReconciliationCategory.SUCCESS:
            if report.is_profitable:
                # Reset consecutive failure counter on profitable execution
                self.consecutive_reverts = 0
            else:
                # Unprofitable trade counts towards consecutive failures and loss
                self.consecutive_reverts += 1
                if report.realized_net_atoms < 0:
                    self.cumulative_loss_atoms += abs(report.realized_net_atoms)

        # Evaluate Trip Conditions
        if self.consecutive_reverts >= self.max_consecutive_reverts:
            self._trip(
                f"Consecutive failure threshold reached: {self.consecutive_reverts} >= {self.max_consecutive_reverts}",
                current_time,
            )
        elif self.cumulative_loss_atoms >= self.max_daily_loss_atoms:
            self._trip(
                f"Cumulative daily loss ceiling breached: {self.cumulative_loss_atoms} >= {self.max_daily_loss_atoms} atoms",
                current_time,
            )

        if self.state_file_path:
            self.save_to_disk(self.state_file_path)

    def _trip(self, reason: str, now_utc: float) -> None:
        self.state = CircuitBreakerState.OPEN
        self.trip_reason = reason
        self.last_tripped_at_utc = now_utc

    def reset_manual(self, admin_token: str, now_utc: float | None = None) -> None:
        """Manual reset by administrator to clear OPEN state."""
        if not admin_token:
            raise ValueError("admin_token required for manual circuit breaker reset")
        self.state = CircuitBreakerState.CLOSED
        self.consecutive_reverts = 0
        self.trip_reason = None
        self.last_updated_at_utc = now_utc if now_utc is not None else time.time()
        if self.state_file_path:
            self.save_to_disk(self.state_file_path)

    def save_to_disk(self, file_path: str) -> None:
        """Persist current state snapshot to disk."""
        os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
        snapshot = CircuitBreakerSnapshot(
            state=self.state,
            consecutive_reverts=self.consecutive_reverts,
            cumulative_loss_atoms=self.cumulative_loss_atoms,
            max_consecutive_reverts=self.max_consecutive_reverts,
            max_daily_loss_atoms=self.max_daily_loss_atoms,
            trip_reason=self.trip_reason,
            last_tripped_at_utc=self.last_tripped_at_utc,
            last_updated_at_utc=self.last_updated_at_utc,
        )
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(asdict(snapshot), f, indent=2)

    def load_from_disk(self, file_path: str) -> None:
        """Load state snapshot from disk to restore loss totals and circuit trip state."""
        with open(file_path, encoding="utf-8") as f:
            data = json.load(f)
        self.state = CircuitBreakerState(data["state"])
        self.consecutive_reverts = data["consecutive_reverts"]
        self.cumulative_loss_atoms = data["cumulative_loss_atoms"]
        self.trip_reason = data.get("trip_reason")
        self.last_tripped_at_utc = data.get("last_tripped_at_utc")
        self.last_updated_at_utc = data.get("last_updated_at_utc", time.time())
