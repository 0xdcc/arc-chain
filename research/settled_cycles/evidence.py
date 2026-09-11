"""Strict offline EvidenceBundle loading and invariant validation."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arbitrage_contracts.identity import validate_bytes32, validate_positive_integer


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    """Immutable raw on-chain evidence for exactly one transaction."""

    chain_id: int
    tx_hash: str
    block_header: Mapping[str, Any]
    transaction: Mapping[str, Any]
    receipt: Mapping[str, Any]
    trace: Mapping[str, Any] | None
    evidence_metadata: Mapping[str, Any]

    def __post_init__(self) -> None:
        validate_positive_integer(self.chain_id, "chain_id")
        object.__setattr__(self, "tx_hash", validate_bytes32(self.tx_hash))
        for field_name in ("block_header", "transaction", "receipt", "evidence_metadata"):
            if not isinstance(getattr(self, field_name), Mapping):
                raise TypeError(f"{field_name} must be a mapping")
        if self.trace is not None and not isinstance(self.trace, Mapping):
            raise TypeError("trace must be a mapping or None")
        if "source_sha256" not in self.evidence_metadata:
            raise ValueError("evidence_metadata must contain source_sha256")
        if self.receipt.get("status") not in (0, 1):
            raise ValueError("receipt.status must be 0 or 1")
        if not isinstance(self.receipt.get("logs"), list):
            raise ValueError("receipt.logs must be a list")

    @property
    def trace_available(self) -> bool:
        """Return whether an available internal-call trace was supplied."""
        return self.trace is not None and self.trace.get("status") != "unavailable"

    @classmethod
    def from_dict(cls, value: Any) -> EvidenceBundle:
        """Construct from validated mapping data."""
        if not isinstance(value, Mapping):
            raise TypeError("EvidenceBundle must be a mapping")
        required = {
            "chain_id",
            "tx_hash",
            "block_header",
            "transaction",
            "receipt",
            "trace",
            "evidence_metadata",
        }
        if set(value) != required:
            raise ValueError("EvidenceBundle keys do not match the frozen schema")
        return cls(
            chain_id=value["chain_id"],
            tx_hash=value["tx_hash"],
            block_header=value["block_header"],
            transaction=value["transaction"],
            receipt=value["receipt"],
            trace=value["trace"],
            evidence_metadata=value["evidence_metadata"],
        )


BundleResult = tuple[Path, EvidenceBundle | ValueError | TypeError]
DeduplicationResult = tuple[list[EvidenceBundle], list[dict[str, Any]]]


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def load_bundle(path: Path | str) -> EvidenceBundle:
    """Load one JSON or one-line JSONL evidence file from an explicit path."""
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"Evidence file does not exist: {source}")
    try:
        content = source.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"Unable to read evidence file: {source}") from error
    try:
        value = json.loads(content, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ValueError(f"Malformed JSON evidence: {source}") from error
    return EvidenceBundle.from_dict(value)


def load_bundle_from_directory(directory: Path | str) -> list[BundleResult]:
    """Load all JSON and JSONL files directly inside an explicit directory."""
    root = Path(directory)
    if not root.is_dir():
        raise FileNotFoundError(f"Evidence directory does not exist: {root}")
    resolved_root = root.resolve()
    results: list[BundleResult] = []
    for path in sorted((*root.glob("*.json"), *root.glob("*.jsonl"))):
        resolved_path = path.resolve()
        if resolved_path.parent != resolved_root or ".." in path.parts:
            results.append((path, ValueError("Evidence path escapes the explicit directory")))
            continue
        try:
            results.append((path, load_bundle(path)))
        except (ValueError, TypeError) as error:
            results.append((path, error))
    return results


def deduplicate_bundles(bundles: Iterable[EvidenceBundle]) -> DeduplicationResult:
    """Deduplicate by chain and transaction, retaining block-hash conflicts."""
    materialized = list(bundles)
    unique: dict[tuple[int, str], EvidenceBundle] = {}
    conflicts: list[dict[str, Any]] = []
    for bundle in materialized:
        key = (bundle.chain_id, bundle.tx_hash)
        existing = unique.get(key)
        if existing is None:
            unique[key] = bundle
            continue
        existing_block_hash = existing.block_header.get("hash")
        incoming_block_hash = bundle.block_header.get("hash")
        if existing_block_hash == incoming_block_hash:
            continue
        conflicts.append(
            {
                "chain_id": bundle.chain_id,
                "tx_hash": bundle.tx_hash,
                "block_hashes": [existing_block_hash, incoming_block_hash],
            }
        )
    return list(unique.values()), conflicts


def validate_bundle_invariants(bundle: EvidenceBundle) -> list[str]:
    """Validate cross-field receipt invariants and return reorg-log warnings."""
    warnings: list[str] = []
    receipt_hash = bundle.receipt.get("transactionHash")
    transaction_hash = bundle.transaction.get("hash")
    if receipt_hash is not None and transaction_hash is not None:
        if receipt_hash != transaction_hash:
            raise ValueError("receipt.transactionHash and transaction.hash do not match")
    log_indices: list[Any] = []
    for log_index, log in enumerate(bundle.receipt["logs"]):
        if not isinstance(log, Mapping):
            raise ValueError("Each receipt log must be a mapping")
        if "logIndex" in log:
            log_indices.append(log["logIndex"])
        if log.get("removed") is True:
            warnings.append(f"Reorged log retained at receipt position {log_index}")
    if len(log_indices) != len(set(log_indices)):
        raise ValueError("Duplicate logIndex values in receipt")
    return warnings
