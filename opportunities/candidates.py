"""Explicit 2/3-hop route adapter for bounded W2 shadow candidates."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arbitrage_contracts.identity import (
    UINT256_MAX,
    Amount,
    AssetRef,
    FeeModel,
    PoolDescriptor,
    PoolKey,
    TokenKey,
)
from arbitrage_contracts.quote import HopRef, RouteRef
from opportunities.input_gate import ValidatedRegistry

MAX_POLICY_HOPS = 3
_ATOMS_DIGITS = frozenset("0123456789")
_ZERO_HOOK = "0x" + "00" * 20


class CandidateInputError(ValueError):
    """Raised when an explicit candidate route is malformed or out of policy bounds."""


class DeterministicOfflineTransport:
    """File-backed read-only transport bound to the checked-in W2 fixture root."""

    def __init__(self, raw: dict[str, Any], source_root: Path | None = None) -> None:
        if raw.get("schema_version") != "offline":
            raise CandidateInputError("quote_request.schema_version must be offline")
        self.source_root = (source_root or Path.cwd()).resolve()
        self.requests = raw.get("requests", {})
        if not isinstance(self.requests, dict):
            raise CandidateInputError("quote_request.requests must be an object")
        self.calls: list[dict[str, Any]] = []

    def fixture_path(self, route_id: str, amount: str, hop_index: int) -> str | None:
        """Return the explicit relative fixture path for one bounded route/amount/hop."""
        for route_requests in self.requests.values():
            if (
                isinstance(route_requests, dict)
                and route_requests.get("route") == route_id
                and isinstance(route_requests.get("amounts"), dict)
            ):
                paths = route_requests["amounts"].get(amount)
                if isinstance(paths, list) and hop_index < len(paths):
                    return paths[hop_index] if type(paths[hop_index]) is str else None
        return None

    def call(
        self,
        method: str,
        params: list[Any] | tuple[Any, ...] | None = None,
        block_identifier: str | None = None,
    ) -> dict[str, Any]:
        """Read one explicit fixture response after validating its identity bindings."""
        if method != "fixture":
            raise CandidateInputError("offline transport rejects non-fixture methods")
        if not isinstance(params, list) or len(params) != 1 or not isinstance(params[0], dict):
            raise CandidateInputError("offline fixture request must contain one object")
        self.calls.append({"method": method, "params": params, "block": block_identifier})
        relative = params[0].get("fixture")
        if not isinstance(relative, str) or Path(relative).is_absolute():
            raise CandidateInputError("offline fixture path must be relative")
        path = (
            self.source_root / "tests" / "fixtures" / "opportunities" / "v1" / relative
        ).resolve()
        try:
            path.relative_to(self.source_root)
        except ValueError as error:
            raise CandidateInputError("offline fixture path escapes source root") from error
        if not path.is_file():
            raise CandidateInputError(f"offline fixture is missing: {relative}")
        try:
            response = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CandidateInputError(f"invalid offline fixture: {error}") from error
        if not isinstance(response, dict) or response.get("schema_version") != "offline":
            raise CandidateInputError("offline fixture schema_version must be offline")
        if params[0].get("quote_id") != response.get("request_quote_id"):
            raise CandidateInputError("offline fixture request quote_id mismatch")
        if (
            block_identifier is not None
            and response.get("block_identifier") is not None
            and response["block_identifier"] != block_identifier
        ):
            raise CandidateInputError("offline fixture block mismatch")
        return response


def offline_quote_id(
    route_id: str | None, amount_atoms: str, hop_index: int, block_identifier: str
) -> str:
    """Derive the stable identity expected from an offline fixture response."""
    payload = {
        "route_id": route_id,
        "hop_index": hop_index,
        "amount_in": amount_atoms,
        "block": block_identifier,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "offline:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Candidate:
    """One explicit, policy-qualified route and principal amount."""

    route: RouteRef
    amount_in: Amount


@dataclass(frozen=True, slots=True)
class CandidateRejection:
    """An audit-ready route rejection with a stable reason."""

    route_id: str
    amount_atoms: str
    reason: str


@dataclass(frozen=True, slots=True)
class CandidateSelection:
    """Explicit route selection output, preserving parallel and distinct-amount candidates."""

    candidates: tuple[Candidate, ...]
    rejections: tuple[CandidateRejection, ...]
    truncated: bool = False


def _mapping(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CandidateInputError(f"{field_name} must be an object")
    return value


def _text(value: Any, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise CandidateInputError(f"{field_name} must be a non-empty string")
    return value


def _atoms(value: Any, field_name: str) -> str:
    text = _text(value, field_name)
    if (text.startswith("0") and text != "0") or not _ATOMS_DIGITS.issuperset(text):
        raise CandidateInputError(f"{field_name} must be a strict decimal string")
    if int(text) > UINT256_MAX:
        raise CandidateInputError(f"{field_name} exceeds uint256")
    return text


def _asset(raw: Any, field_name: str) -> AssetRef:
    if isinstance(raw, str):
        return AssetRef.erc20(TokenKey(4663, raw))
    item = _mapping(raw, field_name)
    chain_id = item.get("chain_id")
    if type(chain_id) is not int or chain_id <= 0:
        raise CandidateInputError(f"{field_name}.chain_id must be a positive integer")
    return AssetRef.erc20(TokenKey(chain_id, _text(item.get("address"), f"{field_name}.address")))


def _pool_key(raw: Any, field_name: str) -> PoolKey:
    item = _mapping(raw, field_name)
    chain_id = item.get("chain_id")
    if type(chain_id) is not int or chain_id <= 0:
        raise CandidateInputError(f"{field_name}.chain_id must be a positive integer")
    return PoolKey(
        chain_id,
        _text(item.get("protocol_id"), f"{field_name}.protocol_id"),
        _text(item.get("venue_kind"), f"{field_name}.venue_kind"),
        _text(item.get("venue_address"), f"{field_name}.venue_address"),
        _text(item.get("pool_id_kind"), f"{field_name}.pool_id_kind"),
        _text(item.get("pool_id"), f"{field_name}.pool_id"),
    )


def _asset_rejections(assets: tuple[AssetRef, ...], registry: ValidatedRegistry) -> list[str]:
    reasons: list[str] = []
    if any(asset not in registry.assets for asset in assets):
        reasons.append("asset_not_registered")
        return reasons
    for asset in assets:
        eligibility = registry.assets[asset]
        if eligibility.review_status != "approved":
            reasons.append(f"asset_review_{eligibility.review_status}")
        if eligibility.decimals_status != "verified":
            reasons.append("asset_decimals_unverified")
        if not eligibility.decimals_evidence_ref:
            reasons.append("asset_decimals_evidence_missing")
    return reasons


def _pool_mapping_rejections(descriptor: PoolDescriptor) -> list[str]:
    pool = descriptor.key
    if pool.protocol_id not in {"uniswap_v3", "uniswap_v4"}:
        return [f"protocol_unsupported:{pool.protocol_id}"]
    if pool.protocol_id == "uniswap_v3" and (
        pool.venue_kind != "factory" or pool.pool_id_kind != "address"
    ):
        return ["pool_mapping_incomplete"]
    if pool.protocol_id == "uniswap_v4" and (
        pool.venue_kind != "manager"
        or pool.pool_id_kind != "bytes32"
        or descriptor.hooks is None
        or descriptor.tick_spacing is None
    ):
        return ["pool_mapping_incomplete"]
    if descriptor.deployment_status != "deployed":
        return ["pool_not_deployed"]
    if descriptor.fee_model.kind != "static" or type(descriptor.fee_model.raw_value) is not int:
        return ["pool_fee_unsupported"]
    return []


def _pool_descriptor(raw: Any, field_name: str) -> PoolDescriptor:
    item = _mapping(raw, field_name)
    key = _pool_key(item.get("key"), f"{field_name}.key")
    currency0 = _asset(item.get("currency0"), f"{field_name}.currency0")
    currency1 = _asset(item.get("currency1"), f"{field_name}.currency1")
    fee_model_raw = _mapping(item.get("fee_model"), f"{field_name}.fee_model")
    fee_model = FeeModel(
        _text(fee_model_raw.get("kind"), f"{field_name}.fee_model.kind"),
        raw_value=fee_model_raw.get("raw_value"),
        evidence_ref=fee_model_raw.get("evidence_ref"),
    )
    tick_spacing = item.get("tick_spacing")
    if tick_spacing is not None and (
        type(tick_spacing) is not int or isinstance(tick_spacing, bool)
    ):
        raise CandidateInputError(f"{field_name}.tick_spacing must be an integer")
    hooks = item.get("hooks")
    if hooks is not None and type(hooks) is not str:
        raise CandidateInputError(f"{field_name}.hooks must be a string or null")
    deployment_status = _text(
        item.get("deployment_status", "deployed"), f"{field_name}.deployment_status"
    )
    identity_evidence_refs = item.get("identity_evidence_refs", ())
    if not isinstance(identity_evidence_refs, tuple):
        if isinstance(identity_evidence_refs, list) and all(
            type(ref) is str for ref in identity_evidence_refs
        ):
            identity_evidence_refs = tuple(identity_evidence_refs)
        else:
            raise CandidateInputError(
                f"{field_name}.identity_evidence_refs must be an array of strings"
            )
    try:
        return PoolDescriptor(
            key,
            currency0,
            currency1,
            fee_model,
            tick_spacing=tick_spacing,
            hooks=hooks,
            identity_evidence_refs=identity_evidence_refs,
            deployment_status=deployment_status,
        )
    except ValueError as error:
        raise CandidateInputError(f"invalid W0 pool descriptor: {error}") from error


def _route(raw: dict[str, Any], registry: ValidatedRegistry) -> tuple[RouteRef, tuple[str, ...]]:
    item = _mapping(raw, "route")
    route_id = item.get("route_id")
    if route_id is not None:
        route_id = _text(route_id, "route.route_id")
    chain_id = item.get("chain_id")
    if type(chain_id) is not int or chain_id <= 0:
        raise CandidateInputError("route.chain_id must be a positive integer")
    hops_raw = item.get("hops")
    if not isinstance(hops_raw, list):
        raise CandidateInputError("route.hops must be an array")
    if len(hops_raw) not in (2, 3, 4):
        raise CandidateInputError("candidate routes must contain 2 to 4 parsed hops")
    try:
        route = _route_with_contract_hops(item, route_id)
    except CandidateInputError:
        if len(hops_raw) > MAX_POLICY_HOPS:
            return _route_with_contract_hops(item, route_id), ("route_hops_disabled_by_policy",)
        raise
    if len(hops_raw) > MAX_POLICY_HOPS:
        return route, ("route_hops_disabled_by_policy",)

    assets = [route.base_asset, *(hop.asset_out for hop in route.hops[:-1])]
    rejection_reasons = _asset_rejections(tuple(assets), registry)
    descriptors = [hop.pool_descriptor for hop in route.hops]
    for descriptor in descriptors:
        if descriptor is None:
            rejection_reasons.append("pool_mapping_missing")
            continue
        if descriptor.key not in registry.pools:
            rejection_reasons.append("pool_not_registered")
        elif registry.pools[descriptor.key].can_quote != "supported":
            rejection_reasons.append("pool_unsupported")
        else:
            rejection_reasons.extend(_pool_mapping_rejections(descriptor))
            if descriptor.key.protocol_id == "uniswap_v4" and descriptor.hooks != _ZERO_HOOK:
                rejection_reasons.append("pool_hook_unsupported")
    return route, tuple(dict.fromkeys(rejection_reasons))


def _route_with_contract_hops(raw: dict[str, Any], route_id: str | None) -> RouteRef:
    chain_id = raw["chain_id"]
    base_asset = _asset(raw.get("base_asset"), "route.base_asset")
    hops_raw = raw.get("hops")
    if not isinstance(hops_raw, list):
        raise CandidateInputError("route.hops must be an array")
    descriptors = [
        _pool_descriptor(hop, f"route.hops[{index}]") for index, hop in enumerate(hops_raw)
    ]
    asset_sequence = [base_asset]
    for index, descriptor in enumerate(descriptors):
        if descriptor.currency0 == asset_sequence[-1]:
            asset_sequence.append(descriptor.currency1)
        elif descriptor.currency1 == asset_sequence[-1]:
            asset_sequence.append(descriptor.currency0)
        else:
            raise CandidateInputError("route continuity error")
        if asset_sequence[-1] != _asset(
            hops_raw[index].get("asset_out"), f"route.hops[{index}].asset_out"
        ):
            raise CandidateInputError("route asset_out does not match pool descriptor")
    hops = [
        HopRef(
            descriptor.key,
            asset_sequence[index],
            asset_sequence[index + 1],
            direction="zero_for_one"
            if descriptor.currency0 == asset_sequence[index]
            else "one_for_zero",
            pool_descriptor=descriptor,
        )
        for index, descriptor in enumerate(descriptors)
    ]
    try:
        return RouteRef(chain_id, base_asset, hops, route_id=route_id)
    except ValueError as error:
        raise CandidateInputError(f"invalid W0 route: {error}") from error


def select_candidates(
    raw: Any, registry: ValidatedRegistry, max_candidates: int
) -> CandidateSelection:
    """Select only explicitly listed, policy-qualified 2/3-hop candidates."""
    if type(max_candidates) is not int or max_candidates < 0:
        raise CandidateInputError("max_candidates must be a non-negative integer")
    document = _mapping(raw, "candidates")
    if _text(document.get("schema_id"), "candidates.schema_id") != "w2-shadow-candidates-v1":
        raise CandidateInputError("candidates.schema_id must be w2-shadow-candidates-v1")
    routes = document.get("routes")
    if not isinstance(routes, list):
        raise CandidateInputError("candidates.routes must be an array")

    candidates: list[Candidate] = []
    rejections: list[CandidateRejection] = []
    truncated = False
    for route_index, route_raw in enumerate(routes):
        route_item = _mapping(route_raw, f"candidates.routes[{route_index}]")
        route_id = route_item.get("route_id")
        if route_id is not None:
            route_id = _text(route_id, "route.route_id")
        else:
            route_id = ""
        amounts = route_item.get("amounts")
        if not isinstance(amounts, list) or not amounts:
            raise CandidateInputError("route.amounts must be a non-empty array")
        route, route_rejections = _route(route_item, registry)
        for amount_index, amount_raw in enumerate(amounts):
            amount_item = _mapping(amount_raw, f"route.amounts[{amount_index}]")
            amount_text = _atoms(amount_item.get("amount_atoms"), "amount_atoms")
            decimals_raw = amount_item.get("decimals")
            if type(decimals_raw) is not int or isinstance(decimals_raw, bool):
                raise CandidateInputError("amounts.decimals must be an integer")
            amount_evidence = _text(
                amount_item.get("decimals_evidence_ref"), "amounts.decimals_evidence_ref"
            )
            eligibility = registry.assets.get(route.base_asset)
            if route_rejections:
                rejections.append(CandidateRejection(route_id, amount_text, route_rejections[0]))
                continue
            if len(candidates) >= max_candidates:
                truncated = True
                rejections.append(
                    CandidateRejection(route_id, amount_text, "candidate_budget_exceeded")
                )
                continue
            if (
                eligibility is None
                or eligibility.decimals_evidence_ref is None
                or eligibility.decimals_evidence_ref != amount_evidence
            ):
                rejections.append(
                    CandidateRejection(route_id, amount_text, "amount_precision_evidence_mismatch")
                )
                continue
            amount = Amount.from_atoms_str(
                route.base_asset,
                amount_text,
                registry.asset_decimals[route.base_asset],
                amount_evidence,
            )
            if amount.atoms == 0:
                rejections.append(CandidateRejection(route_id, amount_text, "amount_not_positive"))
                continue
            candidates.append(Candidate(route, amount))
    return CandidateSelection(tuple(candidates), tuple(rejections), truncated)
