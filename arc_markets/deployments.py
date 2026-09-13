"""Arc Chain Deployment Registry and Review Lifecycle Contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from arc_readiness.errors import ArcNetworkMismatchError, ArcValidationError

_HEX_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class ContractRole(StrEnum):
    FACTORY = "factory"
    MANAGER = "manager"
    ROUTER = "router"
    QUOTER = "quoter"
    MULTICALL = "multicall"
    CUSTOM = "custom"


class DeploymentStatus(StrEnum):
    PENDING_REVIEW = "pending_review"
    VERIFIED = "verified"
    REVOKED = "revoked"


@dataclass(frozen=True)
class ArcDeploymentRecord:
    """Immutable record of an Arc protocol contract deployment and its review evidence."""

    name: str
    role: ContractRole
    address: str
    chain_id: int
    abi_hash: str
    source_proof: str
    implementation_address: str | None = None
    valid_from_block: int = 0
    valid_to_block: int | None = None
    status: DeploymentStatus = DeploymentStatus.PENDING_REVIEW
    is_system_precompile: bool = False

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ArcValidationError("Deployment name cannot be empty")

        if self.chain_id not in (5042, 5042002):
            raise ArcNetworkMismatchError(
                f"Invalid chain_id {self.chain_id}: must be 5042 (mainnet) or 5042002 (testnet)"
            )

        if not _HEX_ADDR_RE.match(self.address):
            raise ArcValidationError(f"Invalid 42-char hex contract address: {self.address}")

        if self.implementation_address is not None:
            if not _HEX_ADDR_RE.match(self.implementation_address):
                raise ArcValidationError(
                    f"Invalid 42-char hex implementation_address: {self.implementation_address}"
                )

        if not _SHA256_HEX_RE.match(self.abi_hash):
            raise ArcValidationError(f"abi_hash must be a 64-char sha256 hex string, got {self.abi_hash}")

        if not self.source_proof or not self.source_proof.strip():
            raise ArcValidationError("source_proof cannot be empty")

        if self.valid_from_block < 0:
            raise ArcValidationError(f"valid_from_block cannot be negative: {self.valid_from_block}")

        if self.valid_to_block is not None and self.valid_to_block < self.valid_from_block:
            raise ArcValidationError(
                f"valid_to_block ({self.valid_to_block}) cannot be less than valid_from_block ({self.valid_from_block})"
            )

        # System precompiles require explicit proof, not merely non-empty code
        if self.is_system_precompile:
            if not self.source_proof.startswith(("genesis:", "spec:", "consensus:")):
                raise ArcValidationError(
                    f"System precompile/pre-deploy '{self.name}' requires explicit genesis/spec/consensus proof, "
                    f"got: {self.source_proof}"
                )


class DeploymentsRegistry:
    """Registry managing discovered, reviewed, and verified Arc contract deployments.

    Invariants:
    - Default verified set is strictly EMPTY upon initialization.
    - Zero mixing of Robinhood (4663) or other foreign chain addresses into Arc (5042/5042002).
    - Prevents alias collisions and identical frontend re-registrations.
    """

    def __init__(self, target_chain_id: int = 5042) -> None:
        if target_chain_id not in (5042, 5042002):
            raise ArcNetworkMismatchError(f"Unsupported Arc target chain_id: {target_chain_id}")
        self.target_chain_id = target_chain_id
        self._records_by_name: dict[str, ArcDeploymentRecord] = {}
        self._records_by_address: dict[str, ArcDeploymentRecord] = {}
        self._alias_registry: dict[str, str] = {}  # alias -> canonical_name

    def register(self, record: ArcDeploymentRecord, aliases: tuple[str, ...] = ()) -> None:
        """Register a new deployment record under PENDING_REVIEW or explicit state."""
        if record.chain_id != self.target_chain_id:
            raise ArcNetworkMismatchError(
                f"Cannot register deployment for chain {record.chain_id} in registry for chain {self.target_chain_id}. "
                "Cross-network conflation strictly forbidden."
            )

        norm_addr = record.address.lower()
        if norm_addr in self._records_by_address:
            existing = self._records_by_address[norm_addr]
            if existing.name != record.name or existing.role != record.role:
                raise ArcValidationError(
                    f"Address collision: {norm_addr} already registered as '{existing.name}' ({existing.role}), "
                    f"cannot re-register as '{record.name}' ({record.role})."
                )

        if record.name in self._records_by_name:
            raise ArcValidationError(f"Deployment name '{record.name}' already registered.")

        # Check aliases to prevent frontend pool/venue duplicates
        for alias in aliases:
            norm_alias = alias.strip().lower()
            if norm_alias in self._alias_registry:
                raise ArcValidationError(
                    f"Alias collision: '{alias}' already mapped to '{self._alias_registry[norm_alias]}'. "
                    "Cannot register duplicate venue alias as a separate venue."
                )

        self._records_by_name[record.name] = record
        self._records_by_address[norm_addr] = record
        for alias in aliases:
            self._alias_registry[alias.strip().lower()] = record.name

    def promote_to_verified(
        self,
        name: str,
        audit_evidence_ref: str,
    ) -> None:
        """Promote a pending deployment to VERIFIED following explicit independent review."""
        if not audit_evidence_ref or not audit_evidence_ref.strip():
            raise ArcValidationError("Promotion to VERIFIED requires an explicit audit_evidence_ref.")

        if name not in self._records_by_name:
            raise ArcValidationError(f"Deployment '{name}' not found.")

        current = self._records_by_name[name]
        if current.status == DeploymentStatus.REVOKED:
            raise ArcValidationError(f"Cannot verify revoked deployment: '{name}'.")

        # Create updated immutable record
        promoted = ArcDeploymentRecord(
            name=current.name,
            role=current.role,
            address=current.address,
            chain_id=current.chain_id,
            abi_hash=current.abi_hash,
            source_proof=f"{current.source_proof}; audit:{audit_evidence_ref}",
            implementation_address=current.implementation_address,
            valid_from_block=current.valid_from_block,
            valid_to_block=current.valid_to_block,
            status=DeploymentStatus.VERIFIED,
            is_system_precompile=current.is_system_precompile,
        )
        self._records_by_name[name] = promoted
        self._records_by_address[current.address.lower()] = promoted

    def get_verified(self, name_or_address: str) -> ArcDeploymentRecord | None:
        """Retrieve a deployment ONLY if its status is VERIFIED."""
        rec = self._records_by_name.get(name_or_address)
        if rec is None:
            rec = self._records_by_address.get(name_or_address.lower())

        if rec is not None and rec.status == DeploymentStatus.VERIFIED:
            return rec
        return None

    def get_any(self, name_or_address: str) -> ArcDeploymentRecord | None:
        """Retrieve a deployment regardless of review status (e.g. for discovery inspections)."""
        rec = self._records_by_name.get(name_or_address)
        if rec is None:
            rec = self._records_by_address.get(name_or_address.lower())
        return rec

    def list_all(self) -> list[ArcDeploymentRecord]:
        return list(self._records_by_name.values())

    def list_verified(self) -> list[ArcDeploymentRecord]:
        return [r for r in self._records_by_name.values() if r.status == DeploymentStatus.VERIFIED]
