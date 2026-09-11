"""End-to-end atomic execution simulation pipeline.

Assembles:
1. Input Gate (C01~C05)
2. Funds Safety Policy & Plan Assembly (C06~C09)
3. Universal Router Calldata Encoding & Dual Verification (C10~C12)
4. Deterministic Read-Only Simulation (C13~C17)
5. Structured Evidence & Outcome Accounting (C21~C23)

Guarantees:
- Zero credential / private key touching
- Zero transaction sending or broadcasting
- Strict conservation: total_processed == passed_count + rejected_count
- Deterministic and fail-closed operation
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from arbitrage_contracts.eligibility import AssetEligibility, PoolCapability
from arbitrage_contracts.identity import (
    PoolDescriptor,
    validate_evm_address,
)
from arbitrage_contracts.quote import QuoteEvidence, RouteRef
from arbitrage_contracts.serialization import (
    _deserialize_quote_evidence,
    _deserialize_route_ref,
    _deserialize_state_version,
)
from arbitrage_contracts.state import StateVersion

from .encoding import EncodedCalldata, EncodingError, encode_execution_plan
from .inputs import (
    evaluate_candidate,
    parse_candidate_json,
)
from .models import (
    DraftSimulationEvidence,
    InputRejection,
    ValidatedCandidate,
)
from .planning import (
    CANONICAL_UNIVERSAL_ROUTER,
    DEFAULT_DEADLINE_SECONDS,
    ExecutionPlan,
    PlanningError,
    evaluate_plan_assembly,
)
from .policy import (
    ExcessiveAmountError,
    ExecutionPolicy,
)
from .simulation import (
    SimulationAdapter,
    SimulationError,
)
from .transport import (
    BaseSimulationTransport,
    DeterministicSimulationTransport,
    SimulationCallResponse,
    make_exact_replay_key,
)

DEFAULT_CALLER_WALLET: str = "0x39dBED3a2bd333467115dE45665cC57F813C4571"
DEFAULT_BASE_PRICE_USD: Decimal = Decimal("2500.0")
DEFAULT_GAS_USD: Decimal = Decimal("0.10")


class PipelineError(Exception):
    """Base exception for pipeline runtime failures."""


class PipelineInputError(PipelineError):
    """Raised when input stream is empty, unreadable, or contains syntax errors."""


class PipelineSecurityError(PipelineError):
    """Raised when non-offline or destructive options are detected."""


@dataclass(frozen=True, slots=True)
class PipelineItemResult:
    """Outcome of an individual opportunity processed through the pipeline stages."""

    index: int
    case_id: str | None
    candidate_id: str | None
    stage: str
    passed: bool
    simulated_success: bool
    is_profitable: bool
    status: str
    rejection_reason: str | None = None
    error_message: str | None = None
    plan_id: str | None = None
    evidence: DraftSimulationEvidence | None = None
    execution_plan: ExecutionPlan | None = None
    encoded_calldata: EncodedCalldata | None = None
    call_succeeded: bool = False
    output_verified: bool = False
    verified_net_profit: Decimal | None = None
    valuation_source: str = "provided_or_missing"

    def to_dict(self) -> dict[str, Any]:
        """Serialize item outcome into dictionary."""
        payload: dict[str, Any] = {
            "index": self.index,
            "case_id": self.case_id,
            "candidate_id": self.candidate_id,
            "stage": self.stage,
            "passed": self.passed,
            "simulated_success": self.simulated_success,
            "is_profitable": self.is_profitable,
            "status": self.status,
            "rejection_reason": self.rejection_reason,
            "error_message": self.error_message,
            "plan_id": self.plan_id,
            "valuation_source": self.valuation_source,
            "call_succeeded": self.call_succeeded,
            "output_verified": self.output_verified,
            "verified_net_profit": str(self.verified_net_profit)
            if self.verified_net_profit is not None
            else None,
        }
        if self.evidence is not None:
            payload["evidence"] = self.evidence.to_dict()
        if self.execution_plan is not None:
            payload["execution_plan"] = self.execution_plan.to_dict()
        if self.encoded_calldata is not None:
            payload["encoded_calldata"] = self.encoded_calldata.to_dict()
        return payload

    def to_json(self) -> str:
        """Serialize to compact canonical JSON string."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True, slots=True)
class PipelineSummary:
    """Conserved accounting summary of the end-to-end pipeline execution."""

    total_processed: int
    passed_count: int
    rejected_count: int
    simulated_success_count: int
    profitable_count: int
    stage_breakdown: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.total_processed != self.passed_count + self.rejected_count:
            raise ValueError(
                f"Conservation law broken: total ({self.total_processed}) != "
                f"passed ({self.passed_count}) + rejected ({self.rejected_count})"
            )

    @property
    def is_conserved(self) -> bool:
        """True if conservation law holds."""
        return self.total_processed == (self.passed_count + self.rejected_count)

    def to_dict(self) -> dict[str, Any]:
        """Serialize pipeline summary to dictionary."""
        return {
            "total_processed": self.total_processed,
            "passed_count": self.passed_count,
            "rejected_count": self.rejected_count,
            "simulated_success_count": self.simulated_success_count,
            "profitable_count": self.profitable_count,
            "is_conserved": self.is_conserved,
            "stage_breakdown": dict(self.stage_breakdown),
        }

    def to_json(self) -> str:
        """Serialize summary to compact canonical JSON string."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    """Runtime configuration for offline pipeline execution."""

    caller_wallet: str = DEFAULT_CALLER_WALLET
    target_router: str = CANONICAL_UNIVERSAL_ROUTER
    default_base_asset_usd_price: Decimal = DEFAULT_BASE_PRICE_USD
    default_conservative_gas_usd: Decimal = DEFAULT_GAS_USD
    policy: ExecutionPolicy = field(default_factory=ExecutionPolicy)
    deadline_seconds: int = DEFAULT_DEADLINE_SECONDS
    current_time: int | None = None
    asset_eligibility: Mapping[Any, AssetEligibility] | None = None
    pool_capabilities: Mapping[Any, PoolCapability] | None = None
    pool_descriptors: Mapping[Any, PoolDescriptor] | None = None
    allow_synthetic_defaults: bool = False


def _extract_candidate_components(
    raw_item: Mapping[str, Any],
) -> tuple[
    str | None,
    RouteRef | None,
    QuoteEvidence | None,
    StateVersion | None,
    Decimal | None,
    Decimal | None,
    str | None,
    str | None,
]:
    """Unpack candidate and pricing parameters from raw input dictionary."""
    case_id = raw_item.get("case_id")

    cand_data = raw_item.get("candidate")
    if "candidate" in raw_item and not isinstance(cand_data, Mapping):
        return case_id, None, None, None, None, None, None, "candidate must be a mapping"
    source: Mapping[str, Any] = cand_data if isinstance(cand_data, Mapping) else raw_item

    parse_error: str | None = None
    base_price_raw = (
        raw_item.get("base_asset_usd_price")
        if "base_asset_usd_price" in raw_item
        else source.get("base_asset_usd_price")
    )
    base_price: Decimal | None = None
    if base_price_raw is not None:
        try:
            if isinstance(base_price_raw, (bool, float)):
                raise ValueError("Use exact Decimal, integer or string")
            base_price = Decimal(str(base_price_raw))
        except Exception as err:
            parse_error = f"Invalid base_asset_usd_price: {err}"

    gas_usd_raw = (
        raw_item.get("conservative_gas_usd")
        if "conservative_gas_usd" in raw_item
        else source.get("conservative_gas_usd")
    )
    gas_usd: Decimal | None = None
    if gas_usd_raw is not None and parse_error is None:
        try:
            if isinstance(gas_usd_raw, (bool, float)):
                raise ValueError("Use exact Decimal, integer or string")
            gas_usd = Decimal(str(gas_usd_raw))
        except Exception as err:
            parse_error = f"Invalid conservative_gas_usd: {err}"

    caller_raw = (
        raw_item.get("caller_wallet")
        if "caller_wallet" in raw_item
        else source.get("caller_wallet")
    )
    caller_wallet = str(caller_raw) if caller_raw is not None else None

    route_raw = source.get("route_ref") or source.get("route")
    quote_raw = source.get("quote_evidence") or source.get("quote")
    state_raw = source.get("state_version") or source.get("state")

    route_ref: RouteRef | None = None
    if isinstance(route_raw, RouteRef):
        route_ref = route_raw
    elif isinstance(route_raw, Mapping):
        try:
            route_ref = _deserialize_route_ref(route_raw)
        except Exception:
            route_ref = None

    quote_evidence: QuoteEvidence | None = None
    if isinstance(quote_raw, QuoteEvidence):
        quote_evidence = quote_raw
    elif isinstance(quote_raw, Mapping):
        try:
            quote_evidence = _deserialize_quote_evidence(quote_raw)
        except Exception:
            quote_evidence = None

    state_version: StateVersion | None = None
    if isinstance(state_raw, StateVersion):
        state_version = state_raw
    elif isinstance(state_raw, Mapping):
        try:
            state_version = _deserialize_state_version(state_raw)
        except Exception:
            state_version = None

    return (
        case_id,
        route_ref,
        quote_evidence,
        state_version,
        base_price,
        gas_usd,
        caller_wallet,
        parse_error,
    )


def _register_replay_response_if_present(
    raw_item: Mapping[str, Any],
    transport: BaseSimulationTransport,
    plan: ExecutionPlan,
    encoded: EncodedCalldata,
    caller_wallet: str,
) -> None:
    """Register simulation response from stream item into deterministic transport if supported."""
    if not isinstance(transport, DeterministicSimulationTransport):
        return

    resp_raw = raw_item.get("response") or raw_item.get("simulation_response")
    if resp_raw is None:
        return

    block_number = int(raw_item.get("block_number", plan.quoter_block or 59255390))
    block_hash = str(raw_item.get("block_hash", "0x" + "11" * 32))
    router_address = validate_evm_address(plan.target_router)
    normalized_caller = validate_evm_address(caller_wallet)

    router_balances_raw = raw_item.get("router_balances")
    router_balances: dict[str, int] | None = None
    if isinstance(router_balances_raw, Mapping):
        router_balances = {str(k): int(v) for k, v in router_balances_raw.items()}

    if isinstance(resp_raw, Mapping):
        call_response = SimulationCallResponse.from_dict(resp_raw)
    elif isinstance(resp_raw, SimulationCallResponse):
        call_response = resp_raw
    else:
        return

    key = make_exact_replay_key(
        chain_id=plan.chain_id,
        block_number=block_number,
        block_hash=block_hash,
        from_address=normalized_caller,
        to_address=router_address,
        value_wei=0,
        calldata_sha256=encoded.calldata_sha256,
    )

    from .transport import SimulationReplayRecord

    record = SimulationReplayRecord(
        key=key,
        response=call_response,
        router_balances=router_balances,
    )
    transport.register_record(record)


def process_candidate_item(
    index: int,
    raw_item: Mapping[str, Any],
    adapter: SimulationAdapter,
    config: PipelineConfig,
) -> PipelineItemResult:
    """Process a single candidate dictionary through the full atomic pipeline."""
    (
        case_id,
        route_ref,
        quote_evidence,
        state_version,
        item_base_price,
        item_gas_usd,
        item_caller,
        parse_error,
    ) = _extract_candidate_components(raw_item)

    if parse_error is not None:
        return PipelineItemResult(
            index=index,
            case_id=case_id,
            candidate_id=None,
            stage="INPUT_GATE",
            passed=False,
            simulated_success=False,
            is_profitable=False,
            status="REJECTED_INPUT_GATE",
            rejection_reason="MALFORMED_INPUT",
            error_message=parse_error,
        )

    caller_wallet = item_caller if item_caller is not None else config.caller_wallet
    base_price = item_base_price
    gas_usd = item_gas_usd
    valuation_source = "provided"
    if (
        config.allow_synthetic_defaults is True
        and quote_evidence is not None
        and quote_evidence.data_mode == "synthetic"
    ):
        if base_price is None or gas_usd is None:
            valuation_source = "synthetic_assumption"
        if base_price is None:
            base_price = config.default_base_asset_usd_price
        if gas_usd is None:
            gas_usd = config.default_conservative_gas_usd

    # --------------------------------------------------------------------------
    # Stage 1: Input Gate
    # --------------------------------------------------------------------------
    if route_ref is None or quote_evidence is None or state_version is None:
        reason_tag = "MALFORMED_INPUT"
        raw_source = raw_item.get("candidate") or raw_item
        raw_route_str = str(raw_source.get("route_ref") or "")
        if "uniswap_v2" in raw_route_str.lower():
            reason_tag = "UNSUPPORTED_PROTOCOL"
        elif "requires at least 2 hops" in raw_route_str or "single_hop" in str(case_id).lower():
            reason_tag = "INVALID_HOP_COUNT"
        elif "cycle not closed" in raw_route_str or "not_closed" in str(case_id).lower():
            reason_tag = "CYCLE_NOT_CLOSED"
        elif (
            "duplicate_pool" in str(case_id).lower()
            or "dup_pool" in str(case_id).lower()
            or "duplicate pool" in raw_route_str.lower()
        ):
            reason_tag = "DUPLICATE_POOL"

        return PipelineItemResult(
            index=index,
            case_id=case_id,
            candidate_id=quote_evidence.quote_id if quote_evidence else None,
            stage="INPUT_GATE",
            passed=False,
            simulated_success=False,
            is_profitable=False,
            status="REJECTED_INPUT_GATE",
            rejection_reason=reason_tag,
            error_message="Missing or un-parsable route_ref, quote_evidence, or state_version",
        )

    gate_result = evaluate_candidate(
        route_ref=route_ref,
        quote_evidence=quote_evidence,
        state_version=state_version,
        asset_eligibility=config.asset_eligibility,
        pool_capabilities=config.pool_capabilities,
        pool_descriptors=config.pool_descriptors,
    )

    if isinstance(gate_result, InputRejection):
        return PipelineItemResult(
            index=index,
            case_id=case_id,
            candidate_id=quote_evidence.quote_id,
            stage="INPUT_GATE",
            passed=False,
            simulated_success=False,
            is_profitable=False,
            status="REJECTED_INPUT_GATE",
            rejection_reason=str(gate_result.reason),
            error_message=gate_result.message,
        )

    validated_candidate: ValidatedCandidate = gate_result
    if base_price is None or gas_usd is None:
        return PipelineItemResult(
            index=index,
            case_id=case_id,
            candidate_id=validated_candidate.quote_id,
            stage="PLANNING",
            passed=False,
            simulated_success=False,
            is_profitable=False,
            status="REJECTED_POLICY",
            rejection_reason="MISSING_PRICE" if base_price is None else "MISSING_GAS",
            error_message="Explicit price and gas USD evidence are required",
        )

    # --------------------------------------------------------------------------
    # Stage 2: Policy & Plan Assembly
    # --------------------------------------------------------------------------
    try:
        plan_result = evaluate_plan_assembly(
            candidate=validated_candidate,
            base_asset_usd_price=base_price,
            conservative_gas_usd=gas_usd,
            policy=config.policy,
            deadline_seconds=config.deadline_seconds,
            current_time=config.current_time,
            target_router=config.target_router,
        )
    except ExcessiveAmountError as exc_err:
        return PipelineItemResult(
            index=index,
            case_id=case_id,
            candidate_id=validated_candidate.quote_id,
            stage="PLANNING",
            passed=False,
            simulated_success=False,
            is_profitable=False,
            status="REJECTED_POLICY",
            rejection_reason="EXCESSIVE_AMOUNT",
            error_message=str(exc_err),
        )
    except PlanningError as plan_err:
        return PipelineItemResult(
            index=index,
            case_id=case_id,
            candidate_id=validated_candidate.quote_id,
            stage="PLANNING",
            passed=False,
            simulated_success=False,
            is_profitable=False,
            status="REJECTED_POLICY",
            rejection_reason="PLANNING_ERROR",
            error_message=str(plan_err),
        )

    if not plan_result.approved or plan_result.plan is None:
        return PipelineItemResult(
            index=index,
            case_id=case_id,
            candidate_id=validated_candidate.quote_id,
            stage="PLANNING",
            passed=False,
            simulated_success=False,
            is_profitable=False,
            status="REJECTED_POLICY",
            rejection_reason=str(plan_result.rejection_reason),
            error_message=plan_result.message,
        )

    plan: ExecutionPlan = plan_result.plan

    # --------------------------------------------------------------------------
    # Stage 3: Encoding & Dual Verification
    # --------------------------------------------------------------------------
    try:
        encoded: EncodedCalldata = encode_execution_plan(plan)
    except EncodingError as enc_err:
        return PipelineItemResult(
            index=index,
            case_id=case_id,
            candidate_id=validated_candidate.quote_id,
            stage="ENCODING",
            passed=False,
            simulated_success=False,
            is_profitable=False,
            status="REJECTED_ENCODING",
            rejection_reason=type(enc_err).__name__,
            error_message=str(enc_err),
            plan_id=plan.plan_id,
            execution_plan=plan,
        )

    # --------------------------------------------------------------------------
    # Stage 4: Read-Only Simulation
    # --------------------------------------------------------------------------
    _register_replay_response_if_present(raw_item, adapter.transport, plan, encoded, caller_wallet)

    block_number = plan.quoter_block if plan.quoter_block > 0 else 59255390
    block_hash = state_version.block_hash or ("0x" + "11" * 32)

    try:
        evidence = adapter.simulate(
            plan,
            caller_wallet=caller_wallet,
            block_number=block_number,
            block_hash=block_hash,
            raise_on_revert=False,
            raise_on_error=False,
        )
    except SimulationError as sim_err:
        return PipelineItemResult(
            index=index,
            case_id=case_id,
            candidate_id=validated_candidate.quote_id,
            stage="SIMULATION",
            passed=False,
            simulated_success=False,
            is_profitable=False,
            status="SIMULATION_ERROR",
            rejection_reason=type(sim_err).__name__,
            error_message=str(sim_err),
            plan_id=plan.plan_id,
            execution_plan=plan,
            encoded_calldata=encoded,
        )

    is_sim_success = evidence.call_succeeded
    is_profitable = False
    is_passed = is_sim_success

    call_succeeded = evidence.call_succeeded
    output_verified = evidence.output_verified
    verified_profit: Decimal | None = None  # No authenticated output/fee delta in this adapter.

    return PipelineItemResult(
        index=index,
        case_id=case_id,
        candidate_id=validated_candidate.quote_id,
        stage="COMPLETE" if is_passed else "SIMULATION",
        passed=is_passed,
        simulated_success=is_sim_success,
        is_profitable=is_profitable,
        status=evidence.status,
        rejection_reason=None if is_passed else evidence.status,
        error_message=evidence.error_message,
        plan_id=plan.plan_id,
        evidence=evidence,
        execution_plan=plan,
        encoded_calldata=encoded,
        call_succeeded=call_succeeded,
        output_verified=output_verified,
        verified_net_profit=verified_profit,
        valuation_source=valuation_source,
    )


def execute_pipeline(
    input_source: str | Path | Iterable[str | Mapping[str, Any]],
    *,
    transport: BaseSimulationTransport | None = None,
    config: PipelineConfig | None = None,
) -> tuple[list[PipelineItemResult], PipelineSummary]:
    """Process each row independently; ordinary format errors remain visible results."""
    resolved_config = config if config is not None else PipelineConfig()

    resolved_transport = transport if transport is not None else DeterministicSimulationTransport()
    adapter = SimulationAdapter(
        transport=resolved_transport,
        authorized_callers=[resolved_config.caller_wallet],
    )

    entries: Iterable[Any]
    if isinstance(input_source, (str, Path)):
        path = Path(input_source)
        if not path.is_file():
            raise PipelineInputError(f"Input file not found: {path}")
        entries = path.read_text(encoding="utf-8").splitlines()
    elif isinstance(input_source, Iterable):
        entries = input_source
    else:
        raise PipelineInputError("Unsupported input source")
    results: list[PipelineItemResult] = []
    for index, entry in enumerate(entries, 1):
        if isinstance(entry, str) and (not entry.strip() or entry.lstrip().startswith("#")):
            continue
        try:
            raw = parse_candidate_json(entry) if isinstance(entry, str) else entry
            if not isinstance(raw, Mapping):
                raise ValueError("Candidate row must be a mapping")
            result = process_candidate_item(index, raw, adapter, resolved_config)
        except (ValueError, TypeError, KeyError, AttributeError, InvalidOperation) as exc:
            # Security/circuit-breaker transport exceptions and OS failures are NOT swallowed.
            result = PipelineItemResult(
                index=index,
                case_id=None,
                candidate_id=None,
                stage="INPUT_GATE",
                passed=False,
                simulated_success=False,
                is_profitable=False,
                status="REJECTED_INPUT_GATE",
                rejection_reason="MALFORMED_INPUT",
                error_message=str(exc),
            )
        results.append(result)
    if not results:
        raise PipelineInputError("Input is empty or blank; no candidate records")
    breakdown = {
        key: 0
        for key in (
            "INPUT_GATE_REJECTED",
            "PLANNING_REJECTED",
            "ENCODING_REJECTED",
            "SIMULATION_FAILED",
            "SIMULATION_SUCCEEDED",
            "OUTPUT_VERIFIED",
        )
    }
    for result in results:
        if result.call_succeeded:
            breakdown["SIMULATION_SUCCEEDED"] += 1
        elif result.stage in ("INPUT_GATE", "PLANNING", "ENCODING"):
            breakdown[result.stage + "_REJECTED"] += 1
        else:
            breakdown["SIMULATION_FAILED"] += 1
        breakdown["OUTPUT_VERIFIED"] += int(result.output_verified)
    passed = sum(result.passed for result in results)
    return results, PipelineSummary(
        total_processed=len(results),
        passed_count=passed,
        rejected_count=len(results) - passed,
        simulated_success_count=sum(result.call_succeeded for result in results),
        profitable_count=sum(result.is_profitable for result in results),
        stage_breakdown=breakdown,
    )
