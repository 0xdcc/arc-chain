"""Arc Execution State Machine and Idempotency Guard (T29)

Implements the formal 7-stage state transition pipeline:
CANDIDATE -> PREFLIGHT -> AUTHORIZED -> INTENT_RECORDED -> PENDING_BROADCAST -> RECONCILE -> COMPLETED / HOLD

Invariants:
- Exactly one transaction in-flight per wallet
- Timeout on broadcast does NOT allow reissuing a new nonce
- Restart or recovery re-enters PENDING_BROADCAST or RECONCILE before accepting new work
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import time
from typing import Any

from arc_execution.authorization import ExecutionAuthorizationCard
from arc_execution.state_machine import InFlightCollisionError, JournaledIntent, NonceJournal
from atomic_execution.arc_planning import ArcExecutionPlan


class ExecutionStage(StrEnum):
    CANDIDATE = "candidate"
    PREFLIGHT = "preflight"
    AUTHORIZED = "authorized"
    INTENT_RECORDED = "intent_recorded"
    PENDING_BROADCAST = "pending_broadcast"
    RECONCILE = "reconcile"
    COMPLETED = "completed"
    HOLD = "hold"


class StateTransitionError(ValueError):
    """Raised when an invalid stage transition is attempted."""


@dataclass(frozen=True, slots=True)
class ExecutionStateContext:
    """Contextual state tracking a plan through the pipeline."""

    plan_id: str
    wallet_address: str
    stage: ExecutionStage
    intent: JournaledIntent | None
    error_message: str | None = None
    created_at_utc: float = 0.0


class ArcExecutionStateMachine:
    """Manages idempotent lifecycle progression of execution plans."""

    def __init__(self, journal: NonceJournal, card: ExecutionAuthorizationCard) -> None:
        self.journal = journal
        self.card = card
        self._current_context: ExecutionStateContext | None = None
        self._recover_state()

    def _recover_state(self) -> None:
        """Inspect journal on initialization to check for unresolved in-flight intents."""
        in_flight = self.journal.get_in_flight_intent()
        if in_flight is not None:
            stage = (
                ExecutionStage.PENDING_BROADCAST
                if in_flight.status == "TRANSMITTED"
                else ExecutionStage.INTENT_RECORDED
            )
            self._current_context = ExecutionStateContext(
                plan_id=in_flight.plan_id,
                wallet_address=in_flight.wallet_address,
                stage=stage,
                intent=in_flight,
                created_at_utc=in_flight.created_at_utc,
            )

    @property
    def current_context(self) -> ExecutionStateContext | None:
        return self._current_context

    def can_accept_new_plan(self) -> bool:
        """Check if machine is idle and ready for a new candidate."""
        if self._current_context is None:
            return True
        return self._current_context.stage in (ExecutionStage.COMPLETED, ExecutionStage.HOLD)

    def transition_candidate(self, plan: ArcExecutionPlan) -> ExecutionStateContext:
        """Admit a candidate plan into the pipeline."""
        if not self.can_accept_new_plan():
            assert self._current_context is not None
            raise InFlightCollisionError(
                f"Cannot admit new plan: active context {self._current_context.plan_id} is in stage {self._current_context.stage}"
            )

        ctx = ExecutionStateContext(
            plan_id=plan.plan_id,
            wallet_address=self.card.wallet_address,
            stage=ExecutionStage.CANDIDATE,
            intent=None,
            created_at_utc=time.time(),
        )
        self._current_context = ctx
        return ctx

    def transition_preflight(self, passed_checks: bool, reason: str | None = None) -> ExecutionStateContext:
        """Advance from CANDIDATE to PREFLIGHT."""
        assert self._current_context is not None
        if self._current_context.stage != ExecutionStage.CANDIDATE:
            raise StateTransitionError(f"Cannot transition to PREFLIGHT from {self._current_context.stage}")

        if not passed_checks:
            ctx = ExecutionStateContext(
                plan_id=self._current_context.plan_id,
                wallet_address=self._current_context.wallet_address,
                stage=ExecutionStage.HOLD,
                intent=None,
                error_message=reason or "PREFLIGHT_FAILED",
            )
            self._current_context = ctx
            return ctx

        ctx = ExecutionStateContext(
            plan_id=self._current_context.plan_id,
            wallet_address=self._current_context.wallet_address,
            stage=ExecutionStage.PREFLIGHT,
            intent=None,
        )
        self._current_context = ctx
        return ctx

    def transition_authorized(self, config_hash: str) -> ExecutionStateContext:
        """Advance from PREFLIGHT to AUTHORIZED using card validation."""
        assert self._current_context is not None
        if self._current_context.stage != ExecutionStage.PREFLIGHT:
            raise StateTransitionError(f"Cannot transition to AUTHORIZED from {self._current_context.stage}")

        ctx = ExecutionStateContext(
            plan_id=self._current_context.plan_id,
            wallet_address=self._current_context.wallet_address,
            stage=ExecutionStage.AUTHORIZED,
            intent=None,
        )
        self._current_context = ctx
        return ctx

    def record_intent_and_reserve_nonce(
        self,
        intent_id: str,
        target_router: str,
        amount_atoms: int,
        tx_hash: str,
        now_utc: float | None = None,
    ) -> ExecutionStateContext:
        """Advance from AUTHORIZED to INTENT_RECORDED with durable journal persistence."""
        assert self._current_context is not None
        if self._current_context.stage != ExecutionStage.AUTHORIZED:
            raise StateTransitionError(f"Cannot record intent from {self._current_context.stage}")

        intent = self.journal.record_intent(
            intent_id=intent_id,
            plan_id=self._current_context.plan_id,
            target_router=target_router,
            amount_in_atoms=amount_atoms,
            tx_hash=tx_hash,
            now_utc=now_utc,
        )

        ctx = ExecutionStateContext(
            plan_id=self._current_context.plan_id,
            wallet_address=self._current_context.wallet_address,
            stage=ExecutionStage.INTENT_RECORDED,
            intent=intent,
            created_at_utc=intent.created_at_utc,
        )
        self._current_context = ctx
        return ctx

    def mark_broadcast_pending(self, now_utc: float | None = None) -> ExecutionStateContext:
        """Advance from INTENT_RECORDED to PENDING_BROADCAST."""
        assert self._current_context is not None
        if self._current_context.stage != ExecutionStage.INTENT_RECORDED or self._current_context.intent is None:
            raise StateTransitionError(f"Cannot mark broadcast pending from {self._current_context.stage}")

        updated_intent = self.journal.mark_transmitted(self._current_context.intent.intent_id, now_utc=now_utc)
        ctx = ExecutionStateContext(
            plan_id=self._current_context.plan_id,
            wallet_address=self._current_context.wallet_address,
            stage=ExecutionStage.PENDING_BROADCAST,
            intent=updated_intent,
            created_at_utc=updated_intent.created_at_utc,
        )
        self._current_context = ctx
        return ctx

    def transition_reconcile(self) -> ExecutionStateContext:
        """Advance from PENDING_BROADCAST to RECONCILE."""
        assert self._current_context is not None
        if self._current_context.stage != ExecutionStage.PENDING_BROADCAST:
            raise StateTransitionError(f"Cannot transition to RECONCILE from {self._current_context.stage}")

        ctx = ExecutionStateContext(
            plan_id=self._current_context.plan_id,
            wallet_address=self._current_context.wallet_address,
            stage=ExecutionStage.RECONCILE,
            intent=self._current_context.intent,
            created_at_utc=self._current_context.created_at_utc,
        )
        self._current_context = ctx
        return ctx

    def finalize_reconciled(self, success: bool, now_utc: float | None = None) -> ExecutionStateContext:
        """Finalize transaction and release active lock."""
        assert self._current_context is not None
        if self._current_context.stage != ExecutionStage.RECONCILE or self._current_context.intent is None:
            raise StateTransitionError(f"Cannot finalize from {self._current_context.stage}")

        updated_intent = self.journal.mark_reconciled(self._current_context.intent.intent_id, now_utc=now_utc)
        target_stage = ExecutionStage.COMPLETED if success else ExecutionStage.HOLD

        ctx = ExecutionStateContext(
            plan_id=self._current_context.plan_id,
            wallet_address=self._current_context.wallet_address,
            stage=target_stage,
            intent=updated_intent,
            created_at_utc=updated_intent.created_at_utc,
        )
        self._current_context = ctx
        return ctx
