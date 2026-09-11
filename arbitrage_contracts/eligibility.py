"""Domain eligibility, provenance evidence, and capability contracts."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from .identity import (
    AssetRef,
    PoolKey,
    validate_non_negative_integer,
    validate_positive_integer,
)

_SHA256_HEX_REGEX = re.compile(r"^[0-9a-fA-F]{64}$")


def validate_sha256_hex(digest_value: str) -> str:
    """Validate 64-character hexadecimal SHA256 string."""
    if type(digest_value) is not str:
        raise TypeError(f"Digest must be a string, got {type(digest_value).__name__}")
    if len(digest_value) != 64 or not _SHA256_HEX_REGEX.match(digest_value):
        raise ValueError(f"Invalid SHA256 hex string: {digest_value!r}")
    return digest_value


class EvidenceLevel(StrEnum):
    """Rigid 5-step ladder of evidence credibility."""

    SPOT_CANDIDATE = "spot_candidate"
    LOCAL_QUOTE = "local_quote"
    RPC_QUOTE = "rpc_quote"
    ATOMIC_SIMULATION = "atomic_simulation"
    CONFIRMED_EXECUTION = "confirmed_execution"


@dataclass(frozen=True, slots=True)
class SourceEvidence:
    """Explicit provenance metadata for domain claims and observations."""

    evidence_id: str
    source_type: str
    source_locator: str
    raw_sha256: str | None = None
    captured_at_ms: int = 0
    chain_id: int | None = None
    block_ref: str | None = None
    request_id: str | None = None
    params_hash: str | None = None
    collector_version: str = "1.0.0"
    limitations: tuple[str, ...] = ()
    parent_refs: tuple[str, ...] = ()

    def __init__(
        self,
        evidence_id: str,
        source_type: str,
        source_locator: str,
        raw_sha256: str | None = None,
        captured_at_ms: int = 0,
        chain_id: int | None = None,
        block_ref: str | None = None,
        request_id: str | None = None,
        params_hash: str | None = None,
        collector_version: str = "1.0.0",
        limitations: Sequence[str] = (),
        parent_refs: Sequence[str] = (),
    ) -> None:
        if not evidence_id or type(evidence_id) is not str:
            raise ValueError("evidence_id must be a non-empty string")
        if not source_type or type(source_type) is not str:
            raise ValueError("source_type must be a non-empty string")
        if not source_locator or type(source_locator) is not str:
            raise ValueError("source_locator must be a non-empty string")
        if raw_sha256 is not None:
            validate_sha256_hex(raw_sha256)
        validated_captured = validate_non_negative_integer(captured_at_ms, "captured_at_ms")
        validated_chain_id = None
        if chain_id is not None:
            validated_chain_id = validate_positive_integer(chain_id, "chain_id")

        object.__setattr__(self, "evidence_id", evidence_id)
        object.__setattr__(self, "source_type", source_type)
        object.__setattr__(self, "source_locator", source_locator)
        object.__setattr__(self, "raw_sha256", raw_sha256)
        object.__setattr__(self, "captured_at_ms", validated_captured)
        object.__setattr__(self, "chain_id", validated_chain_id)
        object.__setattr__(self, "block_ref", block_ref)
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "params_hash", params_hash)
        object.__setattr__(self, "collector_version", collector_version)
        object.__setattr__(self, "limitations", tuple(limitations))
        object.__setattr__(self, "parent_refs", tuple(parent_refs))


class ReviewStatus(StrEnum):
    """Lifecycle review status for assets and markets."""

    DISCOVERED = "discovered"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVOKED = "revoked"
    UNKNOWN = "unknown"


class RestrictionStatus(StrEnum):
    """Verification state of a specific asset constraint."""

    VERIFIED_TRUE = "verified_true"
    VERIFIED_FALSE = "verified_false"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class AssetEligibility:
    """Eligibility decision and constraint profile for an economic asset."""

    asset_ref: AssetRef
    issuer_id: str | None = None
    issuance_or_bridge_version: str | None = None
    decimals_status: str = "unknown"
    decimals_evidence_ref: str | None = None
    contract_restrictions: tuple[tuple[str, str], ...] = ()
    review_status: str = "discovered"
    reviewer_ref: str | None = None
    reviewed_at_ms: int | None = None
    validity: str | None = None
    subject_scope: str = "unknown"
    evidence_refs: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    registry_revision: str | None = None

    def __init__(
        self,
        asset_ref: AssetRef,
        issuer_id: str | None = None,
        issuance_or_bridge_version: str | None = None,
        decimals_status: str = "unknown",
        decimals_evidence_ref: str | None = None,
        contract_restrictions: Mapping[str, str] | Sequence[tuple[str, str]] = (),
        review_status: str = "discovered",
        reviewer_ref: str | None = None,
        reviewed_at_ms: int | None = None,
        validity: str | None = None,
        subject_scope: str = "unknown",
        evidence_refs: Sequence[str] = (),
        reasons: Sequence[str] = (),
        registry_revision: str | None = None,
    ) -> None:
        if not isinstance(asset_ref, AssetRef):
            raise TypeError("asset_ref must be an instance of AssetRef")
        if decimals_status not in ("verified", "unsupported", "unknown"):
            raise ValueError(f"Invalid decimals_status: {decimals_status!r}")
        if review_status not in (
            "discovered",
            "pending_review",
            "approved",
            "rejected",
            "revoked",
            "unknown",
        ):
            raise ValueError(f"Invalid review_status: {review_status!r}")
        if subject_scope not in ("public", "role", "wallet_ref", "unknown"):
            raise ValueError(f"Invalid subject_scope: {subject_scope!r}")

        processed_evidence = tuple(evidence_refs)
        if review_status == "approved":
            if not reviewer_ref or type(reviewer_ref) is not str:
                raise ValueError("Approved asset must specify a non-empty reviewer_ref")
            if reviewed_at_ms is None:
                raise ValueError("Approved asset must specify reviewed_at_ms")
            validate_non_negative_integer(reviewed_at_ms, "reviewed_at_ms")
            if not processed_evidence:
                raise ValueError("Approved asset must specify at least one evidence_ref")
            if decimals_status == "unsupported":
                raise ValueError("Cannot approve asset with unsupported decimals")

        if isinstance(contract_restrictions, Mapping):
            restriction_items = sorted(contract_restrictions.items())
        else:
            restriction_items = sorted(contract_restrictions)

        validated_restrictions: list[tuple[str, str]] = []
        for restriction_name, restriction_state in restriction_items:
            resolved_state = str(restriction_state)
            if resolved_state not in ("verified_true", "verified_false", "unknown"):
                raise ValueError(
                    f"Invalid state for restriction {restriction_name!r}: {restriction_state!r}"
                )
            validated_restrictions.append((str(restriction_name), resolved_state))

        object.__setattr__(self, "asset_ref", asset_ref)
        object.__setattr__(self, "issuer_id", issuer_id)
        object.__setattr__(self, "issuance_or_bridge_version", issuance_or_bridge_version)
        object.__setattr__(self, "decimals_status", decimals_status)
        object.__setattr__(self, "decimals_evidence_ref", decimals_evidence_ref)
        object.__setattr__(self, "contract_restrictions", tuple(validated_restrictions))
        object.__setattr__(self, "review_status", review_status)
        object.__setattr__(self, "reviewer_ref", reviewer_ref)
        object.__setattr__(self, "reviewed_at_ms", reviewed_at_ms)
        object.__setattr__(self, "validity", validity)
        object.__setattr__(self, "subject_scope", subject_scope)
        object.__setattr__(self, "evidence_refs", processed_evidence)
        object.__setattr__(self, "reasons", tuple(reasons))
        object.__setattr__(self, "registry_revision", registry_revision)

    def is_approved(self) -> bool:
        """Indicate whether the asset has achieved approved status."""
        return self.review_status == "approved"

    def get_restriction(self, restriction_name: str) -> str:
        """Return restriction status or 'unknown' if not explicitly evaluated."""
        for name, status in self.contract_restrictions:
            if name == restriction_name:
                return status
        return "unknown"


class CapabilityStatus(StrEnum):
    """Support classification for an operational pool capability."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PoolCapability:
    """Operational capability profile enforcing strict separation between quote, simulation, and execution."""

    pool_key: PoolKey
    can_quote: str = "unknown"
    can_simulate: str = "unknown"
    can_atomic_execute: str = "unknown"
    evidence_refs: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    def __init__(
        self,
        pool_key: PoolKey,
        can_quote: str = "unknown",
        can_simulate: str = "unknown",
        can_atomic_execute: str = "unknown",
        evidence_refs: Sequence[str] = (),
        reasons: Sequence[str] = (),
    ) -> None:
        if not isinstance(pool_key, PoolKey):
            raise TypeError("pool_key must be an instance of PoolKey")
        for capability_name, capability_value in (
            ("can_quote", can_quote),
            ("can_simulate", can_simulate),
            ("can_atomic_execute", can_atomic_execute),
        ):
            if capability_value not in ("supported", "unsupported", "unknown"):
                raise ValueError(f"Invalid status for {capability_name}: {capability_value!r}")

        object.__setattr__(self, "pool_key", pool_key)
        object.__setattr__(self, "can_quote", can_quote)
        object.__setattr__(self, "can_simulate", can_simulate)
        object.__setattr__(self, "can_atomic_execute", can_atomic_execute)
        object.__setattr__(self, "evidence_refs", tuple(evidence_refs))
        object.__setattr__(self, "reasons", tuple(reasons))
