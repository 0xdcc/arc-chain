"""Canonical serialization, JSON schema validation, and hashing for domain records."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .eligibility import (
    AssetEligibility,
    PoolCapability,
    SourceEvidence,
)
from .identity import Amount, AssetRef, FeeModel, PoolDescriptor, PoolKey, TokenKey
from .opportunity import Observation, OpportunityRecord
from .quote import (
    EconomicAssessment,
    FeeComponent,
    GasEvidence,
    HopQuote,
    HopRef,
    PriceEvidence,
    QuoteEvidence,
    RouteRef,
)
from .state import Cursor, StateVersion

ALLOWED_RECORD_TYPES = frozenset(
    {
        "quote_evidence",
        "opportunity_record",
        "route_ref",
        "state_version",
        "token_key",
        "pool_key",
        "pool_descriptor",
        "asset_eligibility",
        "pool_capability",
        "source_evidence",
    }
)

ALLOWED_DATA_MODES = frozenset(
    {
        "synthetic",
        "historical_replay",
        "live_readonly",
        "confirmed_chain_history",
    }
)


@dataclass(frozen=True, slots=True)
class ContractRecord:
    """Strict top-level envelope container for all persisted/transmitted contract records."""

    schema_id: str
    schema_version: str
    record_type: str
    run_id: str
    data_mode: str
    provenance: dict[str, Any]
    payload: Any

    def __init__(
        self,
        schema_id: str,
        schema_version: str,
        record_type: str,
        run_id: str,
        data_mode: str,
        provenance: Mapping[str, Any],
        payload: Any,
    ) -> None:
        if schema_id != "arbitrage-evidence":
            raise ValueError(f"Invalid schema_id: {schema_id!r}, expected 'arbitrage-evidence'")
        if schema_version != "1.0.0":
            raise ValueError(f"Unsupported schema_version: {schema_version!r}, expected '1.0.0'")
        if record_type not in ALLOWED_RECORD_TYPES:
            raise ValueError(f"Unknown or unsupported record_type: {record_type!r}")
        if type(run_id) is not str or not run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        if data_mode not in ALLOWED_DATA_MODES:
            raise ValueError(f"Invalid data_mode: {data_mode!r}")
        if not isinstance(provenance, Mapping):
            raise TypeError("provenance must be a Mapping")

        object.__setattr__(self, "schema_id", schema_id)
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "record_type", record_type)
        object.__setattr__(self, "run_id", run_id.strip())
        object.__setattr__(self, "data_mode", data_mode)
        object.__setattr__(self, "provenance", dict(provenance))
        object.__setattr__(self, "payload", payload)


def _serialize_asset_ref(obj: AssetRef) -> dict[str, Any]:
    return {
        "address": obj.token_key.address if obj.token_key else obj.native_identifier,
        "balance_domain_id": obj.balance_domain_id,
        "chain_id": obj.chain_id,
        "interface_kind": str(obj.interface_kind),
    }


def _serialize_amount(obj: Amount) -> dict[str, Any]:
    return {
        "asset_ref": _serialize_asset_ref(obj.asset_ref),
        "atoms": obj.to_atoms_str(),
        "decimals": obj.decimals,
        "decimals_evidence_ref": obj.decimals_evidence_ref,
    }


def _serialize_pool_key(obj: PoolKey) -> dict[str, Any]:
    return {
        "chain_id": obj.chain_id,
        "pool_id": obj.pool_id.lower(),
        "pool_id_kind": str(obj.pool_id_kind),
        "protocol_id": obj.protocol_id,
        "venue_address": obj.venue_address.lower(),
        "venue_kind": str(obj.venue_kind),
    }


def _serialize_fee_model(obj: FeeModel) -> dict[str, Any]:
    return {
        "denominator": obj.denominator,
        "evidence_ref": obj.evidence_ref,
        "hook_ref": obj.hook_ref,
        "kind": str(obj.kind),
        "model_version": obj.model_version,
        "numerator": obj.numerator,
        "raw_value": obj.raw_value,
        "unit": obj.unit,
    }


def _serialize_pool_descriptor(obj: PoolDescriptor) -> dict[str, Any]:
    return {
        "currency0": _serialize_asset_ref(obj.currency0),
        "currency1": _serialize_asset_ref(obj.currency1),
        "deployment_status": obj.deployment_status,
        "fee_model": _serialize_fee_model(obj.fee_model),
        "hooks": obj.hooks,
        "identity_evidence_refs": list(obj.identity_evidence_refs),
        "key": _serialize_pool_key(obj.key),
        "tick_spacing": obj.tick_spacing,
        "underlying_pool_refs": [_serialize_pool_key(p) for p in obj.underlying_pool_refs],
    }


def _serialize_hop_ref(obj: HopRef) -> dict[str, Any]:
    return {
        "asset_in": _serialize_asset_ref(obj.asset_in),
        "asset_out": _serialize_asset_ref(obj.asset_out),
        "direction": str(obj.direction),
        "pool_descriptor": _serialize_pool_descriptor(obj.pool_descriptor)
        if obj.pool_descriptor
        else None,
        "pool_key": _serialize_pool_key(obj.pool_key),
    }


def _serialize_route_ref(obj: RouteRef) -> dict[str, Any]:
    return {
        "base_asset": _serialize_asset_ref(obj.base_asset),
        "chain_id": obj.chain_id,
        "hops": [_serialize_hop_ref(h) for h in obj.hops],
        "max_hops": obj.max_hops,
        "route_id": obj.route_id,
        "route_kind": obj.route_kind,
    }


def _serialize_hop_quote(obj: HopQuote) -> dict[str, Any]:
    return {
        "amount_in": _serialize_amount(obj.amount_in),
        "amount_out": _serialize_amount(obj.amount_out) if obj.amount_out else None,
        "asset_in": _serialize_asset_ref(obj.asset_in),
        "asset_out": _serialize_asset_ref(obj.asset_out),
        "error": obj.error,
        "fee_model": _serialize_fee_model(obj.fee_model) if obj.fee_model else None,
        "gas_estimate": obj.gas_estimate,
        "hop_index": obj.hop_index,
        "latency_ns": obj.latency_ns,
        "pool_key": _serialize_pool_key(obj.pool_key),
        "status": str(obj.status),
    }


def _serialize_gas_evidence(obj: GasEvidence) -> dict[str, Any]:
    return {
        "already_included_components": list(obj.already_included_components),
        "block_ref": obj.block_ref,
        "evidence_ref": obj.evidence_ref,
        "gas_kind": str(obj.gas_kind),
        "gas_price_atoms": str(obj.gas_price_atoms) if obj.gas_price_atoms is not None else None,
        "gas_units": obj.gas_units,
        "is_applicable": obj.is_applicable,
        "l1_fee_atoms": str(obj.l1_fee_atoms) if obj.l1_fee_atoms is not None else None,
        "payer_asset": _serialize_asset_ref(obj.payer_asset) if obj.payer_asset else None,
        "payer_subject": obj.payer_subject,
        "quoted_at_ms": obj.quoted_at_ms,
        "source_refs": list(obj.source_refs),
        "valid_until_ms": obj.valid_until_ms,
    }


def _serialize_price_evidence(obj: PriceEvidence) -> dict[str, Any]:
    return {
        "base_asset": _serialize_asset_ref(obj.base_asset),
        "evidence_ref": obj.evidence_ref,
        "market_status": obj.market_status,
        "price_denominator": str(obj.price_denominator)
        if obj.price_denominator is not None
        else None,
        "price_numerator": str(obj.price_numerator) if obj.price_numerator is not None else None,
        "price_source_kind": obj.price_source_kind,
        "price_type": str(obj.price_type),
        "quote_asset": _serialize_asset_ref(obj.quote_asset),
        "source_refs": list(obj.source_refs),
        "timestamp_ms": obj.timestamp_ms,
        "valid_until_ms": obj.valid_until_ms,
    }


def _serialize_fee_component(obj: FeeComponent) -> dict[str, Any]:
    return {
        "amount_atoms": str(obj.amount_atoms),
        "asset": _serialize_asset_ref(obj.asset),
        "component_id": obj.component_id,
        "deduction_stage": obj.deduction_stage,
        "evidence_ref": obj.evidence_ref,
        "is_estimated": obj.is_estimated,
    }


def _serialize_economic_assessment(obj: EconomicAssessment) -> dict[str, Any]:
    return {
        "calculation_refs": list(obj.calculation_refs),
        "economic_status": str(obj.economic_status),
        "fee_components": [_serialize_fee_component(fc) for fc in obj.fee_components],
        "gas_cost_atoms": str(obj.gas_cost_atoms) if obj.gas_cost_atoms is not None else None,
        "is_estimated": obj.is_estimated,
        "net_atoms": str(obj.net_atoms) if obj.net_atoms is not None else None,
        "net_usd_micros": obj.net_usd_micros,
    }


def _serialize_quote_evidence(obj: QuoteEvidence) -> dict[str, Any]:
    return {
        "actor_scope": str(obj.actor_scope),
        "amount_in": _serialize_amount(obj.amount_in),
        "amount_out": _serialize_amount(obj.amount_out) if obj.amount_out else None,
        "data_mode": str(obj.data_mode),
        "delta_atoms": str(obj.delta_atoms) if obj.delta_atoms is not None else None,
        "economic_assessment": _serialize_economic_assessment(obj.economic_assessment)
        if obj.economic_assessment
        else None,
        "error": obj.error,
        "evidence_level": str(obj.evidence_level),
        "fee_included": str(obj.fee_included),
        "finished_at_ms": obj.finished_at_ms,
        "gas_evidence": _serialize_gas_evidence(obj.gas_evidence) if obj.gas_evidence else None,
        "hop_quotes": [_serialize_hop_quote(hq) for hq in obj.hop_quotes],
        "impact_included": str(obj.impact_included),
        "latency_ns": obj.latency_ns,
        "limitations": list(obj.limitations),
        "price_evidence": _serialize_price_evidence(obj.price_evidence)
        if obj.price_evidence
        else None,
        "quote_id": obj.quote_id,
        "route_ref": _serialize_route_ref(obj.route_ref),
        "run_baseline_ref": obj.run_baseline_ref,
        "source_refs": list(obj.source_refs),
        "started_at_ms": obj.started_at_ms,
        "state_version_ref": obj.state_version_ref,
        "status": str(obj.status),
        "usable_at_observation": obj.usable_at_observation,
    }


def _serialize_observation(obj: Observation) -> dict[str, Any]:
    return {
        "evidence_refs": list(obj.evidence_refs),
        "is_truncated": obj.is_truncated,
        "observation_id": obj.observation_id,
        "observed_at_ms": obj.observed_at_ms,
        "phase": str(obj.phase),
        "quote_evidence": _serialize_quote_evidence(obj.quote_evidence)
        if obj.quote_evidence
        else None,
        "rejection_reason": obj.rejection_reason,
        "result": obj.result,
        "run_id": obj.run_id,
        "state_version_ref": obj.state_version_ref,
    }


def _serialize_opportunity_record(obj: OpportunityRecord) -> dict[str, Any]:
    return {
        "actor_scope": str(obj.actor_scope),
        "amount_in": _serialize_amount(obj.amount_in),
        "data_mode": str(obj.data_mode),
        "disappeared_at_ms": obj.disappeared_at_ms,
        "evidence_refs": list(obj.evidence_refs),
        "first_seen_at_ms": obj.first_seen_at_ms,
        "last_rechecked_at_ms": obj.last_rechecked_at_ms,
        "last_seen_at_ms": obj.last_seen_at_ms,
        "observations": [_serialize_observation(obs) for obs in obj.observations],
        "opportunity_id": obj.opportunity_id,
        "phase": str(obj.phase),
        "rejection_reasons": list(obj.rejection_reasons),
        "registry_revision": obj.registry_revision,
        "route_id": obj.route_id,
        "run_baseline_ref": obj.run_baseline_ref,
    }


def _serialize_source_evidence(obj: SourceEvidence) -> dict[str, Any]:
    return {
        "block_ref": obj.block_ref,
        "captured_at_ms": obj.captured_at_ms,
        "chain_id": obj.chain_id,
        "collector_version": obj.collector_version,
        "evidence_id": obj.evidence_id,
        "limitations": list(obj.limitations),
        "params_hash": obj.params_hash,
        "parent_refs": list(obj.parent_refs),
        "raw_sha256": obj.raw_sha256,
        "request_id": obj.request_id,
        "source_locator": obj.source_locator,
        "source_type": obj.source_type,
    }


def _serialize_asset_eligibility(obj: AssetEligibility) -> dict[str, Any]:
    return {
        "asset_ref": _serialize_asset_ref(obj.asset_ref),
        "contract_restrictions": {k: v for k, v in obj.contract_restrictions},
        "decimals_evidence_ref": obj.decimals_evidence_ref,
        "decimals_status": str(obj.decimals_status),
        "evidence_refs": list(obj.evidence_refs),
        "issuance_or_bridge_version": obj.issuance_or_bridge_version,
        "issuer_id": obj.issuer_id,
        "reasons": list(obj.reasons),
        "registry_revision": obj.registry_revision,
        "review_status": str(obj.review_status),
        "reviewed_at_ms": obj.reviewed_at_ms,
        "reviewer_ref": obj.reviewer_ref,
        "subject_scope": str(obj.subject_scope),
        "validity": obj.validity,
    }


def _serialize_pool_capability(obj: PoolCapability) -> dict[str, Any]:
    return {
        "can_atomic_execute": str(obj.can_atomic_execute),
        "can_quote": str(obj.can_quote),
        "can_simulate": str(obj.can_simulate),
        "evidence_refs": list(obj.evidence_refs),
        "pool_key": _serialize_pool_key(obj.pool_key),
        "reasons": list(obj.reasons),
    }


def _serialize_cursor(obj: Cursor) -> dict[str, Any]:
    return {
        "block_hash": obj.block_hash,
        "log_index": obj.log_index,
        "transaction_hash": obj.transaction_hash,
        "transaction_index": obj.transaction_index,
    }


def _serialize_state_version(obj: StateVersion) -> dict[str, Any]:
    return {
        "applied_cursor": _serialize_cursor(obj.applied_cursor) if obj.applied_cursor else None,
        "block_domain": obj.block_domain,
        "block_hash": obj.block_hash,
        "block_number": obj.block_number,
        "block_timestamp_s": obj.block_timestamp_s,
        "chain_id": obj.chain_id,
        "complete_through_block": obj.complete_through_block,
        "completeness": str(obj.completeness),
        "coverage": list(obj.coverage),
        "epoch_id": obj.epoch_id,
        "finality": str(obj.finality),
        "finality_evidence_ref": obj.finality_evidence_ref,
        "optional_l1_anchor": obj.optional_l1_anchor,
        "parent_hash": obj.parent_hash,
        "received_at_ms": obj.received_at_ms,
        "source_ref": obj.source_ref,
        "stale_reasons": list(obj.stale_reasons),
    }


def _serialize_payload(obj: Any) -> Any:
    if isinstance(obj, QuoteEvidence):
        return _serialize_quote_evidence(obj)
    if isinstance(obj, OpportunityRecord):
        return _serialize_opportunity_record(obj)
    if isinstance(obj, RouteRef):
        return _serialize_route_ref(obj)
    if isinstance(obj, HopRef):
        return _serialize_hop_ref(obj)
    if isinstance(obj, Amount):
        return _serialize_amount(obj)
    if isinstance(obj, AssetRef):
        return _serialize_asset_ref(obj)
    if isinstance(obj, PoolKey):
        return _serialize_pool_key(obj)
    if isinstance(obj, PoolDescriptor):
        return _serialize_pool_descriptor(obj)
    if isinstance(obj, FeeModel):
        return _serialize_fee_model(obj)
    if isinstance(obj, TokenKey):
        return {"chain_id": obj.chain_id, "address": obj.address}
    if isinstance(obj, Cursor):
        return _serialize_cursor(obj)
    if isinstance(obj, StateVersion):
        return _serialize_state_version(obj)
    if isinstance(obj, AssetEligibility):
        return _serialize_asset_eligibility(obj)
    if isinstance(obj, PoolCapability):
        return _serialize_pool_capability(obj)
    if isinstance(obj, SourceEvidence):
        return _serialize_source_evidence(obj)
    if isinstance(obj, Mapping):
        return {k: _serialize_payload(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize_payload(v) for v in obj]
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            raise ValueError("Float NaN or Inf is prohibited")
        return obj
    return obj


def _check_no_nan_inf(item: Any) -> None:
    if isinstance(item, float):
        if math.isnan(item) or math.isinf(item):
            raise ValueError(f"Float NaN/Inf prohibited: {item}")
    elif isinstance(item, Mapping):
        for v in item.values():
            _check_no_nan_inf(v)
    elif isinstance(item, (list, tuple)):
        for v in item:
            _check_no_nan_inf(v)


def _deserialize_asset_ref(raw: Mapping[str, Any]) -> AssetRef:
    kind = raw.get("interface_kind", "erc20")
    if kind == "erc20":
        return AssetRef.erc20(
            TokenKey(raw["chain_id"], raw["address"]),
            balance_domain_id=raw.get("balance_domain_id"),
        )
    elif kind == "native":
        return AssetRef.native(
            chain_id=raw["chain_id"],
            native_identifier=raw.get("native_identifier", raw.get("address", "NATIVE")),
            balance_domain_id=raw.get("balance_domain_id"),
        )
    raise ValueError(f"Unknown interface_kind: {kind!r}")


def _deserialize_amount(raw: Mapping[str, Any]) -> Amount:
    return Amount.from_atoms_str(
        asset_ref=_deserialize_asset_ref(raw["asset_ref"]),
        atoms_string=str(raw["atoms"]),
        decimals=raw["decimals"],
        decimals_evidence_ref=raw.get("decimals_evidence_ref"),
    )


def _deserialize_pool_key(raw: Mapping[str, Any]) -> PoolKey:
    return PoolKey(
        chain_id=raw["chain_id"],
        protocol_id=raw["protocol_id"],
        venue_kind=raw["venue_kind"],
        venue_address=raw["venue_address"],
        pool_id_kind=raw["pool_id_kind"],
        pool_id=raw["pool_id"],
    )


def _deserialize_fee_model(raw: Mapping[str, Any]) -> FeeModel:
    return FeeModel(
        kind=raw["kind"],
        raw_value=raw.get("raw_value"),
        unit=raw.get("unit"),
        numerator=raw.get("numerator"),
        denominator=raw.get("denominator"),
        hook_ref=raw.get("hook_ref"),
        model_version=raw.get("model_version"),
        evidence_ref=raw.get("evidence_ref"),
    )


def _deserialize_pool_descriptor(raw: Mapping[str, Any]) -> PoolDescriptor:
    return PoolDescriptor(
        key=_deserialize_pool_key(raw["key"]),
        currency0=_deserialize_asset_ref(raw["currency0"]),
        currency1=_deserialize_asset_ref(raw["currency1"]),
        fee_model=_deserialize_fee_model(raw["fee_model"]),
        tick_spacing=raw.get("tick_spacing"),
        hooks=raw.get("hooks"),
        identity_evidence_refs=raw.get("identity_evidence_refs", ()),
        deployment_status=raw.get("deployment_status", "deployed"),
        underlying_pool_refs=[
            _deserialize_pool_key(p) for p in raw.get("underlying_pool_refs", ())
        ],
    )


def _deserialize_hop_ref(raw: Mapping[str, Any]) -> HopRef:
    return HopRef(
        pool_key=_deserialize_pool_key(raw["pool_key"]),
        asset_in=_deserialize_asset_ref(raw["asset_in"]),
        asset_out=_deserialize_asset_ref(raw["asset_out"]),
        direction=raw.get("direction", "zero_for_one"),
        pool_descriptor=_deserialize_pool_descriptor(raw["pool_descriptor"])
        if raw.get("pool_descriptor")
        else None,
    )


def _deserialize_route_ref(raw: Mapping[str, Any]) -> RouteRef:
    return RouteRef(
        chain_id=raw["chain_id"],
        base_asset=_deserialize_asset_ref(raw["base_asset"]),
        hops=[_deserialize_hop_ref(h) for h in raw["hops"]],
        route_kind=raw.get("route_kind", "same_chain_cycle"),
        max_hops=raw.get("max_hops", 4),
        route_id=raw.get("route_id"),
    )


def _deserialize_hop_quote(raw: Mapping[str, Any]) -> HopQuote:
    return HopQuote(
        hop_index=raw["hop_index"],
        pool_key=_deserialize_pool_key(raw["pool_key"]),
        asset_in=_deserialize_asset_ref(raw["asset_in"]),
        asset_out=_deserialize_asset_ref(raw["asset_out"]),
        amount_in=_deserialize_amount(raw["amount_in"]),
        amount_out=_deserialize_amount(raw["amount_out"]) if raw.get("amount_out") else None,
        status=raw.get("status", "quoted"),
        fee_model=_deserialize_fee_model(raw["fee_model"]) if raw.get("fee_model") else None,
        gas_estimate=raw.get("gas_estimate"),
        latency_ns=raw.get("latency_ns"),
        error=raw.get("error"),
    )


def _deserialize_gas_evidence(raw: Mapping[str, Any]) -> GasEvidence:
    return GasEvidence(
        gas_kind=raw["gas_kind"],
        payer_asset=_deserialize_asset_ref(raw["payer_asset"]) if raw.get("payer_asset") else None,
        gas_units=raw.get("gas_units"),
        gas_price_atoms=int(raw["gas_price_atoms"])
        if raw.get("gas_price_atoms") is not None
        else None,
        l1_fee_atoms=int(raw["l1_fee_atoms"]) if raw.get("l1_fee_atoms") is not None else None,
        payer_subject=raw.get("payer_subject"),
        source_refs=raw.get("source_refs", ()),
        already_included_components=raw.get("already_included_components", ()),
        quoted_at_ms=raw.get("quoted_at_ms"),
        valid_until_ms=raw.get("valid_until_ms"),
        block_ref=raw.get("block_ref"),
        is_applicable=raw.get("is_applicable", True),
        evidence_ref=raw.get("evidence_ref"),
    )


def _deserialize_price_evidence(raw: Mapping[str, Any]) -> PriceEvidence:
    return PriceEvidence(
        base_asset=_deserialize_asset_ref(raw["base_asset"]),
        quote_asset=_deserialize_asset_ref(raw["quote_asset"]),
        price_source_kind=raw.get("price_source_kind", "oracle_reference"),
        price_type=raw.get("price_type", "reference"),
        price_numerator=int(raw["price_numerator"])
        if raw.get("price_numerator") is not None
        else None,
        price_denominator=int(raw["price_denominator"])
        if raw.get("price_denominator") is not None
        else None,
        timestamp_ms=raw.get("timestamp_ms"),
        valid_until_ms=raw.get("valid_until_ms"),
        market_status=raw.get("market_status", "active"),
        source_refs=raw.get("source_refs", ()),
        evidence_ref=raw.get("evidence_ref"),
    )


def _deserialize_fee_component(raw: Mapping[str, Any]) -> FeeComponent:
    return FeeComponent(
        component_id=raw["component_id"],
        amount_atoms=int(raw["amount_atoms"]),
        asset=_deserialize_asset_ref(raw["asset"]),
        deduction_stage=raw.get("deduction_stage", "pool_fee"),
        evidence_ref=raw.get("evidence_ref"),
        is_estimated=raw.get("is_estimated", True),
    )


def _deserialize_economic_assessment(raw: Mapping[str, Any]) -> EconomicAssessment:
    return EconomicAssessment(
        net_atoms=int(raw["net_atoms"]) if raw.get("net_atoms") is not None else None,
        net_usd_micros=raw.get("net_usd_micros"),
        economic_status=raw.get("economic_status", "unknown"),
        fee_components=[_deserialize_fee_component(fc) for fc in raw.get("fee_components", ())],
        gas_cost_atoms=int(raw["gas_cost_atoms"])
        if raw.get("gas_cost_atoms") is not None
        else None,
        calculation_refs=raw.get("calculation_refs", ()),
        is_estimated=raw.get("is_estimated", True),
    )


def _deserialize_quote_evidence(raw: Mapping[str, Any]) -> QuoteEvidence:
    return QuoteEvidence(
        quote_id=raw["quote_id"],
        route_ref=_deserialize_route_ref(raw["route_ref"]),
        amount_in=_deserialize_amount(raw["amount_in"]),
        amount_out=_deserialize_amount(raw["amount_out"]) if raw.get("amount_out") else None,
        delta_atoms=int(raw["delta_atoms"]) if raw.get("delta_atoms") is not None else None,
        hop_quotes=[_deserialize_hop_quote(hq) for hq in raw.get("hop_quotes", ())],
        state_version_ref=raw.get("state_version_ref"),
        started_at_ms=raw.get("started_at_ms"),
        finished_at_ms=raw.get("finished_at_ms"),
        latency_ns=raw.get("latency_ns"),
        status=raw.get("status", "quoted"),
        evidence_level=raw.get("evidence_level", "local_quote"),
        data_mode=raw.get("data_mode", "synthetic"),
        actor_scope=raw.get("actor_scope", "synthetic"),
        fee_included=raw.get("fee_included", "unknown"),
        impact_included=raw.get("impact_included", "unknown"),
        gas_evidence=_deserialize_gas_evidence(raw["gas_evidence"])
        if raw.get("gas_evidence")
        else None,
        price_evidence=_deserialize_price_evidence(raw["price_evidence"])
        if raw.get("price_evidence")
        else None,
        economic_assessment=_deserialize_economic_assessment(raw["economic_assessment"])
        if raw.get("economic_assessment")
        else None,
        limitations=raw.get("limitations", ()),
        error=raw.get("error"),
        source_refs=raw.get("source_refs", ()),
        run_baseline_ref=raw.get("run_baseline_ref"),
        usable_at_observation=raw.get("usable_at_observation", True),
    )


def _deserialize_observation(raw: Mapping[str, Any]) -> Observation:
    return Observation(
        observation_id=raw["observation_id"],
        run_id=raw["run_id"],
        observed_at_ms=raw["observed_at_ms"],
        phase=raw.get("phase", "observed"),
        state_version_ref=raw.get("state_version_ref"),
        quote_evidence=_deserialize_quote_evidence(raw["quote_evidence"])
        if raw.get("quote_evidence")
        else None,
        result=raw.get("result", "neutral"),
        rejection_reason=raw.get("rejection_reason"),
        is_truncated=raw.get("is_truncated", False),
        evidence_refs=raw.get("evidence_refs", ()),
    )


def _deserialize_opportunity_record(raw: Mapping[str, Any]) -> OpportunityRecord:
    return OpportunityRecord(
        opportunity_id=raw["opportunity_id"],
        route_id=raw["route_id"],
        amount_in=_deserialize_amount(raw["amount_in"]),
        first_seen_at_ms=raw["first_seen_at_ms"],
        last_seen_at_ms=raw["last_seen_at_ms"],
        last_rechecked_at_ms=raw["last_rechecked_at_ms"],
        phase=raw.get("phase", "observed"),
        observations=[_deserialize_observation(obs) for obs in raw.get("observations", ())],
        rejection_reasons=raw.get("rejection_reasons", ()),
        evidence_refs=raw.get("evidence_refs", ()),
        disappeared_at_ms=raw.get("disappeared_at_ms"),
        registry_revision=raw.get("registry_revision", "v1"),
        run_baseline_ref=raw.get("run_baseline_ref"),
        data_mode=raw.get("data_mode", "synthetic"),
        actor_scope=raw.get("actor_scope", "synthetic"),
    )


def _deserialize_source_evidence(raw: Mapping[str, Any]) -> SourceEvidence:
    if not isinstance(raw, Mapping):
        raise TypeError(f"source_evidence payload must be Mapping, got {type(raw).__name__}")
    return SourceEvidence(
        evidence_id=raw["evidence_id"],
        source_type=raw["source_type"],
        source_locator=raw["source_locator"],
        raw_sha256=raw.get("raw_sha256"),
        captured_at_ms=raw.get("captured_at_ms", 0),
        chain_id=raw.get("chain_id"),
        block_ref=raw.get("block_ref"),
        request_id=raw.get("request_id"),
        params_hash=raw.get("params_hash"),
        collector_version=raw.get("collector_version", "1.0.0"),
        limitations=raw.get("limitations", ()),
        parent_refs=raw.get("parent_refs", ()),
    )


def _deserialize_asset_eligibility(raw: Mapping[str, Any]) -> AssetEligibility:
    if not isinstance(raw, Mapping):
        raise TypeError(f"asset_eligibility payload must be Mapping, got {type(raw).__name__}")
    asset_ref_raw = raw.get("asset_ref")
    if not isinstance(asset_ref_raw, Mapping):
        raise TypeError(f"asset_ref must be a Mapping, got {type(asset_ref_raw).__name__}")
    return AssetEligibility(
        asset_ref=_deserialize_asset_ref(asset_ref_raw),
        issuer_id=raw.get("issuer_id"),
        issuance_or_bridge_version=raw.get("issuance_or_bridge_version"),
        decimals_status=raw.get("decimals_status", "unknown"),
        decimals_evidence_ref=raw.get("decimals_evidence_ref"),
        contract_restrictions=raw.get("contract_restrictions", ()),
        review_status=raw.get("review_status", "discovered"),
        reviewer_ref=raw.get("reviewer_ref"),
        reviewed_at_ms=raw.get("reviewed_at_ms"),
        validity=raw.get("validity"),
        subject_scope=raw.get("subject_scope", "unknown"),
        evidence_refs=raw.get("evidence_refs", ()),
        reasons=raw.get("reasons", ()),
        registry_revision=raw.get("registry_revision"),
    )


def _deserialize_pool_capability(raw: Mapping[str, Any]) -> PoolCapability:
    if not isinstance(raw, Mapping):
        raise TypeError(f"pool_capability payload must be Mapping, got {type(raw).__name__}")
    pool_key_raw = raw.get("pool_key")
    if not isinstance(pool_key_raw, Mapping):
        raise TypeError(f"pool_key must be a Mapping, got {type(pool_key_raw).__name__}")
    return PoolCapability(
        pool_key=_deserialize_pool_key(pool_key_raw),
        can_quote=raw.get("can_quote", "unknown"),
        can_simulate=raw.get("can_simulate", "unknown"),
        can_atomic_execute=raw.get("can_atomic_execute", "unknown"),
        evidence_refs=raw.get("evidence_refs", ()),
        reasons=raw.get("reasons", ()),
    )


def _deserialize_token_key(raw: Mapping[str, Any]) -> TokenKey:
    if not isinstance(raw, Mapping):
        raise TypeError(f"token_key payload must be Mapping, got {type(raw).__name__}")
    return TokenKey(
        chain_id=raw["chain_id"],
        address=raw.get("raw_address", raw["address"]),
    )


def _deserialize_cursor(raw: Mapping[str, Any]) -> Cursor:
    if not isinstance(raw, Mapping):
        raise TypeError(f"cursor payload must be Mapping, got {type(raw).__name__}")
    return Cursor(
        block_hash=raw["block_hash"],
        transaction_hash=raw.get("transaction_hash"),
        transaction_index=raw.get("transaction_index"),
        log_index=raw.get("log_index"),
    )


def _deserialize_state_version(raw: Mapping[str, Any]) -> StateVersion:
    if not isinstance(raw, Mapping):
        raise TypeError(f"state_version payload must be Mapping, got {type(raw).__name__}")
    applied_cursor = None
    if raw.get("applied_cursor") is not None:
        applied_cursor = _deserialize_cursor(raw["applied_cursor"])
    return StateVersion(
        chain_id=raw["chain_id"],
        block_domain=raw.get("block_domain", "l2"),
        block_number=raw["block_number"],
        block_hash=raw["block_hash"],
        parent_hash=raw.get("parent_hash"),
        epoch_id=raw.get("epoch_id"),
        source_ref=raw.get("source_ref"),
        received_at_ms=raw["received_at_ms"],
        block_timestamp_s=raw.get("block_timestamp_s"),
        applied_cursor=applied_cursor,
        complete_through_block=raw.get("complete_through_block"),
        completeness=raw.get("completeness", "syncing"),
        coverage=raw.get("coverage", ()),
        stale_reasons=raw.get("stale_reasons", ()),
        finality=raw.get("finality", "unsafe"),
        finality_evidence_ref=raw.get("finality_evidence_ref"),
        optional_l1_anchor=raw.get("optional_l1_anchor"),
    )


def validate_record(raw: Mapping[str, Any]) -> ContractRecord:
    """Validate raw dictionary against domain schema and instantiate strongly typed ContractRecord."""
    if not isinstance(raw, Mapping):
        raise TypeError(f"Record must be a Mapping, got {type(raw).__name__}")
    _check_no_nan_inf(raw)

    schema_id = raw.get("schema_id")
    schema_version = raw.get("schema_version")
    record_type = raw.get("record_type")
    run_id = raw.get("run_id")
    data_mode = raw.get("data_mode")
    provenance = raw.get("provenance")
    payload = raw.get("payload")

    if schema_id != "arbitrage-evidence":
        raise ValueError(f"Invalid schema_id: {schema_id!r}, expected 'arbitrage-evidence'")
    if schema_version != "1.0.0":
        raise ValueError(f"Unsupported schema_version: {schema_version!r}, expected '1.0.0'")
    if record_type not in ALLOWED_RECORD_TYPES:
        raise ValueError(f"Unknown record_type: {record_type!r}")
    if type(run_id) is not str or not run_id.strip():
        raise ValueError("run_id must be a non-empty string")
    if data_mode not in ALLOWED_DATA_MODES:
        raise ValueError(f"Invalid data_mode: {data_mode!r}")
    if not isinstance(provenance, Mapping):
        raise TypeError("provenance must be a Mapping")
    if payload is None:
        raise ValueError("payload cannot be None")

    parsed_payload: Any
    if record_type == "quote_evidence":
        if isinstance(payload, Mapping):
            parsed_payload = _deserialize_quote_evidence(payload)
        elif isinstance(payload, QuoteEvidence):
            parsed_payload = payload
        else:
            raise TypeError(
                f"payload for quote_evidence must be Mapping or QuoteEvidence, got {type(payload).__name__}"
            )
    elif record_type == "opportunity_record":
        if isinstance(payload, Mapping):
            parsed_payload = _deserialize_opportunity_record(payload)
        elif isinstance(payload, OpportunityRecord):
            parsed_payload = payload
        else:
            raise TypeError(
                f"payload for opportunity_record must be Mapping or OpportunityRecord, got {type(payload).__name__}"
            )
    elif record_type == "route_ref":
        if isinstance(payload, Mapping):
            parsed_payload = _deserialize_route_ref(payload)
        elif isinstance(payload, RouteRef):
            parsed_payload = payload
        else:
            raise TypeError(
                f"payload for route_ref must be Mapping or RouteRef, got {type(payload).__name__}"
            )
    elif record_type == "state_version":
        if isinstance(payload, Mapping):
            parsed_payload = _deserialize_state_version(payload)
        elif isinstance(payload, StateVersion):
            parsed_payload = payload
        else:
            raise TypeError(
                f"payload for state_version must be Mapping or StateVersion, got {type(payload).__name__}"
            )
    elif record_type == "asset_eligibility":
        if isinstance(payload, Mapping):
            parsed_payload = _deserialize_asset_eligibility(payload)
        elif isinstance(payload, AssetEligibility):
            parsed_payload = payload
        else:
            raise TypeError(
                f"payload for asset_eligibility must be Mapping or AssetEligibility, got {type(payload).__name__}"
            )
    elif record_type == "pool_capability":
        if isinstance(payload, Mapping):
            parsed_payload = _deserialize_pool_capability(payload)
        elif isinstance(payload, PoolCapability):
            parsed_payload = payload
        else:
            raise TypeError(
                f"payload for pool_capability must be Mapping or PoolCapability, got {type(payload).__name__}"
            )
    elif record_type == "source_evidence":
        if isinstance(payload, Mapping):
            parsed_payload = _deserialize_source_evidence(payload)
        elif isinstance(payload, SourceEvidence):
            parsed_payload = payload
        else:
            raise TypeError(
                f"payload for source_evidence must be Mapping or SourceEvidence, got {type(payload).__name__}"
            )
    elif record_type == "token_key":
        if isinstance(payload, Mapping):
            parsed_payload = _deserialize_token_key(payload)
        elif isinstance(payload, TokenKey):
            parsed_payload = payload
        else:
            raise TypeError(
                f"payload for token_key must be Mapping or TokenKey, got {type(payload).__name__}"
            )
    elif record_type == "pool_key":
        if isinstance(payload, Mapping):
            parsed_payload = _deserialize_pool_key(payload)
        elif isinstance(payload, PoolKey):
            parsed_payload = payload
        else:
            raise TypeError(
                f"payload for pool_key must be Mapping or PoolKey, got {type(payload).__name__}"
            )
    elif record_type == "pool_descriptor":
        if isinstance(payload, Mapping):
            parsed_payload = _deserialize_pool_descriptor(payload)
        elif isinstance(payload, PoolDescriptor):
            parsed_payload = payload
        else:
            raise TypeError(
                f"payload for pool_descriptor must be Mapping or PoolDescriptor, got {type(payload).__name__}"
            )
    else:
        parsed_payload = payload

    return ContractRecord(
        schema_id=schema_id,
        schema_version=schema_version,
        record_type=record_type,
        run_id=run_id,
        data_mode=data_mode,
        provenance=provenance,
        payload=parsed_payload,
    )


def encode_record_json(record: ContractRecord) -> str:
    """Encode ContractRecord to canonical UTF-8 JSON with sorted keys and compact separators."""
    if not isinstance(record, ContractRecord):
        raise TypeError(f"record must be a ContractRecord, got {type(record).__name__}")
    _check_no_nan_inf(record.provenance)

    raw_dict = {
        "schema_id": record.schema_id,
        "schema_version": record.schema_version,
        "record_type": record.record_type,
        "run_id": record.run_id,
        "data_mode": str(record.data_mode),
        "provenance": record.provenance,
        "payload": _serialize_payload(record.payload),
    }

    _check_no_nan_inf(raw_dict)
    return json.dumps(
        raw_dict, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key detected: {key!r}")
        result[key] = value
    return result


def _reject_constant(val: str) -> None:
    raise ValueError(f"Prohibited JSON constant (NaN/Inf): {val!r}")


def decode_record_json(text: str) -> ContractRecord:
    """Decode canonical JSON string to validated ContractRecord with duplicate key and NaN rejection."""
    if type(text) is not str:
        raise TypeError(f"text must be a string, got {type(text).__name__}")
    try:
        raw = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON syntax: {exc}") from exc
    return validate_record(raw)


def canonical_record_hash(record: ContractRecord) -> str:
    """Compute deterministic SHA-256 digest of canonically encoded record."""
    canonical_json_str = encode_record_json(record)
    return hashlib.sha256(canonical_json_str.encode("utf-8")).hexdigest()
