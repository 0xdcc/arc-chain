"""C20/C23 deterministic public records and fail-closed snapshot reconciliation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import is_dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from arbitrage_contracts import (
    AssetEligibility,
    PoolCapability,
    PoolDescriptor,
    SourceEvidence,
    decode_record_json,
    encode_record_json,
    validate_record,
)
from market_catalog.export import (
    CatalogHeader,
    CatalogSnapshot,
    build_snapshot,
    read_snapshot,
    snapshot_files,
    submission_files,
    verify_submission,
    write_snapshot,
)
from market_catalog.inputs import ReviewTrust, load_inputs

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/catalog/v1"


def header(source: Path) -> CatalogHeader:
    """Supply explicit synthetic provenance for a test input."""
    return CatalogHeader(
        "w1e-test",
        "1" * 64,
        "2" * 64,
        "3" * 64,
        hashlib.sha256(source.read_bytes()).hexdigest(),
        "revision-1",
        None,
        1200,
        "synthetic",
        ("synthetic:discovery",),
    )


def example(tmp_path: Path, *, reviewed: bool = False) -> CatalogSnapshot:
    """Build one real fixture snapshot, optionally using its pinned review bundle."""
    source = FIXTURES / "synthetic_valid.jsonl"
    if not reviewed:
        return build_snapshot(source, header(source))
    fixture = json.loads((FIXTURES / "manifest.json").read_text())
    review = tmp_path / "review.json"
    review.write_text(fixture["review_manifest_text"])
    return build_snapshot(
        source, header(source), review=review, trust=ReviewTrust(**fixture["review_trust"])
    )


@pytest.mark.parametrize("raw", [b"", b" \n\t\n"])
def test_empty_discovery_input_rejected(tmp_path: Path, raw: bytes) -> None:
    source = tmp_path / "empty.jsonl"
    source.write_bytes(raw)
    with pytest.raises(ValueError, match="^Discovery input contains no records$"):
        build_snapshot(source, header(source))


@pytest.mark.parametrize(
    "raw,format", [(b"", "jsonl"), (b" \n\t", "jsonl"), (b"", "json"), (b"[]", "json")]
)
def test_empty_loaded_inputs_rejected(tmp_path: Path, raw: bytes, format: str) -> None:
    source = tmp_path / "empty"
    source.write_bytes(raw)
    with pytest.raises(ValueError, match="^Discovery input contains no records$"):
        load_inputs(source, format=format)


def test_c20_export_read_export_bytes(tmp_path: Path) -> None:
    snapshot = example(tmp_path, reviewed=True)
    first, second = tmp_path / "first", tmp_path / "second"
    write_snapshot(snapshot, first)
    rebuilt = read_snapshot(first)
    write_snapshot(rebuilt, second)
    assert {p.name: p.read_bytes() for p in first.iterdir()} == {
        p.name: p.read_bytes() for p in second.iterdir()
    }
    for records in rebuilt.records.values():
        for record in records:
            assert is_dataclass(record.payload)
            assert encode_record_json(
                validate_record(json.loads(encode_record_json(record)))
            ) == encode_record_json(record)
    assert isinstance(rebuilt.records["eligibility"][0].payload, AssetEligibility)
    assert isinstance(rebuilt.records["capabilities"][0].payload, PoolCapability)
    assert isinstance(rebuilt.records["evidence"][0].payload, SourceEvidence)


def test_order_independent_records(tmp_path: Path) -> None:
    snapshot = example(tmp_path)
    reverse = replace(
        snapshot, records={key: tuple(reversed(rows)) for key, rows in snapshot.records.items()}
    )
    assert snapshot_files(snapshot) == snapshot_files(reverse)


def test_counts_are_disjoint_and_native_is_retained(tmp_path: Path) -> None:
    snapshot = example(tmp_path)
    manifest = json.loads(snapshot_files(snapshot)["catalog_manifest.json"])
    assert manifest["counts"] == {"approved": 0, "pending": 5, "rejected": 0, "unresolved": 0}
    assert manifest["partitions"]["assets"]["rows"] == 3
    assert manifest["partitions"]["eligibility"]["rows"] == 4
    assert manifest["coverage"]["complete"] is False
    assert any(r.payload.asset_ref.token_key is None for r in snapshot.records["eligibility"])
    assert snapshot_files(snapshot)["quote_curves.jsonl"] == b""
    assert snapshot_files(snapshot)["changes.jsonl"] == b""


def test_review_does_not_promote_quote_or_atomic(tmp_path: Path) -> None:
    snapshot = example(tmp_path, reviewed=True)
    assert any(r.payload.review_status == "approved" for r in snapshot.records["eligibility"])
    capability = snapshot.records["capabilities"][0].payload
    assert capability.can_quote == "unknown"
    assert capability.can_simulate == "unknown"
    assert capability.can_atomic_execute == "unsupported"


@pytest.mark.parametrize("field", ["venue_address", "pool_id"])
@pytest.mark.parametrize("value", [None, "", "absent"])
def test_missing_pool_identity_remains_raw(tmp_path: Path, field: str, value: Any) -> None:
    rows = [
        json.loads(line) for line in (FIXTURES / "synthetic_valid.jsonl").read_text().splitlines()
    ]
    if value == "absent":
        del rows[-1]["key"][field]
    else:
        rows[-1]["key"][field] = value
    source = tmp_path / "in.jsonl"
    source.write_text("\n".join(json.dumps(row) for row in rows))
    snapshot = build_snapshot(source, header(source))
    assert snapshot.records["pools"] == ()
    assert snapshot.records["capabilities"] == ()
    assert snapshot.unresolved[0]["raw"] == rows[-1]
    write_snapshot(snapshot, tmp_path / "out")
    assert read_snapshot(tmp_path / "out").unresolved == snapshot.unresolved


def test_unresolved_endpoint_has_exclusive_count(tmp_path: Path) -> None:
    rows = (FIXTURES / "synthetic_valid.jsonl").read_text().splitlines()[1:]
    source = tmp_path / "in.jsonl"
    source.write_text("\n".join(rows))
    snapshot = build_snapshot(source, header(source))
    counts = json.loads(snapshot_files(snapshot)["catalog_manifest.json"])["counts"]
    assert counts == {"approved": 0, "rejected": 0, "pending": 3, "unresolved": 1}


def test_only_unresolved_discovery_is_not_empty(tmp_path: Path) -> None:
    row = json.loads((FIXTURES / "synthetic_valid.jsonl").read_text().splitlines()[-1])
    del row["key"]["pool_id"]
    source = tmp_path / "unresolved.jsonl"
    source.write_text(json.dumps(row))
    snapshot = build_snapshot(source, header(source))
    assert all(not records for records in snapshot.records.values())
    assert snapshot.unresolved[0]["raw"] == row
    write_snapshot(snapshot, tmp_path / "out")
    assert read_snapshot(tmp_path / "out").unresolved == snapshot.unresolved


def test_giga_v3_never_approved(tmp_path: Path) -> None:
    rows = [
        json.loads(line) for line in (FIXTURES / "synthetic_valid.jsonl").read_text().splitlines()
    ]
    rows[-1]["key"]["protocol_id"] = "giga-v3"
    source = tmp_path / "in.jsonl"
    source.write_text("\n".join(json.dumps(row) for row in rows))
    fixture = json.loads((FIXTURES / "manifest.json").read_text())
    doc = json.loads(fixture["review_manifest_text"])
    doc["records_sha256"] = header(source).fixture_hash
    text = json.dumps(doc)
    review = tmp_path / "review.json"
    review.write_text(text)
    trust = ReviewTrust(
        hashlib.sha256(text.encode()).hexdigest(), "reviewer:synthetic-w1", "synthetic", "synthetic"
    )
    snapshot = build_snapshot(source, header(source), review=review, trust=trust)
    record = snapshot.records["pools"][0]
    assert record.provenance["review_status"] == "pending_review"
    assert "AUTH_EVIDENCE_PENDING" in record.provenance["reasons"]


def test_sabotage_extra_file_rejected_then_restored(tmp_path: Path) -> None:
    write_snapshot(example(tmp_path), tmp_path / "out")
    extra = tmp_path / "out/unregistered.jsonl"
    extra.write_text("{}\n")
    with pytest.raises(ValueError, match="inventory mismatch"):
        read_snapshot(tmp_path / "out")
    extra.unlink()
    assert read_snapshot(tmp_path / "out")


def test_sabotage_record_rejected_even_with_rehashed_partition(tmp_path: Path) -> None:
    out = tmp_path / "out"
    write_snapshot(example(tmp_path), out)
    original = (out / "capabilities.jsonl").read_bytes()
    row = json.loads(original)
    row["payload"]["can_quote"] = "approved"
    with pytest.raises(ValueError):
        validate_record(row)
    tampered = (json.dumps(row) + "\n").encode()
    (out / "capabilities.jsonl").write_bytes(tampered)
    manifest = json.loads((out / "catalog_manifest.json").read_text())
    manifest["partitions"]["capabilities"]["sha256"] = hashlib.sha256(tampered).hexdigest()
    (out / "catalog_manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        read_snapshot(out)
    (out / "capabilities.jsonl").write_bytes(original)
    (out / "catalog_manifest.json").write_bytes(
        snapshot_files(example(tmp_path))["catalog_manifest.json"]
    )
    assert read_snapshot(out)


@pytest.mark.parametrize(
    "mutation", ["missing", "hash", "counts", "traversal", "unknown_field", "symlink"]
)
def test_manifest_mutations_fail_closed(tmp_path: Path, mutation: str) -> None:
    out = tmp_path / "out"
    write_snapshot(example(tmp_path), out)
    manifest = json.loads((out / "catalog_manifest.json").read_text())
    if mutation == "missing":
        (out / "pending.jsonl").unlink()
    elif mutation == "hash":
        manifest["partitions"]["pools"]["sha256"] = "0" * 64
    elif mutation == "counts":
        manifest["counts"]["approved"] += 1
    elif mutation == "traversal":
        manifest["partitions"]["pools"]["path"] = "../secret"
    elif mutation == "unknown_field":
        manifest["unreviewed"] = True
    else:
        (out / "pools.jsonl").unlink()
        (out / "pools.jsonl").symlink_to(tmp_path / "secret")
    (out / "catalog_manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        read_snapshot(out)


@pytest.mark.parametrize(
    "field,value",
    [
        ("can_quote", "supported"),
        ("can_simulate", "supported"),
        ("can_atomic_execute", "supported"),
    ],
)
def test_valid_w0_capability_cannot_escape_bootstrap(
    tmp_path: Path, field: str, value: str
) -> None:
    snapshot = example(tmp_path)
    record = snapshot.records["capabilities"][0]
    forged = replace(record, payload=replace(record.payload, **{field: value}))
    with pytest.raises(ValueError, match="Bootstrap"):
        snapshot_files(replace(snapshot, records={**snapshot.records, "capabilities": (forged,)}))


def test_wrong_run_rejected(tmp_path: Path) -> None:
    snapshot = example(tmp_path)
    records = dict(snapshot.records)
    records["pools"] = (replace(records["pools"][0], run_id="other"),)
    with pytest.raises(ValueError, match="partition/run"):
        snapshot_files(replace(snapshot, records=records))


def test_duplicate_identity_rejected(tmp_path: Path) -> None:
    snapshot = example(tmp_path)
    records = dict(snapshot.records)
    records["pools"] *= 2
    with pytest.raises(ValueError, match="Duplicate pool"):
        snapshot_files(replace(snapshot, records=records))


def test_input_hash_and_unpinned_review_rejected(tmp_path: Path) -> None:
    source = FIXTURES / "synthetic_valid.jsonl"
    with pytest.raises(ValueError, match="fixture hash"):
        build_snapshot(source, replace(header(source), fixture_hash="0" * 64))
    with pytest.raises(ValueError, match="supplied together"):
        build_snapshot(source, header(source), review=tmp_path / "review")


def test_existing_export_is_not_overwritten(tmp_path: Path) -> None:
    out = tmp_path / "out"
    snapshot = example(tmp_path)
    write_snapshot(snapshot, out)
    with pytest.raises(ValueError, match="new or empty"):
        write_snapshot(snapshot, out)
    assert read_snapshot(out)


def test_submission_bidirectional_hash_inventory(tmp_path: Path) -> None:
    root = tmp_path / "source"
    for name in (
        "market_catalog/a.py",
        "apps/market_catalog.py",
        "tests/catalog/test_a.py",
        "tests/fixtures/catalog/v1/manifest.json",
    ):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
    submission = tmp_path / "submission.json"
    submission.write_text(
        json.dumps({"format": "w1-manifest-submission-v1", "files": submission_files(root)})
    )
    verify_submission(root, submission)
    extra = root / "market_catalog/unregistered.py"
    extra.write_text("# injected\n")
    with pytest.raises(ValueError, match="inventory/hash"):
        verify_submission(root, submission)
    extra.unlink()
    verify_submission(root, submission)
    target = root / "market_catalog/a.py"
    target.write_text("changed")
    with pytest.raises(ValueError, match="inventory/hash"):
        verify_submission(root, submission)
    target.unlink()
    with pytest.raises(ValueError, match="inventory/hash"):
        verify_submission(root, submission)


@pytest.mark.parametrize("folder", ["market_catalog", "tests/catalog", "tests/fixtures/catalog/v1"])
def test_submission_directory_symlink_rejected(tmp_path: Path, folder: str) -> None:
    root = tmp_path / "source"
    for name in (
        "market_catalog/a.py",
        "apps/market_catalog.py",
        "tests/catalog/test_a.py",
        "tests/fixtures/catalog/v1/manifest.json",
    ):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
    before = submission_files(root)
    submission = tmp_path / "submission.json"
    submission.write_text(json.dumps({"format": "w1-manifest-submission-v1", "files": before}))
    verify_submission(root, submission)
    external = tmp_path / "external"
    external.mkdir()
    (external / "unregistered.py").write_text("# synthetic external file\n")
    link = root / folder / "external-directory"
    link.symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match="Symlinks forbidden in submission:"):
        submission_files(root)
    with pytest.raises(ValueError, match="Symlinks forbidden in submission:"):
        verify_submission(root, submission)
    link.unlink()
    assert submission_files(root) == before
    verify_submission(root, submission)
