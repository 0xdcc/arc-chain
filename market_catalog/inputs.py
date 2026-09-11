"""Explicit offline input parsing; fixture formats are private to W1, not public schemas."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from arbitrage_contracts.eligibility import (
    RestrictionStatus,
    ReviewStatus,
    SourceEvidence,
    validate_sha256_hex,
)
from arbitrage_contracts.identity import (
    Amount,
    AssetRef,
    FeeModel,
    PoolDescriptor,
    PoolKey,
    TokenKey,
    validate_non_negative_integer,
    validate_positive_integer,
)


@dataclass(frozen=True)
class RawTokenRecord:
    """Discovery metadata, never an authorization claim."""

    record_id: str
    asset: AssetRef
    symbol: str
    decimals: int
    source_ref: str
    issuer_id: str | None = None
    bridge_version: str | None = None


@dataclass(frozen=True)
class RawPoolRecord:
    """One pool observation; frontend labels do not identify liquidity."""

    record_id: str
    pool: PoolDescriptor
    source_ref: str
    frontend: str


@dataclass(frozen=True)
class LoadedInputs:
    """Parsed discovery records bound to the exact input bytes."""

    records: tuple[RawTokenRecord | RawPoolRecord, ...]
    sha256: str
    raw_bytes: bytes
    format: str


@dataclass(frozen=True)
class ReviewTrust:
    """Caller-supplied, out-of-band trust anchor; never inferred from input claims."""

    manifest_sha256: str
    reviewer_ref: str
    domain: str
    source_type: str

    def __post_init__(self) -> None:
        validate_sha256_hex(self.manifest_sha256)
        _reviewer(self.reviewer_ref)
        _mode(self.domain, self.source_type)


@dataclass(frozen=True)
class ReviewScope:
    """Internal, bounded review applicability; not a public quote envelope."""

    domain: str
    chain_id: int
    block_from: int
    block_to: int
    valid_from_ms: int
    valid_until_ms: int
    subject_kind: str
    subject_ref: str | None

    def __post_init__(self) -> None:
        _text(self.domain)
        validate_positive_integer(self.chain_id, "chain_id")
        for name in ("block_from", "block_to", "valid_from_ms", "valid_until_ms"):
            validate_non_negative_integer(getattr(self, name), name)
        if self.block_to < self.block_from or self.valid_until_ms <= self.valid_from_ms:
            raise ValueError("Review scope must have ordered block/time bounds")
        if self.subject_kind not in ("public", "wallet_ref", "role"):
            raise ValueError("Unsupported subject scope")
        if self.subject_kind == "public":
            if self.subject_ref is not None:
                raise ValueError("Public scope must not contain a subject_ref")
        else:
            _text(self.subject_ref)

    def applies(
        self,
        *,
        domain: str,
        chain_id: int,
        block: int,
        at_ms: int,
        subject_kind: str,
        subject_ref: str | None,
    ) -> bool:
        """Require exact actor scope, chain/domain and bounded block/time."""
        return (
            self.domain == domain
            and self.chain_id == chain_id
            and self.block_from <= block <= self.block_to
            and self.valid_from_ms <= at_ms < self.valid_until_ms
            and self.subject_kind == subject_kind
            and self.subject_ref == subject_ref
        )


@dataclass(frozen=True)
class RawReviewDecision:
    """Validated review record; only a pinned manifest may authorize its use."""

    subject_key: str
    status: ReviewStatus
    reviewer_ref: str
    reviewed_at_ms: int
    evidence_refs: tuple[str, ...]
    scope: ReviewScope
    restrictions: tuple[tuple[str, str], ...]
    decimals_evidence_ref: str | None
    can_quote: bool


@dataclass(frozen=True)
class ReviewedManifest:
    """Content-verified local review bundle; no implicit trust store or network."""

    decisions: tuple[RawReviewDecision, ...]
    evidence: tuple[SourceEvidence, ...]
    records_sha256: str


def _text(value: Any) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError("Expected a non-empty string without surrounding whitespace")
    return value


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def _fields(
    data: dict[str, Any], required: set[str], optional: set[str] | frozenset[str] = frozenset()
) -> None:
    if not required <= data.keys() or data.keys() - required - optional:
        raise ValueError("Missing or unsupported fields")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON member")
        result[key] = value
    return result


def _json(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_pairs, parse_constant=_reject_constant)


def _reject_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON constant: {value}")


def _bytes(source: Path | TextIO) -> bytes:
    if isinstance(source, Path):
        return source.read_bytes()
    return source.read().encode("utf-8")


def parse_asset(data: dict[str, Any]) -> AssetRef:
    """Use the public ERC20/native constructors without address repair."""
    common = {"kind", "chain_id", "balance_domain_id"}
    if data.get("kind") == "erc20":
        _fields(data, common | {"address"})
        return AssetRef.erc20(
            TokenKey(data["chain_id"], data["address"]), _text(data["balance_domain_id"])
        )
    if data.get("kind") == "native":
        _fields(data, common | {"native_identifier"})
        return AssetRef.native(
            data["chain_id"], _text(data["native_identifier"]), _text(data["balance_domain_id"])
        )
    raise ValueError("Unknown asset kind")


def _pool_key(data: dict[str, Any]) -> PoolKey:
    _fields(
        data, {"chain_id", "protocol_id", "venue_kind", "venue_address", "pool_id_kind", "pool_id"}
    )
    return PoolKey(**data)


def load_inputs(source: Path | TextIO, *, format: str = "jsonl") -> LoadedInputs:
    """Load explicit discovery input; self-declared approval is an unsupported field."""
    raw = _bytes(source)
    if not raw:
        raise ValueError("Discovery input contains no records")
    if format == "jsonl":
        rows = [_json(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    elif format == "json":
        rows = _json(raw.decode("utf-8"))
        if not isinstance(rows, list):
            raise ValueError("JSON discovery input must be an array")
    else:
        raise ValueError("Unsupported input format")
    records: list[RawTokenRecord | RawPoolRecord] = []
    ids: set[str] = set()
    for item in rows:
        row = _object(item)
        common = {"kind", "record_id", "source_ref"}
        record_id, source_ref = _text(row.get("record_id")), _text(row.get("source_ref"))
        if record_id in ids:
            raise ValueError("Duplicate record_id")
        ids.add(record_id)
        if row.get("kind") == "asset":
            _fields(row, common | {"asset", "symbol", "decimals"}, {"issuer_id", "bridge_version"})
            asset = parse_asset(_object(row["asset"]))
            # The sole precision validator is the W0 Amount contract; no fallback to 18.
            Amount(asset, 0, row["decimals"])
            records.append(
                RawTokenRecord(
                    record_id,
                    asset,
                    _text(row["symbol"]),
                    row["decimals"],
                    source_ref,
                    row.get("issuer_id"),
                    row.get("bridge_version"),
                )
            )
        elif row.get("kind") == "pool":
            _fields(
                row,
                common | {"key", "currency0", "currency1", "fee_model", "frontend"},
                {"identity_evidence_refs", "deployment_status", "tick_spacing", "hooks"},
            )
            fee = _object(row["fee_model"])
            _fields(
                fee,
                {"kind"},
                {
                    "raw_value",
                    "unit",
                    "numerator",
                    "denominator",
                    "hook_ref",
                    "model_version",
                    "evidence_ref",
                },
            )
            pool = PoolDescriptor(
                _pool_key(_object(row["key"])),
                parse_asset(_object(row["currency0"])),
                parse_asset(_object(row["currency1"])),
                FeeModel(**fee),
                tick_spacing=row.get("tick_spacing"),
                hooks=row.get("hooks"),
                identity_evidence_refs=_refs(row.get("identity_evidence_refs", [])),
                deployment_status=row.get("deployment_status", "unknown"),
            )
            records.append(RawPoolRecord(record_id, pool, source_ref, _text(row["frontend"])))
        else:
            raise ValueError("Unsupported discovery record kind")
    if not records:
        raise ValueError("Discovery input contains no records")
    return LoadedInputs(tuple(records), hashlib.sha256(raw).hexdigest(), raw, format)


def _refs(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("Evidence refs must be an array")
    return tuple(_text(item) for item in value)


def _reviewer(value: str) -> None:
    if type(value) is not str or re.fullmatch(r"reviewer:[A-Za-z0-9_.-]+", value) is None:
        raise ValueError("Invalid reviewer identity")


def _mode(domain: str, source_type: str) -> None:
    if (domain, source_type) not in (
        ("synthetic", "synthetic"),
        ("production", "reviewed_snapshot"),
    ):
        raise ValueError("Source mode does not match trust domain")


def load_review_manifest(source: Path | TextIO, *, trust: ReviewTrust) -> ReviewedManifest:
    """Authenticate exact content against an explicitly supplied trust anchor."""
    raw = _bytes(source)
    if hashlib.sha256(raw).hexdigest() != trust.manifest_sha256:
        raise ValueError("Review manifest content hash mismatch")
    doc = _object(_json(raw.decode("utf-8")))
    _fields(
        doc,
        {
            "format",
            "domain",
            "source_type",
            "reviewer_ref",
            "records_sha256",
            "evidence",
            "decisions",
        },
    )
    if (
        doc["format"] != "w1-review-internal-v1"
        or doc["domain"] != trust.domain
        or doc["source_type"] != trust.source_type
        or doc["reviewer_ref"] != trust.reviewer_ref
    ):
        raise ValueError("Review authority/domain mismatch")
    validate_sha256_hex(doc["records_sha256"])
    evidence: list[SourceEvidence] = []
    if not isinstance(doc["evidence"], list) or not isinstance(doc["decisions"], list):
        raise ValueError("Evidence and decisions must be arrays")
    for item in doc["evidence"]:
        row = _object(item)
        _fields(
            row,
            {
                "evidence_id",
                "source_type",
                "source_locator",
                "raw_sha256",
                "raw_text",
                "captured_at_ms",
                "chain_id",
                "block_ref",
            },
        )
        if row["source_type"] != trust.source_type:
            raise ValueError("Evidence source mode mismatch")
        if hashlib.sha256(_text(row["raw_text"]).encode()).hexdigest() != row["raw_sha256"]:
            raise ValueError("Evidence content hash mismatch")
        evidence.append(
            SourceEvidence(**{key: value for key, value in row.items() if key != "raw_text"})
        )
    if len({e.evidence_id for e in evidence}) != len(evidence):
        raise ValueError("Duplicate evidence_id")
    decisions: list[RawReviewDecision] = []
    for item in doc["decisions"]:
        row = _object(item)
        _fields(
            row,
            {
                "subject_key",
                "status",
                "reviewer_ref",
                "reviewed_at_ms",
                "evidence_refs",
                "scope",
                "restrictions",
                "decimals_evidence_ref",
                "can_quote",
            },
        )
        _reviewer(row["reviewer_ref"])
        if row["reviewer_ref"] != trust.reviewer_ref:
            raise ValueError("Decision reviewer mismatch")
        validate_positive_integer(row["reviewed_at_ms"], "reviewed_at_ms")
        status = ReviewStatus(row["status"])
        refs = _refs(row["evidence_refs"])
        if status == ReviewStatus.APPROVED and not refs:
            raise ValueError("Approved decision requires evidence")
        scope = ReviewScope(**_object(row["scope"]))
        if scope.domain != trust.domain:
            raise ValueError("Decision domain mismatch")
        restrictions = tuple(
            sorted(
                (name, RestrictionStatus(value).value)
                for name, value in _object(row["restrictions"]).items()
            )
        )
        if type(row["can_quote"]) is not bool:
            raise ValueError("can_quote must be boolean")
        decimals_ref = row["decimals_evidence_ref"]
        if decimals_ref is not None:
            decimals_ref = _text(decimals_ref)
        decisions.append(
            RawReviewDecision(
                _text(row["subject_key"]),
                status,
                row["reviewer_ref"],
                row["reviewed_at_ms"],
                refs,
                scope,
                restrictions,
                decimals_ref,
                row["can_quote"],
            )
        )
    if len({d.subject_key for d in decisions}) != len(decisions):
        raise ValueError("Duplicate review subject")
    return ReviewedManifest(tuple(decisions), tuple(evidence), doc["records_sha256"])
