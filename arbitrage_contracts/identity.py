"""Domain identity contracts for tokens, assets, pools, and fee models."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

UINT256_MAX = (1 << 256) - 1
_EVM_ADDRESS_REGEX = re.compile(r"^0x[0-9a-fA-F]{40}$")
_BYTES32_REGEX = re.compile(r"^0x[0-9a-fA-F]{64}$")
_DECIMAL_ATOMS_REGEX = re.compile(r"^(0|[1-9][0-9]*)$")


def validate_evm_address(address_value: str) -> str:
    """Validate 20-byte EVM address format without altering input."""
    if type(address_value) is not str:
        raise TypeError(f"Address must be a string, got {type(address_value).__name__}")
    if len(address_value) != 42 or not address_value.startswith("0x"):
        raise ValueError(f"Invalid EVM address length or prefix: {address_value!r}")
    if not _EVM_ADDRESS_REGEX.match(address_value):
        raise ValueError(f"Invalid EVM address characters: {address_value!r}")
    return address_value


def validate_bytes32(bytes32_value: str) -> str:
    """Validate 32-byte hex string (66 characters beginning with 0x)."""
    if type(bytes32_value) is not str:
        raise TypeError(f"Bytes32 must be a string, got {type(bytes32_value).__name__}")
    if len(bytes32_value) != 66 or not bytes32_value.startswith("0x"):
        raise ValueError(f"Invalid bytes32 length or prefix: {bytes32_value!r}")
    if not _BYTES32_REGEX.match(bytes32_value):
        raise ValueError(f"Invalid bytes32 hex characters: {bytes32_value!r}")
    return bytes32_value


def validate_positive_integer(numeric_value: int, field_name: str) -> int:
    """Validate that an integer is strictly greater than zero and not boolean."""
    if type(numeric_value) is not int or isinstance(numeric_value, bool):
        raise TypeError(f"{field_name} must be an integer, got {type(numeric_value).__name__}")
    if numeric_value <= 0:
        raise ValueError(f"{field_name} must be strictly positive, got {numeric_value}")
    return numeric_value


def validate_non_negative_integer(numeric_value: int, field_name: str) -> int:
    """Validate that an integer is non-negative and not boolean."""
    if type(numeric_value) is not int or isinstance(numeric_value, bool):
        raise TypeError(f"{field_name} must be an integer, got {type(numeric_value).__name__}")
    if numeric_value < 0:
        raise ValueError(f"{field_name} must be non-negative, got {numeric_value}")
    return numeric_value


@dataclass(frozen=True, slots=True)
class TokenKey:
    """Unique canonical key for an EVM token."""

    chain_id: int
    raw_address: str
    address: str = field(init=False)

    def __init__(self, chain_id: int, address: str) -> None:
        validated_chain_id = validate_positive_integer(chain_id, "chain_id")
        validated_raw = validate_evm_address(address)
        object.__setattr__(self, "chain_id", validated_chain_id)
        object.__setattr__(self, "raw_address", validated_raw)
        object.__setattr__(self, "address", validated_raw.lower())

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TokenKey):
            return False
        return self.chain_id == other.chain_id and self.address == other.address

    def __hash__(self) -> int:
        return hash((self.chain_id, self.address))


class AssetInterfaceKind(StrEnum):
    """Supported interface kinds for economic assets."""

    ERC20 = "erc20"
    NATIVE = "native"


@dataclass(frozen=True, slots=True)
class AssetRef:
    """Distinct reference to an economic asset (ERC20 or Native)."""

    interface_kind: str
    chain_id: int
    token_key: TokenKey | None = None
    native_identifier: str | None = None
    balance_domain_id: str | None = None

    def __post_init__(self) -> None:
        validate_positive_integer(self.chain_id, "chain_id")
        if self.interface_kind == AssetInterfaceKind.ERC20 or self.interface_kind == "erc20":
            object.__setattr__(self, "interface_kind", "erc20")
            if self.token_key is None:
                raise ValueError("ERC20 AssetRef must provide token_key")
            if not isinstance(self.token_key, TokenKey):
                raise TypeError("token_key must be an instance of TokenKey")
            if self.token_key.chain_id != self.chain_id:
                raise ValueError("AssetRef chain_id must match token_key.chain_id")
            if self.native_identifier is not None:
                raise ValueError("ERC20 AssetRef must not provide native_identifier")
        elif self.interface_kind == AssetInterfaceKind.NATIVE or self.interface_kind == "native":
            object.__setattr__(self, "interface_kind", "native")
            if self.token_key is not None:
                raise ValueError("Native AssetRef must not provide token_key")
            if not self.native_identifier or type(self.native_identifier) is not str:
                raise ValueError("Native AssetRef must provide a non-empty native_identifier")
        else:
            raise ValueError(f"Unknown interface_kind: {self.interface_kind!r}")

    @classmethod
    def erc20(cls, token_key: TokenKey, balance_domain_id: str | None = None) -> AssetRef:
        """Create an ERC20 AssetRef from a TokenKey."""
        return cls(
            interface_kind="erc20",
            chain_id=token_key.chain_id,
            token_key=token_key,
            native_identifier=None,
            balance_domain_id=balance_domain_id,
        )

    @classmethod
    def native(
        cls, chain_id: int, native_identifier: str, balance_domain_id: str | None = None
    ) -> AssetRef:
        """Create a Native AssetRef."""
        return cls(
            interface_kind="native",
            chain_id=chain_id,
            token_key=None,
            native_identifier=native_identifier,
            balance_domain_id=balance_domain_id,
        )


@dataclass(frozen=True, slots=True)
class Amount:
    """Atomic token amount with strict uint256 and decimal invariants."""

    asset_ref: AssetRef
    atoms: int
    decimals: int
    decimals_evidence_ref: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.asset_ref, AssetRef):
            raise TypeError("asset_ref must be an instance of AssetRef")
        if type(self.atoms) is not int or isinstance(self.atoms, bool):
            raise TypeError(f"atoms must be an integer, got {type(self.atoms).__name__}")
        if self.atoms < 0 or self.atoms > UINT256_MAX:
            raise ValueError(f"atoms out of uint256 bounds: {self.atoms}")
        if type(self.decimals) is not int or isinstance(self.decimals, bool):
            raise TypeError(f"decimals must be an integer, got {type(self.decimals).__name__}")
        if self.decimals < 0 or self.decimals > 255:
            raise ValueError(f"decimals must be in range 0..255, got {self.decimals}")

    def to_atoms_str(self) -> str:
        """Serialize atomic amount into strict decimal string without exponents or leading zeros."""
        return str(self.atoms)

    @classmethod
    def from_atoms_str(
        cls,
        asset_ref: AssetRef,
        atoms_string: str,
        decimals: int,
        decimals_evidence_ref: str | None = None,
    ) -> Amount:
        """Parse atomic amount from strict decimal string."""
        if type(atoms_string) is not str:
            raise TypeError(f"atoms_string must be a string, got {type(atoms_string).__name__}")
        if not _DECIMAL_ATOMS_REGEX.match(atoms_string):
            raise ValueError(f"Invalid atoms decimal string format: {atoms_string!r}")
        atoms_integer = int(atoms_string)
        return cls(
            asset_ref=asset_ref,
            atoms=atoms_integer,
            decimals=decimals,
            decimals_evidence_ref=decimals_evidence_ref,
        )


class VenueKind(StrEnum):
    """Venue kind for pools."""

    FACTORY = "factory"
    MANAGER = "manager"


class PoolIdKind(StrEnum):
    """Identifier kind for pools."""

    ADDRESS = "address"
    BYTES32 = "bytes32"


@dataclass(frozen=True, slots=True)
class PoolKey:
    """Canonical identifier for liquidity pools across V2, V3, and V4 architectures."""

    chain_id: int
    protocol_id: str
    venue_kind: str
    venue_address: str
    pool_id_kind: str
    pool_id: str
    canonical_venue_address: str = field(init=False)
    canonical_pool_id: str = field(init=False)

    def __init__(
        self,
        chain_id: int,
        protocol_id: str,
        venue_kind: str,
        venue_address: str,
        pool_id_kind: str,
        pool_id: str,
    ) -> None:
        validated_chain_id = validate_positive_integer(chain_id, "chain_id")
        if not protocol_id or type(protocol_id) is not str:
            raise ValueError("protocol_id must be a non-empty string")
        if venue_kind not in ("factory", "manager"):
            raise ValueError(f"venue_kind must be 'factory' or 'manager', got {venue_kind!r}")
        validated_venue = validate_evm_address(venue_address)
        if pool_id_kind not in ("address", "bytes32"):
            raise ValueError(f"pool_id_kind must be 'address' or 'bytes32', got {pool_id_kind!r}")
        if pool_id_kind == "address":
            validated_pool_id = validate_evm_address(pool_id)
        else:
            validated_pool_id = validate_bytes32(pool_id)
        object.__setattr__(self, "chain_id", validated_chain_id)
        object.__setattr__(self, "protocol_id", protocol_id)
        object.__setattr__(self, "venue_kind", venue_kind)
        object.__setattr__(self, "venue_address", validated_venue)
        object.__setattr__(self, "canonical_venue_address", validated_venue.lower())
        object.__setattr__(self, "pool_id_kind", pool_id_kind)
        object.__setattr__(self, "pool_id", validated_pool_id)
        object.__setattr__(self, "canonical_pool_id", validated_pool_id.lower())

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PoolKey):
            return False
        return (
            self.chain_id == other.chain_id
            and self.protocol_id == other.protocol_id
            and self.venue_kind == other.venue_kind
            and self.canonical_venue_address == other.canonical_venue_address
            and self.pool_id_kind == other.pool_id_kind
            and self.canonical_pool_id == other.canonical_pool_id
        )

    def __hash__(self) -> int:
        return hash(
            (
                self.chain_id,
                self.protocol_id,
                self.venue_kind,
                self.canonical_venue_address,
                self.pool_id_kind,
                self.canonical_pool_id,
            )
        )


class FeeModelKind(StrEnum):
    """Fee model classification."""

    STATIC = "static"
    DYNAMIC = "dynamic"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class FeeModel:
    """Explicit pool fee structure distinguishing static zero fees, dynamic fees, and unknown fees."""

    kind: str
    raw_value: int | None = None
    unit: str | None = None
    numerator: int | None = None
    denominator: int | None = None
    hook_ref: str | None = None
    model_version: str | None = None
    evidence_ref: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("static", "dynamic", "unknown"):
            raise ValueError(f"kind must be 'static', 'dynamic', or 'unknown', got {self.kind!r}")
        if self.raw_value is not None:
            if type(self.raw_value) is not int or isinstance(self.raw_value, bool):
                raise TypeError("raw_value must be an integer or None")
            if self.raw_value < 0:
                raise ValueError(f"raw_value cannot be negative: {self.raw_value}")
        if self.numerator is not None:
            if type(self.numerator) is not int or isinstance(self.numerator, bool):
                raise TypeError("numerator must be an integer or None")
            if self.numerator < 0:
                raise ValueError("numerator cannot be negative")
        if self.denominator is not None:
            if type(self.denominator) is not int or isinstance(self.denominator, bool):
                raise TypeError("denominator must be an integer or None")
            if self.denominator <= 0:
                raise ValueError("denominator must be strictly positive")

    @classmethod
    def static(
        cls,
        raw_value: int,
        unit: str = "hundredths_of_bip",
        numerator: int | None = None,
        denominator: int | None = None,
        evidence_ref: str | None = None,
    ) -> FeeModel:
        """Create a static fee model (zero fee is permitted)."""
        return cls(
            kind="static",
            raw_value=raw_value,
            unit=unit,
            numerator=numerator,
            denominator=denominator,
            evidence_ref=evidence_ref,
        )

    @classmethod
    def dynamic(
        cls,
        hook_ref: str | None = None,
        model_version: str | None = None,
        evidence_ref: str | None = None,
        raw_value: int | None = None,
        unit: str | None = None,
    ) -> FeeModel:
        """Create a dynamic fee model."""
        return cls(
            kind="dynamic",
            hook_ref=hook_ref,
            model_version=model_version,
            evidence_ref=evidence_ref,
            raw_value=raw_value,
            unit=unit,
        )

    @classmethod
    def unknown(cls, evidence_ref: str | None = None) -> FeeModel:
        """Create an unknown fee model."""
        return cls(kind="unknown", evidence_ref=evidence_ref)


@dataclass(frozen=True, slots=True)
class PoolDescriptor:
    """Comprehensive liquidity pool descriptor."""

    key: PoolKey
    currency0: AssetRef
    currency1: AssetRef
    fee_model: FeeModel
    tick_spacing: int | None = None
    hooks: str | None = None
    identity_evidence_refs: tuple[str, ...] = ()
    deployment_status: str = "deployed"
    underlying_pool_refs: tuple[PoolKey, ...] = ()

    def __init__(
        self,
        key: PoolKey,
        currency0: AssetRef,
        currency1: AssetRef,
        fee_model: FeeModel,
        tick_spacing: int | None = None,
        hooks: str | None = None,
        identity_evidence_refs: Sequence[str] = (),
        deployment_status: str = "deployed",
        underlying_pool_refs: Sequence[PoolKey] = (),
    ) -> None:
        if not isinstance(key, PoolKey):
            raise TypeError("key must be an instance of PoolKey")
        if not isinstance(currency0, AssetRef) or not isinstance(currency1, AssetRef):
            raise TypeError("currency0 and currency1 must be instances of AssetRef")
        if currency0 == currency1:
            raise ValueError("currency0 and currency1 must be distinct assets")
        if currency0.chain_id != key.chain_id or currency1.chain_id != key.chain_id:
            raise ValueError("Currencies must reside on the same chain as pool key")
        if not isinstance(fee_model, FeeModel):
            raise TypeError("fee_model must be an instance of FeeModel")
        if tick_spacing is not None:
            if type(tick_spacing) is not int or isinstance(tick_spacing, bool):
                raise TypeError("tick_spacing must be an integer or None")
        if hooks is not None:
            validate_evm_address(hooks)
        if deployment_status not in ("deployed", "pending", "destroyed", "unknown"):
            raise ValueError(f"Invalid deployment_status: {deployment_status!r}")

        object.__setattr__(self, "key", key)
        object.__setattr__(self, "currency0", currency0)
        object.__setattr__(self, "currency1", currency1)
        object.__setattr__(self, "fee_model", fee_model)
        object.__setattr__(self, "tick_spacing", tick_spacing)
        object.__setattr__(self, "hooks", hooks)
        object.__setattr__(self, "identity_evidence_refs", tuple(identity_evidence_refs))
        object.__setattr__(self, "deployment_status", deployment_status)
        object.__setattr__(self, "underlying_pool_refs", tuple(underlying_pool_refs))
