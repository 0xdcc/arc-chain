"""C05/C08/C09/C10 end-to-end discovery, review and scoped catalog queries."""

from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path
from typing import Any

import pytest

from arbitrage_contracts.identity import AssetRef, TokenKey
from market_catalog import CatalogRegistry, ReviewTrust, load_inputs

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/catalog/v1"


def rows() -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in (FIXTURES / "synthetic_valid.jsonl").read_text().splitlines()
    ]


def registry(data: list[dict[str, Any]] | None = None) -> CatalogRegistry:
    source = (
        FIXTURES / "synthetic_valid.jsonl"
        if data is None
        else io.StringIO("".join(json.dumps(row) + "\n" for row in data))
    )
    return CatalogRegistry(load_inputs(source), domain="synthetic")


def review_doc() -> dict[str, Any]:
    return json.loads(json.loads((FIXTURES / "manifest.json").read_text())["review_manifest_text"])


def approve(catalog: CatalogRegistry, doc: dict[str, Any] | None = None) -> None:
    if doc is None:
        fixture = json.loads((FIXTURES / "manifest.json").read_text())
        text, trust = fixture["review_manifest_text"], ReviewTrust(**fixture["review_trust"])
    else:
        text = json.dumps(doc)
        trust = ReviewTrust(
            hashlib.sha256(text.encode()).hexdigest(),
            "reviewer:synthetic-w1",
            "synthetic",
            "synthetic",
        )
    catalog.apply_review_manifest(io.StringIO(text), trust=trust)


def eligible(catalog: CatalogRegistry, **kwargs: Any) -> list[Any]:
    return catalog.list_eligible_pools(**{"chain_id": 4663, "block": 100, "at_ms": 1200, **kwargs})


def test_token_key_no_symbol_merge() -> None:
    catalog = registry()
    weth = [
        asset
        for asset in catalog.list_assets()
        if asset.token_key and asset.token_key.address in ("0x" + f"{1:040x}", "0x" + f"{3:040x}")
    ]
    assert len(weth) == 2
    assert weth[0] != weth[1]
    assert len(catalog.list_assets()) == 4
    for asset in weth:
        assert asset.token_key is not None
        assert catalog.get_token(asset.token_key) == asset


def test_token_same_address_different_chain_and_case() -> None:
    data = rows()
    token = copy.deepcopy(data[0])
    token["record_id"] = "other-chain"
    token["asset"]["chain_id"] = 56
    data.append(token)
    catalog = registry(data)
    assert len(catalog.list_assets()) == 5
    assert catalog.get_token(TokenKey(56, token["asset"]["address"])) != catalog.get_token(
        TokenKey(4663, token["asset"]["address"])
    )
    data = rows()
    data[0]["asset"]["address"] = "0x" + "aB" * 20
    token = copy.deepcopy(data[0])
    token["record_id"] = "same-key-lowercase"
    token["asset"]["address"] = token["asset"]["address"].lower()
    data.append(token)
    catalog = registry(data)
    assert len(catalog.list_assets()) == 4
    resolved = catalog.get_token(TokenKey(4663, token["asset"]["address"]))
    assert resolved is not None and resolved.token_key is not None
    assert resolved.token_key.raw_address == "0x" + "aB" * 20


@pytest.mark.parametrize(
    "field,value",
    [("decimals", 6), ("symbol", "FAKE"), ("issuer_id", "other-issuer"), ("bridge_version", "v2")],
)
def test_same_token_key_metadata_conflict(field: str, value: Any) -> None:
    data = rows()
    token = copy.deepcopy(data[0])
    token["record_id"] = "conflicting"
    token[field] = value
    with pytest.raises(ValueError, match="Conflicting asset metadata"):
        registry([*data, token])


def test_native_never_indexed_as_erc20_or_wrapped() -> None:
    data = rows()
    native = copy.deepcopy(data[3])
    native["record_id"] = "native-other-domain"
    native["asset"]["balance_domain_id"] = "native-separate"
    catalog = registry([*data, native])
    assert len(catalog.list_assets()) == 5
    assert catalog.get_token(TokenKey(4663, "0x" + "0" * 40)) is None
    assert len([asset for asset in catalog.list_assets() if asset.interface_kind == "native"]) == 2
    assert AssetRef.native(4663, "ETH", "native") in catalog.list_pending_reviews()
    assert eligible(catalog) == []


def test_token_balance_domain_conflict_visible() -> None:
    data = rows()
    token = copy.deepcopy(data[0])
    token["record_id"] = "other-domain"
    token["asset"]["balance_domain_id"] = "another"
    with pytest.raises(ValueError, match="balance domain"):
        registry([*data, token])


def test_review_gate_requires_both_assets_and_pool() -> None:
    catalog = registry()
    assert len(catalog.list_pending_reviews()) == 5
    assert eligible(catalog) == []
    approve(catalog)
    assert len(eligible(catalog)) == 1
    assert len(catalog.list_pending_reviews()) == 2
    for subject in ("weth-a", "usdc", "pool-a"):
        doc = review_doc()
        doc["decisions"] = [d for d in doc["decisions"] if d["subject_key"] != subject]
        approve(catalog, doc)
        assert eligible(catalog) == []


@pytest.mark.parametrize(
    "status", ["revoked", "rejected", "pending_review", "discovered", "unknown"]
)
@pytest.mark.parametrize("subject", ["weth-a", "usdc", "pool-a"])
def test_nonapproved_never_eligible(status: str, subject: str) -> None:
    catalog = registry()
    doc = review_doc()
    next(d for d in doc["decisions"] if d["subject_key"] == subject)["status"] = status
    approve(catalog, doc)
    assert eligible(catalog) == []


@pytest.mark.parametrize(
    "query",
    [
        {"chain_id": 56},
        {"block": 99},
        {"block": 111},
        {"at_ms": 999},
        {"at_ms": 1099},
        {"at_ms": 2000},
        {"subject_kind": "wallet_ref", "subject_ref": "wallet:a"},
        {"subject_kind": "role", "subject_ref": "role:a"},
    ],
)
def test_eligibility_scope_boundary(query: dict[str, Any]) -> None:
    catalog = registry()
    approve(catalog)
    assert len(eligible(catalog)) == 1
    assert eligible(catalog, **query) == []
    assert len(catalog.list_identity_approved_pools()) == 1


def test_wallet_review_never_generalizes() -> None:
    catalog = registry()
    doc = review_doc()
    for decision in doc["decisions"]:
        decision["scope"].update(subject_kind="wallet_ref", subject_ref="wallet:a")
    approve(catalog, doc)
    assert len(eligible(catalog, subject_kind="wallet_ref", subject_ref="wallet:a")) == 1
    assert eligible(catalog) == []
    assert eligible(catalog, subject_kind="wallet_ref", subject_ref="wallet:b") == []


@pytest.mark.parametrize("restriction", ["tax", "pause", "rebase", "blacklist", "whitelist"])
@pytest.mark.parametrize("value", [None, "unknown", "verified_true"])
def test_unknown_or_active_restrictions_never_default_false(restriction: str, value: Any) -> None:
    catalog = registry()
    doc = review_doc()
    if value is None:
        del doc["decisions"][0]["restrictions"][restriction]
    else:
        doc["decisions"][0]["restrictions"][restriction] = value
    approve(catalog, doc)
    assert eligible(catalog) == []
    token = catalog.list_assets()[0]
    eligibility = catalog.get_asset_eligibility(token)
    assert eligibility is not None
    assert eligibility.get_restriction(restriction) == (value or "unknown")


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "wrong_chain",
        "future_capture",
        "wrong_block",
        "no_decimals",
        "unresolved_decimals",
        "quote_unknown",
    ],
)
def test_evidence_gaps_excluded(change: str) -> None:
    catalog = registry()
    doc = review_doc()
    if change == "missing":
        doc["evidence"] = []
    if change == "wrong_chain":
        doc["evidence"][0]["chain_id"] = 56
    if change == "future_capture":
        doc["evidence"][0]["captured_at_ms"] = 1200
    if change == "wrong_block":
        doc["evidence"][0]["block_ref"] = "99"
    if change == "no_decimals":
        doc["decisions"][0]["decimals_evidence_ref"] = None
    if change == "unresolved_decimals":
        doc["decisions"][0]["decimals_evidence_ref"] = "missing"
    if change == "quote_unknown":
        doc["decisions"][2]["can_quote"] = False
    approve(catalog, doc)
    assert len(catalog.list_identity_approved_pools()) == 1
    assert eligible(catalog) == []


def test_review_subject_hash_and_identity_binding_atomic() -> None:
    catalog = registry()
    approve(catalog)
    for change in ("input_hash", "unknown_subject", "wrong_chain"):
        doc = review_doc()
        if change == "input_hash":
            doc["records_sha256"] = "0" * 64
        if change == "unknown_subject":
            doc["decisions"][0]["subject_key"] = "missing"
        if change == "wrong_chain":
            doc["decisions"][0]["scope"]["chain_id"] = 56
        with pytest.raises(ValueError):
            approve(catalog, doc)
        assert len(eligible(catalog)) == 1


def test_synthetic_approval_never_usable_in_production_catalog() -> None:
    catalog = CatalogRegistry(load_inputs(FIXTURES / "synthetic_valid.jsonl"), domain="production")
    with pytest.raises(ValueError, match="catalog domain"):
        approve(catalog)
    assert eligible(catalog) == []


def test_no_atomic_execution_claim() -> None:
    catalog = registry()
    approve(catalog)
    key = catalog.list_pools()[0].key
    capability = catalog.get_pool_capability(key, chain_id=4663, block=100, at_ms=1200)
    assert capability is not None
    assert capability.can_quote == "supported"
    assert capability.can_atomic_execute == "unknown"
    assert capability.can_simulate == "unknown"


def test_pool_underlying_independence() -> None:
    data = rows()
    alias = copy.deepcopy(data[-1])
    alias.update(record_id="pool-front-b", frontend="front-b")
    catalog = registry([*data, alias])
    assert len(catalog.list_pools()) == 1
    assert catalog.get_pool(catalog.list_pools()[0].key) == catalog.list_pools()[0]
    for index, (field, value) in enumerate(
        [
            ("venue_address", "0x" + "b" * 40),
            ("protocol_id", "synthetic-v3"),
            ("venue_kind", "factory"),
            ("pool_id", "0x" + "b" * 64),
            ("chain_id", 56),
        ]
    ):
        other = copy.deepcopy(data[-1])
        other["record_id"] = f"independent-{index}"
        other["key"][field] = value
        if field == "chain_id":
            other["currency0"]["chain_id"] = other["currency1"]["chain_id"] = value
        data.append(other)
    catalog = registry(data)
    assert len(catalog.list_pools()) == 6
    assert all(catalog.get_pool(pool.key) == pool for pool in catalog.list_pools())


def test_pool_metadata_conflict_rejected() -> None:
    data = rows()
    alias = copy.deepcopy(data[-1])
    alias["record_id"] = "conflict"
    alias["fee_model"]["raw_value"] = 100
    with pytest.raises(ValueError, match="Conflicting pool metadata"):
        registry([*data, alias])


def test_unresolved_discovery_retained() -> None:
    catalog = registry([rows()[-1]])
    assert len(catalog.list_pools()) == 1
    assert len(catalog.list_unresolved_pools()) == 1
    assert len(catalog.list_pending_reviews()) == 1
    assert eligible(catalog) == []


def test_forged_loaded_records_cannot_reuse_manifest_hash() -> None:
    from dataclasses import replace

    from market_catalog.inputs import RawTokenRecord

    inputs = load_inputs(FIXTURES / "synthetic_valid.jsonl")
    token = inputs.records[0]
    assert isinstance(token, RawTokenRecord)
    forged = replace(token, asset=AssetRef.erc20(TokenKey(4663, "0x" + "f" * 40)), decimals=6)
    with pytest.raises(ValueError, match="authenticated bytes"):
        CatalogRegistry(replace(inputs, records=(forged, *inputs.records[1:])), domain="synthetic")


@pytest.mark.parametrize("value", ["unknown", "verified_true", "verified_false"])
def test_pool_restriction_gate(value: str) -> None:
    catalog = registry()
    doc = review_doc()
    doc["decisions"][2]["restrictions"] = {"pause": value}
    approve(catalog, doc)
    assert len(eligible(catalog)) == (1 if value == "verified_false" else 0)
