"""Domain contracts for route topology, hop references, and quote evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from .eligibility import EvidenceLevel as EvidenceLevel
from .identity import (
    Amount,
    AssetRef,
    FeeModel,
    PoolDescriptor,
    PoolKey,
    validate_non_negative_integer,
    validate_positive_integer,
)


class HopLimitExceededError(ValueError):
    """Raised when the number of hops in a route exceeds the allowed limit."""


class HopDirection(StrEnum):
    """Direction of swap through a liquidity pool."""

    ZERO_FOR_ONE = "zero_for_one"
    ONE_FOR_ZERO = "one_for_zero"


class RouteKind(StrEnum):
    """Topological structure of the arbitrage route."""

    SAME_CHAIN_CYCLE = "same_chain_cycle"


class QuoteStatus(StrEnum):
    """Status of quote attempt."""

    QUOTED = "quoted"
    CONTRACT_REVERT = "contract_revert"
    RPC_ERROR = "rpc_error"
    NODE_LIMITATION = "node_limitation"
    INVALID_RESPONSE = "invalid_response"
    INCOMPLETE_STATE = "incomplete_state"
    STALE = "stale"
    UNSUPPORTED = "unsupported"
    INVALID_INPUT = "invalid_input"


class TriState(StrEnum):
    """Strict tri-state indicator for cost and impact inclusion."""

    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class DataMode(StrEnum):
    """Operating mode under which evidence was gathered."""

    SYNTHETIC = "synthetic"
    HISTORICAL_REPLAY = "historical_replay"
    LIVE_READONLY = "live_readonly"
    CONFIRMED_CHAIN_HISTORY = "confirmed_chain_history"


class ActorScope(StrEnum):
    """Attribution domain of executing actor."""

    OWN_AUTHORIZED = "own_authorized"
    THIRD_PARTY = "third_party"
    SYNTHETIC = "synthetic"
    UNKNOWN = "unknown"


class GasEvidenceKind(StrEnum):
    """Nature and precision of gas cost evidence."""

    QUOTER_ESTIMATE = "quoter_estimate"
    RPC_ESTIMATE = "rpc_estimate"
    ATOMIC_SIMULATION = "atomic_simulation"
    CONFIRMED_ACTUAL = "confirmed_actual"
    UNKNOWN = "unknown"


class EconomicStatus(StrEnum):
    """Economic classification of arbitrage yield."""

    EVALUATED = "evaluated"
    PROFITABLE = "profitable"
    UNPROFITABLE = "unprofitable"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class HopRef:
    """Directed hop within a multi-hop swap route."""

    pool_key: PoolKey
    asset_in: AssetRef
    asset_out: AssetRef
    direction: str = HopDirection.ZERO_FOR_ONE
    pool_descriptor: PoolDescriptor | None = None

    def __init__(
        self,
        pool_key: PoolKey,
        asset_in: AssetRef,
        asset_out: AssetRef,
        direction: str = HopDirection.ZERO_FOR_ONE,
        pool_descriptor: PoolDescriptor | None = None,
    ) -> None:
        if not isinstance(pool_key, PoolKey):
            raise TypeError(
                f"pool_key must be an instance of PoolKey, got {type(pool_key).__name__}"
            )
        if not isinstance(asset_in, AssetRef) or not isinstance(asset_out, AssetRef):
            raise TypeError("asset_in and asset_out must be instances of AssetRef")
        if asset_in == asset_out:
            raise ValueError(f"asset_in and asset_out must be distinct, got {asset_in}")
        if asset_in.chain_id != pool_key.chain_id or asset_out.chain_id != pool_key.chain_id:
            raise ValueError("asset_in, asset_out, and pool_key must all reside on the same chain")
        if direction not in ("zero_for_one", "one_for_zero"):
            raise ValueError(
                f"direction must be 'zero_for_one' or 'one_for_zero', got {direction!r}"
            )
        if pool_descriptor is not None:
            if not isinstance(pool_descriptor, PoolDescriptor):
                raise TypeError("pool_descriptor must be an instance of PoolDescriptor or None")
            if pool_descriptor.key != pool_key:
                raise ValueError(
                    f"pool_descriptor key {pool_descriptor.key} does not match hop pool_key {pool_key}"
                )
            if direction == "zero_for_one":
                if asset_in != pool_descriptor.currency0 or asset_out != pool_descriptor.currency1:
                    raise ValueError(
                        "Hop assets do not match pool currencies for zero_for_one direction"
                    )
            elif direction == "one_for_zero":
                if asset_in != pool_descriptor.currency1 or asset_out != pool_descriptor.currency0:
                    raise ValueError(
                        "Hop assets do not match pool currencies for one_for_zero direction"
                    )

        object.__setattr__(self, "pool_key", pool_key)
        object.__setattr__(self, "asset_in", asset_in)
        object.__setattr__(self, "asset_out", asset_out)
        object.__setattr__(self, "direction", direction)
        object.__setattr__(self, "pool_descriptor", pool_descriptor)


def compute_route_id(
    chain_id: int,
    base_asset: AssetRef,
    hops: Sequence[HopRef],
    route_kind: str = "same_chain_cycle",
) -> str:
    """Compute deterministic SHA-256 hash identifying a directed route topology."""
    payload = {
        "schema_id": "arbitrage-evidence",
        "route_kind": route_kind,
        "chain_id": chain_id,
        "base_asset": {
            "chain_id": base_asset.chain_id,
            "address": base_asset.token_key.address
            if base_asset.token_key
            else base_asset.native_identifier,
            "interface_kind": str(base_asset.interface_kind),
        },
        "hops": [
            {
                "pool_key": {
                    "chain_id": hop.pool_key.chain_id,
                    "protocol_id": hop.pool_key.protocol_id,
                    "venue_kind": str(hop.pool_key.venue_kind),
                    "venue_address": hop.pool_key.venue_address.lower(),
                    "pool_id_kind": str(hop.pool_key.pool_id_kind),
                    "pool_id": hop.pool_key.pool_id.lower(),
                },
                "asset_in": {
                    "chain_id": hop.asset_in.chain_id,
                    "address": hop.asset_in.token_key.address
                    if hop.asset_in.token_key
                    else hop.asset_in.native_identifier,
                    "interface_kind": str(hop.asset_in.interface_kind),
                },
                "asset_out": {
                    "chain_id": hop.asset_out.chain_id,
                    "address": hop.asset_out.token_key.address
                    if hop.asset_out.token_key
                    else hop.asset_out.native_identifier,
                    "interface_kind": str(hop.asset_out.interface_kind),
                },
                "direction": str(hop.direction),
            }
            for hop in hops
        ],
    }
    canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RouteRef:
    """Bounded closed-cycle arbitrage route with deterministic identity."""

    chain_id: int
    base_asset: AssetRef
    hops: tuple[HopRef, ...]
    route_kind: str = "same_chain_cycle"
    max_hops: int = 4
    route_id: str = field(init=False)

    def __init__(
        self,
        chain_id: int,
        base_asset: AssetRef,
        hops: Sequence[HopRef],
        route_kind: str = "same_chain_cycle",
        max_hops: int | None = 4,
        route_id: str | None = None,
    ) -> None:
        validated_chain_id = validate_positive_integer(chain_id, "chain_id")
        if not isinstance(base_asset, AssetRef):
            raise TypeError(f"base_asset must be an AssetRef, got {type(base_asset).__name__}")
        if base_asset.chain_id != validated_chain_id:
            raise ValueError(
                f"base_asset chain_id ({base_asset.chain_id}) does not match route chain_id ({validated_chain_id})"
            )
        if route_kind != "same_chain_cycle":
            raise ValueError(f"route_kind must be 'same_chain_cycle', got {route_kind!r}")

        hops_tuple = tuple(hops)
        if len(hops_tuple) < 2:
            raise ValueError(f"Route cycle requires at least 2 hops, got {len(hops_tuple)}")

        validated_max_hops = (
            4 if max_hops is None else validate_positive_integer(max_hops, "max_hops")
        )
        if len(hops_tuple) > validated_max_hops:
            raise HopLimitExceededError(
                f"Hop count {len(hops_tuple)} exceeds maximum allowed hops {validated_max_hops}"
            )

        for i, hop in enumerate(hops_tuple):
            if not isinstance(hop, HopRef):
                raise TypeError(f"hop at index {i} must be an instance of HopRef")
            if hop.pool_key.chain_id != validated_chain_id:
                raise ValueError(
                    f"Cross-chain hop rejected: hop {i} pool on chain {hop.pool_key.chain_id} != {validated_chain_id}"
                )
            if (
                hop.asset_in.chain_id != validated_chain_id
                or hop.asset_out.chain_id != validated_chain_id
            ):
                raise ValueError(
                    f"Cross-chain asset rejected: hop {i} asset on chain != {validated_chain_id}"
                )

        if hops_tuple[0].asset_in != base_asset:
            raise ValueError(
                f"Route starting asset {hops_tuple[0].asset_in} does not match base_asset {base_asset}"
            )
        if hops_tuple[-1].asset_out != base_asset:
            raise ValueError(
                f"Route terminal asset {hops_tuple[-1].asset_out} does not match base_asset {base_asset} (cycle not closed)"
            )

        for i in range(len(hops_tuple) - 1):
            if hops_tuple[i].asset_out != hops_tuple[i + 1].asset_in:
                raise ValueError(
                    f"Direction/continuity error: hop {i} asset_out ({hops_tuple[i].asset_out}) "
                    f"does not match hop {i + 1} asset_in ({hops_tuple[i + 1].asset_in})"
                )

        seen_pools = set()
        for i, hop in enumerate(hops_tuple):
            if hop.pool_key in seen_pools:
                raise ValueError(f"Duplicate pool {hop.pool_key} detected in route at hop {i}")
            seen_pools.add(hop.pool_key)

        intermediate_tokens = [hops_tuple[i].asset_out for i in range(len(hops_tuple) - 1)]
        if len(intermediate_tokens) != len(set(intermediate_tokens)):
            raise ValueError("Duplicate intermediate token detected in route cycle")
        if base_asset in intermediate_tokens:
            raise ValueError(
                "Base asset cannot appear as an intermediate token (early cycle closure)"
            )

        computed_id = compute_route_id(validated_chain_id, base_asset, hops_tuple, route_kind)
        if route_id is not None and route_id != computed_id:
            raise ValueError(
                f"Provided route_id {route_id} does not match computed deterministic ID {computed_id}"
            )

        object.__setattr__(self, "chain_id", validated_chain_id)
        object.__setattr__(self, "base_asset", base_asset)
        object.__setattr__(self, "hops", hops_tuple)
        object.__setattr__(self, "route_kind", route_kind)
        object.__setattr__(self, "max_hops", validated_max_hops)
        object.__setattr__(self, "route_id", computed_id)


@dataclass(frozen=True, slots=True)
class HopQuote:
    """Individual hop quote result within a multi-hop execution attempt."""

    hop_index: int
    pool_key: PoolKey
    asset_in: AssetRef
    asset_out: AssetRef
    amount_in: Amount
    amount_out: Amount | None = None
    status: str = QuoteStatus.QUOTED
    fee_model: FeeModel | None = None
    gas_estimate: int | None = None
    latency_ns: int | None = None
    error: str | None = None

    def __init__(
        self,
        hop_index: int,
        pool_key: PoolKey,
        asset_in: AssetRef,
        asset_out: AssetRef,
        amount_in: Amount,
        amount_out: Amount | None = None,
        status: str = QuoteStatus.QUOTED,
        fee_model: FeeModel | None = None,
        gas_estimate: int | None = None,
        latency_ns: int | None = None,
        error: str | None = None,
    ) -> None:
        val_idx = validate_non_negative_integer(hop_index, "hop_index")
        if not isinstance(pool_key, PoolKey):
            raise TypeError("pool_key must be an instance of PoolKey")
        if not isinstance(asset_in, AssetRef) or not isinstance(asset_out, AssetRef):
            raise TypeError("asset_in and asset_out must be AssetRef")
        if not isinstance(amount_in, Amount):
            raise TypeError("amount_in must be Amount")
        if amount_in.asset_ref != asset_in:
            raise ValueError("amount_in asset_ref does not match asset_in")

        if status != QuoteStatus.QUOTED:
            if amount_out is not None:
                raise ValueError(
                    f"amount_out must be None when status != QUOTED (got status={status!r})"
                )
        else:
            if amount_out is None:
                raise ValueError("amount_out must not be None when status == QUOTED")
            if not isinstance(amount_out, Amount):
                raise TypeError("amount_out must be an Amount")
            if amount_out.asset_ref != asset_out:
                raise ValueError("amount_out asset_ref does not match asset_out")

        val_gas = (
            None
            if gas_estimate is None
            else validate_non_negative_integer(gas_estimate, "gas_estimate")
        )
        val_lat = (
            None if latency_ns is None else validate_non_negative_integer(latency_ns, "latency_ns")
        )

        object.__setattr__(self, "hop_index", val_idx)
        object.__setattr__(self, "pool_key", pool_key)
        object.__setattr__(self, "asset_in", asset_in)
        object.__setattr__(self, "asset_out", asset_out)
        object.__setattr__(self, "amount_in", amount_in)
        object.__setattr__(self, "amount_out", amount_out)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "fee_model", fee_model)
        object.__setattr__(self, "gas_estimate", val_gas)
        object.__setattr__(self, "latency_ns", val_lat)
        object.__setattr__(self, "error", error)


@dataclass(frozen=True, slots=True)
class GasEvidence:
    """Gas consumption and cost evidence for transaction execution."""

    gas_kind: str
    payer_asset: AssetRef | None = None
    gas_units: int | None = None
    gas_price_atoms: int | None = None
    l1_fee_atoms: int | None = None
    payer_subject: str | None = None
    source_refs: tuple[str, ...] = ()
    already_included_components: tuple[str, ...] = ()
    quoted_at_ms: int | None = None
    valid_until_ms: int | None = None
    block_ref: str | None = None
    is_applicable: bool = True
    evidence_ref: str | None = None

    def __init__(
        self,
        gas_kind: str,
        payer_asset: AssetRef | None = None,
        gas_units: int | None = None,
        gas_price_atoms: int | None = None,
        l1_fee_atoms: int | None = None,
        payer_subject: str | None = None,
        source_refs: Sequence[str] = (),
        already_included_components: Sequence[str] = (),
        quoted_at_ms: int | None = None,
        valid_until_ms: int | None = None,
        block_ref: str | None = None,
        is_applicable: bool = True,
        evidence_ref: str | None = None,
    ) -> None:
        if gas_kind not in (
            "quoter_estimate",
            "rpc_estimate",
            "atomic_simulation",
            "confirmed_actual",
            "unknown",
        ):
            raise ValueError(f"Invalid gas_kind: {gas_kind!r}")
        if payer_asset is not None and not isinstance(payer_asset, AssetRef):
            raise TypeError("payer_asset must be an AssetRef or None")
        val_units = (
            None if gas_units is None else validate_non_negative_integer(gas_units, "gas_units")
        )
        val_price = (
            None
            if gas_price_atoms is None
            else validate_non_negative_integer(gas_price_atoms, "gas_price_atoms")
        )
        val_l1 = (
            None
            if l1_fee_atoms is None
            else validate_non_negative_integer(l1_fee_atoms, "l1_fee_atoms")
        )
        val_quoted = (
            None
            if quoted_at_ms is None
            else validate_non_negative_integer(quoted_at_ms, "quoted_at_ms")
        )
        val_until = (
            None
            if valid_until_ms is None
            else validate_non_negative_integer(valid_until_ms, "valid_until_ms")
        )

        object.__setattr__(self, "gas_kind", gas_kind)
        object.__setattr__(self, "payer_asset", payer_asset)
        object.__setattr__(self, "gas_units", val_units)
        object.__setattr__(self, "gas_price_atoms", val_price)
        object.__setattr__(self, "l1_fee_atoms", val_l1)
        object.__setattr__(self, "payer_subject", payer_subject)
        object.__setattr__(self, "source_refs", tuple(source_refs))
        object.__setattr__(self, "already_included_components", tuple(already_included_components))
        object.__setattr__(self, "quoted_at_ms", val_quoted)
        object.__setattr__(self, "valid_until_ms", val_until)
        object.__setattr__(self, "block_ref", block_ref)
        object.__setattr__(self, "is_applicable", is_applicable)
        object.__setattr__(self, "evidence_ref", evidence_ref)


@dataclass(frozen=True, slots=True)
class PriceEvidence:
    """External valuation or reference pricing evidence."""

    base_asset: AssetRef
    quote_asset: AssetRef
    price_source_kind: str = "oracle_reference"
    price_type: str = "reference"
    price_numerator: int | None = None
    price_denominator: int | None = None
    timestamp_ms: int | None = None
    valid_until_ms: int | None = None
    market_status: str = "active"
    source_refs: tuple[str, ...] = ()
    evidence_ref: str | None = None

    def __init__(
        self,
        base_asset: AssetRef,
        quote_asset: AssetRef,
        price_source_kind: str = "oracle_reference",
        price_type: str = "reference",
        price_numerator: int | None = None,
        price_denominator: int | None = None,
        timestamp_ms: int | None = None,
        valid_until_ms: int | None = None,
        market_status: str = "active",
        source_refs: Sequence[str] = (),
        evidence_ref: str | None = None,
    ) -> None:
        if not isinstance(base_asset, AssetRef) or not isinstance(quote_asset, AssetRef):
            raise TypeError("base_asset and quote_asset must be AssetRef")
        if base_asset == quote_asset:
            raise ValueError("base_asset and quote_asset must be distinct")
        if price_type not in ("reference", "bid", "ask", "actual_fill"):
            raise ValueError(f"Invalid price_type: {price_type!r}")
        if market_status not in ("active", "paused", "closed", "unknown"):
            raise ValueError(f"Invalid market_status: {market_status!r}")

        val_num = (
            None
            if price_numerator is None
            else validate_non_negative_integer(price_numerator, "price_numerator")
        )
        val_den = (
            None
            if price_denominator is None
            else validate_positive_integer(price_denominator, "price_denominator")
        )
        val_ts = (
            None
            if timestamp_ms is None
            else validate_non_negative_integer(timestamp_ms, "timestamp_ms")
        )
        val_until = (
            None
            if valid_until_ms is None
            else validate_non_negative_integer(valid_until_ms, "valid_until_ms")
        )

        object.__setattr__(self, "base_asset", base_asset)
        object.__setattr__(self, "quote_asset", quote_asset)
        object.__setattr__(self, "price_source_kind", price_source_kind)
        object.__setattr__(self, "price_type", price_type)
        object.__setattr__(self, "price_numerator", val_num)
        object.__setattr__(self, "price_denominator", val_den)
        object.__setattr__(self, "timestamp_ms", val_ts)
        object.__setattr__(self, "valid_until_ms", val_until)
        object.__setattr__(self, "market_status", market_status)
        object.__setattr__(self, "source_refs", tuple(source_refs))
        object.__setattr__(self, "evidence_ref", evidence_ref)


@dataclass(frozen=True, slots=True)
class FeeComponent:
    """Discrete cost element accounted against route proceeds."""

    component_id: str
    amount_atoms: int
    asset: AssetRef
    deduction_stage: str = "pool_fee"
    evidence_ref: str | None = None
    is_estimated: bool = True

    def __init__(
        self,
        component_id: str,
        amount_atoms: int,
        asset: AssetRef,
        deduction_stage: str = "pool_fee",
        evidence_ref: str | None = None,
        is_estimated: bool = True,
    ) -> None:
        if type(component_id) is not str or not component_id.strip():
            raise ValueError("component_id must be a non-empty string")
        val_atoms = validate_non_negative_integer(amount_atoms, "amount_atoms")
        if not isinstance(asset, AssetRef):
            raise TypeError("asset must be an AssetRef")

        object.__setattr__(self, "component_id", component_id.strip())
        object.__setattr__(self, "amount_atoms", val_atoms)
        object.__setattr__(self, "asset", asset)
        object.__setattr__(self, "deduction_stage", deduction_stage)
        object.__setattr__(self, "evidence_ref", evidence_ref)
        object.__setattr__(self, "is_estimated", bool(is_estimated))


@dataclass(frozen=True, slots=True)
class EconomicAssessment:
    """Precalculated net yield payload provided by downstream calculation engines."""

    net_atoms: int | None = None
    net_usd_micros: int | None = None
    economic_status: str = "unknown"
    fee_components: tuple[FeeComponent, ...] = ()
    gas_cost_atoms: int | None = None
    calculation_refs: tuple[str, ...] = ()
    is_estimated: bool = True

    def __init__(
        self,
        net_atoms: int | None = None,
        net_usd_micros: int | None = None,
        economic_status: str = "unknown",
        fee_components: Sequence[FeeComponent] = (),
        gas_cost_atoms: int | None = None,
        calculation_refs: Sequence[str] = (),
        is_estimated: bool = True,
    ) -> None:
        if economic_status not in ("evaluated", "profitable", "unprofitable", "unknown"):
            raise ValueError(f"Invalid economic_status: {economic_status!r}")

        component_ids: list[str] = []
        for comp in fee_components:
            if not isinstance(comp, FeeComponent):
                raise TypeError("All items in fee_components must be FeeComponent")
            if comp.component_id in component_ids:
                raise ValueError(f"Duplicate fee component ID detected: {comp.component_id!r}")
            component_ids.append(comp.component_id)

        val_net = None
        if net_atoms is not None:
            if type(net_atoms) is not int or isinstance(net_atoms, bool):
                raise TypeError("net_atoms must be an int or None")
            val_net = net_atoms

        val_usd = None
        if net_usd_micros is not None:
            if type(net_usd_micros) is not int or isinstance(net_usd_micros, bool):
                raise TypeError("net_usd_micros must be an int or None")
            val_usd = net_usd_micros

        val_gas = None
        if gas_cost_atoms is not None:
            val_gas = validate_non_negative_integer(gas_cost_atoms, "gas_cost_atoms")

        if val_net is None and economic_status != "unknown":
            raise ValueError(
                f"economic_status cannot be {economic_status!r} when net_atoms is None"
            )
        if val_net is not None and economic_status == "unknown":
            raise ValueError("net_atoms must be None when economic_status is 'unknown'")

        object.__setattr__(self, "net_atoms", val_net)
        object.__setattr__(self, "net_usd_micros", val_usd)
        object.__setattr__(self, "economic_status", economic_status)
        object.__setattr__(self, "fee_components", tuple(fee_components))
        object.__setattr__(self, "gas_cost_atoms", val_gas)
        object.__setattr__(self, "calculation_refs", tuple(calculation_refs))
        object.__setattr__(self, "is_estimated", bool(is_estimated))


def compute_quote_id(
    route_id: str,
    amount_in: Amount,
    started_at_ms: int,
    state_version_ref: str | None = None,
) -> str:
    """Compute deterministic quote identifier."""
    payload = {
        "schema_id": "arbitrage-evidence",
        "route_id": route_id,
        "amount_in_atoms": str(amount_in.atoms),
        "started_at_ms": started_at_ms,
        "state_version_ref": state_version_ref,
    }
    canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class QuoteEvidence:
    """Rigid quote evidence container enforcing fail-closed invariant defenses."""

    quote_id: str
    route_ref: RouteRef
    amount_in: Amount
    amount_out: Amount | None = None
    delta_atoms: int | None = None
    hop_quotes: tuple[HopQuote, ...] = ()
    state_version_ref: str | None = None
    started_at_ms: int | None = None
    finished_at_ms: int | None = None
    latency_ns: int | None = None
    status: str = QuoteStatus.QUOTED
    evidence_level: str = EvidenceLevel.LOCAL_QUOTE
    data_mode: str = DataMode.SYNTHETIC
    actor_scope: str = ActorScope.SYNTHETIC
    fee_included: str = TriState.UNKNOWN
    impact_included: str = TriState.UNKNOWN
    gas_evidence: GasEvidence | None = None
    price_evidence: PriceEvidence | None = None
    economic_assessment: EconomicAssessment | None = None
    limitations: tuple[str, ...] = ()
    error: str | None = None
    source_refs: tuple[str, ...] = ()
    run_baseline_ref: str | None = None
    usable_at_observation: bool = True

    def __init__(
        self,
        quote_id: str,
        route_ref: RouteRef,
        amount_in: Amount,
        amount_out: Amount | None = None,
        delta_atoms: int | None = None,
        hop_quotes: Sequence[HopQuote] = (),
        state_version_ref: str | None = None,
        started_at_ms: int | None = None,
        finished_at_ms: int | None = None,
        latency_ns: int | None = None,
        status: str = QuoteStatus.QUOTED,
        evidence_level: str = EvidenceLevel.LOCAL_QUOTE,
        data_mode: str = DataMode.SYNTHETIC,
        actor_scope: str = ActorScope.SYNTHETIC,
        fee_included: str = TriState.UNKNOWN,
        impact_included: str = TriState.UNKNOWN,
        gas_evidence: GasEvidence | None = None,
        price_evidence: PriceEvidence | None = None,
        economic_assessment: EconomicAssessment | None = None,
        limitations: Sequence[str] = (),
        error: str | None = None,
        source_refs: Sequence[str] = (),
        run_baseline_ref: str | None = None,
        usable_at_observation: bool = True,
    ) -> None:
        if type(quote_id) is not str or not quote_id.strip():
            raise ValueError("quote_id must be a non-empty string")
        if not isinstance(route_ref, RouteRef):
            raise TypeError("route_ref must be an instance of RouteRef")
        if not isinstance(amount_in, Amount):
            raise TypeError("amount_in must be an instance of Amount")
        if amount_in.asset_ref != route_ref.base_asset:
            raise ValueError("amount_in asset_ref does not match route_ref base_asset")

        if status not in (
            QuoteStatus.QUOTED,
            QuoteStatus.CONTRACT_REVERT,
            QuoteStatus.RPC_ERROR,
            QuoteStatus.NODE_LIMITATION,
            QuoteStatus.INVALID_RESPONSE,
            QuoteStatus.INCOMPLETE_STATE,
            QuoteStatus.STALE,
            QuoteStatus.UNSUPPORTED,
            QuoteStatus.INVALID_INPUT,
        ):
            raise ValueError(f"Invalid status: {status!r}")

        val_amount_out: Amount | None = None
        val_delta_atoms: int | None = None

        if status != QuoteStatus.QUOTED:
            if amount_out is not None:
                raise ValueError(
                    f"amount_out must be None when status != QUOTED (got status={status!r})"
                )
            if delta_atoms is not None:
                raise ValueError(
                    f"delta_atoms must be None when status != QUOTED (got status={status!r})"
                )
            if economic_assessment is not None and economic_assessment.net_atoms is not None:
                raise ValueError(
                    f"economic_assessment.net_atoms must be None when status != QUOTED (got status={status!r})"
                )
        else:
            if amount_out is None:
                raise ValueError("amount_out must not be None when status == QUOTED")
            if not isinstance(amount_out, Amount):
                raise TypeError("amount_out must be an instance of Amount")
            if amount_out.asset_ref != route_ref.base_asset:
                raise ValueError("amount_out asset_ref does not match route_ref base_asset")
            expected_delta = amount_out.atoms - amount_in.atoms
            if delta_atoms is not None:
                if type(delta_atoms) is not int or isinstance(delta_atoms, bool):
                    raise TypeError("delta_atoms must be an integer or None")
                if delta_atoms != expected_delta:
                    raise ValueError(
                        f"delta_atoms ({delta_atoms}) does not match amount_out.atoms - amount_in.atoms ({expected_delta})"
                    )
                val_delta_atoms = delta_atoms
            else:
                val_delta_atoms = expected_delta
            val_amount_out = amount_out

        if economic_assessment is not None:
            if not isinstance(economic_assessment, EconomicAssessment):
                raise TypeError("economic_assessment must be an EconomicAssessment")
            if economic_assessment.net_atoms is not None:
                if gas_evidence is None or gas_evidence.gas_kind == "unknown":
                    raise ValueError(
                        "net_atoms cannot be determined when gas_evidence is missing or unknown"
                    )
                if fee_included == TriState.UNKNOWN or fee_included == "unknown":
                    raise ValueError("net_atoms cannot be determined when fee_included is unknown")
                if (
                    gas_evidence.gas_kind == "quoter_estimate"
                    and not economic_assessment.is_estimated
                ):
                    raise ValueError(
                        "Cannot claim confirmed/exact economic assessment when gas_evidence is quoter_estimate"
                    )

        if data_mode not in (
            "synthetic",
            "historical_replay",
            "live_readonly",
            "confirmed_chain_history",
        ):
            raise ValueError(f"Invalid data_mode: {data_mode!r}")
        if actor_scope not in ("own_authorized", "third_party", "synthetic", "unknown"):
            raise ValueError(f"Invalid actor_scope: {actor_scope!r}")
        if evidence_level not in (
            EvidenceLevel.SPOT_CANDIDATE,
            EvidenceLevel.LOCAL_QUOTE,
            EvidenceLevel.RPC_QUOTE,
            EvidenceLevel.ATOMIC_SIMULATION,
            EvidenceLevel.CONFIRMED_EXECUTION,
        ):
            raise ValueError(f"Invalid evidence_level: {evidence_level!r}")

        if data_mode == DataMode.SYNTHETIC and evidence_level == EvidenceLevel.CONFIRMED_EXECUTION:
            raise ValueError("Synthetic data mode cannot be promoted to CONFIRMED_EXECUTION")

        if (
            gas_evidence is not None
            and gas_evidence.gas_kind == "quoter_estimate"
            and evidence_level == EvidenceLevel.ATOMIC_SIMULATION
        ):
            raise ValueError("Hop-by-hop quoter estimate cannot be promoted to ATOMIC_SIMULATION")

        val_started = (
            None
            if started_at_ms is None
            else validate_non_negative_integer(started_at_ms, "started_at_ms")
        )
        val_finished = (
            None
            if finished_at_ms is None
            else validate_non_negative_integer(finished_at_ms, "finished_at_ms")
        )
        if val_started is not None and val_finished is not None and val_finished < val_started:
            raise ValueError(
                f"finished_at_ms ({val_finished}) cannot precede started_at_ms ({val_started})"
            )
        val_latency = (
            None if latency_ns is None else validate_non_negative_integer(latency_ns, "latency_ns")
        )

        object.__setattr__(self, "quote_id", quote_id.strip())
        object.__setattr__(self, "route_ref", route_ref)
        object.__setattr__(self, "amount_in", amount_in)
        object.__setattr__(self, "amount_out", val_amount_out)
        object.__setattr__(self, "delta_atoms", val_delta_atoms)
        object.__setattr__(self, "hop_quotes", tuple(hop_quotes))
        object.__setattr__(self, "state_version_ref", state_version_ref)
        object.__setattr__(self, "started_at_ms", val_started)
        object.__setattr__(self, "finished_at_ms", val_finished)
        object.__setattr__(self, "latency_ns", val_latency)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "evidence_level", evidence_level)
        object.__setattr__(self, "data_mode", data_mode)
        object.__setattr__(self, "actor_scope", actor_scope)
        object.__setattr__(self, "fee_included", fee_included)
        object.__setattr__(self, "impact_included", impact_included)
        object.__setattr__(self, "gas_evidence", gas_evidence)
        object.__setattr__(self, "price_evidence", price_evidence)
        object.__setattr__(self, "economic_assessment", economic_assessment)
        object.__setattr__(self, "limitations", tuple(limitations))
        object.__setattr__(self, "error", error)
        object.__setattr__(self, "source_refs", tuple(source_refs))
        object.__setattr__(self, "run_baseline_ref", run_baseline_ref)
        object.__setattr__(self, "usable_at_observation", bool(usable_at_observation))
