"""Strict serialization, deserialization, Canonical JSON, and hashing for RWA research records."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from arbitrage_contracts import (
    TokenKey,
)
from arbitrage_contracts.serialization import (
    _deserialize_asset_ref,
    _deserialize_pool_descriptor,
    _deserialize_pool_key,
    _deserialize_state_version,
    _serialize_asset_ref,
    _serialize_pool_descriptor,
    _serialize_pool_key,
    _serialize_state_version,
)

from .models import (
    AmountQuotePoint,
    EligibilityMatrix,
    EquityReference,
    InstrumentBinding,
    OracleObservation,
    RwaResearchRecord,
)

_SCHEMA_ID = "w7-rwa-research"
_SCHEMA_VERSION = "0.1.0-draft"

_RECORD_ALLOWED_KEYS = frozenset(
    {
        "schema_id",
        "schema_version",
        "record_id",
        "is_draft",
        "data_mode",
        "as_of_ms",
        "observed_at_ms",
        "instrument",
        "oracle",
        "equity_ref",
        "quotes",
        "eligibility",
        "reasons",
    }
)

_INSTRUMENT_ALLOWED_KEYS = frozenset(
    {
        "token_key",
        "issuer_id",
        "underlier_id",
        "feed_address",
        "quote_currency",
        "token_decimals",
        "feed_decimals",
        "evidence_refs",
        "valid_from_ms",
        "valid_to_ms",
    }
)

_ORACLE_ALLOWED_KEYS = frozenset(
    {
        "state_version",
        "round_id",
        "answer",
        "started_at_s",
        "updated_at_s",
        "answered_in_round",
        "multiplier_uint",
        "pending_multiplier_uint",
        "effective_at_s",
        "oracle_paused",
        "heartbeat_s",
        "session_kind",
        "sequencer_status",
        "sequencer_started_at_s",
        "grace_period_s",
    }
)

_EQUITY_REF_ALLOWED_KEYS = frozenset(
    {
        "underlier_id",
        "currency",
        "bid_price",
        "ask_price",
        "price_basis",
        "generated_at_ms",
        "received_at_ms",
        "session_kind",
        "trading_halt",
        "source_ref",
    }
)

_QUOTE_POINT_ALLOWED_KEYS = frozenset(
    {
        "pool_key",
        "pool_descriptor",
        "direction",
        "asset_in",
        "asset_out",
        "amount_in_atoms",
        "amount_out_atoms",
        "quote_id",
        "state_version_ref",
        "fee_included",
        "impact_included",
        "gas_estimate",
        "gas_evidence_kind",
        "status",
        "error",
        "data_mode",
        "evidence_level",
    }
)

_ELIGIBILITY_ALLOWED_KEYS = frozenset(
    {
        "token_key",
        "capabilities",
        "evidence_refs",
    }
)


def _check_no_nan_inf(value: Any) -> None:
    if isinstance(value, float):
        raise ValueError("Float values are strictly forbidden in research records")
    if isinstance(value, Mapping):
        for k, v in value.items():
            _check_no_nan_inf(k)
            _check_no_nan_inf(v)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_no_nan_inf(item)


def _validate_keys(mapping: Mapping[str, Any], allowed_keys: frozenset[str], scope: str) -> None:
    extra = set(mapping.keys()) - allowed_keys
    if extra:
        raise ValueError(f"Undeclared extra fields in {scope}: {sorted(extra)}")


def _parse_big_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{field_name} must be int or decimal string, got {type(value).__name__}")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped or stripped != value or not stripped.isdigit():
            raise ValueError(f"{field_name} must be a valid canonical integer string: {value!r}")
        return int(stripped)
    raise TypeError(f"{field_name} must be int or str, got {type(value).__name__}")


def to_dict(record: RwaResearchRecord) -> dict[str, Any]:
    """Serialize RwaResearchRecord to pure Python dict with canonical atom representations."""
    if not isinstance(record, RwaResearchRecord):
        raise TypeError(f"Expected RwaResearchRecord, got {type(record).__name__}")

    inst = record.instrument
    instrument_dict = {
        "token_key": {
            "chain_id": inst.token_key.chain_id,
            "address": inst.token_key.address,
        },
        "issuer_id": inst.issuer_id,
        "underlier_id": inst.underlier_id,
        "feed_address": inst.feed_address,
        "quote_currency": inst.quote_currency,
        "token_decimals": inst.token_decimals,
        "feed_decimals": inst.feed_decimals,
        "evidence_refs": list(inst.evidence_refs),
        "valid_from_ms": inst.valid_from_ms,
        "valid_to_ms": inst.valid_to_ms,
    }

    oracle_dict = None
    if record.oracle is not None:
        ora = record.oracle
        oracle_dict = {
            "state_version": _serialize_state_version(ora.state_version) if ora.state_version else None,
            "round_id": ora.round_id,
            "answer": str(ora.answer),
            "started_at_s": ora.started_at_s,
            "updated_at_s": ora.updated_at_s,
            "answered_in_round": ora.answered_in_round,
            "multiplier_uint": str(ora.multiplier_uint),
            "pending_multiplier_uint": str(ora.pending_multiplier_uint) if ora.pending_multiplier_uint is not None else None,
            "effective_at_s": ora.effective_at_s,
            "oracle_paused": ora.oracle_paused,
            "heartbeat_s": ora.heartbeat_s,
            "session_kind": ora.session_kind,
            "sequencer_status": ora.sequencer_status,
            "sequencer_started_at_s": ora.sequencer_started_at_s,
            "grace_period_s": ora.grace_period_s,
        }

    equity_dict = None
    if record.equity_ref is not None:
        eq = record.equity_ref
        equity_dict = {
            "underlier_id": eq.underlier_id,
            "currency": eq.currency,
            "bid_price": eq.bid_price,
            "ask_price": eq.ask_price,
            "price_basis": eq.price_basis,
            "generated_at_ms": eq.generated_at_ms,
            "received_at_ms": eq.received_at_ms,
            "session_kind": eq.session_kind,
            "trading_halt": eq.trading_halt,
            "source_ref": eq.source_ref,
        }

    quotes_list = []
    for q in record.quotes:
        quotes_list.append(
            {
                "pool_key": _serialize_pool_key(q.pool_key),
                "pool_descriptor": _serialize_pool_descriptor(q.pool_descriptor) if q.pool_descriptor else None,
                "direction": q.direction,
                "asset_in": _serialize_asset_ref(q.asset_in),
                "asset_out": _serialize_asset_ref(q.asset_out),
                "amount_in_atoms": str(q.amount_in_atoms),
                "amount_out_atoms": str(q.amount_out_atoms) if q.amount_out_atoms is not None else None,
                "quote_id": q.quote_id,
                "state_version_ref": q.state_version_ref,
                "fee_included": q.fee_included,
                "impact_included": q.impact_included,
                "gas_estimate": q.gas_estimate,
                "gas_evidence_kind": q.gas_evidence_kind,
                "status": q.status,
                "error": q.error,
                "data_mode": q.data_mode,
                "evidence_level": q.evidence_level,
            }
        )

    eligibility_dict = None
    if record.eligibility is not None:
        el = record.eligibility
        eligibility_dict = {
            "token_key": {
                "chain_id": el.token_key.chain_id,
                "address": el.token_key.address,
            },
            "capabilities": dict(sorted(el.capabilities.items())),
            "evidence_refs": list(el.evidence_refs),
        }

    return {
        "schema_id": record.schema_id,
        "schema_version": record.schema_version,
        "record_id": record.record_id,
        "is_draft": record.is_draft,
        "data_mode": record.data_mode,
        "as_of_ms": record.as_of_ms,
        "observed_at_ms": record.observed_at_ms,
        "instrument": instrument_dict,
        "oracle": oracle_dict,
        "equity_ref": equity_dict,
        "quotes": quotes_list,
        "eligibility": eligibility_dict,
        "reasons": list(record.reasons),
    }


def from_dict(raw: Mapping[str, Any]) -> RwaResearchRecord:
    """Strictly deserialize mapping to RwaResearchRecord with fail-closed validation."""
    if not isinstance(raw, Mapping):
        raise TypeError(f"Record must be a Mapping, got {type(raw).__name__}")

    _check_no_nan_inf(raw)
    _validate_keys(raw, _RECORD_ALLOWED_KEYS, "RwaResearchRecord")

    schema_id = raw.get("schema_id")
    if schema_id != _SCHEMA_ID:
        raise ValueError(f"Invalid schema_id: {schema_id!r}, expected {_SCHEMA_ID!r}")

    schema_version = raw.get("schema_version")
    if schema_version != _SCHEMA_VERSION:
        raise ValueError(f"Invalid schema_version: {schema_version!r}, expected {_SCHEMA_VERSION!r}")

    is_draft = raw.get("is_draft")
    if is_draft is not True:
        raise ValueError("is_draft must be True for draft research records")

    data_mode = raw.get("data_mode", "synthetic")
    if data_mode == "confirmed_execution":
        raise ValueError("data_mode cannot be 'confirmed_execution' in draft research evidence")

    # Instrument
    inst_raw = raw.get("instrument")
    if not isinstance(inst_raw, Mapping):
        raise TypeError("instrument must be a Mapping")
    _validate_keys(inst_raw, _INSTRUMENT_ALLOWED_KEYS, "InstrumentBinding")

    tk_raw = inst_raw.get("token_key")
    if not isinstance(tk_raw, Mapping):
        raise TypeError("instrument.token_key must be a Mapping")
    token_key = TokenKey(chain_id=tk_raw["chain_id"], address=tk_raw["address"])

    instrument = InstrumentBinding(
        token_key=token_key,
        issuer_id=inst_raw["issuer_id"],
        underlier_id=inst_raw["underlier_id"],
        feed_address=inst_raw["feed_address"],
        quote_currency=inst_raw["quote_currency"],
        token_decimals=inst_raw["token_decimals"],
        feed_decimals=inst_raw["feed_decimals"],
        evidence_refs=inst_raw.get("evidence_refs", ()),
        valid_from_ms=inst_raw.get("valid_from_ms", 0),
        valid_to_ms=inst_raw.get("valid_to_ms"),
    )

    # Oracle
    oracle = None
    if raw.get("oracle") is not None:
        ora_raw = raw["oracle"]
        if not isinstance(ora_raw, Mapping):
            raise TypeError("oracle must be a Mapping or None")
        _validate_keys(ora_raw, _ORACLE_ALLOWED_KEYS, "OracleObservation")

        sv = None
        if ora_raw.get("state_version") is not None:
            sv = _deserialize_state_version(ora_raw["state_version"])

        oracle = OracleObservation(
            round_id=_parse_big_int(ora_raw["round_id"], "round_id"),
            answer=_parse_big_int(ora_raw["answer"], "answer"),
            started_at_s=_parse_big_int(ora_raw["started_at_s"], "started_at_s"),
            updated_at_s=_parse_big_int(ora_raw["updated_at_s"], "updated_at_s"),
            answered_in_round=_parse_big_int(ora_raw["answered_in_round"], "answered_in_round"),
            multiplier_uint=_parse_big_int(ora_raw["multiplier_uint"], "multiplier_uint"),
            state_version=sv,
            pending_multiplier_uint=_parse_big_int(ora_raw["pending_multiplier_uint"], "pending_multiplier_uint") if ora_raw.get("pending_multiplier_uint") is not None else None,
            effective_at_s=_parse_big_int(ora_raw["effective_at_s"], "effective_at_s") if ora_raw.get("effective_at_s") is not None else None,
            oracle_paused=ora_raw.get("oracle_paused"),
            heartbeat_s=ora_raw.get("heartbeat_s"),
            session_kind=ora_raw.get("session_kind"),
            sequencer_status=ora_raw.get("sequencer_status"),
            sequencer_started_at_s=ora_raw.get("sequencer_started_at_s"),
            grace_period_s=ora_raw.get("grace_period_s"),
        )

    # EquityRef
    equity_ref = None
    if raw.get("equity_ref") is not None:
        eq_raw = raw["equity_ref"]
        if not isinstance(eq_raw, Mapping):
            raise TypeError("equity_ref must be a Mapping or None")
        _validate_keys(eq_raw, _EQUITY_REF_ALLOWED_KEYS, "EquityReference")

        equity_ref = EquityReference(
            underlier_id=eq_raw["underlier_id"],
            currency=eq_raw["currency"],
            bid_price=eq_raw["bid_price"],
            ask_price=eq_raw["ask_price"],
            price_basis=eq_raw.get("price_basis", "underlying_share"),
            generated_at_ms=eq_raw.get("generated_at_ms", 0),
            received_at_ms=eq_raw.get("received_at_ms", 0),
            session_kind=eq_raw.get("session_kind", "regular"),
            trading_halt=eq_raw.get("trading_halt"),
            source_ref=eq_raw.get("source_ref", "unknown"),
        )

    # Quotes
    quotes_list = []
    if raw.get("quotes") is not None:
        if not isinstance(raw["quotes"], Sequence):
            raise TypeError("quotes must be a Sequence")
        for idx, q_raw in enumerate(raw["quotes"]):
            if not isinstance(q_raw, Mapping):
                raise TypeError(f"quotes[{idx}] must be a Mapping")
            _validate_keys(q_raw, _QUOTE_POINT_ALLOWED_KEYS, f"AmountQuotePoint[{idx}]")

            pk = _deserialize_pool_key(q_raw["pool_key"])
            pd = None
            if q_raw.get("pool_descriptor") is not None:
                pd = _deserialize_pool_descriptor(q_raw["pool_descriptor"])
            ain = _deserialize_asset_ref(q_raw["asset_in"])
            aout = _deserialize_asset_ref(q_raw["asset_out"])

            quotes_list.append(
                AmountQuotePoint(
                    pool_key=pk,
                    direction=q_raw["direction"],
                    asset_in=ain,
                    asset_out=aout,
                    amount_in_atoms=_parse_big_int(q_raw["amount_in_atoms"], "amount_in_atoms"),
                    quote_id=q_raw["quote_id"],
                    pool_descriptor=pd,
                    amount_out_atoms=_parse_big_int(q_raw["amount_out_atoms"], "amount_out_atoms") if q_raw.get("amount_out_atoms") is not None else None,
                    state_version_ref=q_raw.get("state_version_ref"),
                    fee_included=q_raw.get("fee_included", "unknown"),
                    impact_included=q_raw.get("impact_included", "unknown"),
                    gas_estimate=q_raw.get("gas_estimate"),
                    gas_evidence_kind=q_raw.get("gas_evidence_kind"),
                    status=q_raw.get("status", "quoted"),
                    error=q_raw.get("error"),
                    data_mode=q_raw.get("data_mode", "synthetic"),
                    evidence_level=q_raw.get("evidence_level", "local_quote"),
                )
            )

    # Eligibility
    eligibility = None
    if raw.get("eligibility") is not None:
        el_raw = raw["eligibility"]
        if not isinstance(el_raw, Mapping):
            raise TypeError("eligibility must be a Mapping or None")
        _validate_keys(el_raw, _ELIGIBILITY_ALLOWED_KEYS, "EligibilityMatrix")

        el_tk_raw = el_raw.get("token_key")
        if not isinstance(el_tk_raw, Mapping):
            raise TypeError("eligibility.token_key must be a Mapping")
        el_tk = TokenKey(chain_id=el_tk_raw["chain_id"], address=el_tk_raw["address"])

        eligibility = EligibilityMatrix(
            token_key=el_tk,
            capabilities=el_raw.get("capabilities", {}),
            evidence_refs=el_raw.get("evidence_refs", ()),
        )

    return RwaResearchRecord(
        record_id=raw["record_id"],
        instrument=instrument,
        as_of_ms=raw["as_of_ms"],
        observed_at_ms=raw["observed_at_ms"],
        schema_id=schema_id,
        schema_version=schema_version,
        is_draft=is_draft,
        oracle=oracle,
        equity_ref=equity_ref,
        quotes=quotes_list,
        eligibility=eligibility,
        data_mode=data_mode,
        reasons=raw.get("reasons", ()),
    )


def to_canonical_json(record: RwaResearchRecord) -> str:
    """Encode RwaResearchRecord to Canonical JSON format with sorted keys and compact separators."""
    data = to_dict(record)
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_hash(record: RwaResearchRecord) -> str:
    """Calculate deterministic SHA-256 hash of canonical JSON representation."""
    canonical = to_canonical_json(record)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
