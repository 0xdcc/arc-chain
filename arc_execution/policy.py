"""Arc Restricted Execution Policy Engine (T28)

Enforces:
- Formal binding of ArcExecutionPlan with ExecutionAuthorizationCard
- Dispatches authorized requests strictly to offline simulation sinks
- Prevents loading of private keys or real transaction signers
- Validates that min_output_atoms > 0 and amount_in <= 500 USD
"""

from __future__ import annotations

from dataclasses import dataclass

from arc_execution.authorization import (
    AuthorizationError,
    ExecutionAuthorizationCard,
)
from atomic_execution.arc_planning import ArcExecutionPlan


@dataclass(frozen=True, slots=True)
class MockExecutionSinkRecord:
    """Audit log of an authorized transaction delivered to a mock execution sink."""

    record_id: str
    auth_id: str
    plan_id: str
    target_router: str
    amount_in_atoms: int
    min_output_atoms: int
    is_delivered_to_live_network: bool = False  # Permanently False
    status: str = "DELIVERED_TO_MOCK_SINK"


class ArcRestrictedPolicyEngine:
    """Policy engine enforcing execution gates before dispatch."""

    def __init__(self, card: ExecutionAuthorizationCard) -> None:
        self.card = card
        self._sink_records: list[MockExecutionSinkRecord] = []

    def evaluate_and_dispatch(
        self,
        plan: ArcExecutionPlan,
        caller_wallet: str,
        config_hash: str,
        now_utc: float | None = None,
    ) -> MockExecutionSinkRecord:
        """Validate plan against authorization card and dispatch to mock sink.

        Invariants:
        1. Caller wallet must match authorization card.
        2. Router address must match authorization card.
        3. Plan amount_in <= 500 USD equivalent.
        4. Plan min_amount_out > 0.
        5. Execution is strictly routed to mock/simulation sink; live broadcast is prohibited.
        """
        if caller_wallet.lower() != self.card.wallet_address.lower():
            raise AuthorizationError(
                f"Caller wallet {caller_wallet} does not match authorized card wallet {self.card.wallet_address}"
            )

        # Validate request parameters against card
        self.card.validate_request(
            router_address=plan.target_router,
            amount_atoms=plan.amount_in.atoms,
            min_output_atoms=plan.min_amount_out.atoms,
            config_hash=config_hash,
            now_utc=now_utc,
        )

        # Atomic budget reservation
        self.card = self.card.reserve_budget(plan.amount_in.atoms, now_utc=now_utc)

        # Dispatch exclusively to mock/simulation sink
        record = MockExecutionSinkRecord(
            record_id=f"rec-{len(self._sink_records) + 1:04d}",
            auth_id=self.card.auth_id,
            plan_id=plan.plan_id,
            target_router=plan.target_router,
            amount_in_atoms=plan.amount_in.atoms,
            min_output_atoms=plan.min_amount_out.atoms,
            is_delivered_to_live_network=False,
            status="DELIVERED_TO_MOCK_SINK",
        )
        self._sink_records.append(record)
        return record

    def get_audit_records(self) -> tuple[MockExecutionSinkRecord, ...]:
        return tuple(self._sink_records)
