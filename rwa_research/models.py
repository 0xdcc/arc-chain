"""Private domain models for RWA research evidence, oracle observation, and discrete quotes."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from arbitrage_contracts import (
    AssetRef,
    PoolDescriptor,
    PoolKey,
    StateVersion,
    TokenKey,
    validate_evm_address,
)

UINT256_MAX = (1 << 256) - 1
_DECIMAL_REGEX = re.compile(r"^(0|[1-9]\d*)(\.\d+)?$")


def validate_identifier(value: str, field_name: str) -> str:
    """Validate non-empty stripped identifier string."""
    if type(value) is not str:
        raise TypeError(f"{field_name} must be a string, got {type(value).__name__}")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_name} must be a non-empty string")
    return stripped


def validate_decimal_string(value: str, field_name: str) -> str:
    """Validate decimal number string format (finite, non-negative, no NaN/Inf/exp)."""
    if type(value) is not str:
        raise TypeError(f"{field_name} must be a string, got {type(value).__name__}")
    stripped = value.strip()
    if not stripped or stripped != value:
        raise ValueError(f"Invalid decimal string format for {field_name}: {value!r}")
    if not _DECIMAL_REGEX.match(stripped):
        raise ValueError(f"Invalid decimal format for {field_name}: {value!r}")
    try:
        dec = Decimal(stripped)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid decimal value for {field_name}: {value!r}") from exc
    if not dec.is_finite():
        raise ValueError(f"{field_name} must be a finite decimal, got {value!r}")
    if dec < 0:
        raise ValueError(f"{field_name} cannot be negative, got {value!r}")
    return stripped


def validate_integer_field(
    value: int,
    field_name: str,
    *,
    allow_negative: bool = False,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int:
    """Validate integer value, rejecting boolean, float, and non-int types."""
    if type(value) is not int or isinstance(value, bool):
        raise TypeError(f"{field_name} must be an integer, got {type(value).__name__}")
    if not allow_negative and value < 0:
        raise ValueError(f"{field_name} must be non-negative, got {value}")
    if min_value is not None and value < min_value:
        raise ValueError(f"{field_name} must be >= {min_value}, got {value}")
    if max_value is not None and value > max_value:
        raise ValueError(f"{field_name} must be <= {max_value}, got {value}")
    return value


@dataclass(frozen=True, slots=True)
class InstrumentBinding:
    """Token binding metadata linking on-chain token to equity underlier and oracle feed."""

    token_key: TokenKey
    issuer_id: str
    underlier_id: str
    feed_address: str
    quote_currency: str
    token_decimals: int
    feed_decimals: int
    evidence_refs: tuple[str, ...]
    valid_from_ms: int
    valid_to_ms: int | None = None

    def __init__(
        self,
        token_key: TokenKey,
        issuer_id: str,
        underlier_id: str,
        feed_address: str,
        quote_currency: str,
        token_decimals: int,
        feed_decimals: int,
        evidence_refs: Sequence[str] = (),
        valid_from_ms: int = 0,
        valid_to_ms: int | None = None,
    ) -> None:
        if not isinstance(token_key, TokenKey):
            raise TypeError(f"token_key must be an instance of TokenKey, got {type(token_key).__name__}")
        val_issuer = validate_identifier(issuer_id, "issuer_id")
        val_underlier = validate_identifier(underlier_id, "underlier_id")
        val_feed_addr = validate_evm_address(feed_address)
        val_quote_curr = validate_identifier(quote_currency, "quote_currency")
        val_tok_dec = validate_integer_field(token_decimals, "token_decimals", min_value=0, max_value=255)
        val_feed_dec = validate_integer_field(feed_decimals, "feed_decimals", min_value=0, max_value=255)
        val_from_ms = validate_integer_field(valid_from_ms, "valid_from_ms")
        val_to_ms = None
        if valid_to_ms is not None:
            val_to_ms = validate_integer_field(valid_to_ms, "valid_to_ms")
            if val_to_ms < val_from_ms:
                raise ValueError(
                    f"valid_to_ms ({val_to_ms}) cannot be earlier than valid_from_ms ({val_from_ms})"
                )

        refs_tuple: tuple[str, ...] = tuple(validate_identifier(r, "evidence_ref") for r in evidence_refs)

        object.__setattr__(self, "token_key", token_key)
        object.__setattr__(self, "issuer_id", val_issuer)
        object.__setattr__(self, "underlier_id", val_underlier)
        object.__setattr__(self, "feed_address", val_feed_addr.lower())
        object.__setattr__(self, "quote_currency", val_quote_curr)
        object.__setattr__(self, "token_decimals", val_tok_dec)
        object.__setattr__(self, "feed_decimals", val_feed_dec)
        object.__setattr__(self, "evidence_refs", refs_tuple)
        object.__setattr__(self, "valid_from_ms", val_from_ms)
        object.__setattr__(self, "valid_to_ms", val_to_ms)


@dataclass(frozen=True, slots=True)
class OracleObservation:
    """On-chain oracle snapshot preserving raw round data, multipliers, and pause/freshness status."""

    state_version: StateVersion | None
    round_id: int
    answer: int
    started_at_s: int
    updated_at_s: int
    answered_in_round: int
    multiplier_uint: int
    pending_multiplier_uint: int | None = None
    effective_at_s: int | None = None
    oracle_paused: bool | None = None
    heartbeat_s: int | None = None
    session_kind: str | None = None
    sequencer_status: str | None = None
    sequencer_started_at_s: int | None = None
    grace_period_s: int | None = None

    def __init__(
        self,
        round_id: int,
        answer: int,
        started_at_s: int,
        updated_at_s: int,
        answered_in_round: int,
        multiplier_uint: int,
        state_version: StateVersion | None = None,
        pending_multiplier_uint: int | None = None,
        effective_at_s: int | None = None,
        oracle_paused: bool | None = None,
        heartbeat_s: int | None = None,
        session_kind: str | None = None,
        sequencer_status: str | None = None,
        sequencer_started_at_s: int | None = None,
        grace_period_s: int | None = None,
    ) -> None:
        if state_version is not None and not isinstance(state_version, StateVersion):
            raise TypeError("state_version must be an instance of StateVersion or None")
        val_round_id = validate_integer_field(round_id, "round_id")
        val_answer = validate_integer_field(answer, "answer", allow_negative=False)
        val_started_s = validate_integer_field(started_at_s, "started_at_s")
        val_updated_s = validate_integer_field(updated_at_s, "updated_at_s")
        val_answered_round = validate_integer_field(answered_in_round, "answered_in_round")
        val_multiplier = validate_integer_field(multiplier_uint, "multiplier_uint")
        if val_multiplier <= 0:
            raise ValueError(f"multiplier_uint must be strictly positive, got {val_multiplier}")

        val_pending_mult = None
        if pending_multiplier_uint is not None:
            val_pending_mult = validate_integer_field(pending_multiplier_uint, "pending_multiplier_uint")
            if val_pending_mult <= 0:
                raise ValueError(f"pending_multiplier_uint must be strictly positive, got {val_pending_mult}")

        val_eff_s = None
        if effective_at_s is not None:
            val_eff_s = validate_integer_field(effective_at_s, "effective_at_s")

        if oracle_paused is not None and not isinstance(oracle_paused, bool):
            raise TypeError("oracle_paused must be a boolean or None")

        val_heartbeat = None
        if heartbeat_s is not None:
            val_heartbeat = validate_integer_field(heartbeat_s, "heartbeat_s")
            if val_heartbeat <= 0:
                raise ValueError("heartbeat_s must be strictly positive")

        val_session = None
        if session_kind is not None:
            val_session = validate_identifier(session_kind, "session_kind")

        val_seq_status = None
        if sequencer_status is not None:
            val_seq_status = validate_identifier(sequencer_status, "sequencer_status")

        val_seq_started = None
        if sequencer_started_at_s is not None:
            val_seq_started = validate_integer_field(sequencer_started_at_s, "sequencer_started_at_s")

        val_grace = None
        if grace_period_s is not None:
            val_grace = validate_integer_field(grace_period_s, "grace_period_s")

        object.__setattr__(self, "state_version", state_version)
        object.__setattr__(self, "round_id", val_round_id)
        object.__setattr__(self, "answer", val_answer)
        object.__setattr__(self, "started_at_s", val_started_s)
        object.__setattr__(self, "updated_at_s", val_updated_s)
        object.__setattr__(self, "answered_in_round", val_answered_round)
        object.__setattr__(self, "multiplier_uint", val_multiplier)
        object.__setattr__(self, "pending_multiplier_uint", val_pending_mult)
        object.__setattr__(self, "effective_at_s", val_eff_s)
        object.__setattr__(self, "oracle_paused", oracle_paused)
        object.__setattr__(self, "heartbeat_s", val_heartbeat)
        object.__setattr__(self, "session_kind", val_session)
        object.__setattr__(self, "sequencer_status", val_seq_status)
        object.__setattr__(self, "sequencer_started_at_s", val_seq_started)
        object.__setattr__(self, "grace_period_s", val_grace)


@dataclass(frozen=True, slots=True)
class EquityReference:
    """Off-chain / equity reference price snapshot with session kind and trading halt flags."""

    underlier_id: str
    currency: str
    bid_price: str
    ask_price: str
    price_basis: str = "underlying_share"
    generated_at_ms: int = 0
    received_at_ms: int = 0
    session_kind: str = "regular"
    trading_halt: bool | None = None
    source_ref: str = "unknown"

    def __init__(
        self,
        underlier_id: str,
        currency: str,
        bid_price: str,
        ask_price: str,
        price_basis: str = "underlying_share",
        generated_at_ms: int = 0,
        received_at_ms: int = 0,
        session_kind: str = "regular",
        trading_halt: bool | None = None,
        source_ref: str = "unknown",
    ) -> None:
        val_underlier = validate_identifier(underlier_id, "underlier_id")
        val_currency = validate_identifier(currency, "currency")
        val_bid = validate_decimal_string(bid_price, "bid_price")
        val_ask = validate_decimal_string(ask_price, "ask_price")
        if price_basis != "underlying_share":
            raise ValueError(f"price_basis must be 'underlying_share', got {price_basis!r}")
        val_gen_ms = validate_integer_field(generated_at_ms, "generated_at_ms")
        val_rec_ms = validate_integer_field(received_at_ms, "received_at_ms")
        val_session = validate_identifier(session_kind, "session_kind")
        if trading_halt is not None and not isinstance(trading_halt, bool):
            raise TypeError("trading_halt must be a boolean or None")
        val_source = validate_identifier(source_ref, "source_ref")

        object.__setattr__(self, "underlier_id", val_underlier)
        object.__setattr__(self, "currency", val_currency)
        object.__setattr__(self, "bid_price", val_bid)
        object.__setattr__(self, "ask_price", val_ask)
        object.__setattr__(self, "price_basis", price_basis)
        object.__setattr__(self, "generated_at_ms", val_gen_ms)
        object.__setattr__(self, "received_at_ms", val_rec_ms)
        object.__setattr__(self, "session_kind", val_session)
        object.__setattr__(self, "trading_halt", trading_halt)
        object.__setattr__(self, "source_ref", val_source)


@dataclass(frozen=True, slots=True)
class AmountQuotePoint:
    """Discrete amount quote point evidence preserving liquidity curve slice."""

    pool_key: PoolKey
    pool_descriptor: PoolDescriptor | None
    direction: str
    asset_in: AssetRef
    asset_out: AssetRef
    amount_in_atoms: int
    amount_out_atoms: int | None
    quote_id: str
    state_version_ref: str | None = None
    fee_included: str = "unknown"
    impact_included: str = "unknown"
    gas_estimate: int | None = None
    gas_evidence_kind: str | None = None
    status: str = "quoted"
    error: str | None = None
    data_mode: str = "synthetic"
    evidence_level: str = "local_quote"

    def __init__(
        self,
        pool_key: PoolKey,
        direction: str,
        asset_in: AssetRef,
        asset_out: AssetRef,
        amount_in_atoms: int,
        quote_id: str,
        pool_descriptor: PoolDescriptor | None = None,
        amount_out_atoms: int | None = None,
        state_version_ref: str | None = None,
        fee_included: str = "unknown",
        impact_included: str = "unknown",
        gas_estimate: int | None = None,
        gas_evidence_kind: str | None = None,
        status: str = "quoted",
        error: str | None = None,
        data_mode: str = "synthetic",
        evidence_level: str = "local_quote",
    ) -> None:
        if not isinstance(pool_key, PoolKey):
            raise TypeError("pool_key must be an instance of PoolKey")
        if pool_descriptor is not None and not isinstance(pool_descriptor, PoolDescriptor):
            raise TypeError("pool_descriptor must be an instance of PoolDescriptor or None")
        val_dir = validate_identifier(direction, "direction")
        if not isinstance(asset_in, AssetRef):
            raise TypeError("asset_in must be an instance of AssetRef")
        if not isinstance(asset_out, AssetRef):
            raise TypeError("asset_out must be an instance of AssetRef")
        if asset_in == asset_out:
            raise ValueError("asset_in and asset_out must be distinct")
        val_in_atoms = validate_integer_field(amount_in_atoms, "amount_in_atoms")
        if val_in_atoms <= 0:
            raise ValueError("amount_in_atoms must be strictly positive")
        if val_in_atoms > UINT256_MAX:
            raise ValueError(f"amount_in_atoms exceeds uint256 bounds: {val_in_atoms}")

        val_out_atoms = None
        if amount_out_atoms is not None:
            val_out_atoms = validate_integer_field(amount_out_atoms, "amount_out_atoms")
            if val_out_atoms < 0 or val_out_atoms > UINT256_MAX:
                raise ValueError(f"amount_out_atoms out of uint256 bounds: {val_out_atoms}")

        val_quote_id = validate_identifier(quote_id, "quote_id")
        val_fee_inc = validate_identifier(fee_included, "fee_included")
        val_imp_inc = validate_identifier(impact_included, "impact_included")
        val_gas_est = None
        if gas_estimate is not None:
            val_gas_est = validate_integer_field(gas_estimate, "gas_estimate")
        val_status = validate_identifier(status, "status")
        if val_status != "quoted" and val_out_atoms is not None:
            raise ValueError(f"Quote with status {val_status!r} must have amount_out_atoms=None")
        val_data_mode = validate_identifier(data_mode, "data_mode")
        if val_data_mode == "confirmed_execution":
            raise ValueError("data_mode cannot be 'confirmed_execution' in draft research evidence")
        val_ev_level = validate_identifier(evidence_level, "evidence_level")

        object.__setattr__(self, "pool_key", pool_key)
        object.__setattr__(self, "pool_descriptor", pool_descriptor)
        object.__setattr__(self, "direction", val_dir)
        object.__setattr__(self, "asset_in", asset_in)
        object.__setattr__(self, "asset_out", asset_out)
        object.__setattr__(self, "amount_in_atoms", val_in_atoms)
        object.__setattr__(self, "amount_out_atoms", val_out_atoms)
        object.__setattr__(self, "quote_id", val_quote_id)
        object.__setattr__(self, "state_version_ref", state_version_ref)
        object.__setattr__(self, "fee_included", val_fee_inc)
        object.__setattr__(self, "impact_included", val_imp_inc)
        object.__setattr__(self, "gas_estimate", val_gas_est)
        object.__setattr__(self, "gas_evidence_kind", gas_evidence_kind)
        object.__setattr__(self, "status", val_status)
        object.__setattr__(self, "error", error)
        object.__setattr__(self, "data_mode", val_data_mode)
        object.__setattr__(self, "evidence_level", val_ev_level)


@dataclass(frozen=True, slots=True)
class EligibilityMatrix:
    """Multi-dimensional capability and eligibility matrix for token research."""

    token_key: TokenKey
    capabilities: dict[str, str]
    evidence_refs: tuple[str, ...] = ()

    def __init__(
        self,
        token_key: TokenKey,
        capabilities: Mapping[str, str],
        evidence_refs: Sequence[str] = (),
    ) -> None:
        if not isinstance(token_key, TokenKey):
            raise TypeError("token_key must be an instance of TokenKey")
        if not isinstance(capabilities, Mapping):
            raise TypeError("capabilities must be a Mapping")
        caps_dict: dict[str, str] = {}
        for k, v in capabilities.items():
            k_val = validate_identifier(k, "capability_key")
            v_val = validate_identifier(v, "capability_value")
            caps_dict[k_val] = v_val

        refs_tuple = tuple(validate_identifier(r, "evidence_ref") for r in evidence_refs)

        object.__setattr__(self, "token_key", token_key)
        object.__setattr__(self, "capabilities", caps_dict)
        object.__setattr__(self, "evidence_refs", refs_tuple)


@dataclass(frozen=True, slots=True)
class RwaResearchRecord:
    """Top-level unified RWA research observation record envelope."""

    record_id: str
    instrument: InstrumentBinding
    as_of_ms: int
    observed_at_ms: int
    schema_id: str = "w7-rwa-research"
    schema_version: str = "0.1.0-draft"
    is_draft: bool = True
    oracle: OracleObservation | None = None
    equity_ref: EquityReference | None = None
    quotes: tuple[AmountQuotePoint, ...] = ()
    eligibility: EligibilityMatrix | None = None
    data_mode: str = "synthetic"
    reasons: tuple[str, ...] = ()

    def __init__(
        self,
        record_id: str,
        instrument: InstrumentBinding,
        as_of_ms: int,
        observed_at_ms: int,
        schema_id: str = "w7-rwa-research",
        schema_version: str = "0.1.0-draft",
        is_draft: bool = True,
        oracle: OracleObservation | None = None,
        equity_ref: EquityReference | None = None,
        quotes: Sequence[AmountQuotePoint] = (),
        eligibility: EligibilityMatrix | None = None,
        data_mode: str = "synthetic",
        reasons: Sequence[str] = (),
    ) -> None:
        val_rec_id = validate_identifier(record_id, "record_id")
        if schema_id != "w7-rwa-research":
            raise ValueError(f"Invalid schema_id: {schema_id!r}, expected 'w7-rwa-research'")
        if schema_version != "0.1.0-draft":
            raise ValueError(f"Invalid schema_version: {schema_version!r}, expected '0.1.0-draft'")
        if is_draft is not True:
            raise ValueError("Draft research record must have is_draft=True")
        if not isinstance(instrument, InstrumentBinding):
            raise TypeError("instrument must be an instance of InstrumentBinding")
        if oracle is not None and not isinstance(oracle, OracleObservation):
            raise TypeError("oracle must be an instance of OracleObservation or None")
        if equity_ref is not None and not isinstance(equity_ref, EquityReference):
            raise TypeError("equity_ref must be an instance of EquityReference or None")
        quotes_tuple = tuple(quotes)
        for q in quotes_tuple:
            if not isinstance(q, AmountQuotePoint):
                raise TypeError("quotes elements must be instances of AmountQuotePoint")
        if eligibility is not None and not isinstance(eligibility, EligibilityMatrix):
            raise TypeError("eligibility must be an instance of EligibilityMatrix or None")
        val_as_of = validate_integer_field(as_of_ms, "as_of_ms")
        val_observed = validate_integer_field(observed_at_ms, "observed_at_ms")
        val_data_mode = validate_identifier(data_mode, "data_mode")
        if val_data_mode == "confirmed_execution":
            raise ValueError("data_mode cannot be 'confirmed_execution' in draft research record")
        reasons_tuple = tuple(validate_identifier(r, "reason") for r in reasons)

        object.__setattr__(self, "record_id", val_rec_id)
        object.__setattr__(self, "schema_id", schema_id)
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "is_draft", is_draft)
        object.__setattr__(self, "instrument", instrument)
        object.__setattr__(self, "oracle", oracle)
        object.__setattr__(self, "equity_ref", equity_ref)
        object.__setattr__(self, "quotes", quotes_tuple)
        object.__setattr__(self, "eligibility", eligibility)
        object.__setattr__(self, "as_of_ms", val_as_of)
        object.__setattr__(self, "observed_at_ms", val_observed)
        object.__setattr__(self, "data_mode", val_data_mode)
        object.__setattr__(self, "reasons", reasons_tuple)
