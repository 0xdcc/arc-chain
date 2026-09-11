"""Arc Execution Plan Assembler (T25)

Adapts W5 pure planning to Arc chain (5042 L1):
- Eliminates hardcoded Robinhood 4663 and hardcoded router addresses
- Binds target_router strictly to verified ArcExecutionDeploymentBinding
- Preserves funds safety: <= 500 USD max amount, strictly value=0, can_atomic_execute=False
- Binds output_floor ensuring principal + gas recovery
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from arbitrage_contracts.identity import Amount, AssetRef
from arbitrage_contracts.quote import QuoteEvidence, QuoteStatus, RouteRef
from atomic_execution.deployments import ArcExecutionDeploymentBinding
from atomic_execution.policy import ExecutionPolicy, evaluate_execution_policy


class ArcPlanningError(ValueError):
    """Raised for planning parameter or invariant failures on Arc."""


@dataclass(frozen=True, slots=True)
class ArcExecutionPlan:
    """Immutable domain execution plan bound to Arc deployment."""

    plan_id: str
    chain_id: int
    route_ref: RouteRef
    base_asset: AssetRef
    amount_in: Amount
    expected_out: Amount
    min_amount_out: Amount
    output_floor: Amount
    policy: ExecutionPolicy
    target_router: str
    deployment_fingerprint: str
    value_atoms: int = 0
    can_atomic_execute: bool = False
    created_at_s: int = 0

    def __post_init__(self) -> None:
        if self.chain_id not in (5042, 5042002):
            raise ArcPlanningError(f"Invalid chain_id {self.chain_id} for Arc execution plan")
        if self.value_atoms != 0:
            raise ArcPlanningError("Arc execution plan requires strictly value_atoms=0 in initial release")
        if self.can_atomic_execute:
            raise ArcPlanningError("can_atomic_execute is permanently locked to False for readonly research")


class ArcPlanAssembler:
    """Assembles ArcExecutionPlan from verified quotes and deployment bindings."""

    def __init__(self, deployment: ArcExecutionDeploymentBinding) -> None:
        self.deployment = deployment

    def assemble_plan(
        self,
        quote: QuoteEvidence,
        base_asset_usd_price: Decimal | str,
        policy: ExecutionPolicy | None = None,
        conservative_gas_usd: Decimal | str = Decimal("0.10"),
    ) -> ArcExecutionPlan:
        """Assemble an immutable ArcExecutionPlan."""
        if quote.status != QuoteStatus.QUOTED or quote.amount_out is None:
            raise ArcPlanningError(f"Cannot plan unquoted candidate: status={quote.status}")

        if quote.route_ref.chain_id != self.deployment.chain_id:
            raise ArcPlanningError(
                f"Route chain_id ({quote.route_ref.chain_id}) does not match deployment ({self.deployment.chain_id})"
            )

        eff_policy = policy if policy is not None else ExecutionPolicy()

        # Run policy evaluation
        decision = evaluate_execution_policy(
            amount_in=quote.amount_in.atoms,
            expected_out=quote.amount_out.atoms,
            decimals=quote.amount_in.decimals,
            base_asset_usd_price=base_asset_usd_price,
            conservative_gas_usd=conservative_gas_usd,
            policy=eff_policy,
            hop_quotes=quote.hop_quotes,
            quote_status=quote.status,
        )

        if not decision.approved:
            raise ArcPlanningError(f"Policy rejected execution plan: [{decision.reason}] {decision.message}")

        plan_id = f"arc-plan:{uuid.uuid4().hex[:16]}"
        now_s = int(time.time())

        min_amt_out = Amount(
            asset_ref=quote.route_ref.base_asset,
            atoms=decision.min_amount_out,
            decimals=quote.amount_in.decimals,
        )
        floor_amt = Amount(
            asset_ref=quote.route_ref.base_asset,
            atoms=decision.output_floor,
            decimals=quote.amount_in.decimals,
        )

        return ArcExecutionPlan(
            plan_id=plan_id,
            chain_id=self.deployment.chain_id,
            route_ref=quote.route_ref,
            base_asset=quote.route_ref.base_asset,
            amount_in=quote.amount_in,
            expected_out=quote.amount_out,
            min_amount_out=min_amt_out,
            output_floor=floor_amt,
            policy=eff_policy,
            target_router=self.deployment.router_address,
            deployment_fingerprint=self.deployment.deployment_fingerprint,
            value_atoms=0,
            can_atomic_execute=False,
            created_at_s=now_s,
        )
