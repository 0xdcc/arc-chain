"""Pure functional execution plan assembler for atomic execution.

Assembles immutable ExecutionPlan objects from ValidatedCandidate,
CandidateOpportunity, or RouteRef/QuoteEvidence with strict policy enforcement.
Zero network IO, zero credentials, zero floating point arithmetic.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from arbitrage_contracts.identity import (
    Amount,
    AssetRef,
    validate_evm_address,
    validate_positive_integer,
)
from arbitrage_contracts.quote import (
    HopRef,
    QuoteEvidence,
    QuoteStatus,
    RouteRef,
)
from arbitrage_contracts.serialization import (
    _deserialize_amount,
    _deserialize_asset_ref,
    _deserialize_route_ref,
    _serialize_amount,
    _serialize_asset_ref,
    _serialize_route_ref,
)
from arbitrage_contracts.state import StateVersion

from .models import CandidateOpportunity, ValidatedCandidate
from .policy import (
    MAX_TRADE_AMOUNT_USD,
    ExcessiveAmountError,
    ExecutionPolicy,
    PolicyDecision,
    PolicyError,
    PolicyRejectionReason,
    PolicyViolationError,
    evaluate_execution_policy,
)

CANONICAL_UNIVERSAL_ROUTER: str = "0x8876789976dEcBfCbBbe364623C63652db8C0904"
DEFAULT_DEADLINE_SECONDS: int = 120


class PlanningError(PolicyError):
    """Raised when execution plan assembly fails."""


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """Immutable domain execution plan conforming to W5 pure assembly specification."""

    plan_id: str
    route_ref: RouteRef
    base_asset: AssetRef
    amount_in: Amount
    expected_out: Amount
    min_amount_out: Amount
    output_floor: Amount
    policy: ExecutionPolicy
    quoter_block: int
    deadline: int
    target_router: str
    estimated_gas_usd: Decimal
    gas_atoms: int
    net_atoms: int
    net_profit_usd: Decimal
    trade_amount_usd: Decimal
    base_asset_usd_price: Decimal
    candidate_id: str | None = None
    created_at_s: int | None = None

    def __init__(
        self,
        plan_id: str,
        route_ref: RouteRef,
        base_asset: AssetRef,
        amount_in: Amount,
        expected_out: Amount,
        min_amount_out: Amount,
        output_floor: Amount,
        policy: ExecutionPolicy,
        quoter_block: int,
        deadline: int,
        target_router: str,
        estimated_gas_usd: Decimal,
        gas_atoms: int,
        net_atoms: int,
        net_profit_usd: Decimal,
        trade_amount_usd: Decimal,
        base_asset_usd_price: Decimal,
        candidate_id: str | None = None,
        created_at_s: int | None = None,
    ) -> None:
        if type(plan_id) is not str or not plan_id.strip():
            raise PlanningError("plan_id must be a non-empty string")
        if not isinstance(route_ref, RouteRef):
            raise TypeError(f"route_ref must be RouteRef, got {type(route_ref).__name__}")
        if not isinstance(base_asset, AssetRef):
            raise TypeError(f"base_asset must be AssetRef, got {type(base_asset).__name__}")
        if not isinstance(amount_in, Amount):
            raise TypeError(f"amount_in must be Amount, got {type(amount_in).__name__}")
        if not isinstance(expected_out, Amount):
            raise TypeError(f"expected_out must be Amount, got {type(expected_out).__name__}")
        if not isinstance(min_amount_out, Amount):
            raise TypeError(f"min_amount_out must be Amount, got {type(min_amount_out).__name__}")
        if not isinstance(output_floor, Amount):
            raise TypeError(f"output_floor must be Amount, got {type(output_floor).__name__}")
        if not isinstance(policy, ExecutionPolicy):
            raise TypeError(f"policy must be ExecutionPolicy, got {type(policy).__name__}")

        if min_amount_out.atoms <= 0:
            raise PlanningError(
                f"Slippage safety violation: min_amount_out must be > 0, got {min_amount_out.atoms}"
            )
        if min_amount_out.atoms > expected_out.atoms:
            raise PlanningError(
                f"min_amount_out ({min_amount_out.atoms}) exceeds expected_out ({expected_out.atoms})"
            )
        if output_floor.atoms <= 0:
            raise PlanningError(f"output_floor must be > 0, got {output_floor.atoms}")
        if output_floor.atoms > expected_out.atoms:
            raise PlanningError(
                f"output_floor ({output_floor.atoms}) exceeds expected_out ({expected_out.atoms})"
            )
        if min_amount_out.atoms < output_floor.atoms:
            raise PlanningError(
                f"min_amount_out ({min_amount_out.atoms}) cannot be below output_floor ({output_floor.atoms})"
            )

        if amount_in.asset_ref != base_asset:
            raise PlanningError("amount_in asset_ref does not match base_asset")
        if expected_out.asset_ref != base_asset:
            raise PlanningError("expected_out asset_ref does not match base_asset")
        if min_amount_out.asset_ref != base_asset:
            raise PlanningError("min_amount_out asset_ref does not match base_asset")
        if output_floor.asset_ref != base_asset:
            raise PlanningError("output_floor asset_ref does not match base_asset")
        if route_ref.base_asset != base_asset:
            raise PlanningError("route_ref base_asset does not match base_asset")

        if trade_amount_usd > MAX_TRADE_AMOUNT_USD:
            raise ExcessiveAmountError(
                f"Trade amount ${trade_amount_usd:.2f} USD exceeds safety limit of ${MAX_TRADE_AMOUNT_USD:.2f} USD"
            )

        val_router = validate_evm_address(target_router)
        val_block = (
            0
            if quoter_block is None
            else validate_positive_integer(quoter_block, "quoter_block")
            if quoter_block > 0
            else 0
        )
        val_deadline = validate_positive_integer(deadline, "deadline")

        object.__setattr__(self, "plan_id", plan_id.strip())
        object.__setattr__(self, "route_ref", route_ref)
        object.__setattr__(self, "base_asset", base_asset)
        object.__setattr__(self, "amount_in", amount_in)
        object.__setattr__(self, "expected_out", expected_out)
        object.__setattr__(self, "min_amount_out", min_amount_out)
        object.__setattr__(self, "output_floor", output_floor)
        object.__setattr__(self, "policy", policy)
        object.__setattr__(self, "quoter_block", val_block)
        object.__setattr__(self, "deadline", val_deadline)
        object.__setattr__(self, "target_router", val_router)
        object.__setattr__(self, "estimated_gas_usd", estimated_gas_usd)
        object.__setattr__(self, "gas_atoms", gas_atoms)
        object.__setattr__(self, "net_atoms", net_atoms)
        object.__setattr__(self, "net_profit_usd", net_profit_usd)
        object.__setattr__(self, "trade_amount_usd", trade_amount_usd)
        object.__setattr__(self, "base_asset_usd_price", base_asset_usd_price)
        object.__setattr__(self, "candidate_id", candidate_id)
        object.__setattr__(self, "created_at_s", created_at_s)

    @property
    def route_id(self) -> str:
        """Return the unique deterministic route identifier."""
        return self.route_ref.route_id

    @property
    def chain_id(self) -> int:
        """Return the chain identifier."""
        return self.route_ref.chain_id

    @property
    def hops(self) -> tuple[HopRef, ...]:
        """Return the route hops sequence."""
        return self.route_ref.hops

    def to_dict(self) -> dict[str, Any]:
        """Serialize execution plan into dictionary."""
        return {
            "plan_id": self.plan_id,
            "route_ref": _serialize_route_ref(self.route_ref),
            "base_asset": _serialize_asset_ref(self.base_asset),
            "amount_in": _serialize_amount(self.amount_in),
            "expected_out": _serialize_amount(self.expected_out),
            "min_amount_out": _serialize_amount(self.min_amount_out),
            "output_floor": _serialize_amount(self.output_floor),
            "policy": self.policy.to_dict(),
            "quoter_block": self.quoter_block,
            "deadline": self.deadline,
            "target_router": self.target_router,
            "estimated_gas_usd": str(self.estimated_gas_usd),
            "gas_atoms": self.gas_atoms,
            "net_atoms": self.net_atoms,
            "net_profit_usd": str(self.net_profit_usd),
            "trade_amount_usd": str(self.trade_amount_usd),
            "base_asset_usd_price": str(self.base_asset_usd_price),
            "candidate_id": self.candidate_id,
            "created_at_s": self.created_at_s,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ExecutionPlan:
        """Deserialize execution plan from dictionary."""
        route_raw = data.get("route_ref")
        if not isinstance(route_raw, Mapping):
            raise ValueError("route_ref must be a mapping")
        base_raw = data.get("base_asset")
        if not isinstance(base_raw, Mapping):
            raise ValueError("base_asset must be a mapping")
        in_raw = data.get("amount_in")
        if not isinstance(in_raw, Mapping):
            raise ValueError("amount_in must be a mapping")
        out_raw = data.get("expected_out")
        if not isinstance(out_raw, Mapping):
            raise ValueError("expected_out must be a mapping")
        min_raw = data.get("min_amount_out")
        if not isinstance(min_raw, Mapping):
            raise ValueError("min_amount_out must be a mapping")
        floor_raw = data.get("output_floor")
        if not isinstance(floor_raw, Mapping):
            raise ValueError("output_floor must be a mapping")
        policy_raw = data.get("policy")
        if not isinstance(policy_raw, Mapping):
            raise ValueError("policy must be a mapping")

        return cls(
            plan_id=data["plan_id"],
            route_ref=_deserialize_route_ref(route_raw),
            base_asset=_deserialize_asset_ref(base_raw),
            amount_in=_deserialize_amount(in_raw),
            expected_out=_deserialize_amount(out_raw),
            min_amount_out=_deserialize_amount(min_raw),
            output_floor=_deserialize_amount(floor_raw),
            policy=ExecutionPolicy.from_dict(policy_raw),
            quoter_block=int(data["quoter_block"]),
            deadline=int(data["deadline"]),
            target_router=data["target_router"],
            estimated_gas_usd=Decimal(str(data["estimated_gas_usd"])),
            gas_atoms=int(data["gas_atoms"]),
            net_atoms=int(data["net_atoms"]),
            net_profit_usd=Decimal(str(data["net_profit_usd"])),
            trade_amount_usd=Decimal(str(data["trade_amount_usd"])),
            base_asset_usd_price=Decimal(str(data["base_asset_usd_price"])),
            candidate_id=data.get("candidate_id"),
            created_at_s=data.get("created_at_s"),
        )

    def to_json(self) -> str:
        """Serialize execution plan to compact canonical JSON."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> ExecutionPlan:
        """Deserialize execution plan from canonical JSON string."""
        return cls.from_dict(json.loads(text))


@dataclass(frozen=True, slots=True)
class PlanAssemblyResult:
    """Non-throwing evaluation result for execution plan assembly attempts."""

    approved: bool
    plan: ExecutionPlan | None
    rejection_reason: PolicyRejectionReason | None
    message: str
    decision: PolicyDecision | None = None


def build_execution_plan(
    candidate: ValidatedCandidate | CandidateOpportunity | None = None,
    *,
    route_ref: RouteRef | None = None,
    quote_evidence: QuoteEvidence | None = None,
    state_version: StateVersion | None = None,
    base_asset_usd_price: Decimal | str | int | None,
    conservative_gas_usd: Decimal | str | int | None = None,
    policy: ExecutionPolicy | None = None,
    deadline_seconds: int = DEFAULT_DEADLINE_SECONDS,
    current_time: int | None = None,
    target_router: str | None = None,
    plan_id: str | None = None,
    quoter_block: int | None = None,
) -> ExecutionPlan:
    """Build an immutable ExecutionPlan under strict funds policy and risk invariants.

    Raises:
        ExcessiveAmountError: If trade amount exceeds $500.00 USD.
        PlanningError: If candidate topology or quotes are invalid.
        PolicyViolationError: If funds safety policy cannot be satisfied.
    """
    res = evaluate_plan_assembly(
        candidate=candidate,
        route_ref=route_ref,
        quote_evidence=quote_evidence,
        state_version=state_version,
        base_asset_usd_price=base_asset_usd_price,
        conservative_gas_usd=conservative_gas_usd,
        policy=policy,
        deadline_seconds=deadline_seconds,
        current_time=current_time,
        target_router=target_router,
        plan_id=plan_id,
        quoter_block=quoter_block,
    )
    if not res.approved or res.plan is None:
        raise PolicyViolationError(f"[{res.rejection_reason}] {res.message}")
    return res.plan


def evaluate_plan_assembly(
    candidate: ValidatedCandidate | CandidateOpportunity | None = None,
    *,
    route_ref: RouteRef | None = None,
    quote_evidence: QuoteEvidence | None = None,
    state_version: StateVersion | None = None,
    base_asset_usd_price: Decimal | str | int | None,
    conservative_gas_usd: Decimal | str | int | None = None,
    policy: ExecutionPolicy | None = None,
    deadline_seconds: int = DEFAULT_DEADLINE_SECONDS,
    current_time: int | None = None,
    target_router: str | None = None,
    plan_id: str | None = None,
    quoter_block: int | None = None,
) -> PlanAssemblyResult:
    """Evaluate plan assembly, returning PlanAssemblyResult without throwing on business rejection."""
    # Resolve candidate components
    res_route: RouteRef
    res_quote: QuoteEvidence
    res_state: StateVersion | None = state_version
    res_base: AssetRef
    res_amount_in: Amount
    cand_id: str | None = None

    if isinstance(candidate, ValidatedCandidate):
        res_route = candidate.route_ref
        res_quote = candidate.quote_evidence
        res_state = candidate.state_version
        res_base = candidate.base_asset
        res_amount_in = candidate.amount_in
        cand_id = candidate.quote_id
    elif isinstance(candidate, CandidateOpportunity):
        res_route = candidate.route_ref
        res_quote = candidate.quote_evidence
        res_state = candidate.state_version
        res_base = candidate.route_ref.base_asset
        res_amount_in = candidate.quote_evidence.amount_in
        cand_id = candidate.opportunity_id or candidate.quote_evidence.quote_id
    elif route_ref is not None and quote_evidence is not None:
        res_route = route_ref
        res_quote = quote_evidence
        res_base = route_ref.base_asset
        res_amount_in = quote_evidence.amount_in
        cand_id = quote_evidence.quote_id
    else:
        return PlanAssemblyResult(
            approved=False,
            plan=None,
            rejection_reason=PolicyRejectionReason.POLICY_VIOLATION,
            message="Either candidate or both route_ref and quote_evidence must be provided",
        )

    # Basic route and quote invariants
    if res_quote.status != QuoteStatus.QUOTED:
        return PlanAssemblyResult(
            approved=False,
            plan=None,
            rejection_reason=PolicyRejectionReason.QUOTE_STATUS_INVALID,
            message=f"Quote status is not QUOTED: {res_quote.status}",
        )
    if res_quote.amount_out is None:
        return PlanAssemblyResult(
            approved=False,
            plan=None,
            rejection_reason=PolicyRejectionReason.POLICY_VIOLATION,
            message="Quote evidence missing amount_out",
        )
    if len(res_route.hops) < 2:
        return PlanAssemblyResult(
            approved=False,
            plan=None,
            rejection_reason=PolicyRejectionReason.POLICY_VIOLATION,
            message=f"Route hops must be >= 2, got {len(res_route.hops)}",
        )

    effective_policy = policy if policy is not None else ExecutionPolicy()

    # Evaluate funds safety policy (may raise ExcessiveAmountError per C08 red line)
    decision = evaluate_execution_policy(
        amount_in=res_amount_in.atoms,
        expected_out=res_quote.amount_out.atoms,
        decimals=res_amount_in.decimals,
        base_asset_usd_price=base_asset_usd_price,
        conservative_gas_usd=conservative_gas_usd,
        policy=effective_policy,
        gas_evidence=res_quote.gas_evidence,
        hop_quotes=res_quote.hop_quotes,
        quote_status=res_quote.status,
    )

    if not decision.approved:
        return PlanAssemblyResult(
            approved=False,
            plan=None,
            rejection_reason=decision.reason,
            message=decision.message,
            decision=decision,
        )

    # Resolve deadline and quoter block
    now = current_time if current_time is not None else int(time.time())
    if deadline_seconds <= 0:
        return PlanAssemblyResult(
            approved=False,
            plan=None,
            rejection_reason=PolicyRejectionReason.POLICY_VIOLATION,
            message=f"deadline_seconds must be positive, got {deadline_seconds}",
            decision=decision,
        )
    deadline = now + deadline_seconds

    router_addr = target_router if target_router is not None else CANONICAL_UNIVERSAL_ROUTER
    try:
        val_router = validate_evm_address(router_addr)
    except (ValueError, TypeError) as exc:
        return PlanAssemblyResult(
            approved=False,
            plan=None,
            rejection_reason=PolicyRejectionReason.POLICY_VIOLATION,
            message=f"Invalid target_router: {exc}",
            decision=decision,
        )

    resolved_quoter_block: int
    if quoter_block is not None:
        resolved_quoter_block = quoter_block
    elif res_state is not None:
        resolved_quoter_block = res_state.block_number
    else:
        resolved_quoter_block = 0

    pid = plan_id if plan_id is not None else f"plan_{uuid.uuid4().hex[:12]}"

    min_out = Amount(
        asset_ref=res_base,
        atoms=decision.output_floor,
        decimals=res_amount_in.decimals,
    )
    floor_amt = Amount(
        asset_ref=res_base,
        atoms=decision.output_floor,
        decimals=res_amount_in.decimals,
    )

    plan = ExecutionPlan(
        plan_id=pid,
        route_ref=res_route,
        base_asset=res_base,
        amount_in=res_amount_in,
        expected_out=res_quote.amount_out,
        min_amount_out=min_out,
        output_floor=floor_amt,
        policy=effective_policy,
        quoter_block=resolved_quoter_block,
        deadline=deadline,
        target_router=val_router,
        estimated_gas_usd=decision.conservative_gas_usd,
        gas_atoms=decision.gas_atoms,
        net_atoms=decision.net_atoms,
        net_profit_usd=decision.net_profit_usd,
        trade_amount_usd=decision.trade_amount_usd,
        base_asset_usd_price=decision.base_asset_usd_price,
        candidate_id=cand_id,
        created_at_s=now,
    )

    return PlanAssemblyResult(
        approved=True,
        plan=plan,
        rejection_reason=None,
        message="Plan assembled successfully",
        decision=decision,
    )
