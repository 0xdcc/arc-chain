"""Strict, immutable, standard-library domain models and draft DTOs for Arc readiness."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Self

from arc_readiness.errors import ArcValidationError

_HEX_CHARS = frozenset("0123456789abcdefABCDEF")


def validate_bool(val: Any, field_name: str) -> bool:
    if not isinstance(val, bool):
        raise ArcValidationError(f"{field_name} must be a bool, got {type(val).__name__}")
    return val


def validate_positive_int(val: Any, field_name: str) -> int:
    if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
        raise ArcValidationError(f"{field_name} must be a positive integer, got {val!r}")
    return val


def validate_non_negative_int(val: Any, field_name: str) -> int:
    if isinstance(val, bool) or not isinstance(val, int) or val < 0:
        raise ArcValidationError(f"{field_name} must be a non-negative integer, got {val!r}")
    return val


def validate_optional_non_negative_int(val: Any, field_name: str) -> int | None:
    if val is None:
        return None
    return validate_non_negative_int(val, field_name)


def validate_address(val: Any, field_name: str) -> str:
    if not isinstance(val, str):
        raise ArcValidationError(f"{field_name} must be a string, got {type(val).__name__}")
    if len(val) != 42 or not val.startswith("0x") or not all(c in _HEX_CHARS for c in val[2:]):
        raise ArcValidationError(f"{field_name} must be a valid 42-char hex address, got {val!r}")
    return val.lower()


def validate_optional_address(val: Any, field_name: str) -> str | None:
    if val is None:
        return None
    return validate_address(val, field_name)


def validate_bytes32(val: Any, field_name: str) -> str:
    if not isinstance(val, str):
        raise ArcValidationError(f"{field_name} must be a string, got {type(val).__name__}")
    if len(val) != 66 or not val.startswith("0x") or not all(c in _HEX_CHARS for c in val[2:]):
        raise ArcValidationError(f"{field_name} must be a valid 66-char hex bytes32, got {val!r}")
    return val.lower()


def validate_string(val: Any, field_name: str, allow_empty: bool = False) -> str:
    if not isinstance(val, str):
        raise ArcValidationError(f"{field_name} must be a string, got {type(val).__name__}")
    if not allow_empty and not val.strip():
        raise ArcValidationError(f"{field_name} must be a non-empty string")
    return val


def validate_optional_string(val: Any, field_name: str) -> str | None:
    if val is None:
        return None
    return validate_string(val, field_name, allow_empty=False)


def validate_str_tuple(val: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(val, (tuple, list)):
        raise ArcValidationError(
            f"{field_name} must be a sequence of strings, got {type(val).__name__}"
        )
    items = []
    for i, item in enumerate(val):
        if not isinstance(item, str):
            raise ArcValidationError(
                f"{field_name}[{i}] must be a string, got {type(item).__name__}"
            )
        items.append(item)
    return tuple(items)


def validate_enum(val: Any, field_name: str, allowed: set[str]) -> str:
    if not isinstance(val, str) or val not in allowed:
        raise ArcValidationError(f"{field_name} must be one of {sorted(allowed)}, got {val!r}")
    return val


# ==============================================================================
# 1. Network Identity & Constants
# ==============================================================================

ARC_TESTNET_CHAIN_ID = 5042002
ARC_USDC_ERC20_ADDRESS = "0x3600000000000000000000000000000000000000"
ARC_SYSTEM_TRANSFER_EMITTER = "0xfffffffffffffffffffffffffffffffffffffffe"
ARC_LEGACY_AUTHORITY_PRECOMPILE = "0x1800000000000000000000000000000000000000"

_VERIFICATION_STATUSES = {"unverified", "synthetic_verified", "readonly_verified", "failed"}
_KNOWN_STAGES = {"testnet", "private_mainnet", "public_mainnet", "unknown"}


@dataclass(frozen=True, slots=True)
class ArcNetworkIdentity:
    """Immutable network identity and contract parameter declaration."""

    chain_id: int
    network_name: str
    rpc_endpoint: str
    native_currency: str = "USDC"
    native_decimals: int = 18
    erc20_usdc_decimals: int = 6
    erc20_usdc_address: str = ARC_USDC_ERC20_ADDRESS
    verification_status: str = "unverified"
    known_stage: str = "testnet"
    source_evidence_ref: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "chain_id", validate_positive_int(self.chain_id, "chain_id"))
        object.__setattr__(self, "network_name", validate_string(self.network_name, "network_name"))
        object.__setattr__(self, "rpc_endpoint", validate_string(self.rpc_endpoint, "rpc_endpoint"))
        object.__setattr__(
            self, "native_currency", validate_string(self.native_currency, "native_currency")
        )
        object.__setattr__(
            self, "native_decimals", validate_positive_int(self.native_decimals, "native_decimals")
        )
        object.__setattr__(
            self,
            "erc20_usdc_decimals",
            validate_positive_int(self.erc20_usdc_decimals, "erc20_usdc_decimals"),
        )
        object.__setattr__(
            self,
            "erc20_usdc_address",
            validate_address(self.erc20_usdc_address, "erc20_usdc_address"),
        )
        object.__setattr__(
            self,
            "verification_status",
            validate_enum(self.verification_status, "verification_status", _VERIFICATION_STATUSES),
        )
        object.__setattr__(
            self, "known_stage", validate_enum(self.known_stage, "known_stage", _KNOWN_STAGES)
        )
        object.__setattr__(
            self,
            "source_evidence_ref",
            validate_optional_string(self.source_evidence_ref, "source_evidence_ref"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "network_name": self.network_name,
            "rpc_endpoint": self.rpc_endpoint,
            "native_currency": self.native_currency,
            "native_decimals": self.native_decimals,
            "erc20_usdc_decimals": self.erc20_usdc_decimals,
            "erc20_usdc_address": self.erc20_usdc_address,
            "verification_status": self.verification_status,
            "known_stage": self.known_stage,
            "source_evidence_ref": self.source_evidence_ref,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        if not isinstance(data, Mapping):
            raise ArcValidationError(
                f"Expected mapping for {cls.__name__}, got {type(data).__name__}"
            )
        return cls(
            chain_id=data.get("chain_id"),  # type: ignore[arg-type]
            network_name=data.get("network_name"),  # type: ignore[arg-type]
            rpc_endpoint=data.get("rpc_endpoint"),  # type: ignore[arg-type]
            native_currency=data.get("native_currency", "USDC"),
            native_decimals=data.get("native_decimals", 18),
            erc20_usdc_decimals=data.get("erc20_usdc_decimals", 6),
            erc20_usdc_address=data.get("erc20_usdc_address", ARC_USDC_ERC20_ADDRESS),
            verification_status=data.get("verification_status", "unverified"),
            known_stage=data.get("known_stage", "testnet"),
            source_evidence_ref=data.get("source_evidence_ref"),
        )


# ==============================================================================
# 2. Dual-Interface Balance Observation
# ==============================================================================


@dataclass(frozen=True, slots=True)
class ArcBalanceObservation:
    """Observed account balance across native (18d) and ERC-20 (6d) interfaces."""

    account_address: str
    chain_id: int
    block_number: int
    block_hash: str
    native_atoms: int | None = None
    erc20_atoms: int | None = None
    balance_domain_id: str | None = None
    dust_atoms: int | None = None
    verified_consistency: bool = False
    stale_or_incomplete_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "account_address", validate_address(self.account_address, "account_address")
        )
        object.__setattr__(self, "chain_id", validate_positive_int(self.chain_id, "chain_id"))
        object.__setattr__(
            self, "block_number", validate_non_negative_int(self.block_number, "block_number")
        )
        object.__setattr__(self, "block_hash", validate_bytes32(self.block_hash, "block_hash"))
        object.__setattr__(
            self,
            "native_atoms",
            validate_optional_non_negative_int(self.native_atoms, "native_atoms"),
        )
        object.__setattr__(
            self, "erc20_atoms", validate_optional_non_negative_int(self.erc20_atoms, "erc20_atoms")
        )
        object.__setattr__(
            self,
            "balance_domain_id",
            validate_optional_string(self.balance_domain_id, "balance_domain_id"),
        )
        object.__setattr__(
            self, "dust_atoms", validate_optional_non_negative_int(self.dust_atoms, "dust_atoms")
        )
        object.__setattr__(
            self,
            "verified_consistency",
            validate_bool(self.verified_consistency, "verified_consistency"),
        )
        object.__setattr__(
            self,
            "stale_or_incomplete_reasons",
            validate_str_tuple(self.stale_or_incomplete_reasons, "stale_or_incomplete_reasons"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_address": self.account_address,
            "chain_id": self.chain_id,
            "block_number": self.block_number,
            "block_hash": self.block_hash,
            "native_atoms": self.native_atoms,
            "erc20_atoms": self.erc20_atoms,
            "balance_domain_id": self.balance_domain_id,
            "dust_atoms": self.dust_atoms,
            "verified_consistency": self.verified_consistency,
            "stale_or_incomplete_reasons": list(self.stale_or_incomplete_reasons),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        if not isinstance(data, Mapping):
            raise ArcValidationError(
                f"Expected mapping for {cls.__name__}, got {type(data).__name__}"
            )
        return cls(
            account_address=data.get("account_address"),  # type: ignore[arg-type]
            chain_id=data.get("chain_id"),  # type: ignore[arg-type]
            block_number=data.get("block_number"),  # type: ignore[arg-type]
            block_hash=data.get("block_hash"),  # type: ignore[arg-type]
            native_atoms=data.get("native_atoms"),
            erc20_atoms=data.get("erc20_atoms"),
            balance_domain_id=data.get("balance_domain_id"),
            dust_atoms=data.get("dust_atoms"),
            verified_consistency=data.get("verified_consistency", False),
            stale_or_incomplete_reasons=data.get("stale_or_incomplete_reasons", ()),
        )


# ==============================================================================
# 3. Gas & Fee Observation
# ==============================================================================

_FEE_SOURCES = {"receipt", "estimate", "header", "simulated"}


@dataclass(frozen=True, slots=True)
class ArcFeeObservation:
    """Gas and fee metrics on Arc (native 18d USDC units)."""

    chain_id: int
    block_number: int
    block_hash: str
    gas_used: int | None = None
    effective_gas_price_wei: int | None = None
    total_fee_atoms: int | None = None
    base_fee_gwei: int | None = None
    priority_fee_gwei: int | None = None
    fee_source: str = "receipt"
    is_estimate: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "chain_id", validate_positive_int(self.chain_id, "chain_id"))
        object.__setattr__(
            self, "block_number", validate_non_negative_int(self.block_number, "block_number")
        )
        object.__setattr__(self, "block_hash", validate_bytes32(self.block_hash, "block_hash"))
        object.__setattr__(
            self, "gas_used", validate_optional_non_negative_int(self.gas_used, "gas_used")
        )
        object.__setattr__(
            self,
            "effective_gas_price_wei",
            validate_optional_non_negative_int(
                self.effective_gas_price_wei, "effective_gas_price_wei"
            ),
        )
        object.__setattr__(
            self,
            "total_fee_atoms",
            validate_optional_non_negative_int(self.total_fee_atoms, "total_fee_atoms"),
        )
        object.__setattr__(
            self,
            "base_fee_gwei",
            validate_optional_non_negative_int(self.base_fee_gwei, "base_fee_gwei"),
        )
        object.__setattr__(
            self,
            "priority_fee_gwei",
            validate_optional_non_negative_int(self.priority_fee_gwei, "priority_fee_gwei"),
        )
        object.__setattr__(
            self, "fee_source", validate_enum(self.fee_source, "fee_source", _FEE_SOURCES)
        )
        object.__setattr__(self, "is_estimate", validate_bool(self.is_estimate, "is_estimate"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "block_number": self.block_number,
            "block_hash": self.block_hash,
            "gas_used": self.gas_used,
            "effective_gas_price_wei": self.effective_gas_price_wei,
            "total_fee_atoms": self.total_fee_atoms,
            "base_fee_gwei": self.base_fee_gwei,
            "priority_fee_gwei": self.priority_fee_gwei,
            "fee_source": self.fee_source,
            "is_estimate": self.is_estimate,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        if not isinstance(data, Mapping):
            raise ArcValidationError(
                f"Expected mapping for {cls.__name__}, got {type(data).__name__}"
            )
        return cls(
            chain_id=data.get("chain_id"),  # type: ignore[arg-type]
            block_number=data.get("block_number"),  # type: ignore[arg-type]
            block_hash=data.get("block_hash"),  # type: ignore[arg-type]
            gas_used=data.get("gas_used"),
            effective_gas_price_wei=data.get("effective_gas_price_wei"),
            total_fee_atoms=data.get("total_fee_atoms"),
            base_fee_gwei=data.get("base_fee_gwei"),
            priority_fee_gwei=data.get("priority_fee_gwei"),
            fee_source=data.get("fee_source", "receipt"),
            is_estimate=data.get("is_estimate", False),
        )


# ==============================================================================
# 4. Permission & Allowance Status
# ==============================================================================


@dataclass(frozen=True, slots=True)
class ArcPermissionStatus:
    """ERC-20 allowance vs Native spending authority status."""

    account_address: str
    target_contract: str
    erc20_allowance_atoms: int | None = None
    native_spending_authorized: bool = False
    source_ref: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "account_address", validate_address(self.account_address, "account_address")
        )
        object.__setattr__(
            self, "target_contract", validate_address(self.target_contract, "target_contract")
        )
        object.__setattr__(
            self,
            "erc20_allowance_atoms",
            validate_optional_non_negative_int(self.erc20_allowance_atoms, "erc20_allowance_atoms"),
        )
        object.__setattr__(
            self,
            "native_spending_authorized",
            validate_bool(self.native_spending_authorized, "native_spending_authorized"),
        )
        object.__setattr__(
            self, "source_ref", validate_optional_string(self.source_ref, "source_ref")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_address": self.account_address,
            "target_contract": self.target_contract,
            "erc20_allowance_atoms": self.erc20_allowance_atoms,
            "native_spending_authorized": self.native_spending_authorized,
            "source_ref": self.source_ref,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        if not isinstance(data, Mapping):
            raise ArcValidationError(
                f"Expected mapping for {cls.__name__}, got {type(data).__name__}"
            )
        return cls(
            account_address=data.get("account_address"),  # type: ignore[arg-type]
            target_contract=data.get("target_contract"),  # type: ignore[arg-type]
            erc20_allowance_atoms=data.get("erc20_allowance_atoms"),
            native_spending_authorized=data.get("native_spending_authorized", False),
            source_ref=data.get("source_ref"),
        )


# ==============================================================================
# 5. Asset & Market Eligibility Draft DTOs
# ==============================================================================

_INTERFACE_KINDS = {"erc20", "native"}
_REVIEW_STATUSES = {"discovered", "verified", "rejected", "provisional"}
_DECIMALS_STATUSES = {"verified", "unknown", "mismatch"}
_CAPABILITY_STATUSES = {"unknown", "supported", "unsupported"}


@dataclass(frozen=True, slots=True)
class ArcAssetEligibilityDraft:
    """Candidate asset qualification draft on Arc."""

    asset_id: str
    symbol: str
    decimals: int
    contract_address: str | None = None
    interface_kind: str = "erc20"
    review_status: str = "discovered"
    decimals_status: str = "unknown"
    is_usdc_native_domain: bool = False
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "asset_id", validate_string(self.asset_id, "asset_id"))
        object.__setattr__(self, "symbol", validate_string(self.symbol, "symbol"))
        object.__setattr__(self, "decimals", validate_non_negative_int(self.decimals, "decimals"))
        object.__setattr__(
            self,
            "contract_address",
            validate_optional_address(self.contract_address, "contract_address"),
        )
        object.__setattr__(
            self,
            "interface_kind",
            validate_enum(self.interface_kind, "interface_kind", _INTERFACE_KINDS),
        )
        object.__setattr__(
            self,
            "review_status",
            validate_enum(self.review_status, "review_status", _REVIEW_STATUSES),
        )
        object.__setattr__(
            self,
            "decimals_status",
            validate_enum(self.decimals_status, "decimals_status", _DECIMALS_STATUSES),
        )
        object.__setattr__(
            self,
            "is_usdc_native_domain",
            validate_bool(self.is_usdc_native_domain, "is_usdc_native_domain"),
        )
        object.__setattr__(self, "reasons", validate_str_tuple(self.reasons, "reasons"))

        if self.interface_kind == "erc20" and self.contract_address is None:
            raise ArcValidationError("ERC-20 asset must provide contract_address")
        if self.interface_kind == "native" and self.contract_address is not None:
            raise ArcValidationError("Native asset must not provide contract_address")

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "symbol": self.symbol,
            "decimals": self.decimals,
            "contract_address": self.contract_address,
            "interface_kind": self.interface_kind,
            "review_status": self.review_status,
            "decimals_status": self.decimals_status,
            "is_usdc_native_domain": self.is_usdc_native_domain,
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        if not isinstance(data, Mapping):
            raise ArcValidationError(
                f"Expected mapping for {cls.__name__}, got {type(data).__name__}"
            )
        return cls(
            asset_id=data.get("asset_id"),  # type: ignore[arg-type]
            symbol=data.get("symbol"),  # type: ignore[arg-type]
            decimals=data.get("decimals"),  # type: ignore[arg-type]
            contract_address=data.get("contract_address"),
            interface_kind=data.get("interface_kind", "erc20"),
            review_status=data.get("review_status", "discovered"),
            decimals_status=data.get("decimals_status", "unknown"),
            is_usdc_native_domain=data.get("is_usdc_native_domain", False),
            reasons=data.get("reasons", ()),
        )


@dataclass(frozen=True, slots=True)
class ArcMarketEligibilityDraft:
    """Candidate trading venue / pool qualification draft on Arc."""

    market_id: str
    protocol_id: str
    pool_address: str
    base_asset_id: str
    quote_asset_id: str
    can_quote: str = "unknown"
    can_simulate: str = "unknown"
    can_atomic_execute: str = "unknown"
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "market_id", validate_string(self.market_id, "market_id"))
        object.__setattr__(self, "protocol_id", validate_string(self.protocol_id, "protocol_id"))
        object.__setattr__(
            self, "pool_address", validate_address(self.pool_address, "pool_address")
        )
        object.__setattr__(
            self, "base_asset_id", validate_string(self.base_asset_id, "base_asset_id")
        )
        object.__setattr__(
            self, "quote_asset_id", validate_string(self.quote_asset_id, "quote_asset_id")
        )
        object.__setattr__(
            self, "can_quote", validate_enum(self.can_quote, "can_quote", _CAPABILITY_STATUSES)
        )
        object.__setattr__(
            self,
            "can_simulate",
            validate_enum(self.can_simulate, "can_simulate", _CAPABILITY_STATUSES),
        )
        object.__setattr__(
            self,
            "can_atomic_execute",
            validate_enum(self.can_atomic_execute, "can_atomic_execute", _CAPABILITY_STATUSES),
        )
        object.__setattr__(self, "reasons", validate_str_tuple(self.reasons, "reasons"))

        if self.base_asset_id == self.quote_asset_id:
            raise ArcValidationError("Market base and quote assets cannot be identical")

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_id": self.market_id,
            "protocol_id": self.protocol_id,
            "pool_address": self.pool_address,
            "base_asset_id": self.base_asset_id,
            "quote_asset_id": self.quote_asset_id,
            "can_quote": self.can_quote,
            "can_simulate": self.can_simulate,
            "can_atomic_execute": self.can_atomic_execute,
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        if not isinstance(data, Mapping):
            raise ArcValidationError(
                f"Expected mapping for {cls.__name__}, got {type(data).__name__}"
            )
        return cls(
            market_id=data.get("market_id"),  # type: ignore[arg-type]
            protocol_id=data.get("protocol_id"),  # type: ignore[arg-type]
            pool_address=data.get("pool_address"),  # type: ignore[arg-type]
            base_asset_id=data.get("base_asset_id"),  # type: ignore[arg-type]
            quote_asset_id=data.get("quote_asset_id"),  # type: ignore[arg-type]
            can_quote=data.get("can_quote", "unknown"),
            can_simulate=data.get("can_simulate", "unknown"),
            can_atomic_execute=data.get("can_atomic_execute", "unknown"),
            reasons=data.get("reasons", ()),
        )


# ==============================================================================
# 6. Event Record DTO
# ==============================================================================


@dataclass(frozen=True, slots=True)
class ArcEventRecordDraft:
    """Standardized event log representation for Arc balance and transfer monitoring."""

    chain_id: int
    block_number: int
    block_hash: str
    tx_hash: str
    log_index: int
    emitter_address: str
    event_type: str
    from_address: str
    to_address: str
    raw_value_atoms: int
    decimals_view: int
    is_system_emitter: bool = False
    idempotency_key: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "chain_id", validate_positive_int(self.chain_id, "chain_id"))
        object.__setattr__(
            self, "block_number", validate_non_negative_int(self.block_number, "block_number")
        )
        object.__setattr__(self, "block_hash", validate_bytes32(self.block_hash, "block_hash"))
        object.__setattr__(self, "tx_hash", validate_bytes32(self.tx_hash, "tx_hash"))
        object.__setattr__(
            self, "log_index", validate_non_negative_int(self.log_index, "log_index")
        )
        object.__setattr__(
            self, "emitter_address", validate_address(self.emitter_address, "emitter_address")
        )
        object.__setattr__(self, "event_type", validate_string(self.event_type, "event_type"))
        object.__setattr__(
            self, "from_address", validate_address(self.from_address, "from_address")
        )
        object.__setattr__(self, "to_address", validate_address(self.to_address, "to_address"))
        object.__setattr__(
            self,
            "raw_value_atoms",
            validate_non_negative_int(self.raw_value_atoms, "raw_value_atoms"),
        )
        object.__setattr__(
            self, "decimals_view", validate_positive_int(self.decimals_view, "decimals_view")
        )
        object.__setattr__(
            self, "is_system_emitter", validate_bool(self.is_system_emitter, "is_system_emitter")
        )

        calc_key = f"{self.chain_id}:{self.block_hash}:{self.tx_hash}:{self.log_index}:{self.emitter_address}"
        if not self.idempotency_key:
            object.__setattr__(self, "idempotency_key", calc_key)
        else:
            object.__setattr__(
                self, "idempotency_key", validate_string(self.idempotency_key, "idempotency_key")
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "block_number": self.block_number,
            "block_hash": self.block_hash,
            "tx_hash": self.tx_hash,
            "log_index": self.log_index,
            "emitter_address": self.emitter_address,
            "event_type": self.event_type,
            "from_address": self.from_address,
            "to_address": self.to_address,
            "raw_value_atoms": self.raw_value_atoms,
            "decimals_view": self.decimals_view,
            "is_system_emitter": self.is_system_emitter,
            "idempotency_key": self.idempotency_key,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        if not isinstance(data, Mapping):
            raise ArcValidationError(
                f"Expected mapping for {cls.__name__}, got {type(data).__name__}"
            )
        return cls(
            chain_id=data.get("chain_id"),  # type: ignore[arg-type]
            block_number=data.get("block_number"),  # type: ignore[arg-type]
            block_hash=data.get("block_hash"),  # type: ignore[arg-type]
            tx_hash=data.get("tx_hash"),  # type: ignore[arg-type]
            log_index=data.get("log_index"),  # type: ignore[arg-type]
            emitter_address=data.get("emitter_address"),  # type: ignore[arg-type]
            event_type=data.get("event_type"),  # type: ignore[arg-type]
            from_address=data.get("from_address"),  # type: ignore[arg-type]
            to_address=data.get("to_address"),  # type: ignore[arg-type]
            raw_value_atoms=data.get("raw_value_atoms"),  # type: ignore[arg-type]
            decimals_view=data.get("decimals_view"),  # type: ignore[arg-type]
            is_system_emitter=data.get("is_system_emitter", False),
            idempotency_key=data.get("idempotency_key", ""),
        )
