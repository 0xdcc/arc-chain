"""Private settled-cycle research records for W3.

The model deliberately validates every field at construction time so malformed
offline evidence cannot enter research calculations.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from arbitrage_contracts.identity import (
    AssetInterfaceKind,
    AssetRef,
    PoolIdKind,
    PoolKey,
    TokenKey,
    VenueKind,
    validate_bytes32,
    validate_evm_address,
    validate_non_negative_integer,
    validate_positive_integer,
)

SCHEMA_ID = "w3-settled-research"
SCHEMA_VERSION = "1.0.0"

_DECIMAL_STRING_REGEX = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$")
_SHA256_REGEX = re.compile(r"^[0-9a-f]{64}$")
_PROVENANCE_PATH_FIELDS = frozenset({"source_file", "path", "file_path"})


def _require_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    return value


def _require_str(value: Any, field_name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be a string, got {type(value).__name__}")
    return str(value)


def _require_nonempty_str(value: Any, field_name: str) -> str:
    res = _require_str(value, field_name)
    if not res:
        raise ValueError(f"{field_name} must be non-empty")
    return res


def _require_optional_int(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or isinstance(value, bool):
        raise TypeError(f"{field_name} must be an integer or None, got {type(value).__name__}")
    if abs(value) > (1 << 256) - 1:
        raise ValueError(f"{field_name} is outside uint256 bounds")
    return value


def _require_non_negative_int(value: Any, field_name: str) -> int:
    if value is None:
        raise TypeError(f"{field_name} must be an integer, got None")
    if type(value) is not int or isinstance(value, bool):
        raise TypeError(f"{field_name} must be an integer, got {type(value).__name__}")
    return validate_non_negative_integer(value, field_name)


def _enum_value[TEnum: StrEnum](enum_type: type[TEnum], value: Any, field_name: str) -> TEnum:
    if not isinstance(value, enum_type):
        try:
            return enum_type(value)
        except ValueError as error:
            raise ValueError(f"Invalid {field_name}: {value!r}") from error
    return value


def _sha256(value: Any, field_name: str) -> str:
    res = _require_str(value, field_name)
    if not _SHA256_REGEX.fullmatch(res):
        raise ValueError(f"{field_name} must be lowercase 64-character SHA-256")
    return res


def _token_key_from_dict(value: Any) -> TokenKey:
    data = _require_mapping(value, "token_key")
    chain_id = data.get("chain_id")
    if not isinstance(chain_id, int) or isinstance(chain_id, bool):
        raise TypeError("token_key.chain_id must be an integer")
    raw_address = data.get("address")
    if not isinstance(raw_address, str):
        raise TypeError("token_key.address must be a string")
    return TokenKey(
        validate_positive_integer(chain_id, "token_key.chain_id"),
        validate_evm_address(raw_address),
    )


def _asset_ref_from_dict(value: Any) -> AssetRef:
    data = _require_mapping(value, "asset")
    raw_chain_id = data.get("chain_id")
    if not isinstance(raw_chain_id, int) or isinstance(raw_chain_id, bool):
        raise TypeError("asset.chain_id must be an integer")
    chain_id = validate_positive_integer(raw_chain_id, "asset.chain_id")
    interface_kind = _enum_value(AssetInterfaceKind, data.get("interface_kind"), "asset.interface_kind")
    token_key = None if data.get("token_key") is None else _token_key_from_dict(data["token_key"])
    native_identifier = data.get("native_identifier")
    balance_domain_id = data.get("balance_domain_id")
    if interface_kind is AssetInterfaceKind.ERC20:
        return AssetRef(interface_kind="erc20", chain_id=chain_id, token_key=token_key)
    if type(native_identifier) is not str or not native_identifier:
        raise ValueError("Native AssetRef must provide native_identifier")
    if balance_domain_id is not None:
        balance_domain_id = _require_nonempty_str(balance_domain_id, "asset.balance_domain_id")
    return AssetRef(
        interface_kind="native",
        chain_id=chain_id,
        native_identifier=native_identifier,
        balance_domain_id=balance_domain_id,
    )


def _pool_key_from_dict(value: Any) -> PoolKey:
    data = _require_mapping(value, "pool_key")
    raw_chain_id = data.get("chain_id")
    if not isinstance(raw_chain_id, int) or isinstance(raw_chain_id, bool):
        raise TypeError("pool_key.chain_id must be an integer")
    chain_id = validate_positive_integer(raw_chain_id, "pool_key.chain_id")
    protocol_id = _require_nonempty_str(data.get("protocol_id"), "pool_key.protocol_id")
    venue_kind = _enum_value(VenueKind, data.get("venue_kind"), "pool_key.venue_kind")
    raw_venue_address = data.get("venue_address")
    if not isinstance(raw_venue_address, str):
        raise TypeError("pool_key.venue_address must be a string")
    venue_address = validate_evm_address(raw_venue_address)
    pool_id_kind = _enum_value(PoolIdKind, data.get("pool_id_kind"), "pool_key.pool_id_kind")
    raw_pool_id = data.get("pool_id")
    if not isinstance(raw_pool_id, str):
        raise TypeError("pool_key.pool_id must be a string")
    pool_id = (
        validate_evm_address(raw_pool_id)
        if pool_id_kind is PoolIdKind.ADDRESS
        else validate_bytes32(raw_pool_id)
    )
    return PoolKey(
        chain_id,
        protocol_id,
        venue_kind,
        venue_address,
        pool_id_kind,
        pool_id,
    )


class ActionKind(StrEnum):
    """Action types retained in the private research record."""

    SWAP = "swap"
    TRANSFER = "transfer"
    WRAP = "wrap"
    UNWRAP = "unwrap"
    BORROW = "borrow"
    REPAY = "repay"
    MINT = "mint"
    BURN = "burn"
    LP = "lp"
    UNKNOWN = "unknown"


class AttributionStatus(StrEnum):
    """Attribution confidence states; unknown-trace states are fail-closed."""

    FULLY_ATTRIBUTED = "fully_attributed"
    PARTIALLY_ATTRIBUTED = "partially_attributed"
    UNVERIFIED_MISSING_TRACE = "unverified_missing_trace"
    AMBIGUOUS_COMPLEX_TX = "ambiguous_complex_tx"
    INCONSISTENT = "inconsistent"


class EconomicStatus(StrEnum):
    """Cycle economic result state."""

    POSITIVE = "positive"
    NONPOSITIVE = "nonpositive"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class InclusionStatus(StrEnum):
    """Transaction inclusion state."""

    CONFIRMED = "confirmed"
    REORGED = "reorged"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TransactionSubjects:
    """Explicit three-way separation of transaction participants."""

    tx_origin: str | None
    executor_contract: str | None
    beneficiary: str | None
    gas_payer: str | None
    economic_bearer: str | None

    def __post_init__(self) -> None:
        for field_name in (
            "tx_origin",
            "executor_contract",
            "beneficiary",
            "gas_payer",
            "economic_bearer",
        ):
            value = getattr(self, field_name)
            if value is None:
                continue
            if type(value) is not str:
                raise TypeError(f"{field_name} must be an address string or None")
            object.__setattr__(self, field_name, validate_evm_address(value))

    def to_dict(self) -> dict[str, str | None]:
        """Return a JSON-compatible mapping."""
        return {
            "tx_origin": self.tx_origin,
            "executor_contract": self.executor_contract,
            "beneficiary": self.beneficiary,
            "gas_payer": self.gas_payer,
            "economic_bearer": self.economic_bearer,
        }

    @classmethod
    def from_dict(cls, value: Any) -> TransactionSubjects:
        """Construct from validated mapping data."""
        data = _require_mapping(value, "subjects")
        required = {
            "tx_origin",
            "executor_contract",
            "beneficiary",
            "gas_payer",
            "economic_bearer",
        }
        if set(data) != required:
            raise ValueError("subjects keys do not match the frozen schema")
        return cls(
            tx_origin=data["tx_origin"],
            executor_contract=data["executor_contract"],
            beneficiary=data["beneficiary"],
            gas_payer=data["gas_payer"],
            economic_bearer=data["economic_bearer"],
        )


@dataclass(frozen=True, slots=True)
class CycleAction:
    """One validated step in the transaction action graph."""

    step_id: int
    action_kind: ActionKind
    pool_key: PoolKey | None
    asset_in: AssetRef | None
    asset_out: AssetRef | None
    amount_in_atoms: int | None
    amount_out_atoms: int | None
    direction: str | None
    log_index: int | None
    trace_address: str | None
    parent_step_id: int | None
    execution_status: str
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        validate_positive_integer(self.step_id, "step_id")
        if not isinstance(self.action_kind, ActionKind):
            raise TypeError("action_kind must be ActionKind")
        for field_name in ("pool_key", "asset_in", "asset_out"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, (PoolKey, AssetRef)):
                raise TypeError(f"{field_name} has the wrong identity type")
        _require_optional_int(self.amount_in_atoms, "amount_in_atoms")
        _require_optional_int(self.amount_out_atoms, "amount_out_atoms")
        direction = self.direction
        if direction is not None:
            object.__setattr__(self, "direction", _require_nonempty_str(direction, "direction"))
        if self.log_index is not None:
            _require_non_negative_int(self.log_index, "log_index")
        if self.trace_address is not None:
            object.__setattr__(
                self,
                "trace_address",
                _require_nonempty_str(self.trace_address, "trace_address"),
            )
        if self.parent_step_id is not None:
            _require_non_negative_int(self.parent_step_id, "parent_step_id")
        if self.execution_status not in ("success", "reverted", "unknown"):
            raise ValueError(f"Invalid execution_status: {self.execution_status!r}")
        if type(self.evidence_refs) is not tuple or not all(
            type(item) is str and item for item in self.evidence_refs
        ):
            raise ValueError("evidence_refs must be a tuple of non-empty strings")

    def to_dict(self) -> dict[str, Any]:
        """Return a stable JSON-compatible mapping."""
        return {
            "step_id": self.step_id,
            "action_kind": self.action_kind.value,
            "pool_key": None if self.pool_key is None else _pool_key_to_dict(self.pool_key),
            "asset_in": None if self.asset_in is None else _asset_ref_to_dict(self.asset_in),
            "asset_out": None if self.asset_out is None else _asset_ref_to_dict(self.asset_out),
            "amount_in_atoms": self.amount_in_atoms,
            "amount_out_atoms": self.amount_out_atoms,
            "direction": self.direction,
            "log_index": self.log_index,
            "trace_address": self.trace_address,
            "parent_step_id": self.parent_step_id,
            "execution_status": self.execution_status,
            "evidence_refs": list(self.evidence_refs),
        }

    @classmethod
    def from_dict(cls, value: Any) -> CycleAction:
        """Construct from validated mapping data."""
        data = _require_mapping(value, "action")
        required = {
            "step_id",
            "action_kind",
            "pool_key",
            "asset_in",
            "asset_out",
            "amount_in_atoms",
            "amount_out_atoms",
            "direction",
            "log_index",
            "trace_address",
            "parent_step_id",
            "execution_status",
            "evidence_refs",
        }
        if set(data) != required:
            raise ValueError("action keys do not match the frozen schema")
        return cls(
            step_id=data["step_id"],
            action_kind=_enum_value(ActionKind, data["action_kind"], "action_kind"),
            pool_key=None if data["pool_key"] is None else _pool_key_from_dict(data["pool_key"]),
            asset_in=None if data["asset_in"] is None else _asset_ref_from_dict(data["asset_in"]),
            asset_out=None if data["asset_out"] is None else _asset_ref_from_dict(data["asset_out"]),
            amount_in_atoms=data["amount_in_atoms"],
            amount_out_atoms=data["amount_out_atoms"],
            direction=data["direction"],
            log_index=data["log_index"],
            trace_address=data["trace_address"],
            parent_step_id=data["parent_step_id"],
            execution_status=_require_nonempty_str(data["execution_status"], "execution_status"),
            evidence_refs=tuple(data["evidence_refs"]),
        )


@dataclass(frozen=True, slots=True)
class SubjectBalanceDelta:
    """A per-subject, per-asset balance delta with reconciliation metadata."""

    subject_address: str
    asset: AssetRef
    delta_atoms: int | None
    basis: str
    completeness: str
    reconciliation_diff_atoms: int | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "subject_address", validate_evm_address(self.subject_address))
        if not isinstance(self.asset, AssetRef):
            raise TypeError("asset must be an AssetRef")
        _require_optional_int(self.delta_atoms, "delta_atoms")
        _require_optional_int(
            self.reconciliation_diff_atoms, "reconciliation_diff_atoms"
        )
        if self.basis not in ("events", "tx_state_diff", "trace_transfer", "mixed", "unknown"):
            raise ValueError(f"Invalid delta basis: {self.basis!r}")
        if self.completeness not in ("complete", "partial", "unknown"):
            raise ValueError(f"Invalid delta completeness: {self.completeness!r}")

    def to_dict(self) -> dict[str, Any]:
        """Return a stable JSON-compatible mapping."""
        return {
            "subject_address": self.subject_address,
            "asset": _asset_ref_to_dict(self.asset),
            "delta_atoms": self.delta_atoms,
            "basis": self.basis,
            "completeness": self.completeness,
            "reconciliation_diff_atoms": self.reconciliation_diff_atoms,
        }

    @classmethod
    def from_dict(cls, value: Any) -> SubjectBalanceDelta:
        """Construct from validated mapping data."""
        data = _require_mapping(value, "subject_delta")
        required = {
            "subject_address",
            "asset",
            "delta_atoms",
            "basis",
            "completeness",
            "reconciliation_diff_atoms",
        }
        if set(data) != required:
            raise ValueError("subject_delta keys do not match the frozen schema")
        return cls(
            subject_address=data["subject_address"],
            asset=_asset_ref_from_dict(data["asset"]),
            delta_atoms=data["delta_atoms"],
            basis=data["basis"],
            completeness=data["completeness"],
            reconciliation_diff_atoms=data["reconciliation_diff_atoms"],
        )


@dataclass(frozen=True, slots=True)
class FeeComponentRecord:
    """A uniquely identified fee or execution-cost component."""

    component_id: str
    asset: AssetRef
    amount_atoms: int
    payer: str | None
    economic_bearer: str | None
    source: str
    counted_in: str
    dedup_key: str | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "component_id", _require_nonempty_str(self.component_id, "component_id")
        )
        if not isinstance(self.asset, AssetRef):
            raise TypeError("asset must be an AssetRef")
        if type(self.amount_atoms) is not int or isinstance(self.amount_atoms, bool):
            raise TypeError("amount_atoms must be an integer")
        if self.amount_atoms < 0 or self.amount_atoms > (1 << 256) - 1:
            raise ValueError("amount_atoms is outside uint256 bounds")
        if self.payer is not None:
            object.__setattr__(self, "payer", validate_evm_address(self.payer))
        if self.economic_bearer is not None:
            object.__setattr__(
                self, "economic_bearer", validate_evm_address(self.economic_bearer)
            )
        if self.source not in ("receipt", "trace", "event", "state_diff"):
            raise ValueError(f"Invalid fee source: {self.source!r}")
        object.__setattr__(self, "counted_in", _require_nonempty_str(self.counted_in, "counted_in"))
        if self.dedup_key is not None:
            object.__setattr__(self, "dedup_key", _require_nonempty_str(self.dedup_key, "dedup_key"))

    def to_dict(self) -> dict[str, Any]:
        """Return a stable JSON-compatible mapping."""
        return {
            "component_id": self.component_id,
            "asset": _asset_ref_to_dict(self.asset),
            "amount_atoms": self.amount_atoms,
            "payer": self.payer,
            "economic_bearer": self.economic_bearer,
            "source": self.source,
            "counted_in": self.counted_in,
            "dedup_key": self.dedup_key,
        }

    @classmethod
    def from_dict(cls, value: Any) -> FeeComponentRecord:
        """Construct from validated mapping data."""
        data = _require_mapping(value, "fee_component")
        required = {
            "component_id",
            "asset",
            "amount_atoms",
            "payer",
            "economic_bearer",
            "source",
            "counted_in",
            "dedup_key",
        }
        if set(data) != required:
            raise ValueError("fee_component keys do not match the frozen schema")
        return cls(
            component_id=data["component_id"],
            asset=_asset_ref_from_dict(data["asset"]),
            amount_atoms=data["amount_atoms"],
            payer=data["payer"],
            economic_bearer=data["economic_bearer"],
            source=data["source"],
            counted_in=data["counted_in"],
            dedup_key=data["dedup_key"],
        )


@dataclass(frozen=True, slots=True)
class ExecutionCostBreakdown:
    """Cost components with component-id uniqueness enforced."""

    components: tuple[FeeComponentRecord, ...]

    def __post_init__(self) -> None:
        if type(self.components) is not tuple or not all(
            isinstance(item, FeeComponentRecord) for item in self.components
        ):
            raise TypeError("components must be a tuple of FeeComponentRecord")
        component_ids = [component.component_id for component in self.components]
        if len(component_ids) != len(set(component_ids)):
            raise ValueError("component_id values must be unique")

    def to_dict(self) -> dict[str, Any]:
        """Return a stable JSON-compatible mapping."""
        return {"components": [component.to_dict() for component in self.components]}

    @classmethod
    def from_dict(cls, value: Any) -> ExecutionCostBreakdown:
        """Construct from validated mapping data."""
        data = _require_mapping(value, "cost_breakdown")
        if set(data) != {"components"} or not isinstance(data["components"], list):
            raise ValueError("cost_breakdown must contain components")
        return cls(tuple(FeeComponentRecord.from_dict(item) for item in data["components"]))


@dataclass(frozen=True, slots=True)
class SettledCycleRecord:
    """Versioned, immutable research record for one settled EVM cycle."""

    schema_id: str
    schema_version: str
    tx_hash: str
    chain_id: int
    block_number: int
    block_hash: str
    transaction_index: int
    timestamp_s: int
    subjects: TransactionSubjects
    actions: tuple[CycleAction, ...]
    subject_deltas: tuple[SubjectBalanceDelta, ...]
    cost_breakdown: ExecutionCostBreakdown
    attribution_status: AttributionStatus
    economic_status: EconomicStatus
    attributed_net_atoms: int | None
    attributed_net_usd: Decimal | None
    rejection_or_unknown_reasons: tuple[str, ...]
    provenance: Mapping[str, Any]
    data_mode: str = "synthetic"
    verified: bool = False

    def __post_init__(self) -> None:
        if self.schema_id != SCHEMA_ID or self.schema_version != SCHEMA_VERSION:
            raise ValueError("Unknown settled-cycle schema identity or version")
        object.__setattr__(self, "tx_hash", validate_bytes32(self.tx_hash))
        object.__setattr__(self, "block_hash", validate_bytes32(self.block_hash))
        validate_positive_integer(self.chain_id, "chain_id")
        validate_non_negative_integer(self.block_number, "block_number")
        validate_non_negative_integer(self.transaction_index, "transaction_index")
        validate_non_negative_integer(self.timestamp_s, "timestamp_s")
        if not isinstance(self.subjects, TransactionSubjects):
            raise TypeError("subjects must be TransactionSubjects")
        if type(self.actions) is not tuple or not all(
            isinstance(item, CycleAction) for item in self.actions
        ):
            raise TypeError("actions must be a tuple of CycleAction")
        previous_step_id = 0
        for action in self.actions:
            if action.step_id <= previous_step_id:
                raise ValueError("action step_id values must be strictly increasing")
            previous_step_id = action.step_id
        if type(self.subject_deltas) is not tuple or not all(
            isinstance(item, SubjectBalanceDelta) for item in self.subject_deltas
        ):
            raise TypeError("subject_deltas must be a tuple of SubjectBalanceDelta")
        if not isinstance(self.cost_breakdown, ExecutionCostBreakdown):
            raise TypeError("cost_breakdown must be ExecutionCostBreakdown")
        object.__setattr__(
            self,
            "attribution_status",
            _enum_value(AttributionStatus, self.attribution_status, "attribution_status"),
        )
        object.__setattr__(
            self,
            "economic_status",
            _enum_value(EconomicStatus, self.economic_status, "economic_status")
        )
        _require_optional_int(self.attributed_net_atoms, "attributed_net_atoms")
        if self.attributed_net_usd is not None and not isinstance(
            self.attributed_net_usd, Decimal
        ):
            raise TypeError("attributed_net_usd must be Decimal or None")
        fail_closed = self.attribution_status in (
            AttributionStatus.UNVERIFIED_MISSING_TRACE,
            AttributionStatus.AMBIGUOUS_COMPLEX_TX,
        )
        if fail_closed and (
            self.attributed_net_atoms is not None or self.attributed_net_usd is not None
        ):
            raise ValueError(
                "attributed results must be None for unverified_missing_trace "
                "or ambiguous_complex_tx"
            )
        if (
            self.economic_status
            in (EconomicStatus.POSITIVE, EconomicStatus.NONPOSITIVE)
            and self.attributed_net_atoms is None
        ):
            raise ValueError("economic_status requires attributed_net_atoms")
        if type(self.rejection_or_unknown_reasons) is not tuple or not all(
            type(reason) is str and reason for reason in self.rejection_or_unknown_reasons
        ):
            raise ValueError("rejection_or_unknown_reasons must be a tuple of non-empty strings")
        provenance = _require_mapping(self.provenance, "provenance")
        source_sha256 = provenance.get("source_sha256")
        _sha256(source_sha256, "provenance.source_sha256")
        data_mode = _require_str(self.data_mode, "data_mode")
        if data_mode not in ("synthetic", "historical"):
            raise ValueError("data_mode must be synthetic or historical")
        if type(self.verified) is not bool:
            raise TypeError("verified must be a boolean")
        if data_mode == "synthetic" and self.verified:
            raise ValueError("Synthetic fixtures must not be marked verified")

    def to_dict(self) -> dict[str, Any]:
        """Return a stable, JSON-compatible record mapping."""
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "tx_hash": self.tx_hash,
            "chain_id": self.chain_id,
            "block_number": self.block_number,
            "block_hash": self.block_hash,
            "transaction_index": self.transaction_index,
            "timestamp_s": self.timestamp_s,
            "subjects": self.subjects.to_dict(),
            "actions": [action.to_dict() for action in self.actions],
            "subject_deltas": [delta.to_dict() for delta in self.subject_deltas],
            "cost_breakdown": self.cost_breakdown.to_dict(),
            "attribution_status": self.attribution_status.value,
            "economic_status": self.economic_status.value,
            "attributed_net_atoms": self.attributed_net_atoms,
            "attributed_net_usd": (
                None if self.attributed_net_usd is None else str(self.attributed_net_usd)
            ),
            "rejection_or_unknown_reasons": list(self.rejection_or_unknown_reasons),
            "provenance": dict(self.provenance),
            "data_mode": self.data_mode,
            "verified": self.verified,
        }

    @classmethod
    def from_dict(cls, value: Any) -> SettledCycleRecord:
        """Construct from validated mapping data, including strict Decimal parsing."""
        data = _require_mapping(value, "record")
        required = {
            "schema_id",
            "schema_version",
            "tx_hash",
            "chain_id",
            "block_number",
            "block_hash",
            "transaction_index",
            "timestamp_s",
            "subjects",
            "actions",
            "subject_deltas",
            "cost_breakdown",
            "attribution_status",
            "economic_status",
            "attributed_net_atoms",
            "attributed_net_usd",
            "rejection_or_unknown_reasons",
            "provenance",
            "data_mode",
            "verified",
        }
        if set(data) != required:
            raise ValueError("record keys do not match the frozen schema")
        net_usd = data["attributed_net_usd"]
        if net_usd is not None:
            net_usd_text = _require_str(net_usd, "attributed_net_usd")
            if not _DECIMAL_STRING_REGEX.fullmatch(net_usd_text):
                raise ValueError("Invalid attributed_net_usd decimal string")
            net_usd = Decimal(net_usd_text)
        return cls(
            schema_id=data["schema_id"],
            schema_version=data["schema_version"],
            tx_hash=data["tx_hash"],
            chain_id=data["chain_id"],
            block_number=data["block_number"],
            block_hash=data["block_hash"],
            transaction_index=data["transaction_index"],
            timestamp_s=data["timestamp_s"],
            subjects=TransactionSubjects.from_dict(data["subjects"]),
            actions=tuple(CycleAction.from_dict(item) for item in data["actions"]),
            subject_deltas=tuple(
                SubjectBalanceDelta.from_dict(item) for item in data["subject_deltas"]
            ),
            cost_breakdown=ExecutionCostBreakdown.from_dict(data["cost_breakdown"]),
            attribution_status=_enum_value(
                AttributionStatus, data["attribution_status"], "attribution_status"
            ),
            economic_status=_enum_value(
                EconomicStatus, data["economic_status"], "economic_status"
            ),
            attributed_net_atoms=data["attributed_net_atoms"],
            attributed_net_usd=net_usd,
            rejection_or_unknown_reasons=tuple(data["rejection_or_unknown_reasons"]),
            provenance=data["provenance"],
            data_mode=data["data_mode"],
            verified=data["verified"],
        )


def _pool_key_to_dict(value: PoolKey) -> dict[str, Any]:
    return {
        "chain_id": value.chain_id,
        "protocol_id": value.protocol_id,
        "venue_kind": value.venue_kind,
        "venue_address": value.venue_address,
        "pool_id_kind": value.pool_id_kind,
        "pool_id": value.pool_id,
    }


def _token_key_to_dict(value: TokenKey) -> dict[str, Any]:
    return {"chain_id": value.chain_id, "address": value.raw_address}


def _asset_ref_to_dict(value: AssetRef) -> dict[str, Any]:
    return {
        "interface_kind": value.interface_kind,
        "chain_id": value.chain_id,
        "token_key": None if value.token_key is None else _token_key_to_dict(value.token_key),
        "native_identifier": value.native_identifier,
        "balance_domain_id": value.balance_domain_id,
    }


def _business_record_dict(record: SettledCycleRecord) -> dict[str, Any]:
    data = record.to_dict()
    provenance = {
        key: value
        for key, value in data["provenance"].items()
        if key not in _PROVENANCE_PATH_FIELDS
    }
    data["provenance"] = provenance
    return data


def canonical_hash(record: SettledCycleRecord) -> str:
    """Return SHA-256 of canonical JSON with business fields only.

    Path-like provenance fields are excluded so relocation of evidence files
    does not alter economic-record identity.
    """
    serialized = json.dumps(
        _business_record_dict(record), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
