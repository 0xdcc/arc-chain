"""Strict W1 registry validation for explicit W2 shadow inputs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arbitrage_contracts.eligibility import AssetEligibility, PoolCapability
from arbitrage_contracts.identity import AssetRef, PoolDescriptor, PoolKey, TokenKey
from arbitrage_contracts.serialization import decode_record_json


class InputGateError(ValueError):
    """Raised when a registry snapshot has an invalid schema or integrity."""


@dataclass(frozen=True, slots=True)
class ValidatedRegistry:
    """Canonical registry objects and the exact eligibility reasons retained for audit."""

    data_mode: str
    registry_semantic_revision: str
    assets: dict[AssetRef, AssetEligibility]
    asset_decimals: dict[AssetRef, int]
    pools: dict[PoolKey, PoolCapability]
    rejection_reasons: dict[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class ValidatedCatalog:
    """Eligible W1 bootstrap catalog objects and retained rejection attribution."""

    data_mode: str
    registry_semantic_revision: str
    assets: dict[AssetRef, AssetEligibility]
    asset_decimals: dict[AssetRef, int]
    descriptors: dict[PoolKey, PoolDescriptor]
    pools: dict[PoolKey, PoolCapability]
    rejection_reasons: dict[str, tuple[str, ...]]


def _mapping(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InputGateError(f"{field_name} must be an object")
    return value


def _text(value: Any, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise InputGateError(f"{field_name} must be a non-empty string")
    return value


def _asset(raw: Any, field_name: str) -> AssetRef:
    item = _mapping(raw, field_name)
    chain_id = item.get("chain_id")
    if type(chain_id) is not int or chain_id <= 0:
        raise InputGateError(f"{field_name}.chain_id must be a positive integer")
    address = _text(item.get("address"), f"{field_name}.address")
    return AssetRef.erc20(TokenKey(chain_id, address))


def _pool_key(raw: Any, field_name: str) -> PoolKey:
    item = _mapping(raw, field_name)
    chain_id = item.get("chain_id")
    if type(chain_id) is not int or chain_id <= 0:
        raise InputGateError(f"{field_name}.chain_id must be a positive integer")
    return PoolKey(
        chain_id,
        _text(item.get("protocol_id"), f"{field_name}.protocol_id"),
        _text(item.get("venue_kind"), f"{field_name}.venue_kind"),
        _text(item.get("venue_address"), f"{field_name}.venue_address"),
        _text(item.get("pool_id_kind"), f"{field_name}.pool_id_kind"),
        _text(item.get("pool_id"), f"{field_name}.pool_id"),
    )


def _eligible_asset(raw: Any) -> AssetEligibility:
    item = _mapping(raw, "asset")
    return AssetEligibility(
        asset_ref=_asset(item, "asset"),
        decimals_status=_text(item.get("decimals_status"), "asset.decimals_status"),
        decimals_evidence_ref=item.get("decimals_evidence_ref"),
        contract_restrictions=item.get("contract_restrictions", ()),
        review_status=_text(item.get("review_status"), "asset.review_status"),
        reviewer_ref=item.get("reviewer_ref"),
        reviewed_at_ms=item.get("reviewed_at_ms"),
        evidence_refs=item.get("evidence_refs", ()),
        reasons=item.get("reasons", ()),
        registry_revision=item.get("registry_revision"),
    )


def _asset_decimals(raw: Any) -> int:
    item = _mapping(raw, "asset")
    decimals = item.get("decimals")
    if type(decimals) is not int or isinstance(decimals, bool) or not 0 <= decimals <= 255:
        raise InputGateError("asset.decimals must be an integer in range 0..255")
    return decimals


def _pool_capability(raw: Any) -> PoolCapability:
    item = _mapping(raw, "pool")
    return PoolCapability(
        _pool_key(item, "pool"),
        can_quote=_text(item.get("can_quote"), "pool.can_quote"),
        can_simulate=_text(item.get("can_simulate"), "pool.can_simulate"),
        can_atomic_execute=_text(item.get("can_atomic_execute"), "pool.can_atomic_execute"),
        evidence_refs=item.get("evidence_refs", ()),
        reasons=item.get("reasons", ()),
    )


def load_registry(raw: Any) -> ValidatedRegistry:
    """Validate a W1-style snapshot without relaxing unsupported or unreviewed inputs."""
    document = _mapping(raw, "registry")
    schema_id = _text(document.get("schema_id"), "registry.schema_id")
    if schema_id != "w2-shadow-registry-v1":
        raise InputGateError("registry.schema_id must be w2-shadow-registry-v1")
    data_mode = _text(document.get("data_mode"), "registry.data_mode")
    revision = _text(
        document.get("registry_semantic_revision"),
        "registry.registry_semantic_revision",
    )
    assets_raw = document.get("assets")
    pools_raw = document.get("pools")
    if not isinstance(assets_raw, list) or not isinstance(pools_raw, list):
        raise InputGateError("registry.assets and registry.pools must be arrays")

    assets: dict[AssetRef, AssetEligibility] = {}
    asset_decimals: dict[AssetRef, int] = {}
    rejection_reasons: dict[str, tuple[str, ...]] = {}
    for index, item in enumerate(assets_raw):
        try:
            eligibility = _eligible_asset(item)
        except (TypeError, ValueError) as error:
            raise InputGateError(f"assets[{index}]: {error}") from error
        reasons: list[str] = []
        if eligibility.review_status != "approved":
            reasons.append(f"asset_review_{eligibility.review_status}")
        if eligibility.decimals_status != "verified":
            reasons.append("asset_decimals_unverified")
        if (
            not eligibility.decimals_evidence_ref
            or type(eligibility.decimals_evidence_ref) is not str
        ):
            reasons.append("asset_decimals_evidence_missing")
        if reasons:
            rejection_reasons[f"asset:{eligibility.asset_ref}"] = tuple(reasons)
        asset_decimals[eligibility.asset_ref] = _asset_decimals(item)
        previous = assets.setdefault(eligibility.asset_ref, eligibility)
        if previous != eligibility:
            raise InputGateError("duplicate asset with conflicting eligibility")

    pools: dict[PoolKey, PoolCapability] = {}
    for index, item in enumerate(pools_raw):
        try:
            capability = _pool_capability(item)
        except (TypeError, ValueError) as error:
            raise InputGateError(f"pools[{index}]: {error}") from error
        if capability.can_quote != "supported":
            rejection_reasons[f"pool:{capability.pool_key}"] = (
                f"pool_quote_{capability.can_quote}",
                *capability.reasons,
            )
        existing_pool = pools.setdefault(capability.pool_key, capability)
        if existing_pool != capability:
            raise InputGateError("duplicate pool with conflicting capability")

    return ValidatedRegistry(
        data_mode=data_mode,
        registry_semantic_revision=revision,
        assets=assets,
        asset_decimals=asset_decimals,
        pools=pools,
        rejection_reasons=rejection_reasons,
    )


def _load_json(path: Path, field_name: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise InputGateError(f"{field_name} must be a regular file")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_json_object,
            parse_constant=_json_constant,
        )
    except (OSError, UnicodeError, TypeError, ValueError) as error:
        raise InputGateError(f"{field_name} could not be parsed: {error}") from error
    if not isinstance(value, dict):
        raise InputGateError(f"{field_name} must be an object")
    return value


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"Duplicate JSON key: {key}")
        value[key] = item
    return value


def _json_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON value: {value}")


def _load_records(path: Path, record_type: str) -> list[Any]:
    if path.is_symlink() or not path.is_file():
        raise InputGateError(f"{record_type} partition must be a regular file")
    records: list[Any] = []
    try:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            record = decode_record_json(line)
            if record.record_type != record_type:
                raise InputGateError(
                    f"{record_type} partition line {line_number} has type {record.record_type}"
                )
            records.append(record)
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        if isinstance(error, InputGateError):
            raise
        raise InputGateError(f"{record_type} partition could not be decoded: {error}") from error
    return records


def _partition_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise InputGateError(f"partition {path.name} could not be read: {error}") from error


def load_w1_bootstrap_catalog(catalog_dir: Path) -> ValidatedCatalog:
    """Validate a W1 export and admit only fully reviewed, verified pools."""
    if catalog_dir.is_symlink() or not catalog_dir.is_dir():
        raise InputGateError("catalog directory must be a real directory")

    manifest = _load_json(catalog_dir / "catalog_manifest.json", "catalog_manifest")
    header = manifest.get("header")
    if not isinstance(header, dict):
        raise InputGateError("catalog_manifest.header must be an object")
    if header.get("catalog_schema") != "w1-catalog-manifest":
        raise InputGateError("catalog_manifest.catalog_schema must be w1-catalog-manifest")
    if header.get("version") != "1.0.0-draft":
        raise InputGateError("unsupported W1 catalog version")
    data_mode = _text(header.get("data_mode"), "catalog_manifest.data_mode")
    revision = _text(
        header.get("registry_revision"),
        "catalog_manifest.registry_revision",
    )

    eligibility_records = _load_records(
        catalog_dir / "eligibility.jsonl",
        "asset_eligibility",
    )
    pool_records = _load_records(catalog_dir / "pools.jsonl", "pool_descriptor")
    capability_records = _load_records(
        catalog_dir / "capabilities.jsonl",
        "pool_capability",
    )
    records_by_partition = {
        "eligibility": eligibility_records,
        "pools": pool_records,
        "capabilities": capability_records,
    }

    partitions = manifest.get("partitions")
    if not isinstance(partitions, dict):
        raise InputGateError("catalog_manifest.partitions must be an object")
    expected_row_counts: dict[str, int] = {}
    for name in ("eligibility", "pools", "capabilities"):
        partition = partitions.get(name)
        path = catalog_dir / f"{name}.jsonl"
        if not isinstance(partition, dict):
            raise InputGateError(f"catalog_manifest.partitions.{name} must be an object")
        if partition.get("path") != f"{name}.jsonl":
            raise InputGateError(f"catalog_manifest partition path mismatch for {name}")
        rows = partition.get("rows")
        if type(rows) is not int or isinstance(rows, bool) or rows < 0:
            raise InputGateError(f"catalog_manifest partition rows invalid for {name}")
        if len(records_by_partition[name]) != rows:
            raise InputGateError(f"catalog_manifest partition row count mismatch for {name}")
        if partition.get("sha256") != _partition_hash(path):
            raise InputGateError(f"catalog_manifest partition hash mismatch for {name}")
        expected_row_counts[name] = rows

    assets: dict[AssetRef, AssetEligibility] = {}
    asset_decimals: dict[AssetRef, int] = {}
    rejection_reasons: dict[str, tuple[str, ...]] = {}
    for record in eligibility_records:
        eligibility = record.payload
        if not isinstance(eligibility, AssetEligibility):
            raise InputGateError("asset_eligibility payload must be AssetEligibility")
        asset_ref = eligibility.asset_ref
        reasons: list[str] = []
        if eligibility.review_status != "approved":
            reasons.append(f"asset_review_{eligibility.review_status}")
        if eligibility.decimals_status != "verified":
            reasons.append("asset_decimals_unverified")
        if not eligibility.decimals_evidence_ref:
            reasons.append("asset_decimals_evidence_missing")
        decimals = record.provenance.get("decimals")
        if type(decimals) is not int or isinstance(decimals, bool) or not 0 <= decimals <= 255:
            raise InputGateError("eligibility record provenance lacks verified decimals")
        if asset_ref in assets:
            raise InputGateError("duplicate asset eligibility")
        assets[asset_ref] = eligibility
        asset_decimals[asset_ref] = decimals
        if reasons:
            rejection_reasons[f"asset:{asset_ref}"] = tuple(reasons)

    descriptors: dict[PoolKey, PoolDescriptor] = {}
    for record in pool_records:
        descriptor = record.payload
        if not isinstance(descriptor, PoolDescriptor):
            raise InputGateError("pool_descriptor payload must be PoolDescriptor")
        if descriptor.key in descriptors:
            raise InputGateError("duplicate pool descriptor")
        descriptors[descriptor.key] = descriptor

    capabilities: dict[PoolKey, PoolCapability] = {}
    for record in capability_records:
        capability = record.payload
        if not isinstance(capability, PoolCapability):
            raise InputGateError("pool_capability payload must be PoolCapability")
        if capability.pool_key in capabilities:
            raise InputGateError("duplicate pool capability")
        capabilities[capability.pool_key] = capability
        if capability.pool_key not in descriptors:
            raise InputGateError("pool capability lacks a descriptor")

    if capabilities.keys() != descriptors.keys():
        raise InputGateError("pool descriptor/capability inventory mismatch")

    admitted_descriptors: dict[PoolKey, PoolDescriptor] = {}
    admitted_capabilities: dict[PoolKey, PoolCapability] = {}
    for pool_key, descriptor in descriptors.items():
        pool_reasons: list[str] = []
        for asset_ref in (descriptor.currency0, descriptor.currency1):
            asset_reasons = rejection_reasons.get(f"asset:{asset_ref}")
            if asset_reasons is not None:
                pool_reasons.extend(f"{reason}:{asset_ref}" for reason in asset_reasons)
        capability = capabilities[pool_key]
        if capability.can_quote != "supported":
            pool_reasons.append(f"pool_quote_{capability.can_quote}")
            pool_reasons.extend(capability.reasons)
        if pool_reasons:
            rejection_reasons[f"pool:{pool_key}"] = tuple(dict.fromkeys(pool_reasons))
            continue
        admitted_descriptors[pool_key] = descriptor
        admitted_capabilities[pool_key] = capability

    del expected_row_counts
    return ValidatedCatalog(
        data_mode=data_mode,
        registry_semantic_revision=revision,
        assets=assets,
        asset_decimals=asset_decimals,
        descriptors=admitted_descriptors,
        pools=admitted_capabilities,
        rejection_reasons=rejection_reasons,
    )
