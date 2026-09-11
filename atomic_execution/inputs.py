"""Strict fail-closed input consumption gate for atomic execution."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

from arbitrage_contracts.eligibility import AssetEligibility, PoolCapability, ReviewStatus
from arbitrage_contracts.identity import (
    UINT256_MAX,
    FeeModelKind,
    PoolDescriptor,
    PoolKey,
)
from arbitrage_contracts.quote import QuoteEvidence, QuoteStatus, RouteRef
from arbitrage_contracts.serialization import (
    _deserialize_quote_evidence,
    _deserialize_route_ref,
    _deserialize_state_version,
)
from arbitrage_contracts.state import StateVersion, matches_state_ref

from .models import (
    CandidateOpportunity,
    InputRejection,
    InputRejectionReason,
    ValidatedCandidate,
)

ROBINHOOD_CHAIN_ID: int = 4663
WETH_ADDRESS_4663: str = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
USDG_ADDRESS_4663: str = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
ZERO_ADDRESS: str = "0x0000000000000000000000000000000000000000"
SUPPORTED_BASE_ADDRESSES: frozenset[str] = frozenset(
    {WETH_ADDRESS_4663.lower(), USDG_ADDRESS_4663.lower()}
)


class InputGateError(ValueError):
    """Raised when candidate input fails input gate validation under fail-closed policies."""

    def __init__(self, rejection: InputRejection) -> None:
        super().__init__(f"[{rejection.reason}] {rejection.message}")
        self.rejection = rejection

    @property
    def reason(self) -> InputRejectionReason:
        """Return the structured rejection reason."""
        return self.rejection.reason


def match_state_version_ref(reference_value: str | None, state_version: StateVersion) -> bool:
    """Require a recomputed full state anchor; do not upgrade legacy references."""
    return matches_state_ref(reference_value, state_version)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key detected: {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"Non-finite JSON constant: {value}")


def parse_candidate_json(text: str) -> dict[str, Any]:
    """Parse JSON string with duplicate key and empty input fail-closed guards."""
    if type(text) is not str or not text.strip():
        raise InputGateError(
            InputRejection(
                reason=InputRejectionReason.MALFORMED_INPUT,
                message="Input JSON text is empty or blank",
            )
        )
    try:
        raw = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_float=Decimal,
            parse_constant=_reject_json_constant,
        )
    except ValueError as exc:
        if "Duplicate JSON key detected" in str(exc):
            raise InputGateError(
                InputRejection(
                    reason=InputRejectionReason.DUPLICATE_KEY,
                    message=str(exc),
                )
            ) from exc
        raise InputGateError(
            InputRejection(
                reason=InputRejectionReason.MALFORMED_INPUT,
                message=f"Invalid JSON syntax: {exc}",
            )
        ) from exc
    if not isinstance(raw, dict):
        raise InputGateError(
            InputRejection(
                reason=InputRejectionReason.MALFORMED_INPUT,
                message=f"JSON root must be an object, got {type(raw).__name__}",
            )
        )
    return raw


def evaluate_candidate(
    route_ref: RouteRef | Any,
    quote_evidence: QuoteEvidence | Any,
    state_version: StateVersion | Any,
    *,
    asset_eligibility: Mapping[Any, AssetEligibility] | None = None,
    pool_capabilities: Mapping[Any, PoolCapability] | None = None,
    pool_descriptors: Mapping[Any, PoolDescriptor] | None = None,
) -> ValidatedCandidate | InputRejection:
    """Evaluate candidate against C01~C05 gates, returning ValidatedCandidate or InputRejection."""
    # C01: Format & Types presence
    if route_ref is None:
        return InputRejection(
            reason=InputRejectionReason.MISSING_FIELD,
            message="Missing required field: route_ref",
        )
    if not isinstance(route_ref, RouteRef):
        return InputRejection(
            reason=InputRejectionReason.TYPE_ERROR,
            message=f"route_ref must be an instance of RouteRef, got {type(route_ref).__name__}",
        )

    if quote_evidence is None:
        return InputRejection(
            reason=InputRejectionReason.MISSING_FIELD,
            message="Missing required field: quote_evidence",
            route_id=route_ref.route_id,
        )
    if not isinstance(quote_evidence, QuoteEvidence):
        return InputRejection(
            reason=InputRejectionReason.TYPE_ERROR,
            message=(
                "quote_evidence must be an instance of QuoteEvidence, "
                f"got {type(quote_evidence).__name__}"
            ),
            route_id=route_ref.route_id,
        )

    if state_version is None:
        return InputRejection(
            reason=InputRejectionReason.MISSING_FIELD,
            message="Missing required field: state_version",
            route_id=route_ref.route_id,
        )
    if not isinstance(state_version, StateVersion):
        return InputRejection(
            reason=InputRejectionReason.TYPE_ERROR,
            message=(
                "state_version must be an instance of StateVersion, "
                f"got {type(state_version).__name__}"
            ),
            route_id=route_ref.route_id,
        )

    # C02: Same-chain, Same-contract Principal Loop Closure
    if route_ref.chain_id != ROBINHOOD_CHAIN_ID:
        return InputRejection(
            reason=InputRejectionReason.UNSUPPORTED_CHAIN,
            message=(
                f"Chain {route_ref.chain_id} is unsupported; "
                f"only Robinhood {ROBINHOOD_CHAIN_ID} is supported"
            ),
            route_id=route_ref.route_id,
        )
    if state_version.chain_id != ROBINHOOD_CHAIN_ID:
        return InputRejection(
            reason=InputRejectionReason.UNSUPPORTED_CHAIN,
            message=(
                f"state_version chain_id ({state_version.chain_id}) mismatch; "
                f"expected {ROBINHOOD_CHAIN_ID}"
            ),
            route_id=route_ref.route_id,
        )

    base_asset = route_ref.base_asset
    if base_asset.chain_id != ROBINHOOD_CHAIN_ID:
        return InputRejection(
            reason=InputRejectionReason.UNSUPPORTED_CHAIN,
            message=f"base_asset chain_id ({base_asset.chain_id}) mismatch with route chain_id",
            route_id=route_ref.route_id,
        )
    if base_asset.interface_kind != "erc20" or base_asset.token_key is None:
        return InputRejection(
            reason=InputRejectionReason.ETH_WETH_MIXED,
            message="Native ETH is prohibited as base asset; must use canonical ERC20 WETH or USDG",
            route_id=route_ref.route_id,
        )

    base_address = base_asset.token_key.address.lower()
    if base_address not in SUPPORTED_BASE_ADDRESSES:
        return InputRejection(
            reason=InputRejectionReason.UNSUPPORTED_BASE_ASSET,
            message=(
                f"Unsupported base asset {base_address}; only canonical WETH "
                f"({WETH_ADDRESS_4663}) and USDG ({USDG_ADDRESS_4663}) are permitted on chain 4663"
            ),
            route_id=route_ref.route_id,
        )

    # C03: Hop Count & Pool Topology
    hop_count = len(route_ref.hops)
    if hop_count < 2:
        return InputRejection(
            reason=InputRejectionReason.INVALID_HOP_COUNT,
            message=f"Route has {hop_count} hops; strictly requires 2 or 3 hops",
            route_id=route_ref.route_id,
        )
    if hop_count > 3:
        return InputRejection(
            reason=InputRejectionReason.UNSUPPORTED_HOP_COUNT,
            message=f"Hop count {hop_count} unsupported in v1 (strictly 2 or 3 hops)",
            route_id=route_ref.route_id,
        )

    start_asset = route_ref.hops[0].asset_in
    terminal_asset = route_ref.hops[-1].asset_out
    if start_asset.interface_kind != "erc20" or start_asset.token_key is None:
        return InputRejection(
            reason=InputRejectionReason.ETH_WETH_MIXED,
            message="Route start asset is native ETH; ERC20 token required",
            route_id=route_ref.route_id,
        )
    if terminal_asset.interface_kind != "erc20" or terminal_asset.token_key is None:
        return InputRejection(
            reason=InputRejectionReason.ETH_WETH_MIXED,
            message="Route terminal asset is native ETH; ERC20 token required",
            route_id=route_ref.route_id,
        )

    start_address = start_asset.token_key.address.lower()
    terminal_address = terminal_asset.token_key.address.lower()

    if start_address != terminal_address:
        return InputRejection(
            reason=InputRejectionReason.CYCLE_NOT_CLOSED,
            message=(
                f"Route cycle is not closed: starts with {start_address} "
                f"but ends with {terminal_address}"
            ),
            route_id=route_ref.route_id,
        )
    if start_address != base_address:
        return InputRejection(
            reason=InputRejectionReason.ASSET_MISMATCH,
            message=f"Route start asset {start_address} does not match base asset {base_address}",
            route_id=route_ref.route_id,
        )

    seen_pool_ids: set[str] = set()
    seen_pool_keys: set[PoolKey] = set()
    for hop_index, hop in enumerate(route_ref.hops):
        protocol = hop.pool_key.protocol_id.lower()
        if "v2" in protocol:
            return InputRejection(
                reason=InputRejectionReason.UNSUPPORTED_PROTOCOL,
                message=f"Protocol {hop.pool_key.protocol_id} is unsupported on Robinhood 4663",
                route_id=route_ref.route_id,
            )

        pool_id = hop.pool_key.pool_id.lower()
        if hop.pool_key in seen_pool_keys or pool_id in seen_pool_ids:
            return InputRejection(
                reason=InputRejectionReason.DUPLICATE_POOL,
                message=f"Duplicate pool {pool_id} detected in route at hop {hop_index}",
                route_id=route_ref.route_id,
            )
        seen_pool_keys.add(hop.pool_key)
        seen_pool_ids.add(pool_id)

        desc = hop.pool_descriptor
        if desc is None and pool_descriptors is not None:
            desc = pool_descriptors.get(hop.pool_key) or pool_descriptors.get(pool_id)
        if desc is not None:
            for underlying_key in desc.underlying_pool_refs:
                underlying_id = underlying_key.pool_id.lower()
                if underlying_id in seen_pool_ids:
                    return InputRejection(
                        reason=InputRejectionReason.DUPLICATE_POOL,
                        message=f"Underlying pool {underlying_id} overlaps with route pool",
                        route_id=route_ref.route_id,
                    )
                seen_pool_ids.add(underlying_id)

    # C01 detail checks on QuoteEvidence
    amount_in = quote_evidence.amount_in
    if isinstance(amount_in.atoms, bool) or type(amount_in.atoms) is not int:
        return InputRejection(
            reason=InputRejectionReason.TYPE_ERROR,
            message=f"amount_in atoms must be int and cannot be bool, got {type(amount_in.atoms).__name__}",
            route_id=route_ref.route_id,
        )
    if amount_in.atoms <= 0:
        return InputRejection(
            reason=InputRejectionReason.TYPE_ERROR,
            message=f"amount_in atoms must be positive, got {amount_in.atoms}",
            route_id=route_ref.route_id,
        )
    if amount_in.atoms > UINT256_MAX:
        return InputRejection(
            reason=InputRejectionReason.UINT256_OVERFLOW,
            message=f"amount_in atoms exceed uint256 bounds: {amount_in.atoms}",
            route_id=route_ref.route_id,
        )

    if amount_in.asset_ref != base_asset:
        return InputRejection(
            reason=InputRejectionReason.ASSET_MISMATCH,
            message="amount_in asset does not match route base_asset",
            route_id=route_ref.route_id,
        )

    if quote_evidence.amount_out is not None:
        out_atoms = quote_evidence.amount_out.atoms
        if isinstance(out_atoms, bool) or type(out_atoms) is not int:
            return InputRejection(
                reason=InputRejectionReason.TYPE_ERROR,
                message=f"amount_out atoms must be int and cannot be bool, got {type(out_atoms).__name__}",
                route_id=route_ref.route_id,
            )
        if out_atoms < 0 or out_atoms > UINT256_MAX:
            return InputRejection(
                reason=InputRejectionReason.UINT256_OVERFLOW,
                message=f"amount_out atoms out of uint256 bounds: {out_atoms}",
                route_id=route_ref.route_id,
            )

    if quote_evidence.route_ref.route_id != route_ref.route_id:
        return InputRejection(
            reason=InputRejectionReason.MALFORMED_INPUT,
            message=(
                f"quote_evidence.route_ref.route_id ({quote_evidence.route_ref.route_id}) "
                f"does not match route_ref.route_id ({route_ref.route_id})"
            ),
            route_id=route_ref.route_id,
        )

    # C04: Asset Eligibility & Pool Capability Admissions
    if asset_eligibility is not None:
        all_assets = [route_ref.base_asset]
        for hop in route_ref.hops:
            all_assets.extend([hop.asset_in, hop.asset_out])

        for asset in all_assets:
            elig = asset_eligibility.get(asset)
            if elig is None and asset.token_key:
                elig = asset_eligibility.get(
                    asset.token_key.address.lower()
                ) or asset_eligibility.get(asset.token_key.raw_address)
            if elig is None:
                return InputRejection(
                    reason=InputRejectionReason.ASSET_NOT_APPROVED,
                    message=f"Asset {asset} is missing from asset eligibility registry",
                    route_id=route_ref.route_id,
                )
            if elig.review_status != ReviewStatus.APPROVED:
                return InputRejection(
                    reason=InputRejectionReason.ASSET_NOT_APPROVED,
                    message=f"Asset {asset} review status is {elig.review_status}, expected APPROVED",
                    route_id=route_ref.route_id,
                )

    for hop_quote in quote_evidence.hop_quotes:
        if hop_quote.fee_model and (
            hop_quote.fee_model.kind == FeeModelKind.DYNAMIC
            or hop_quote.fee_model.kind == "dynamic"
        ):
            return InputRejection(
                reason=InputRejectionReason.DYNAMIC_FEE_UNSUPPORTED,
                message=f"Hop quote {hop_quote.hop_index} declares dynamic fee model",
                route_id=route_ref.route_id,
            )

    for hop_index, hop in enumerate(route_ref.hops):
        desc = hop.pool_descriptor
        if desc is None and pool_descriptors is not None:
            desc = pool_descriptors.get(hop.pool_key) or pool_descriptors.get(
                hop.pool_key.pool_id.lower()
            )
        if desc is not None:
            if desc.fee_model and (
                desc.fee_model.kind == FeeModelKind.DYNAMIC or desc.fee_model.kind == "dynamic"
            ):
                return InputRejection(
                    reason=InputRejectionReason.DYNAMIC_FEE_UNSUPPORTED,
                    message=f"Pool descriptor at hop {hop_index} declares dynamic fee model",
                    route_id=route_ref.route_id,
                )
            if desc.hooks is not None and desc.hooks.lower() != ZERO_ADDRESS.lower():
                return InputRejection(
                    reason=InputRejectionReason.V4_NON_ZERO_HOOK,
                    message=f"V4 pool at hop {hop_index} has non-zero hook {desc.hooks}, unsupported in W5-B",
                    route_id=route_ref.route_id,
                )

    if pool_capabilities is not None:
        for hop_index, hop in enumerate(route_ref.hops):
            cap = pool_capabilities.get(hop.pool_key) or pool_capabilities.get(
                hop.pool_key.pool_id.lower()
            )
            if cap is not None:
                if (
                    cap.can_quote == "unsupported"
                    or cap.can_simulate == "unsupported"
                    or cap.can_atomic_execute == "unsupported"
                ):
                    return InputRejection(
                        reason=InputRejectionReason.POOL_CAPABILITY_UNSUPPORTED,
                        message=f"Pool at hop {hop_index} capability is unsupported",
                        route_id=route_ref.route_id,
                    )

    # C05: State and Timing Monotonicity & Block Pinning
    if quote_evidence.status != QuoteStatus.QUOTED:
        return InputRejection(
            reason=InputRejectionReason.QUOTE_STATUS_INVALID,
            message=f"Quote status is {quote_evidence.status}, expected QUOTED",
            route_id=route_ref.route_id,
        )

    if not state_version.is_ready() or state_version.stale_reasons:
        return InputRejection(
            reason=InputRejectionReason.STATE_NOT_READY,
            message=f"StateVersion completeness is {state_version.completeness}, expected ready",
            route_id=route_ref.route_id,
        )

    quote_state_ref = quote_evidence.state_version_ref
    if quote_state_ref is None:
        return InputRejection(
            reason=InputRejectionReason.STATE_VERSION_MISMATCH,
            message="quote_evidence.state_version_ref is missing/None, cannot verify block binding",
            route_id=route_ref.route_id,
        )

    if not match_state_version_ref(quote_state_ref, state_version):
        return InputRejection(
            reason=InputRejectionReason.STATE_VERSION_MISMATCH,
            message=(
                f"Quote evidence state reference '{quote_state_ref}' does not match "
                f"state_version block {state_version.block_number} ({state_version.block_hash})"
            ),
            route_id=route_ref.route_id,
        )

    return ValidatedCandidate(
        route_ref=route_ref,
        quote_evidence=quote_evidence,
        state_version=state_version,
        base_asset=base_asset,
        amount_in=amount_in,
    )


def validate_candidate(
    route_ref: RouteRef | Any,
    quote_evidence: QuoteEvidence | Any,
    state_version: StateVersion | Any,
    *,
    asset_eligibility: Mapping[Any, AssetEligibility] | None = None,
    pool_capabilities: Mapping[Any, PoolCapability] | None = None,
    pool_descriptors: Mapping[Any, PoolDescriptor] | None = None,
) -> ValidatedCandidate:
    """Validate candidate inputs and return ValidatedCandidate or raise InputGateError."""
    result = evaluate_candidate(
        route_ref=route_ref,
        quote_evidence=quote_evidence,
        state_version=state_version,
        asset_eligibility=asset_eligibility,
        pool_capabilities=pool_capabilities,
        pool_descriptors=pool_descriptors,
    )
    if isinstance(result, InputRejection):
        raise InputGateError(result)
    return result


def validate_opportunity(
    opportunity: CandidateOpportunity,
    *,
    asset_eligibility: Mapping[Any, AssetEligibility] | None = None,
    pool_capabilities: Mapping[Any, PoolCapability] | None = None,
    pool_descriptors: Mapping[Any, PoolDescriptor] | None = None,
) -> ValidatedCandidate:
    """Validate CandidateOpportunity envelope and return ValidatedCandidate."""
    if not isinstance(opportunity, CandidateOpportunity):
        raise InputGateError(
            InputRejection(
                reason=InputRejectionReason.TYPE_ERROR,
                message=f"opportunity must be CandidateOpportunity, got {type(opportunity).__name__}",
            )
        )
    return validate_candidate(
        route_ref=opportunity.route_ref,
        quote_evidence=opportunity.quote_evidence,
        state_version=opportunity.state_version,
        asset_eligibility=asset_eligibility,
        pool_capabilities=pool_capabilities,
        pool_descriptors=pool_descriptors,
    )


def load_candidate_from_dict(
    raw: Mapping[str, Any],
    state_version: StateVersion | None = None,
    *,
    asset_eligibility: Mapping[Any, AssetEligibility] | None = None,
    pool_capabilities: Mapping[Any, PoolCapability] | None = None,
    pool_descriptors: Mapping[Any, PoolDescriptor] | None = None,
) -> ValidatedCandidate:
    """Load and validate candidate from dictionary mapping."""
    if not isinstance(raw, Mapping):
        raise InputGateError(
            InputRejection(
                reason=InputRejectionReason.TYPE_ERROR,
                message=f"Input must be a mapping, got {type(raw).__name__}",
            )
        )

    if "candidate" in raw and isinstance(raw["candidate"], Mapping):
        raw = raw["candidate"]

    route_raw = raw.get("route_ref") or raw.get("route")
    if route_raw is None:
        raise InputGateError(
            InputRejection(
                reason=InputRejectionReason.MISSING_FIELD,
                message="Missing required field: route_ref",
            )
        )

    quote_raw = raw.get("quote_evidence") or raw.get("quote")
    if quote_raw is None:
        raise InputGateError(
            InputRejection(
                reason=InputRejectionReason.MISSING_FIELD,
                message="Missing required field: quote_evidence",
            )
        )

    state_raw = raw.get("state_version") or raw.get("state")
    resolved_state = state_version
    if resolved_state is None:
        if state_raw is None:
            raise InputGateError(
                InputRejection(
                    reason=InputRejectionReason.MISSING_FIELD,
                    message="Missing required field: state_version",
                )
            )
        if isinstance(state_raw, StateVersion):
            resolved_state = state_raw
        elif isinstance(state_raw, Mapping):
            resolved_state = _deserialize_state_version(state_raw)
        else:
            raise InputGateError(
                InputRejection(
                    reason=InputRejectionReason.TYPE_ERROR,
                    message="state_version must be Mapping or StateVersion",
                )
            )

    try:
        route_ref = (
            route_raw if isinstance(route_raw, RouteRef) else _deserialize_route_ref(route_raw)
        )
    except Exception as exc:
        raise InputGateError(
            InputRejection(
                reason=InputRejectionReason.TYPE_ERROR,
                message=f"Failed to deserialize route_ref: {exc}",
            )
        ) from exc

    try:
        quote_evidence = (
            quote_raw
            if isinstance(quote_raw, QuoteEvidence)
            else _deserialize_quote_evidence(quote_raw)
        )
    except Exception as exc:
        raise InputGateError(
            InputRejection(
                reason=InputRejectionReason.TYPE_ERROR,
                message=f"Failed to deserialize quote_evidence: {exc}",
            )
        ) from exc

    return validate_candidate(
        route_ref=route_ref,
        quote_evidence=quote_evidence,
        state_version=resolved_state,
        asset_eligibility=asset_eligibility,
        pool_capabilities=pool_capabilities,
        pool_descriptors=pool_descriptors,
    )


def load_candidates_from_jsonl(
    source: str | Path | Iterable[str],
    state_version: StateVersion | None = None,
    *,
    raise_on_rejection: bool = False,
    asset_eligibility: Mapping[Any, AssetEligibility] | None = None,
    pool_capabilities: Mapping[Any, PoolCapability] | None = None,
    pool_descriptors: Mapping[Any, PoolDescriptor] | None = None,
) -> tuple[list[ValidatedCandidate], list[InputRejection]]:
    """Parse JSONL lines and return partitioned lists of validated candidates and rejections."""
    lines: list[str]
    if isinstance(source, Path):
        text = source.read_text(encoding="utf-8")
        if not text.strip():
            empty_rejection = InputRejection(
                reason=InputRejectionReason.MALFORMED_INPUT,
                message="JSONL file is empty or blank",
            )
            if raise_on_rejection:
                raise InputGateError(empty_rejection)
            return [], [empty_rejection]
        lines = text.splitlines()
    elif isinstance(source, str):
        path_candidate = Path(source)
        if path_candidate.is_file():
            text = path_candidate.read_text(encoding="utf-8")
            if not text.strip():
                empty_rejection = InputRejection(
                    reason=InputRejectionReason.MALFORMED_INPUT,
                    message="JSONL file is empty or blank",
                )
                if raise_on_rejection:
                    raise InputGateError(empty_rejection)
                return [], [empty_rejection]
            lines = text.splitlines()
        else:
            if not source.strip():
                empty_rejection = InputRejection(
                    reason=InputRejectionReason.MALFORMED_INPUT,
                    message="JSONL string input is empty or blank",
                )
                if raise_on_rejection:
                    raise InputGateError(empty_rejection)
                return [], [empty_rejection]
            lines = source.splitlines()
    else:
        lines = list(source)
        if not lines:
            empty_rejection = InputRejection(
                reason=InputRejectionReason.MALFORMED_INPUT,
                message="JSONL input iterable is empty",
            )
            if raise_on_rejection:
                raise InputGateError(empty_rejection)
            return [], [empty_rejection]

    validated_list: list[ValidatedCandidate] = []
    rejections_list: list[InputRejection] = []

    for line_index, line in enumerate(lines, start=1):
        line_content = line.strip()
        if not line_content:
            rejection = InputRejection(
                reason=InputRejectionReason.MALFORMED_INPUT,
                message=f"Blank line detected at line {line_index}",
            )
            if raise_on_rejection:
                raise InputGateError(rejection)
            rejections_list.append(rejection)
            continue

        try:
            parsed_dict = parse_candidate_json(line_content)
            candidate = load_candidate_from_dict(
                raw=parsed_dict,
                state_version=state_version,
                asset_eligibility=asset_eligibility,
                pool_capabilities=pool_capabilities,
                pool_descriptors=pool_descriptors,
            )
            validated_list.append(candidate)
        except InputGateError as gate_err:
            if raise_on_rejection:
                raise
            rejections_list.append(gate_err.rejection)

    return validated_list, rejections_list
