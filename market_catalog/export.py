"""Versioned, deterministic bootstrap snapshots referencing W0 record contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any

from arbitrage_contracts import (
    ALLOWED_DATA_MODES,
    AssetEligibility,
    ContractRecord,
    PoolCapability,
    PoolDescriptor,
    ReviewStatus,
    SourceEvidence,
    TokenKey,
    decode_record_json,
    encode_record_json,
    validate_record,
    validate_sha256_hex,
)

from .inputs import RawTokenRecord, ReviewTrust, load_inputs, load_review_manifest
from .registry import CatalogRegistry

CATALOG_SCHEMA = "w1-catalog-manifest"
CATALOG_VERSION = "1.0.0-draft"
PUBLIC_PARTITIONS = {
    "assets": "token_key",
    "eligibility": "asset_eligibility",
    "pools": "pool_descriptor",
    "capabilities": "pool_capability",
    "evidence": "source_evidence",
}
PARTITIONS = (*PUBLIC_PARTITIONS, "pending", "unresolved", "changes", "quote_curves")


def _json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON value: {value}")


def _parse(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class CatalogHeader:
    """Explicit caller provenance; hashes identify bytes and do not confer trust."""

    run_id: str
    code_hash: str
    config_hash: str
    schema_hash: str
    fixture_hash: str
    registry_revision: str
    parent_revision: str | None
    as_of: int
    data_mode: str
    source_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        for value in (self.run_id, self.registry_revision, *self.source_refs):
            if type(value) is not str or not value.strip() or value != value.strip():
                raise ValueError("Header identifiers must be non-empty strings")
        for value in (self.code_hash, self.config_hash, self.schema_hash, self.fixture_hash):
            validate_sha256_hex(value)
        if self.parent_revision is not None and (
            not isinstance(self.parent_revision, str) or not self.parent_revision.strip()
        ):
            raise ValueError("Invalid parent revision")
        if type(self.as_of) is not int or self.as_of < 0:
            raise ValueError("as_of must be non-negative Unix milliseconds")
        if self.data_mode not in ALLOWED_DATA_MODES or not self.source_refs:
            raise ValueError("Explicit data mode and source refs required")
        if len(set(self.source_refs)) != len(self.source_refs):
            raise ValueError("Duplicate source refs")
        object.__setattr__(self, "source_refs", tuple(sorted(self.source_refs)))

    def to_dict(self) -> dict[str, Any]:
        """Return only the private manifest header, without serializing domain objects."""
        return {
            "catalog_schema": CATALOG_SCHEMA,
            "version": CATALOG_VERSION,
            "run_id": self.run_id,
            "code_hash": self.code_hash,
            "config_hash": self.config_hash,
            "schema_hash": self.schema_hash,
            "fixture_hash": self.fixture_hash,
            "registry_revision": self.registry_revision,
            "parent_revision": self.parent_revision,
            "as_of": self.as_of,
            "data_mode": self.data_mode,
            "source_refs": list(self.source_refs),
        }


@dataclass(frozen=True)
class CatalogSnapshot:
    """Public records plus private unresolved locators; no execution or quote evidence."""

    header: CatalogHeader
    records: dict[str, tuple[ContractRecord, ...]]
    unresolved: tuple[dict[str, Any], ...] = ()


def _record(header: CatalogHeader, kind: str, payload: Any, **provenance: Any) -> ContractRecord:
    record = ContractRecord(
        "arbitrage-evidence",
        "1.0.0",
        kind,
        header.run_id,
        header.data_mode,
        {"source_refs": list(header.source_refs), **provenance},
        payload,
    )
    # Rebuild using W0, including eligibility, capability and evidence branches.
    return validate_record(_parse(encode_record_json(record)))


def build_snapshot(
    source: Path,
    header: CatalogHeader,
    *,
    review: Path | None = None,
    trust: ReviewTrust | None = None,
) -> CatalogSnapshot:
    """Read explicit JSONL discovery and an optional independently pinned review file."""
    raw = source.read_bytes()
    if _sha(raw) != header.fixture_hash:
        raise ValueError("Input fixture hash mismatch")
    valid: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    ids: set[str] = set()
    for line in raw.decode("utf-8").splitlines():
        if not line.strip():
            continue
        row = _parse(line)
        if not isinstance(row, dict):
            raise ValueError("Discovery row must be an object")
        record_id = row.get("record_id")
        if not isinstance(record_id, str) or not record_id.strip() or record_id in ids:
            raise ValueError("Invalid or duplicate record_id")
        ids.add(record_id)
        key = row.get("key")
        if (
            row.get("kind") == "pool"
            and isinstance(key, dict)
            and any(key.get(field) in (None, "") for field in ("venue_address", "pool_id"))
        ):
            unresolved.append(
                {"record_id": record_id, "reason": "MISSING_POOL_IDENTITY", "raw": row}
            )
        else:
            valid.append(row)
    if not valid and not unresolved:
        raise ValueError("Discovery input contains no records")
    if (review is None) != (trust is None):
        raise ValueError("Review file and out-of-band trust must be supplied together")
    if unresolved and review is not None:
        raise ValueError("Cannot apply a review snapshot to incomplete discovery identities")
    if not valid:
        return CatalogSnapshot(header, {name: () for name in PUBLIC_PARTITIONS}, tuple(unresolved))
    inputs = load_inputs(StringIO(raw.decode() if not unresolved else "\n".join(map(_json, valid))))
    domain = "synthetic" if header.data_mode == "synthetic" else "production"
    registry = CatalogRegistry(inputs, domain=domain)
    evidence: tuple[SourceEvidence, ...] = ()
    if review is not None and trust is not None:
        registry.apply_review_manifest(review, trust=trust)
        evidence = load_review_manifest(review, trust=trust).evidence
    records: dict[str, list[ContractRecord]] = {name: [] for name in PUBLIC_PARTITIONS}
    metadata = {r.asset: r for r in inputs.records if isinstance(r, RawTokenRecord)}
    for asset in registry.list_assets():
        eligibility = registry.get_asset_eligibility(asset)
        assert eligibility is not None
        info = metadata[asset]
        provenance = {
            "symbol": info.symbol,
            "decimals": info.decimals,
            "source_ref": info.source_ref,
        }
        records["eligibility"].append(
            _record(header, "asset_eligibility", eligibility, **provenance)
        )
        if asset.token_key is not None:
            records["assets"].append(_record(header, "token_key", asset.token_key))
    unresolved_keys = {pool.key for pool in registry.list_unresolved_pools()}
    for pool in registry.list_pools():
        status = registry.get_review_status(pool.key).value
        reasons: list[str] = []
        if pool.key.protocol_id == "giga-v3":
            status = ReviewStatus.PENDING_REVIEW.value
            reasons.append("AUTH_EVIDENCE_PENDING")
        if pool.key in unresolved_keys:
            reasons.append("UNRESOLVED_ASSET_ENDPOINT")
            status = ReviewStatus.PENDING_REVIEW.value
        records["pools"].append(
            _record(header, "pool_descriptor", pool, review_status=status, reasons=reasons)
        )
        records["capabilities"].append(
            _record(
                header,
                "pool_capability",
                PoolCapability(
                    pool.key,
                    can_quote="unknown",
                    can_atomic_execute="unsupported",
                    reasons=tuple(reasons) + ("BOOTSTRAP_NO_QUOTE_PROBE",),
                ),
            )
        )
    records["evidence"] = [_record(header, "source_evidence", item) for item in evidence]
    snapshot = CatalogSnapshot(
        header, {key: tuple(value) for key, value in records.items()}, tuple(unresolved)
    )
    _partition_bytes(snapshot)
    return snapshot


def _partition_bytes(snapshot: CatalogSnapshot) -> tuple[dict[str, bytes], dict[str, int]]:
    if set(snapshot.records) != set(PUBLIC_PARTITIONS):
        raise ValueError("Public partition inventory mismatch")
    output: dict[str, bytes] = {}
    pending: list[dict[str, str]] = []
    counts = {"approved": 0, "pending": 0, "rejected": 0, "unresolved": len(snapshot.unresolved)}
    assets: set[Any] = set()
    tokens: set[TokenKey] = set()
    pools: set[Any] = set()
    capabilities: set[Any] = set()
    for name, kind in PUBLIC_PARTITIONS.items():
        lines: list[str] = []
        for record in snapshot.records[name]:
            line = encode_record_json(record)
            decoded = decode_record_json(line)
            if (decoded.record_type, decoded.run_id, decoded.data_mode) != (
                kind,
                snapshot.header.run_id,
                snapshot.header.data_mode,
            ):
                raise ValueError("Record partition/run/data_mode mismatch")
            payload = decoded.payload
            status: str | None = None
            unresolved = False
            if isinstance(payload, AssetEligibility):
                if payload.asset_ref in assets:
                    raise ValueError("Duplicate asset identity")
                assets.add(payload.asset_ref)
                status = ReviewStatus(payload.review_status).value
            elif isinstance(payload, TokenKey):
                if payload in tokens:
                    raise ValueError("Duplicate token identity")
                tokens.add(payload)
            elif isinstance(payload, PoolDescriptor):
                if payload.key in pools:
                    raise ValueError("Duplicate pool identity")
                pools.add(payload.key)
                status = ReviewStatus(decoded.provenance["review_status"]).value
                if payload.key.protocol_id == "giga-v3" and (
                    status != "pending_review"
                    or "AUTH_EVIDENCE_PENDING" not in decoded.provenance.get("reasons", [])
                ):
                    raise ValueError("giga-v3 must remain AUTH_EVIDENCE_PENDING")
                unresolved = "UNRESOLVED_ASSET_ENDPOINT" in decoded.provenance.get("reasons", [])
            elif isinstance(payload, PoolCapability):
                if payload.pool_key in capabilities:
                    raise ValueError("Duplicate capability identity")
                capabilities.add(payload.pool_key)
                if (
                    payload.can_quote != "unknown"
                    or payload.can_simulate != "unknown"
                    or payload.can_atomic_execute != "unsupported"
                ):
                    raise ValueError(
                        "Bootstrap capabilities cannot claim quote, simulation, or atomic support"
                    )
            if status is not None:
                bucket = (
                    "unresolved"
                    if unresolved
                    else (
                        "approved"
                        if status == "approved"
                        else "rejected"
                        if status == "rejected"
                        else "pending"
                    )
                )
                counts[bucket] += 1
                if bucket == "pending":
                    pending.append(
                        {"partition": name, "record_sha256": _sha(line.encode()), "status": status}
                    )
            lines.append(line)
        if len(lines) != len(set(lines)):
            raise ValueError("Duplicate public record")
        output[name] = ("".join(line + "\n" for line in sorted(lines))).encode()
    if tokens != {asset.token_key for asset in assets if asset.token_key is not None}:
        raise ValueError("Asset identity/eligibility inventory mismatch")
    if pools != capabilities:
        raise ValueError("Pool/capability inventory mismatch")
    for record in snapshot.records["pools"]:
        pool = record.payload
        missing = pool.currency0 not in assets or pool.currency1 not in assets
        if missing != ("UNRESOLVED_ASSET_ENDPOINT" in record.provenance.get("reasons", [])):
            raise ValueError("Pool endpoint resolution mismatch")
    ids: set[str] = set()
    for item in snapshot.unresolved:
        if set(item) != {"record_id", "reason", "raw"} or item["reason"] != "MISSING_POOL_IDENTITY":
            raise ValueError("Invalid unresolved locator")
        row = item["raw"]
        if (
            not isinstance(row, dict)
            or row.get("kind") != "pool"
            or row.get("record_id") != item["record_id"]
        ):
            raise ValueError("Invalid unresolved pool observation")
        key = row.get("key")
        if not isinstance(key, dict) or not any(
            key.get(field) in (None, "") for field in ("venue_address", "pool_id")
        ):
            raise ValueError("Unresolved locator must have missing identity")
        if (
            not isinstance(item["record_id"], str)
            or not item["record_id"].strip()
            or item["record_id"] in ids
        ):
            raise ValueError("Invalid or duplicate unresolved id")
        ids.add(item["record_id"])
    for name, rows in (("pending", pending), ("unresolved", snapshot.unresolved)):
        output[name] = "".join(line + "\n" for line in sorted(_json(row) for row in rows)).encode()
    output["changes"] = b""
    output["quote_curves"] = b""
    return output, counts


def snapshot_files(snapshot: CatalogSnapshot) -> dict[str, bytes]:
    """Produce canonical partition bytes and a manifest with exact file hashes."""
    partitions, counts = _partition_bytes(snapshot)
    manifest = {
        "header": snapshot.header.to_dict(),
        "partitions": {
            name: {"path": f"{name}.jsonl", "sha256": _sha(data), "rows": len(data.splitlines())}
            for name, data in partitions.items()
        },
        "counts": counts,
        "coverage": {
            "scope": "explicit_input_only",
            "complete": False,
            "count_unit": "unique_asset_or_pool_plus_raw_unresolved",
            "count_rule": "unresolved_then_approved_then_rejected_else_pending; projections_not_added",
            "changes": "not_computed",
            "quote_curves": "not_probed",
        },
    }
    return {
        "catalog_manifest.json": (_json(manifest) + "\n").encode(),
        **{f"{name}.jsonl": data for name, data in partitions.items()},
    }


def write_snapshot(snapshot: CatalogSnapshot, destination: Path) -> None:
    """Write to a new or empty directory after validating the complete snapshot."""
    files = snapshot_files(snapshot)
    if destination.is_symlink() or (destination.exists() and any(destination.iterdir())):
        raise ValueError("Snapshot destination must be a new or empty directory")
    destination.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        with (destination / name).open("xb") as stream:
            stream.write(content)


def read_snapshot(source: Path) -> CatalogSnapshot:
    """Reconcile both directions and validate W0 records before returning typed data."""
    if source.is_symlink():
        raise ValueError("Snapshot symlinks are forbidden")
    expected = {"catalog_manifest.json", *(f"{name}.jsonl" for name in PARTITIONS)}
    entries = list(source.iterdir())
    if {path.name for path in entries} != expected or any(
        path.is_symlink() or not path.is_file() for path in entries
    ):
        raise ValueError("Snapshot manifest inventory mismatch")
    disk = {path.name: path.read_bytes() for path in entries}
    manifest = _parse(disk["catalog_manifest.json"].decode())
    header_data = dict(manifest["header"])
    if (
        header_data.pop("catalog_schema") != CATALOG_SCHEMA
        or header_data.pop("version") != CATALOG_VERSION
    ):
        raise ValueError("Unsupported catalog schema/version")
    header = CatalogHeader(**header_data)
    records = {
        name: tuple(
            decode_record_json(line) for line in disk[f"{name}.jsonl"].decode().splitlines()
        )
        for name in PUBLIC_PARTITIONS
    }
    unresolved = tuple(_parse(line) for line in disk["unresolved.jsonl"].decode().splitlines())
    snapshot = CatalogSnapshot(header, records, unresolved)
    # Rebuilding verifies hashes, counts, references, exact fields and canonical bytes together.
    if snapshot_files(snapshot) != disk:
        raise ValueError("Snapshot manifest/content reconciliation failed")
    return snapshot


def submission_files(root: Path) -> dict[str, str]:
    """Hash the exact W1 source, tests, explicit CLI and fixture delivery inventory."""
    paths = [root / "apps/market_catalog.py"]
    for folder in ("market_catalog", "tests/catalog", "tests/fixtures/catalog/v1"):
        directory = root / folder
        if directory.is_symlink():
            raise ValueError(f"Symlinks forbidden in submission: {directory}")
        for path in directory.rglob("*"):
            if path.is_symlink():
                raise ValueError(f"Symlinks forbidden in submission: {path}")
            if path.is_file() and "__pycache__" not in path.parts:
                paths.append(path)
    for path in paths:
        if path.is_symlink():
            raise ValueError(f"Symlinks forbidden in submission: {path}")
        if not path.is_file():
            raise ValueError("Submission requires regular files")
    return {path.relative_to(root).as_posix(): _sha(path.read_bytes()) for path in sorted(paths)}


def verify_submission(root: Path, submission: Path) -> None:
    """Reject omitted, injected, modified or ghost delivery files in either direction."""
    document = _parse(submission.read_text())
    if document != {"format": "w1-manifest-submission-v1", "files": submission_files(root)}:
        raise ValueError("Submission manifest inventory/hash mismatch")
