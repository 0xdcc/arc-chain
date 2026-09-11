"""Immutable domain models and envelopes for atomic execution."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from arbitrage_contracts.identity import (
    UINT256_MAX,
    Amount,
    AssetRef,
    validate_bytes32,
    validate_evm_address,
    validate_non_negative_integer,
    validate_positive_integer,
)
from arbitrage_contracts.quote import QuoteEvidence, RouteRef
from arbitrage_contracts.serialization import (
    _deserialize_amount,
    _deserialize_asset_ref,
    _deserialize_quote_evidence,
    _deserialize_route_ref,
    _deserialize_state_version,
    _serialize_amount,
    _serialize_asset_ref,
    _serialize_quote_evidence,
    _serialize_route_ref,
    _serialize_state_version,
)
from arbitrage_contracts.state import StateVersion


class InputRejectionReason(StrEnum):
    """Classification taxonomy for input gate rejection causes."""

    MALFORMED_INPUT = "MALFORMED_INPUT"
    MISSING_FIELD = "MISSING_FIELD"
    TYPE_ERROR = "TYPE_ERROR"
    UINT256_OVERFLOW = "UINT256_OVERFLOW"
    DUPLICATE_KEY = "DUPLICATE_KEY"
    UNSUPPORTED_CHAIN = "UNSUPPORTED_CHAIN"
    UNSUPPORTED_BASE_ASSET = "UNSUPPORTED_BASE_ASSET"
    ASSET_MISMATCH = "ASSET_MISMATCH"
    ETH_WETH_MIXED = "ETH_WETH_MIXED"
    CYCLE_NOT_CLOSED = "CYCLE_NOT_CLOSED"
    INVALID_HOP_COUNT = "INVALID_HOP_COUNT"
    UNSUPPORTED_HOP_COUNT = "UNSUPPORTED_HOP_COUNT"
    DUPLICATE_POOL = "DUPLICATE_POOL"
    UNSUPPORTED_PROTOCOL = "UNSUPPORTED_PROTOCOL"
    ASSET_NOT_APPROVED = "ASSET_NOT_APPROVED"
    V4_NON_ZERO_HOOK = "V4_NON_ZERO_HOOK"
    DYNAMIC_FEE_UNSUPPORTED = "DYNAMIC_FEE_UNSUPPORTED"
    POOL_CAPABILITY_UNSUPPORTED = "POOL_CAPABILITY_UNSUPPORTED"
    QUOTE_STATUS_INVALID = "QUOTE_STATUS_INVALID"
    STATE_NOT_READY = "STATE_NOT_READY"
    STATE_VERSION_MISMATCH = "STATE_VERSION_MISMATCH"


@dataclass(frozen=True, slots=True)
class InputRejection:
    """Audit-ready record detailing why a candidate failed the input gate."""

    reason: InputRejectionReason
    message: str
    route_id: str | None = None
    candidate_id: str | None = None

    def __init__(
        self,
        reason: InputRejectionReason | str,
        message: str,
        route_id: str | None = None,
        candidate_id: str | None = None,
    ) -> None:
        resolved_reason = (
            reason
            if isinstance(reason, InputRejectionReason)
            else InputRejectionReason(str(reason))
        )
        if type(message) is not str or not message.strip():
            raise ValueError("message must be a non-empty string")
        object.__setattr__(self, "reason", resolved_reason)
        object.__setattr__(self, "message", message.strip())
        object.__setattr__(self, "route_id", route_id)
        object.__setattr__(self, "candidate_id", candidate_id)

    def to_dict(self) -> dict[str, Any]:
        """Serialize rejection into dictionary form."""
        return {
            "reason": str(self.reason),
            "message": self.message,
            "route_id": self.route_id,
            "candidate_id": self.candidate_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> InputRejection:
        """Construct InputRejection from dictionary mapping."""
        return cls(
            reason=data["reason"],
            message=data["message"],
            route_id=data.get("route_id"),
            candidate_id=data.get("candidate_id"),
        )

    def to_json(self) -> str:
        """Serialize rejection to compact canonical JSON."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> InputRejection:
        """Deserialize rejection from canonical JSON string."""
        return cls.from_dict(json.loads(text))


@dataclass(frozen=True, slots=True)
class ValidatedCandidate:
    """Immutable domain envelope representing a candidate route verified through input gate."""

    route_ref: RouteRef
    quote_evidence: QuoteEvidence
    state_version: StateVersion
    base_asset: AssetRef
    amount_in: Amount

    def __init__(
        self,
        route_ref: RouteRef,
        quote_evidence: QuoteEvidence,
        state_version: StateVersion,
        base_asset: AssetRef,
        amount_in: Amount,
    ) -> None:
        if not isinstance(route_ref, RouteRef):
            raise TypeError(
                f"route_ref must be an instance of RouteRef, got {type(route_ref).__name__}"
            )
        if not isinstance(quote_evidence, QuoteEvidence):
            raise TypeError(
                "quote_evidence must be an instance of QuoteEvidence, "
                f"got {type(quote_evidence).__name__}"
            )
        if not isinstance(state_version, StateVersion):
            raise TypeError(
                "state_version must be an instance of StateVersion, "
                f"got {type(state_version).__name__}"
            )
        if not isinstance(base_asset, AssetRef):
            raise TypeError(
                f"base_asset must be an instance of AssetRef, got {type(base_asset).__name__}"
            )
        if not isinstance(amount_in, Amount):
            raise TypeError(
                f"amount_in must be an instance of Amount, got {type(amount_in).__name__}"
            )

        if base_asset != route_ref.base_asset:
            raise ValueError(
                f"base_asset {base_asset} does not match route_ref base_asset {route_ref.base_asset}"
            )
        if amount_in.asset_ref != base_asset:
            raise ValueError(
                f"amount_in asset {amount_in.asset_ref} does not match base_asset {base_asset}"
            )
        if quote_evidence.route_ref.route_id != route_ref.route_id:
            raise ValueError("quote_evidence route_id does not match route_ref route_id")
        if quote_evidence.amount_in.atoms != amount_in.atoms:
            raise ValueError("quote_evidence amount_in atoms does not match amount_in atoms")

        object.__setattr__(self, "route_ref", route_ref)
        object.__setattr__(self, "quote_evidence", quote_evidence)
        object.__setattr__(self, "state_version", state_version)
        object.__setattr__(self, "base_asset", base_asset)
        object.__setattr__(self, "amount_in", amount_in)

    @property
    def route_id(self) -> str:
        """Return the deterministic route identifier."""
        return self.route_ref.route_id

    @property
    def quote_id(self) -> str:
        """Return the unique quote identifier."""
        return self.quote_evidence.quote_id

    @property
    def chain_id(self) -> int:
        """Return the chain identifier."""
        return self.route_ref.chain_id

    def to_dict(self) -> dict[str, Any]:
        """Serialize validated candidate to nested dictionary."""
        return {
            "route_ref": _serialize_route_ref(self.route_ref),
            "quote_evidence": _serialize_quote_evidence(self.quote_evidence),
            "state_version": _serialize_state_version(self.state_version),
            "base_asset": _serialize_asset_ref(self.base_asset),
            "amount_in": _serialize_amount(self.amount_in),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ValidatedCandidate:
        """Reconstruct validated candidate from serialized dictionary."""
        route_raw = data.get("route_ref")
        if not isinstance(route_raw, Mapping):
            raise ValueError("route_ref must be a mapping")
        quote_raw = data.get("quote_evidence")
        if not isinstance(quote_raw, Mapping):
            raise ValueError("quote_evidence must be a mapping")
        state_raw = data.get("state_version")
        if not isinstance(state_raw, Mapping):
            raise ValueError("state_version must be a mapping")

        route_ref = _deserialize_route_ref(route_raw)
        quote_evidence = _deserialize_quote_evidence(quote_raw)
        state_version = _deserialize_state_version(state_raw)

        base_asset_raw = data.get("base_asset")
        if isinstance(base_asset_raw, Mapping):
            base_asset = _deserialize_asset_ref(base_asset_raw)
        else:
            base_asset = route_ref.base_asset

        amount_in_raw = data.get("amount_in")
        if isinstance(amount_in_raw, Mapping):
            amount_in = _deserialize_amount(amount_in_raw)
        else:
            amount_in = quote_evidence.amount_in

        return cls(
            route_ref=route_ref,
            quote_evidence=quote_evidence,
            state_version=state_version,
            base_asset=base_asset,
            amount_in=amount_in,
        )

    def to_json(self) -> str:
        """Serialize validated candidate to compact canonical JSON."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> ValidatedCandidate:
        """Deserialize validated candidate from JSON string."""
        return cls.from_dict(json.loads(text))


@dataclass(frozen=True, slots=True)
class CandidateOpportunity:
    """Upstream candidate opportunity package binding route, quote evidence, and state version."""

    route_ref: RouteRef
    quote_evidence: QuoteEvidence
    state_version: StateVersion
    opportunity_id: str | None = None

    def __init__(
        self,
        route_ref: RouteRef,
        quote_evidence: QuoteEvidence,
        state_version: StateVersion,
        opportunity_id: str | None = None,
    ) -> None:
        if not isinstance(route_ref, RouteRef):
            raise TypeError("route_ref must be an instance of RouteRef")
        if not isinstance(quote_evidence, QuoteEvidence):
            raise TypeError("quote_evidence must be an instance of QuoteEvidence")
        if not isinstance(state_version, StateVersion):
            raise TypeError("state_version must be an instance of StateVersion")

        object.__setattr__(self, "route_ref", route_ref)
        object.__setattr__(self, "quote_evidence", quote_evidence)
        object.__setattr__(self, "state_version", state_version)
        object.__setattr__(self, "opportunity_id", opportunity_id)

    def to_dict(self) -> dict[str, Any]:
        """Serialize candidate opportunity package into dictionary."""
        return {
            "route_ref": _serialize_route_ref(self.route_ref),
            "quote_evidence": _serialize_quote_evidence(self.quote_evidence),
            "state_version": _serialize_state_version(self.state_version),
            "opportunity_id": self.opportunity_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CandidateOpportunity:
        """Reconstruct candidate opportunity from dictionary mapping."""
        route_raw = data.get("route_ref")
        if not isinstance(route_raw, Mapping):
            raise ValueError("route_ref must be a mapping")
        quote_raw = data.get("quote_evidence")
        if not isinstance(quote_raw, Mapping):
            raise ValueError("quote_evidence must be a mapping")
        state_raw = data.get("state_version")
        if not isinstance(state_raw, Mapping):
            raise ValueError("state_version must be a mapping")

        return cls(
            route_ref=_deserialize_route_ref(route_raw),
            quote_evidence=_deserialize_quote_evidence(quote_raw),
            state_version=_deserialize_state_version(state_raw),
            opportunity_id=data.get("opportunity_id"),
        )

    def to_json(self) -> str:
        """Serialize candidate opportunity to compact canonical JSON."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> CandidateOpportunity:
        """Deserialize candidate opportunity from JSON string."""
        return cls.from_dict(json.loads(text))


@dataclass(frozen=True, slots=True)
class DraftSimulationEvidence:
    """Internal draft atomic simulation evidence structure conforming to CR-W5-SCHEMA."""

    chain_id: int
    router_address: str
    calldata_hex: str
    calldata_sha256: str
    block_number: int
    block_hash: str
    from_address: str
    value_wei: int = 0
    status: str = "CALL_SUCCEEDED"
    gas_used: int | None = None
    return_data_hex: str = "0x"
    error_message: str | None = None
    is_draft: bool = True

    def __init__(
        self,
        chain_id: int,
        router_address: str,
        calldata_hex: str,
        calldata_sha256: str,
        block_number: int,
        block_hash: str,
        from_address: str,
        value_wei: int = 0,
        status: str = "CALL_SUCCEEDED",
        gas_used: int | None = None,
        return_data_hex: str = "0x",
        error_message: str | None = None,
        is_draft: bool = True,
    ) -> None:
        validated_chain_id = validate_positive_integer(chain_id, "chain_id")
        validated_router = validate_evm_address(router_address)
        validated_block_number = validate_non_negative_integer(block_number, "block_number")
        validated_block_hash = validate_bytes32(block_hash)
        validated_from = validate_evm_address(from_address)
        validated_value = validate_non_negative_integer(value_wei, "value_wei")

        if type(calldata_hex) is not str or not calldata_hex.startswith("0x"):
            raise ValueError("calldata_hex must be a 0x-prefixed hex string")
        if len(calldata_hex) % 2 != 0:
            raise ValueError("calldata_hex must have even hex characters")

        if type(calldata_sha256) is not str or len(calldata_sha256) != 64:
            raise ValueError("calldata_sha256 must be a 64-character hex string")

        validated_gas = None
        if gas_used is not None:
            validated_gas = validate_non_negative_integer(gas_used, "gas_used")

        if type(return_data_hex) is not str or not return_data_hex.startswith("0x"):
            raise ValueError("return_data_hex must be a 0x-prefixed hex string")

        if not is_draft:
            raise ValueError("DraftSimulationEvidence must have is_draft=True")

        if validated_value > UINT256_MAX:
            raise ValueError(f"value_wei exceeds uint256: {validated_value}")

        object.__setattr__(self, "chain_id", validated_chain_id)
        object.__setattr__(self, "router_address", validated_router)
        object.__setattr__(self, "calldata_hex", calldata_hex)
        object.__setattr__(self, "calldata_sha256", calldata_sha256)
        object.__setattr__(self, "block_number", validated_block_number)
        object.__setattr__(self, "block_hash", validated_block_hash)
        object.__setattr__(self, "from_address", validated_from)
        object.__setattr__(self, "value_wei", validated_value)
        object.__setattr__(self, "status", str(status))
        object.__setattr__(self, "gas_used", validated_gas)
        object.__setattr__(self, "return_data_hex", return_data_hex)
        object.__setattr__(self, "error_message", error_message)
        object.__setattr__(self, "is_draft", True)

    @property
    def call_succeeded(self) -> bool:
        """Return True if EVM call executed without revert (eth_call succeeded)."""
        return self.status in ("CALL_SUCCEEDED", "OUTPUT_UNVERIFIED")

    @property
    def output_verified(self) -> bool:
        """Raw eth_call bytes do not verify output amounts or account balances."""
        return False

    def to_dict(self) -> dict[str, Any]:
        """Serialize draft simulation evidence to dictionary."""
        return {
            "chain_id": self.chain_id,
            "router_address": self.router_address,
            "calldata_hex": self.calldata_hex,
            "calldata_sha256": self.calldata_sha256,
            "block_number": self.block_number,
            "block_hash": self.block_hash,
            "from_address": self.from_address,
            "value_wei": self.value_wei,
            "status": self.status,
            "gas_used": self.gas_used,
            "return_data_hex": self.return_data_hex,
            "error_message": self.error_message,
            "is_draft": self.is_draft,
            "call_succeeded": self.call_succeeded,
            "output_verified": self.output_verified,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DraftSimulationEvidence:
        """Construct DraftSimulationEvidence from dictionary mapping."""
        return cls(
            chain_id=data["chain_id"],
            router_address=data["router_address"],
            calldata_hex=data["calldata_hex"],
            calldata_sha256=data["calldata_sha256"],
            block_number=data["block_number"],
            block_hash=data["block_hash"],
            from_address=data["from_address"],
            value_wei=data.get("value_wei", 0),
            status=data.get("status", "CALL_SUCCEEDED"),
            gas_used=data.get("gas_used"),
            return_data_hex=data.get("return_data_hex", "0x"),
            error_message=data.get("error_message"),
            is_draft=data.get("is_draft", True),
        )

    def to_json(self) -> str:
        """Serialize draft simulation evidence to compact canonical JSON."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> DraftSimulationEvidence:
        """Deserialize draft simulation evidence from canonical JSON string."""
        return cls.from_dict(json.loads(text))
