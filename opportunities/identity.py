"""Identity derivation for W2 observation inputs.

The D3 hash is used only inside the W2 ledger until W0 signs off a formal
cross-window identity exchange.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from arbitrage_contracts.identity import Amount, AssetRef
from arbitrage_contracts.opportunity import compute_observation_id

_ATOMS_REGEX = re.compile(r"^(0|[1-9][0-9]*)$")
_SOURCE_HASH_NAMESPACE = "w2-observation-input-v1"
_REQUIRED_FIELDS = (
    "route_id",
    "base_asset",
    "amount_atoms",
    "decimals_evidence_ref",
    "stream_id",
    "source_record_id",
    "registry_semantic_revision",
    "policy_version",
)


def _reject_float(value: object) -> None:
    """Reject JSON numeric values that cannot preserve exact identity."""
    if isinstance(value, float):
        raise ValueError("identity inputs must not contain floating point values")
    if isinstance(value, dict):
        for item in value.values():
            _reject_float(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_float(item)


def _require_text(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class ObservationInput:
    """Strict input tuple used to derive an observation identity."""

    route_id: str
    base_asset: AssetRef
    amount_atoms: str
    decimals_evidence_ref: str
    stream_id: str
    source_record_id: str
    registry_semantic_revision: str
    policy_version: str

    def __post_init__(self) -> None:
        for field_name in _REQUIRED_FIELDS:
            if field_name == "base_asset":
                if not isinstance(self.base_asset, AssetRef):
                    raise ValueError("base_asset must be an AssetRef")
            else:
                _require_text(getattr(self, field_name), field_name)
        if type(self.amount_atoms) is not str or not _ATOMS_REGEX.fullmatch(self.amount_atoms):
            raise ValueError("amount_atoms must be a strict decimal string")


def canonical_asset_ref(asset_ref: AssetRef) -> dict[str, str | int | None]:
    """Serialize an asset reference with the W0 contract's canonical fields."""
    return {
        "address": asset_ref.token_key.address
        if asset_ref.token_key
        else asset_ref.native_identifier,
        "balance_domain_id": asset_ref.balance_domain_id,
        "chain_id": asset_ref.chain_id,
        "interface_kind": str(asset_ref.interface_kind),
    }


def derive_source_hash(observation_input: ObservationInput) -> str:
    """Derive the D3 source hash for an observation input.

    This internal identity is pending W0 confirmation and is not exchanged
    across windows.
    """
    # D3-PENDING-W0-CONFIRMATION: formal cross-window exchange requires W0 sign-off.
    payload = {
        "namespace": _SOURCE_HASH_NAMESPACE,
        "route_id": observation_input.route_id,
        "base_asset": canonical_asset_ref(observation_input.base_asset),
        "amount_atoms": observation_input.amount_atoms,
        "decimals_evidence_ref": observation_input.decimals_evidence_ref,
        "stream_id": observation_input.stream_id,
        "source_record_id": observation_input.source_record_id,
        "registry_semantic_revision": observation_input.registry_semantic_revision,
        "policy_version": observation_input.policy_version,
    }
    _reject_float(payload)
    canonical_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def derive_observation_id(
    observation_input: ObservationInput,
    run_id: str,
    state_version_ref: str,
    amount_in: Amount,
) -> str:
    """Derive an observation ID with the existing W0 contract algorithm."""
    _require_text(run_id, "run_id")
    _require_text(state_version_ref, "state_version_ref")
    if not isinstance(amount_in, Amount):
        raise ValueError("amount_in must be an Amount")
    if amount_in.atoms != int(observation_input.amount_atoms):
        raise ValueError("amount_in.atoms must match amount_atoms")
    return compute_observation_id(
        run_id=run_id,
        source_seq_or_hash=derive_source_hash(observation_input),
        state_version_ref=state_version_ref,
        amount_in=amount_in,
    )
