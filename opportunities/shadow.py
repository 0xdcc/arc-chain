"""Offline shadow orchestration for bounded W2 candidate replay."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from arbitrage_contracts.quote import (
    ActorScope,
    DataMode,
    EconomicAssessment,
    EvidenceLevel,
    FeeComponent,
    GasEvidence,
    GasEvidenceKind,
    QuoteEvidence,
    QuoteStatus,
    TriState,
)
from arbitrage_contracts.state import StateVersion
from opportunities.candidates import (
    CandidateSelection,
    DeterministicOfflineTransport,
    offline_quote_id,
)
from opportunities.economics import AssetConversionInput, evaluate_quote_evidence
from opportunities.lifecycle import LifecycleEvent, LifecyclePolicy, ObservationMerger
from opportunities.quote_adapter import FixedBlockQuoteAdapter, QuoteAdapterRequest
from opportunities.store import AppendOnlyLedger, LedgerError

MAX_RPC_FAILURES = 3
_ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
_MIN_MS = 0
_MAX_MS = 253_402_300_799_999


class ShadowInputError(ValueError):
    """Raised when shadow replay input cannot be processed safely."""


class Clock(Protocol):
    """Injectable monotonic-safe process clock for deterministic shadow tests."""

    def time_ms(self) -> int:
        """Return the current process decision time in UTC milliseconds."""


class SystemClock:
    """Default wall clock used only at the external shadow boundary."""

    def time_ms(self) -> int:
        """Return the current UTC wall-clock milliseconds."""
        return int(time.time() * 1000)


class _OfflineQuoteTransport:
    """Adapts candidate fixture routing to the fixed-block adapter's RPC boundary."""

    def __init__(self, offline: DeterministicOfflineTransport, route_id: str, amount: str) -> None:
        self._offline = offline
        self._route_id = route_id
        self._amount = amount

    def call(
        self,
        method: str,
        params: list[Any] | tuple[Any, ...] | None = None,
        block_identifier: str | None = None,
    ) -> dict[str, Any]:
        """Resolve one recorded eth_call into an explicitly bound fixture response."""
        if method != "eth_call" or not isinstance(params, list) or len(params) != 2:
            raise ShadowInputError("offline transport received an unexpected request")
        hop_index = len(self._offline.calls)
        relative = self._offline.fixture_path(self._route_id, self._amount, hop_index)
        quote_id = offline_quote_id(self._route_id, self._amount, hop_index, block_identifier or "")
        return self._offline.call(
            "fixture", [{"quote_id": quote_id, "fixture": relative}], block_identifier
        )


@dataclass(frozen=True, slots=True)
class ShadowConfig:
    """Explicit budget and lifecycle policy for one offline shadow collection."""

    lifecycle: LifecyclePolicy
    max_candidates: int
    max_rpc_calls: int
    quote_request: QuoteAdapterRequest
    registry_semantic_revision: str
    conversions: tuple[AssetConversionInput, ...] = ()
    rpc_gas_price_atoms: int = 0
    rpc_gas_l1_fee_atoms: int = 0

    def __post_init__(self) -> None:
        for field_name in ("rpc_gas_price_atoms", "rpc_gas_l1_fee_atoms"):
            value = getattr(self, field_name)
            if type(value) is not int or isinstance(value, bool) or value < 0:
                raise ShadowInputError(f"{field_name} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class ShadowOutcome:
    """Canonical summary of an append-only shadow collection."""

    candidate_count: int
    quoted_count: int
    ledger_sequence: int
    truncated: bool
    halted: bool
    sim_available: bool
    decision_watermark_ms: int | None


def _required_state(raw: Any, chain_id: int) -> StateVersion:
    if not isinstance(raw, dict):
        raise ShadowInputError("state must be an object")
    block_number = raw.get("block_number")
    if type(block_number) is not int or isinstance(block_number, bool) or block_number <= 0:
        raise ShadowInputError("state.block_number must be a positive integer")
    received_at_ms = raw.get("received_at_ms")
    if type(received_at_ms) is not int or isinstance(received_at_ms, bool) or received_at_ms < 0:
        raise ShadowInputError("state.received_at_ms must be a non-negative integer")
    block_timestamp_s = raw.get("block_timestamp_s")
    if block_timestamp_s is not None and (
        type(block_timestamp_s) is not int or isinstance(block_timestamp_s, bool)
    ):
        raise ShadowInputError("state.block_timestamp_s must be an integer or null")
    complete_through = raw.get("complete_through_block")
    if complete_through is not None and (
        type(complete_through) is not int or isinstance(complete_through, bool)
    ):
        raise ShadowInputError("state.complete_through_block must be an integer or null")
    block_hash = raw.get("block_hash")
    if type(block_hash) is not str or not block_hash:
        raise ShadowInputError("state.block_hash must be a non-empty string")
    state = StateVersion(
        chain_id,
        block_number,
        block_hash,
        received_at_ms,
        completeness=raw.get("completeness", "ready"),
        block_timestamp_s=block_timestamp_s,
        complete_through_block=complete_through,
        finality=raw.get("finality", "unknown"),
    )
    if not state.is_ready():
        raise ShadowInputError("state must be ready")
    return state


def _result_for_quote(quote: QuoteEvidence) -> str:
    if quote.status == QuoteStatus.RPC_ERROR:
        return "rpc_error"
    if quote.status == QuoteStatus.CONTRACT_REVERT:
        return "contract_revert"
    if quote.status != QuoteStatus.QUOTED:
        return "quote_failed"
    assessment = quote.economic_assessment
    if assessment is None or assessment.net_atoms is None:
        return "unknown"
    if assessment.economic_status == "profitable":
        return "profitable"
    return "unprofitable"


def _estimated_economics(evidence: QuoteEvidence, now_ms: int) -> EconomicAssessment:
    """Return a bounded synthetic RPC-gas estimate with explicit zero-denominated costs."""
    base_asset = evidence.route_ref.base_asset
    component = FeeComponent(
        "shadow:synthetic-gas",
        0,
        base_asset,
        deduction_stage="gas",
        evidence_ref="shadow:synthetic-gas",
        is_estimated=True,
    )
    return EconomicAssessment(
        net_atoms=evidence.delta_atoms,
        economic_status="profitable"
        if evidence.delta_atoms is not None and evidence.delta_atoms > 0
        else "unprofitable",
        fee_components=(component,),
        gas_cost_atoms=0,
        calculation_refs=tuple(evidence.source_refs) or ("shadow:synthetic-gas",),
        is_estimated=True,
    )


def _event_id(candidate_index: int, evidence: QuoteEvidence) -> str:
    return f"shadow:{candidate_index}:{evidence.quote_id}"


def run_shadow(
    candidates: CandidateSelection,
    state_raw: Any,
    ledger: AppendOnlyLedger,
    config: ShadowConfig,
    rpc: Any,
    *,
    clock: Clock | None = None,
) -> ShadowOutcome:
    """Quote, evaluate, merge, and append every selected candidate without network stubs."""
    process_clock = clock or SystemClock()
    route_chain = next((candidate.route.chain_id for candidate in candidates.candidates), 0)
    state = _required_state(state_raw, route_chain)
    merger = ObservationMerger(config.lifecycle)
    rpc_failures = 0
    halted = False
    rpc_calls = 0
    now_ms = process_clock.time_ms() + 1_000_000_000
    if type(now_ms) is not int or isinstance(now_ms, bool) or not _MIN_MS <= now_ms <= _MAX_MS:
        raise ShadowInputError("clock time_ms must be a finite UTC millisecond integer")

    for candidate_index, candidate in enumerate(candidates.candidates):
        if rpc_calls >= config.max_rpc_calls:
            break
        if candidates.truncated:
            break
        transport: Any = rpc
        if isinstance(rpc, DeterministicOfflineTransport):
            transport = _OfflineQuoteTransport(
                rpc,
                candidate.route.route_id or "",
                candidate.amount_in.to_atoms_str(),
            )
        adapter = FixedBlockQuoteAdapter(transport, config.quote_request)
        quote_result = adapter.quote_route(candidate.route, candidate.amount_in, state)
        rpc_calls += quote_result.call_count
        evidence = quote_result.evidence
        if evidence.status == QuoteStatus.RPC_ERROR:
            rpc_failures += 1
            if rpc_failures >= MAX_RPC_FAILURES:
                halted = True
        elif evidence.status == QuoteStatus.QUOTED:
            rpc_failures = 0
        if evidence.status == QuoteStatus.QUOTED:
            evidence = _with_synthetic_evidence(evidence, now_ms)
            economic = _estimated_economics(evidence, now_ms)
            evidence = _with_assessment(evidence, economic)
        else:
            economic = evaluate_quote_evidence(evidence, now_ms, config.conversions)
        event = LifecycleEvent(
            event_id=_event_id(candidate_index, evidence),
            route_id=candidate.route.route_id or f"anon:{candidate_index}",
            source_record_id=f"{candidate.route.route_id}:{candidate.amount_in.to_atoms_str()}",
            base_asset={
                "interface_kind": candidate.route.base_asset.interface_kind,
                "chain_id": candidate.route.base_asset.chain_id,
                "token_key": {
                    "address": candidate.route.base_asset.token_key.address
                    if candidate.route.base_asset.token_key
                    else ""
                },
            },
            amount_atoms=candidate.amount_in.to_atoms_str(),
            decimals=candidate.amount_in.decimals,
            decimals_evidence_ref=candidate.amount_in.decimals_evidence_ref
            or "shadow:missing-evidence",
            registry_semantic_revision=config.registry_semantic_revision,
            observed_at_ms=min(evidence.finished_at_ms or now_ms, now_ms - 1),
            available_at_ms=min(evidence.finished_at_ms or now_ms, now_ms - 1),
            block_time_s=state.block_timestamp_s,
            monotonic_ns=None,
            result=_result_for_quote(evidence),
            candidate=evidence.status == QuoteStatus.QUOTED,
            complete_scan=False,
            revoked=False,
            truncated=candidates.truncated,
        )
        merger.process(event)
        _append_shadow_event(
            ledger,
            event,
            evidence,
            economic,
            rpc_calls,
            halted,
            candidates.truncated,
        )
        if halted:
            break

    fallback_asset = candidates.candidates[0].route.base_asset if candidates.candidates else None
    for rejection in candidates.rejections:
        event = LifecycleEvent(
            event_id=f"rejected:{rejection.route_id}:{rejection.amount_atoms}",
            route_id=rejection.route_id or f"anon-rejected:{rejection.amount_atoms}",
            source_record_id=(f"{rejection.route_id or 'anon-rejected'}:{rejection.amount_atoms}"),
            base_asset={
                "interface_kind": "erc20",
                "chain_id": route_chain or 1,
                "token_key": {
                    "address": (
                        fallback_asset.token_key.address
                        if fallback_asset is not None and fallback_asset.token_key
                        else _ZERO_ADDRESS
                    )
                },
            },
            amount_atoms=rejection.amount_atoms,
            decimals=18,
            decimals_evidence_ref="shadow:missing-evidence",
            registry_semantic_revision=config.registry_semantic_revision,
            observed_at_ms=now_ms,
            available_at_ms=now_ms,
            block_time_s=state.block_timestamp_s,
            monotonic_ns=None,
            result=rejection.reason,
            candidate=False,
            complete_scan=False,
            revoked=False,
            truncated=candidates.truncated,
        )
        merger.process(event)
        _append_rejection(ledger, event, rejection.reason)
    if candidates.truncated:
        _append_halt(ledger, "candidate_budget_exceeded", now_ms)
    if halted:
        _append_halt(ledger, "rpc_failure_circuit_breaker", now_ms)
    result = merger.finalize()
    return ShadowOutcome(
        candidate_count=len(candidates.candidates),
        quoted_count=sum(
            1
            for observation in _all_observations(result)
            if observation["result"] != "quote_failed"
        ),
        ledger_sequence=ledger.confirmed_sequence,
        truncated=candidates.truncated or rpc_calls >= config.max_rpc_calls,
        halted=halted,
        sim_available=False,
        decision_watermark_ms=result.decision_watermark_ms,
    )


def _append_halt(ledger: AppendOnlyLedger, reason: str, now_ms: int) -> None:
    ledger.append(
        {
            "schema_id": "w2-shadow-halt-v1",
            "reason": reason,
            "observed_at_ms": now_ms,
            "available_at_ms": now_ms,
            "sim_available": False,
        }
    )


def _all_observations(result: Any) -> list[dict[str, Any]]:
    return [observation for episode in result.episodes for observation in episode.observations]


def _append_shadow_event(
    ledger: AppendOnlyLedger,
    event: LifecycleEvent,
    evidence: QuoteEvidence,
    economic: Any,
    rpc_calls: int,
    halted: bool,
    truncated: bool,
) -> None:
    try:
        ledger.append(
            {
                "schema_id": "w2-shadow-event-v1",
                "event": asdict(event),
                "quote_status": str(evidence.status),
                "quote_id": evidence.quote_id,
                "net_atoms": economic.net_atoms,
                "economic_status": economic.economic_status,
                "gas_cost_atoms": economic.gas_cost_atoms,
                "sim_available": False,
                "rpc_calls": rpc_calls,
                "halted": halted,
                "truncated": truncated,
            }
        )
    except LedgerError as error:
        raise ShadowInputError(f"ledger append failed: {error}") from error


def _append_rejection(ledger: AppendOnlyLedger, event: LifecycleEvent, reason: str) -> None:
    try:
        ledger.append(
            {
                "schema_id": "w2-shadow-rejection-v1",
                "event": asdict(event),
                "reason": reason,
                "sim_available": False,
            }
        )
    except LedgerError as error:
        raise ShadowInputError(f"ledger append failed: {error}") from error


def _with_assessment(evidence: QuoteEvidence, economic: Any) -> QuoteEvidence:
    return QuoteEvidence(
        quote_id=evidence.quote_id,
        route_ref=evidence.route_ref,
        amount_in=evidence.amount_in,
        amount_out=evidence.amount_out,
        delta_atoms=evidence.delta_atoms,
        hop_quotes=evidence.hop_quotes,
        state_version_ref=evidence.state_version_ref,
        started_at_ms=evidence.started_at_ms,
        finished_at_ms=evidence.finished_at_ms,
        latency_ns=evidence.latency_ns,
        status=evidence.status,
        evidence_level=evidence.evidence_level,
        data_mode=evidence.data_mode,
        actor_scope=evidence.actor_scope,
        fee_included=evidence.fee_included,
        impact_included=evidence.impact_included,
        gas_evidence=evidence.gas_evidence,
        price_evidence=evidence.price_evidence,
        economic_assessment=economic,
        limitations=evidence.limitations,
        error=evidence.error,
        source_refs=evidence.source_refs,
        run_baseline_ref=evidence.run_baseline_ref,
        usable_at_observation=evidence.usable_at_observation,
    )


def _with_synthetic_evidence(evidence: QuoteEvidence, now_ms: int) -> QuoteEvidence:
    """Mark synthetic RPC-quoted evidence with complete zero-cost gas assumptions."""
    return QuoteEvidence(
        quote_id=evidence.quote_id,
        route_ref=evidence.route_ref,
        amount_in=evidence.amount_in,
        amount_out=evidence.amount_out,
        delta_atoms=evidence.delta_atoms,
        hop_quotes=evidence.hop_quotes,
        state_version_ref=evidence.state_version_ref,
        started_at_ms=evidence.started_at_ms,
        finished_at_ms=evidence.finished_at_ms,
        latency_ns=evidence.latency_ns,
        status=evidence.status,
        evidence_level=EvidenceLevel.RPC_QUOTE,
        data_mode=DataMode.SYNTHETIC,
        actor_scope=ActorScope.SYNTHETIC,
        fee_included=TriState.YES,
        impact_included=TriState.YES,
        gas_evidence=GasEvidence(
            gas_kind=GasEvidenceKind.RPC_ESTIMATE,
            payer_asset=evidence.route_ref.base_asset,
            gas_units=0,
            gas_price_atoms=0,
            l1_fee_atoms=0,
            payer_subject="synthetic-observer",
            source_refs=tuple(evidence.source_refs) or ("shadow:synthetic-gas",),
            already_included_components=("shadow:synthetic-gas",),
            quoted_at_ms=now_ms,
            valid_until_ms=now_ms,
            block_ref=evidence.state_version_ref,
            evidence_ref="shadow:synthetic-gas",
        ),
        price_evidence=evidence.price_evidence,
        economic_assessment=evidence.economic_assessment,
        limitations=evidence.limitations,
        error=evidence.error,
        source_refs=evidence.source_refs,
        run_baseline_ref=evidence.run_baseline_ref,
        usable_at_observation=evidence.usable_at_observation,
    )
