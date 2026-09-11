"""Tests for D3 observation identity derivation."""

from __future__ import annotations

import hashlib
import json

import pytest

from arbitrage_contracts.identity import Amount, AssetRef, TokenKey
from opportunities.identity import ObservationInput, derive_observation_id, derive_source_hash

CHAIN_ID = 4663
TOKEN = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"


def _asset_ref() -> AssetRef:
    return AssetRef.erc20(TokenKey(CHAIN_ID, TOKEN))


def _input(route_id: str = "route-a", source_record_id: str = "source-1") -> ObservationInput:
    return ObservationInput(
        route_id=route_id,
        base_asset=_asset_ref(),
        amount_atoms="1000000",
        decimals_evidence_ref="test:decimals",
        stream_id="stream",
        source_record_id=source_record_id,
        registry_semantic_revision="v1",
        policy_version="episode-policy-v1",
    )


def _amount() -> Amount:
    return Amount.from_atoms_str(_asset_ref(), "1000000", 6, "test:decimals")


def test_source_hash_is_canonical_and_fails_closed_on_missing_fields() -> None:
    expected_payload = {
        "namespace": "w2-observation-input-v1",
        "route_id": "route-a",
        "base_asset": {
            "address": TOKEN,
            "balance_domain_id": None,
            "chain_id": CHAIN_ID,
            "interface_kind": "erc20",
        },
        "amount_atoms": "1000000",
        "decimals_evidence_ref": "test:decimals",
        "stream_id": "stream",
        "source_record_id": "source-1",
        "registry_semantic_revision": "v1",
        "policy_version": "episode-policy-v1",
    }
    encoded = json.dumps(expected_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert derive_source_hash(_input()) == hashlib.sha256(encoded).hexdigest()
    with pytest.raises(ValueError, match="route_id"):
        ObservationInput(
            route_id="",
            base_asset=_asset_ref(),
            amount_atoms="1000000",
            decimals_evidence_ref="test",
            stream_id="stream",
            source_record_id="source",
            registry_semantic_revision="v1",
            policy_version="v1",
        )


def test_shared_source_record_across_routes_does_not_collide() -> None:
    route_one = derive_observation_id(_input("route-a"), "run", "state-a", _amount())
    route_two = derive_observation_id(_input("route-b"), "run", "state-b", _amount())
    assert route_one != route_two


def test_observation_id_uses_existing_contract_and_rejects_amount_mismatch() -> None:
    identity = derive_observation_id(_input(), "run", "state", _amount())
    assert identity == derive_observation_id(_input(), "run", "state", _amount())
    other_amount = Amount.from_atoms_str(_asset_ref(), "2000000", 6, "test:decimals")
    with pytest.raises(ValueError, match="amount_in.atoms"):
        derive_observation_id(_input(), "run", "state", other_amount)
