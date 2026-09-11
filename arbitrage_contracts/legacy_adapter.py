"""One-way adapter upgrading legacy arbitrage records to strongly typed contract records."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .eligibility import EvidenceLevel
from .identity import (
    Amount,
    AssetRef,
    FeeModel,
    PoolDescriptor,
    PoolKey,
    TokenKey,
    validate_bytes32,
    validate_evm_address,
)
from .quote import (
    ActorScope,
    DataMode,
    GasEvidence,
    GasEvidenceKind,
    HopDirection,
    HopRef,
    QuoteEvidence,
    QuoteStatus,
    RouteRef,
    TriState,
)
from .serialization import (
    ALLOWED_DATA_MODES,
    ContractRecord,
    validate_record,
)
from .state import StateVersion


@dataclass(frozen=True, slots=True)
class LegacyContext:
    """Explicit migration context provided by caller without implicit fallbacks."""

    run_id: str = "legacy-migration"
    data_mode: str = "synthetic"
    chain_id: int | None = None
    block_hash: str | None = None
    parent_hash: str | None = None
    block_timestamp_s: int | None = None
    completeness: str = "incomplete"
    source_ref: str | None = None
    provenance: Mapping[str, Any] | None = None
    decimals_map: Mapping[str, int] | None = None
    gas_evidence: GasEvidence | None = None
    reviewer: str | None = None
    evidence_level: EvidenceLevel | str | None = None

    def __post_init__(self) -> None:
        if self.data_mode not in ALLOWED_DATA_MODES:
            raise ValueError(f"Invalid data_mode in LegacyContext: {self.data_mode!r}")
        if type(self.run_id) is not str or not self.run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        if self.provenance is not None and not isinstance(self.provenance, Mapping):
            raise TypeError("provenance must be a Mapping")
        if self.decimals_map is not None and not isinstance(self.decimals_map, Mapping):
            raise TypeError("decimals_map must be a Mapping")


@dataclass(frozen=True, slots=True)
class AdaptationResult:
    """Outcome of adapting a legacy record to a standard ContractRecord."""

    success: bool
    status: str
    record: ContractRecord | None
    legacy_view: dict[str, Any]
    missing_fields: tuple[str, ...]
    unresolved_reasons: tuple[str, ...]
    source_hash: str

    @property
    def reasons(self) -> tuple[str, ...]:
        """Convenience alias for unresolved_reasons."""
        return self.unresolved_reasons


def _validate_primitive_types(obj: Any) -> None:
    if obj is None or isinstance(obj, (int, float, str, bool)):
        if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
            raise ValueError(f"Prohibited non-finite float value: {obj}")
        return
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            if not isinstance(key, str):
                raise TypeError(f"Mapping key must be str, got {type(key).__name__}")
            _validate_primitive_types(value)
        return
    if isinstance(obj, (list, tuple)):
        for item in obj:
            _validate_primitive_types(item)
        return
    raise TypeError(f"Prohibited non-primitive input type: {type(obj).__name__}")


def _compute_source_hash(raw: Mapping[str, Any]) -> str:
    try:
        canonical = json.dumps(
            dict(raw),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    except Exception:
        return hashlib.sha256(str(sorted(raw.items())).encode("utf-8")).hexdigest()


def _extract_decimals(
    token_addr: str,
    raw_decimals: Any,
    context: LegacyContext,
) -> int | None:
    if isinstance(raw_decimals, int) and not isinstance(raw_decimals, bool):
        if 0 <= raw_decimals <= 255:
            return raw_decimals
    if context.decimals_map is not None:
        norm_addr = token_addr.lower()
        for mapped_addr, decimals in context.decimals_map.items():
            if mapped_addr.lower() == norm_addr:
                if isinstance(decimals, int) and 0 <= decimals <= 255:
                    return decimals
    return None


def _adapt_state_version(
    raw: Mapping[str, Any],
    context: LegacyContext,
    provenance: dict[str, Any],
) -> tuple[bool, list[str], list[str], ContractRecord | None]:
    missing: list[str] = []
    reasons: list[str] = []

    chain_id = raw.get("chain_id") or context.chain_id
    if chain_id is None:
        missing.append("chain_id")

    block_number = raw.get("block_number")
    if block_number is None:
        missing.append("block_number")

    block_hash = raw.get("block_hash") or context.block_hash
    if not block_hash:
        missing.append("block_hash")
        reasons.append("Missing 32-byte block_hash proof; cannot verify state version")
    else:
        try:
            block_hash = validate_bytes32(block_hash)
        except (ValueError, TypeError) as exc:
            missing.append("block_hash")
            reasons.append(f"Invalid block_hash format: {exc}")

    block_domain = raw.get("block_domain", "l2")

    captured = raw.get("captured_at")
    captured_ms = int(captured * 1000) if isinstance(captured, (int, float)) else None
    block_ts = (
        raw.get("block_timestamp_s") or raw.get("block_timestamp") or context.block_timestamp_s
    )
    received_ms = (
        raw.get("received_at_ms")
        or captured_ms
        or (int(block_ts * 1000) if isinstance(block_ts, int) else None)
    )
    if received_ms is None:
        missing.append("received_at_ms")

    if block_ts is None:
        missing.append("block_timestamp_s")

    parent_hash = raw.get("parent_hash") or context.parent_hash
    if parent_hash:
        try:
            parent_hash = validate_bytes32(parent_hash)
        except (ValueError, TypeError) as exc:
            reasons.append(f"Invalid parent_hash format: {exc}")

    completeness = raw.get("completeness") or context.completeness
    complete_through = raw.get("complete_through_block")
    if completeness == "ready":
        if complete_through is None or (
            isinstance(block_number, int) and complete_through < block_number
        ):
            missing.append("complete_through_block")
            reasons.append(
                "StateVersion completeness cannot be 'ready' without complete_through_block >= block_number"
            )

    if missing or reasons:
        return False, missing, reasons, None

    assert chain_id is not None
    assert block_number is not None
    assert block_hash is not None
    assert received_ms is not None

    try:
        state_version = StateVersion(
            chain_id=chain_id,
            block_domain=block_domain,
            block_number=block_number,
            block_hash=block_hash,
            parent_hash=parent_hash,
            received_at_ms=received_ms,
            block_timestamp_s=block_ts,
            complete_through_block=complete_through,
            completeness=completeness,
            source_ref=raw.get("source_ref") or context.source_ref,
        )
        record = ContractRecord(
            schema_id="arbitrage-evidence",
            schema_version="1.0.0",
            record_type="state_version",
            run_id=context.run_id,
            data_mode=context.data_mode,
            provenance=provenance,
            payload=state_version,
        )
        return True, [], [], record
    except Exception as exc:
        reasons.append(f"StateVersion construction error: {exc}")
        return False, missing, reasons, None


def _adapt_token_key(
    raw: Mapping[str, Any],
    context: LegacyContext,
    provenance: dict[str, Any],
) -> tuple[bool, list[str], list[str], ContractRecord | None]:
    missing: list[str] = []
    reasons: list[str] = []

    chain_id = raw.get("chain_id") or context.chain_id
    if chain_id is None:
        missing.append("chain_id")

    address = raw.get("address")
    if not address:
        missing.append("address")
    else:
        try:
            address = validate_evm_address(address)
        except (ValueError, TypeError) as exc:
            missing.append("address")
            reasons.append(f"Invalid address format: {exc}")

    if missing or reasons:
        return False, missing, reasons, None

    assert chain_id is not None
    assert address is not None

    try:
        token_key = TokenKey(chain_id=chain_id, address=address)
        record = ContractRecord(
            schema_id="arbitrage-evidence",
            schema_version="1.0.0",
            record_type="token_key",
            run_id=context.run_id,
            data_mode=context.data_mode,
            provenance=provenance,
            payload=token_key,
        )
        return True, [], [], record
    except Exception as exc:
        reasons.append(f"TokenKey construction error: {exc}")
        return False, missing, reasons, None


def _adapt_pool_descriptor(
    raw: Mapping[str, Any],
    context: LegacyContext,
    provenance: dict[str, Any],
) -> tuple[bool, list[str], list[str], ContractRecord | None]:
    missing: list[str] = []
    reasons: list[str] = []

    chain_id = raw.get("chain_id") or context.chain_id
    if chain_id is None:
        missing.append("chain_id")

    pool_id = raw.get("pool_id")
    if not pool_id:
        missing.append("pool_id")

    protocol = raw.get("protocol") or raw.get("protocol_id")
    if not protocol:
        missing.append("protocol_id")

    factory = raw.get("factory") or raw.get("venue_address")
    if not factory:
        missing.append("factory")
        reasons.append("Missing pool manager/factory address; cannot construct PoolKey")
    else:
        try:
            factory = validate_evm_address(factory)
        except (ValueError, TypeError) as exc:
            missing.append("factory")
            reasons.append(f"Invalid factory address: {exc}")

    token0 = raw.get("token0")
    if not token0:
        missing.append("token0")
    else:
        try:
            token0 = validate_evm_address(token0)
        except (ValueError, TypeError) as exc:
            missing.append("token0")
            reasons.append(f"Invalid token0 address: {exc}")

    token1 = raw.get("token1")
    if not token1:
        missing.append("token1")
    else:
        try:
            token1 = validate_evm_address(token1)
        except (ValueError, TypeError) as exc:
            missing.append("token1")
            reasons.append(f"Invalid token1 address: {exc}")

    fee_bps = raw.get("fee_bps")
    if fee_bps is None:
        missing.append("fee_bps")

    tick_spacing = raw.get("tick_spacing")
    if tick_spacing is None:
        missing.append("tick_spacing")

    if missing or reasons:
        return False, missing, reasons, None

    assert chain_id is not None
    assert pool_id is not None
    assert protocol is not None
    assert factory is not None
    assert token0 is not None
    assert token1 is not None
    assert fee_bps is not None
    assert tick_spacing is not None

    try:
        pool_id_kind = "bytes32" if len(pool_id) == 66 else "address"
        key = PoolKey(
            chain_id=chain_id,
            protocol_id=protocol,
            venue_kind="factory",
            venue_address=factory,
            pool_id_kind=pool_id_kind,
            pool_id=pool_id,
        )
        currency0 = AssetRef.erc20(TokenKey(chain_id, token0))
        currency1 = AssetRef.erc20(TokenKey(chain_id, token1))
        fee_raw_hundredths = int(round(float(fee_bps) * 100))
        fee_model = FeeModel.static(raw_value=fee_raw_hundredths, unit="hundredths_of_bip")
        descriptor = PoolDescriptor(
            key=key,
            currency0=currency0,
            currency1=currency1,
            fee_model=fee_model,
            tick_spacing=int(tick_spacing),
        )
        record = ContractRecord(
            schema_id="arbitrage-evidence",
            schema_version="1.0.0",
            record_type="pool_descriptor",
            run_id=context.run_id,
            data_mode=context.data_mode,
            provenance=provenance,
            payload=descriptor,
        )
        return True, [], [], record
    except Exception as exc:
        reasons.append(f"PoolDescriptor construction error: {exc}")
        return False, missing, reasons, None


def _adapt_hop_ref(
    hop_dict: Mapping[str, Any],
    default_chain_id: int | None,
    context: LegacyContext,
) -> tuple[HopRef | None, list[str], list[str]]:
    missing: list[str] = []
    reasons: list[str] = []

    pool_raw = hop_dict.get("pool")
    if not isinstance(pool_raw, Mapping):
        missing.append("pool")
        reasons.append("Hop missing pool mapping")
        return None, missing, reasons

    chain_id = pool_raw.get("chain_id") or default_chain_id or context.chain_id
    pool_id = pool_raw.get("pool_id")
    protocol = pool_raw.get("protocol") or pool_raw.get("protocol_id")
    factory = pool_raw.get("factory") or pool_raw.get("venue_address")

    if not factory:
        missing.append("factory")
        reasons.append("Hop pool missing factory address")
        return None, missing, reasons

    if not chain_id or not pool_id or not protocol:
        missing.append("pool_identity")
        reasons.append("Hop pool missing chain_id, pool_id, or protocol")
        return None, missing, reasons

    token_in_addr = hop_dict.get("token_in")
    token_out_addr = hop_dict.get("token_out")
    if isinstance(token_in_addr, Mapping):
        token_in_addr = token_in_addr.get("address")
    if isinstance(token_out_addr, Mapping):
        token_out_addr = token_out_addr.get("address")

    if not token_in_addr or not token_out_addr:
        missing.extend(["token_in", "token_out"])
        reasons.append("Hop missing token_in or token_out address")
        return None, missing, reasons

    try:
        token_in_ref = AssetRef.erc20(TokenKey(chain_id, token_in_addr))
        token_out_ref = AssetRef.erc20(TokenKey(chain_id, token_out_addr))
        pool_id_kind = "bytes32" if len(pool_id) == 66 else "address"
        pool_key = PoolKey(
            chain_id=chain_id,
            protocol_id=protocol,
            venue_kind="factory",
            venue_address=factory,
            pool_id_kind=pool_id_kind,
            pool_id=pool_id,
        )
        direction_raw = hop_dict.get("direction", "zero_for_one")
        direction = (
            HopDirection.ONE_FOR_ZERO
            if str(direction_raw).lower() in ("one_for_zero", "1_for_0", "1")
            else HopDirection.ZERO_FOR_ONE
        )
        hop_ref = HopRef(
            pool_key=pool_key,
            asset_in=token_in_ref,
            asset_out=token_out_ref,
            direction=direction,
        )
        return hop_ref, [], []
    except Exception as exc:
        reasons.append(f"HopRef construction error: {exc}")
        return None, missing, reasons


def _adapt_route_ref(
    raw: Mapping[str, Any],
    context: LegacyContext,
    provenance: dict[str, Any],
) -> tuple[bool, list[str], list[str], ContractRecord | None]:
    missing: list[str] = []
    reasons: list[str] = []

    chain_id = raw.get("chain_id") or context.chain_id
    if chain_id is None:
        missing.append("chain_id")

    base_token = raw.get("base_token") or raw.get("base_asset")
    if isinstance(base_token, Mapping):
        base_token = base_token.get("address")
    if not base_token:
        missing.append("base_token")

    hops_raw = raw.get("hops")
    if not isinstance(hops_raw, Sequence) or not hops_raw:
        missing.append("hops")
        reasons.append("Route missing hops sequence")

    if missing or reasons:
        return False, missing, reasons, None

    assert isinstance(hops_raw, Sequence)
    parsed_hops: list[HopRef] = []
    for idx, hop_dict in enumerate(hops_raw):
        if not isinstance(hop_dict, Mapping):
            missing.append(f"hop_{idx}")
            reasons.append(f"Hop {idx} must be a mapping")
            continue
        hop_ref, hop_missing, hop_reasons = _adapt_hop_ref(hop_dict, chain_id, context)
        if hop_missing or hop_reasons or hop_ref is None:
            missing.extend(hop_missing)
            reasons.extend(hop_reasons)
        else:
            parsed_hops.append(hop_ref)

    if missing or reasons:
        return False, missing, reasons, None

    assert chain_id is not None
    assert base_token is not None

    try:
        base_ref = AssetRef.erc20(TokenKey(chain_id, base_token))
        route = RouteRef(chain_id=chain_id, base_asset=base_ref, hops=tuple(parsed_hops))
        record = ContractRecord(
            schema_id="arbitrage-evidence",
            schema_version="1.0.0",
            record_type="route_ref",
            run_id=context.run_id,
            data_mode=context.data_mode,
            provenance=provenance,
            payload=route,
        )
        return True, [], [], record
    except Exception as exc:
        reasons.append(f"RouteRef construction error: {exc}")
        return False, missing, reasons, None


def _adapt_quote_evidence(
    raw: Mapping[str, Any],
    context: LegacyContext,
    provenance: dict[str, Any],
    source_hash: str,
) -> tuple[bool, list[str], list[str], ContractRecord | None]:
    missing: list[str] = []
    reasons: list[str] = []

    quote_id = str(raw.get("quote_id") or f"legacy-quote-{source_hash[:12]}")

    status_raw = raw.get("status", "QUOTED")
    try:
        status = QuoteStatus(str(status_raw).lower())
    except ValueError:
        reasons.append(f"Unsupported quote status: {status_raw!r}")
        status = QuoteStatus.QUOTED

    route_raw = raw.get("route") or raw.get("route_ref")
    route_ref: RouteRef | None = None
    if isinstance(route_raw, Mapping):
        ok, r_missing, r_reasons, r_rec = _adapt_route_ref(route_raw, context, provenance)
        if ok and r_rec is not None:
            route_ref = r_rec.payload
        else:
            missing.extend(r_missing)
            reasons.extend(r_reasons)
    else:
        missing.append("route_ref")
        reasons.append("Missing route topology for quote evidence")

    amount_in_raw = raw.get("amount_in")
    amount_in: Amount | None = None
    if not amount_in_raw or route_ref is None:
        missing.append("amount_in")
    else:
        if isinstance(amount_in_raw, Mapping):
            atoms_val = amount_in_raw.get("atoms")
            decimals_val = amount_in_raw.get("decimals")
        else:
            atoms_val = amount_in_raw
            decimals_val = None

        base_token_addr = (
            route_ref.base_asset.token_key.address
            if route_ref.base_asset.token_key
            else (route_ref.base_asset.native_identifier or "")
        )
        decimals = _extract_decimals(
            base_token_addr,
            decimals_val,
            context,
        )
        if decimals is None:
            missing.append("amount_in_decimals")
            reasons.append("Missing decimals evidence for base asset amount_in")
        elif atoms_val is None:
            missing.append("amount_in_atoms")
        else:
            try:
                amount_in = Amount.from_atoms_str(route_ref.base_asset, str(atoms_val), decimals)
            except Exception as exc:
                reasons.append(f"Invalid amount_in: {exc}")

    amount_out_raw = raw.get("amount_out")
    amount_out: Amount | None = None
    if status == QuoteStatus.QUOTED:
        if amount_out_raw is None or route_ref is None:
            missing.append("amount_out")
            reasons.append("QUOTED status requires amount_out")
        else:
            if isinstance(amount_out_raw, Mapping):
                atoms_val = amount_out_raw.get("atoms")
                decimals_val = amount_out_raw.get("decimals")
            else:
                atoms_val = amount_out_raw
                decimals_val = None

            base_token_addr = (
                route_ref.base_asset.token_key.address
                if route_ref.base_asset.token_key
                else (route_ref.base_asset.native_identifier or "")
            )
            decimals = _extract_decimals(
                base_token_addr,
                decimals_val,
                context,
            )
            if decimals is None:
                missing.append("amount_out_decimals")
                reasons.append("Missing decimals evidence for base asset amount_out")
            elif atoms_val is None:
                missing.append("amount_out_atoms")
            else:
                try:
                    amount_out = Amount.from_atoms_str(
                        route_ref.base_asset, str(atoms_val), decimals
                    )
                except Exception as exc:
                    reasons.append(f"Invalid amount_out: {exc}")
    else:
        if amount_out_raw is not None:
            reasons.append(f"Non-QUOTED status ({status}) strictly requires amount_out=None")

    gas_estimate = raw.get("gas_estimate")
    gas_evidence: GasEvidence | None = None
    if gas_estimate is not None:
        try:
            gas_evidence = GasEvidence(
                gas_kind=GasEvidenceKind.RPC_ESTIMATE,
                gas_units=int(gas_estimate),
            )
        except Exception as exc:
            reasons.append(f"Invalid gas_estimate: {exc}")
    elif context.gas_evidence is not None:
        gas_evidence = context.gas_evidence

    if missing or reasons or route_ref is None or amount_in is None:
        return False, missing, reasons, None

    try:
        data_mode = (
            context.data_mode
            if context.data_mode != "confirmed_chain_history"
            else DataMode.SYNTHETIC
        )
        evidence_level = (
            EvidenceLevel(str(context.evidence_level).lower())
            if context.evidence_level
            else EvidenceLevel.LOCAL_QUOTE
        )
        quote = QuoteEvidence(
            quote_id=quote_id,
            route_ref=route_ref,
            amount_in=amount_in,
            amount_out=amount_out,
            status=status,
            evidence_level=evidence_level,
            data_mode=data_mode,
            actor_scope=ActorScope.SYNTHETIC,
            fee_included=TriState.YES,
            impact_included=TriState.YES,
            gas_evidence=gas_evidence,
            state_version_ref=raw.get("state_version_ref"),
        )
        record = ContractRecord(
            schema_id="arbitrage-evidence",
            schema_version="1.0.0",
            record_type="quote_evidence",
            run_id=context.run_id,
            data_mode=data_mode,
            provenance=provenance,
            payload=quote,
        )
        return True, [], [], record
    except Exception as exc:
        reasons.append(f"QuoteEvidence construction error: {exc}")
        return False, missing, reasons, None


def adapt_legacy_record(
    raw: Mapping[str, Any],
    context: LegacyContext | None = None,
) -> AdaptationResult:
    """One-way adaptation of a legacy dictionary to a validated ContractRecord."""
    if not isinstance(raw, Mapping):
        raise TypeError(f"raw must be a Mapping[str, Any], got {type(raw).__name__}")

    _validate_primitive_types(raw)

    if context is None:
        context = LegacyContext()

    source_hash = _compute_source_hash(raw)
    legacy_view = dict(raw)

    raw_target: Mapping[str, Any]
    if "raw" in raw and isinstance(raw["raw"], Mapping):
        raw_target = raw["raw"]
        provenance = {
            "migrated_from": "legacy_fixture",
            "source_hash": source_hash,
            **{k: v for k, v in raw.items() if k != "raw"},
        }
    else:
        raw_target = raw
        provenance = {
            "migrated_from": "legacy_record",
            "source_hash": source_hash,
        }
    if context.provenance:
        provenance.update(context.provenance)

    if raw_target.get("schema_id") == "arbitrage-evidence":
        try:
            validated = validate_record(raw_target)
            return AdaptationResult(
                success=True,
                status="complete",
                record=validated,
                legacy_view=legacy_view,
                missing_fields=(),
                unresolved_reasons=(),
                source_hash=source_hash,
            )
        except Exception as exc:
            return AdaptationResult(
                success=False,
                status="rejected",
                record=None,
                legacy_view=legacy_view,
                missing_fields=(),
                unresolved_reasons=(f"ContractRecord validation failed: {exc}",),
                source_hash=source_hash,
            )

    rec_type = str(raw_target.get("record_type") or raw_target.get("type") or "").lower()

    if rec_type in ("state_version", "state", "snapshot") or (
        "block_number" in raw_target
        and (
            "block_hash" in raw_target
            or "pools" in raw_target
            or "sqrt_price_x96" in raw_target
            or "captured_at" in raw_target
        )
    ):
        ok, missing, reasons, record = _adapt_state_version(raw_target, context, provenance)
    elif rec_type in ("token_key", "token") or (
        "address" in raw_target
        and "pool_id" not in raw_target
        and ("symbol" in raw_target or "decimals" in raw_target)
    ):
        ok, missing, reasons, record = _adapt_token_key(raw_target, context, provenance)
    elif rec_type in ("pool_descriptor", "pool_key", "pool") or (
        "pool_id" in raw_target and ("token0" in raw_target or "token1" in raw_target)
    ):
        ok, missing, reasons, record = _adapt_pool_descriptor(raw_target, context, provenance)
    elif rec_type in ("route_ref", "candidate_route", "route") or (
        "hops" in raw_target and "base_token" in raw_target
    ):
        ok, missing, reasons, record = _adapt_route_ref(raw_target, context, provenance)
    elif rec_type in ("quote_evidence", "quote", "candidate_quote") or (
        "amount_in" in raw_target
        or "quote_id" in raw_target
        or (
            "status" in raw_target
            and ("amount_out" in raw_target or "delta_atoms" in raw_target or "hops" in raw_target)
        )
    ):
        ok, missing, reasons, record = _adapt_quote_evidence(
            raw_target, context, provenance, source_hash
        )
    else:
        missing = ["record_type"]
        reasons = [
            f"Cannot determine contract type from legacy fields: {sorted(raw_target.keys())}"
        ]
        ok = False
        record = None

    status = "complete" if ok else ("incomplete" if missing else "rejected")
    return AdaptationResult(
        success=ok,
        status=status,
        record=record,
        legacy_view=legacy_view,
        missing_fields=tuple(missing),
        unresolved_reasons=tuple(reasons),
        source_hash=source_hash,
    )
