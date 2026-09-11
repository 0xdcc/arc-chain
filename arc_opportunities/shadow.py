"""Arc Deterministic Shadow Evaluation Service (T24)

Enforces:
- Four-tier state separation:
  1. Quoted status (QUOTED vs UNSUPPORTED)
  2. Economic assessment (profitable, unprofitable, unknown)
  3. Simulation outcome (CALL_SUCCEEDED, REVERT, OUTPUT_UNVERIFIED)
  4. Execution authorization (strictly False)
- Data mode separation: synthetic vs live evidence never conflated
- Full audit retention: all negative delta, unknown gas, and failed simulation candidates recorded
- CALL_SUCCEEDED is NEVER claimed as verified net profit without output verification
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from arbitrage_contracts.arc_extensions import CostEvidence, SimulationEvidenceBridge, SimulationStatus
from arbitrage_contracts.identity import Amount, AssetRef
from arbitrage_contracts.quote import (
    DataMode,
    QuoteEvidence,
    QuoteStatus,
    RouteRef,
    TriState,
)
from arc_opportunities.costs import ArcCostBreakdown
from arc_opportunities.economics import ArcEconomicEvaluator
from arc_opportunities.ledger import (
    ArcOpportunityLedger,
    ArcOpportunityRecord,
    RecordType,
    compute_observation_id,
)
from arc_opportunities.quote_bridge import ArcQuoteBridge
from arc_opportunities.reasons import ShadowRejectionReason
from state_graph.types import FrozenEpoch


@dataclass(frozen=True, slots=True)
class ShadowEvaluationResult:
    """Consolidated 4-tier outcome for an evaluated opportunity."""

    opportunity_id: str
    route_id: str
    chain_id: int
    data_mode: str
    amount_in: Amount
    quote: QuoteEvidence
    breakdown: ArcCostBreakdown
    simulation_evidence: SimulationEvidenceBridge | None
    execution_authorized: bool = False
    is_actionable: bool = False
    primary_rejection_reason: str | None = None
    evaluated_at_ms: int = 0


@dataclass(frozen=True, slots=True)
class ShadowServiceSummary:
    """Batch summary of shadow evaluation pass."""

    total_evaluated: int
    quoted_count: int
    unsupported_quote_count: int
    profitable_count: int
    unprofitable_count: int
    unknown_economic_count: int
    simulated_calls_count: int
    call_succeeded_count: int
    verified_output_count: int
    results: tuple[ShadowEvaluationResult, ...]


class ArcShadowEvaluationService:
    """Evaluates candidates across the four-tier shadow pipeline."""

    def __init__(
        self,
        bridge: ArcQuoteBridge,
        evaluator: ArcEconomicEvaluator,
        ledger: ArcOpportunityLedger | None = None,
    ) -> None:
        self.bridge = bridge
        self.evaluator = evaluator
        self.ledger = ledger

    def evaluate_candidate(
        self,
        route: RouteRef,
        amount_in: Amount,
        epoch: FrozenEpoch,
        base_asset_usd_price: Decimal | str,
        gas_evidence: CostEvidence | None = None,
        otc_evidence: CostEvidence | None = None,
        simulation_evidence: SimulationEvidenceBridge | None = None,
        data_mode: str = DataMode.SYNTHETIC,
        token_decimals: Mapping[Any, int] | None = None,
    ) -> ShadowEvaluationResult:
        """Evaluate a single candidate route and enforce 4-tier separation."""
        now_ms = int(time.time() * 1000)
        state_ref = epoch.state_version.block_hash
        obs_id = compute_observation_id(route.route_id, state_ref, amount_in.atoms, now_ms)

        # Tier 1: Quoting
        quote = self.bridge.quote_exact_input(
            route=route,
            amount_in=amount_in,
            epoch=epoch,
            data_mode=data_mode,
            token_decimals=token_decimals,
        )

        # Tier 2: Economic Cost Breakdown
        breakdown = self.evaluator.evaluate_quote(
            quote=quote,
            gas_evidence=gas_evidence,
            otc_evidence=otc_evidence,
            base_asset_usd_price=base_asset_usd_price,
        )

        # Tier 3 & 4: Simulation & Execution Reason Determination
        rejection_reason: str | None = None
        is_actionable = False

        if quote.status == QuoteStatus.UNSUPPORTED:
            rejection_reason = self._categorize_quote_unsupported(quote.error)
        elif breakdown.economic_status == "unknown":
            rejection_reason = ShadowRejectionReason.UNKNOWN_GAS_EVIDENCE
        elif breakdown.economic_status == "unprofitable":
            rejection_reason = ShadowRejectionReason.NEGATIVE_NET_PROFIT
        elif simulation_evidence is not None:
            if not simulation_evidence.call_succeeded:
                rejection_reason = ShadowRejectionReason.SIMULATION_CONTRACT_REVERT
            elif not simulation_evidence.output_verified:
                rejection_reason = ShadowRejectionReason.SIMULATION_OUTPUT_UNVERIFIED
            else:
                # Succeeded & verified, but execution is locked permanently in research
                rejection_reason = ShadowRejectionReason.EXECUTION_PERMANENTLY_LOCKED
                is_actionable = False
        else:
            rejection_reason = ShadowRejectionReason.NO_EXECUTION_AUTHORIZATION

        # Persist to ledger if configured
        if self.ledger is not None:
            rec = ArcOpportunityRecord(
                record_type=RecordType.QUOTE if simulation_evidence is None else RecordType.SIM,
                observation_id=obs_id,
                route_id=route.route_id,
                chain_id=route.chain_id,
                state_ref=state_ref,
                amount_in_atoms=amount_in.atoms,
                amount_out_atoms=quote.amount_out.atoms if quote.amount_out else None,
                net_atoms=breakdown.net_atoms,
                economic_status=breakdown.economic_status,
                quote_status=str(quote.status),
                simulation_status=str(simulation_evidence.status) if simulation_evidence else None,
                reconciled_status=None,
                payload={
                    "rejection_reason": rejection_reason,
                    "data_mode": data_mode,
                },
            )
            self.ledger.append(rec)

        return ShadowEvaluationResult(
            opportunity_id=obs_id,
            route_id=route.route_id,
            chain_id=route.chain_id,
            data_mode=data_mode,
            amount_in=amount_in,
            quote=quote,
            breakdown=breakdown,
            simulation_evidence=simulation_evidence,
            execution_authorized=False,
            is_actionable=is_actionable,
            primary_rejection_reason=rejection_reason,
            evaluated_at_ms=now_ms,
        )

    def _categorize_quote_unsupported(self, error: str | None) -> str:
        if not error:
            return ShadowRejectionReason.ZERO_LIQUIDITY
        err_lower = error.lower()
        if "cross" in err_lower and "tick" in err_lower:
            return ShadowRejectionReason.UNSUPPORTED_CROSS_TICK
        if "hook" in err_lower:
            return ShadowRejectionReason.UNSUPPORTED_HOOK
        if "dynamic" in err_lower:
            return ShadowRejectionReason.UNSUPPORTED_DYNAMIC_FEE
        if "missing" in err_lower and "snapshot" in err_lower:
            return ShadowRejectionReason.MISSING_POOL_SNAPSHOT
        if "l1" in err_lower and "domain" in err_lower:
            return ShadowRejectionReason.L2_DOMAIN_REJECTED
        return ShadowRejectionReason.ZERO_LIQUIDITY
