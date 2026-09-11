"""C06/C07/C08: explicit inputs and review authentication, with real public contracts."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any

import pytest

from arbitrage_contracts.identity import UINT256_MAX, Amount, AssetRef, TokenKey
from market_catalog.inputs import RawTokenRecord, ReviewTrust, load_inputs, load_review_manifest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/catalog/v1"


def fixture_manifest() -> dict[str, Any]:
    return json.loads((FIXTURES / "manifest.json").read_text())


def reviewed(doc: dict[str, Any]) -> tuple[io.StringIO, ReviewTrust]:
    text = json.dumps(doc)
    return io.StringIO(text), ReviewTrust(
        hashlib.sha256(text.encode()).hexdigest(), "reviewer:synthetic-w1", "synthetic", "synthetic"
    )


def first_asset() -> dict[str, Any]:
    return json.loads((FIXTURES / "synthetic_valid.jsonl").read_text().splitlines()[0])


def test_explicit_path_and_stream_json_jsonl() -> None:
    path = FIXTURES / "synthetic_valid.jsonl"
    loaded = load_inputs(path)
    assert loaded == load_inputs(io.StringIO(path.read_text()))
    assert (
        loaded.records
        == load_inputs(
            io.StringIO(json.dumps([json.loads(line) for line in path.read_text().splitlines()])),
            format="json",
        ).records
    )
    assert len(loaded.records) == 5


@pytest.mark.parametrize("address", ["0x" + "1" * 40, "0x" + "aB" * 20])
def test_address_strict_validation(address: str) -> None:
    row = first_asset()
    row["asset"]["address"] = address
    token = load_inputs(io.StringIO(json.dumps(row))).records[0]
    assert isinstance(token, RawTokenRecord)
    assert token.asset.token_key is not None
    assert token.asset.token_key.raw_address == address
    assert token.asset.token_key.address == address.lower()


@pytest.mark.parametrize(
    "address",
    [
        "0x" + "1" * 39,
        "0x" + "1" * 41,
        "0x" + "g" * 40,
        " 0x" + "1" * 40,
        "0x" + "1" * 40 + "\n",
        "0X" + "1" * 40,
        None,
        True,
        1,
    ],
)
def test_address_strict_rejection(address: Any) -> None:
    row = first_asset()
    row["asset"]["address"] = address
    with pytest.raises((ValueError, TypeError)):
        load_inputs(io.StringIO(json.dumps(row)))


@pytest.mark.parametrize("decimals", [6, 18])
@pytest.mark.parametrize("atoms", [0, 1, 12345678901234567890123456789012345, UINT256_MAX])
def test_amount_precision_invariants(decimals: int, atoms: int) -> None:
    # Both directions bind the public Amount to a different asset; no symbol-based decimals.
    for address in ("0x" + "1" * 40, "0x" + "2" * 40):
        asset = AssetRef.erc20(TokenKey(4663, address))
        original = Amount(asset, atoms, decimals)
        recovered = Amount.from_atoms_str(asset, original.to_atoms_str(), decimals)
        assert recovered == original
        assert type(recovered.atoms) is int
        assert recovered.to_atoms_str() == str(atoms)


@pytest.mark.parametrize("value", [True, False, 1.0, -1, UINT256_MAX + 1, float("nan"), "1e18"])
def test_amount_invalid_atoms(value: Any) -> None:
    with pytest.raises((ValueError, TypeError)):
        Amount(AssetRef.native(4663, "ETH"), value, 18)


@pytest.mark.parametrize(
    "value", [True, 1.0, "1e18", "-1", str(UINT256_MAX + 1), "NaN", "01", " 1"]
)
def test_amount_invalid_atom_strings(value: Any) -> None:
    with pytest.raises((ValueError, TypeError)):
        Amount.from_atoms_str(AssetRef.native(4663, "ETH"), value, 18)


@pytest.mark.parametrize("value", [True, 18.0, -1, 256, "18", None])
def test_decimals_never_guessed(value: Any) -> None:
    row = first_asset()
    row["decimals"] = value
    with pytest.raises((ValueError, TypeError)):
        load_inputs(io.StringIO(json.dumps(row)))


def test_invalid_fixture_records_rejected() -> None:
    for line in (FIXTURES / "synthetic_invalid.jsonl").read_text().splitlines():
        with pytest.raises((ValueError, TypeError)):
            load_inputs(io.StringIO(json.dumps(json.loads(line)["record"])))


@pytest.mark.parametrize("payload", ['{"kind":"asset","kind":"pool"}', "NaN", "[1]", "{}"])
def test_malformed_inputs_rejected(payload: str) -> None:
    with pytest.raises((ValueError, TypeError)):
        load_inputs(io.StringIO(payload))


def test_no_default_path_or_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CATALOG_PATH", "/must/not/be/read")
    monkeypatch.setenv("AUTO_APPROVE", "true")
    assert len(load_inputs(FIXTURES / "synthetic_valid.jsonl").records) == 5
    with pytest.raises(TypeError):
        load_inputs()  # type: ignore[call-arg]


def test_review_manifest_not_automatic() -> None:
    manifest = fixture_manifest()
    bundle = load_review_manifest(
        io.StringIO(manifest["review_manifest_text"]), trust=ReviewTrust(**manifest["review_trust"])
    )
    assert len(bundle.decisions) == 3
    assert bundle.decisions[0].status == "approved"
    with pytest.raises(ValueError, match="content hash"):
        load_review_manifest(
            io.StringIO(manifest["review_manifest_text"] + " "),
            trust=ReviewTrust(**manifest["review_trust"]),
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("reviewer_ref", ""),
        ("reviewer_ref", "reviewer:impostor"),
        ("reviewed_at_ms", 0),
        ("reviewed_at_ms", True),
        ("evidence_refs", []),
        ("evidence_refs", "ev-main"),
        ("can_quote", "true"),
        ("status", "verified"),
    ],
)
def test_review_claims_fail_closed(field: str, value: Any) -> None:
    doc = json.loads(fixture_manifest()["review_manifest_text"])
    doc["decisions"][0][field] = value
    stream, trust = reviewed(doc)
    with pytest.raises((ValueError, TypeError)):
        load_review_manifest(stream, trust=trust)


@pytest.mark.parametrize(
    "change",
    [
        "authority",
        "source",
        "domain",
        "evidence_hash",
        "evidence_mode",
        "duplicate_evidence",
        "duplicate_decision",
    ],
)
def test_review_authentication_boundaries(change: str) -> None:
    doc = json.loads(fixture_manifest()["review_manifest_text"])
    if change == "authority":
        doc["reviewer_ref"] = "reviewer:impostor"
    if change == "source":
        doc["source_type"] = "rpc"
    if change == "domain":
        doc["domain"] = "production"
    if change == "evidence_hash":
        doc["evidence"][0]["raw_text"] += " tampered"
    if change == "evidence_mode":
        doc["evidence"][0]["source_type"] = "rpc"
    if change == "duplicate_evidence":
        doc["evidence"].append(doc["evidence"][0])
    if change == "duplicate_decision":
        doc["decisions"].append(doc["decisions"][0])
    stream, trust = reviewed(doc)
    with pytest.raises(ValueError):
        load_review_manifest(stream, trust=trust)


def test_synthetic_trust_cannot_claim_production() -> None:
    with pytest.raises(ValueError, match="Source mode"):
        ReviewTrust("0" * 64, "reviewer:synthetic-w1", "production", "synthetic")
