"""Minimal W2/W3/W4 consumers use the same strongly typed bootstrap export."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from arbitrage_contracts import AssetEligibility, PoolCapability, PoolDescriptor
from market_catalog.export import CatalogHeader, build_snapshot, read_snapshot, write_snapshot
from market_catalog.inputs import ReviewTrust

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/catalog/v1"


def test_w2_w3_w4_same_export_preserves_identity_and_gates(tmp_path: Path) -> None:
    source = FIXTURES / "synthetic_valid.jsonl"
    header = CatalogHeader(
        "consumers",
        "1" * 64,
        "2" * 64,
        "3" * 64,
        hashlib.sha256(source.read_bytes()).hexdigest(),
        "r1",
        "r0",
        1200,
        "synthetic",
        ("synthetic:discovery",),
    )
    fixture = json.loads((FIXTURES / "manifest.json").read_text())
    review = tmp_path / "review.json"
    review.write_text(fixture["review_manifest_text"])
    snapshot = build_snapshot(
        source, header, review=review, trust=ReviewTrust(**fixture["review_trust"])
    )
    write_snapshot(snapshot, tmp_path / "export")
    loaded = read_snapshot(tmp_path / "export")
    # W2: quote candidates require both identity review and measured capability.
    pools = [r.payload for r in loaded.records["pools"]]
    capabilities = {r.payload.pool_key: r.payload for r in loaded.records["capabilities"]}
    eligibility = {r.payload.asset_ref: r.payload for r in loaded.records["eligibility"]}
    assert all(isinstance(p, PoolDescriptor) for p in pools)
    assert all(isinstance(e, AssetEligibility) for e in eligibility.values())
    assert all(isinstance(c, PoolCapability) for c in capabilities.values())
    quote_candidates = [
        p
        for p in pools
        if capabilities[p.key].can_quote == "supported"
        and all(eligibility[a].review_status == "approved" for a in (p.currency0, p.currency1))
    ]
    assert quote_candidates == []
    assert any(e.review_status != "approved" for e in eligibility.values())
    # W3: exact full PoolKey, native/ERC20 balance domain and decimals status survive.
    pool = pools[0]
    assert pool.key.protocol_id == "synthetic-v4"
    assert pool.key.venue_kind == "manager"
    assert pool.key.pool_id == "0x" + "a" * 64
    assert pool.currency0 != pool.currency1
    assert eligibility[pool.currency1].decimals_status == "verified"
    assert any(a.token_key is None for a in eligibility)
    # W4: no atomic execution, and no fabricated single-hop or quote-curve evidence.
    assert all(c.can_atomic_execute == "unsupported" for c in capabilities.values())
    assert loaded.header.registry_revision == "r1" and loaded.header.parent_revision == "r0"
    assert (tmp_path / "export/quote_curves.jsonl").read_bytes() == b""


def test_nonapproved_never_enters_consumer_quote_set(tmp_path: Path) -> None:
    source = FIXTURES / "synthetic_valid.jsonl"
    header = CatalogHeader(
        "unreviewed",
        "1" * 64,
        "2" * 64,
        "3" * 64,
        hashlib.sha256(source.read_bytes()).hexdigest(),
        "r1",
        None,
        1200,
        "synthetic",
        ("synthetic:discovery",),
    )
    write_snapshot(build_snapshot(source, header), tmp_path / "export")
    loaded = read_snapshot(tmp_path / "export")
    assert all(r.payload.review_status == "pending_review" for r in loaded.records["eligibility"])
    assert [r for r in loaded.records["pools"] if r.provenance["review_status"] == "approved"] == []
