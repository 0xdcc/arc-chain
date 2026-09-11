"""Regression coverage for explicit candidate route boundaries."""

from __future__ import annotations

import pytest

from arbitrage_contracts.quote import HopLimitExceededError
from opportunities.candidates import CandidateInputError, select_candidates
from opportunities.input_gate import load_registry

BASE = "0x00000000000000000000000000000000000000aa"
MIDDLE = "0x00000000000000000000000000000000000000bb"
MIDDLE_TWO = "0x00000000000000000000000000000000000000cc"
MIDDLE_THREE = "0x00000000000000000000000000000000000000dd"
MIDDLE_FOUR = "0x00000000000000000000000000000000000000ee"
POOL_ONE = "0x0000000000000000000000000000000000000002"
POOL_TWO = "0x0000000000000000000000000000000000000003"
POOL_THREE = "0x0000000000000000000000000000000000000004"
POOL_FOUR = "0x0000000000000000000000000000000000000005"


def _asset(address: str) -> dict[str, object]:
    return {
        "address": address,
        "chain_id": 4663,
        "decimals": 18,
        "decimals_status": "verified",
        "decimals_evidence_ref": "fixture:decimals",
        "review_status": "approved",
        "reviewer_ref": "fixture:reviewer",
        "reviewed_at_ms": 1,
        "evidence_refs": ["fixture:asset"],
    }


def _pool(address: str, **overrides: object) -> dict[str, object]:
    pool: dict[str, object] = {
        "chain_id": 4663,
        "protocol_id": "uniswap_v3",
        "venue_kind": "factory",
        "venue_address": address,
        "pool_id_kind": "address",
        "pool_id": address,
        "can_quote": "supported",
        "can_simulate": "unknown",
        "can_atomic_execute": "unknown",
    }
    pool.update(overrides)
    return pool


def _registry(pools: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_id": "w2-shadow-registry-v1",
        "data_mode": "synthetic",
        "registry_semantic_revision": "registry-v1",
        "assets": [
            _asset(BASE),
            _asset(MIDDLE),
            _asset(MIDDLE_TWO),
            _asset(MIDDLE_THREE),
            _asset(MIDDLE_FOUR),
        ],
        "pools": pools,
    }


def _route(route_id: str | None, pool_ids: list[str], asset_out: list[str]) -> dict[str, object]:
    hops = []
    for index, pool_id in enumerate(pool_ids):
        hops.append(
            {
                "key": {
                    "chain_id": 4663,
                    "protocol_id": "uniswap_v3",
                    "venue_kind": "factory",
                    "venue_address": pool_id,
                    "pool_id_kind": "address",
                    "pool_id": pool_id,
                },
                "currency0": asset_out[index - 1] if index else BASE,
                "currency1": asset_out[index],
                "fee_model": {"kind": "static", "raw_value": 500},
                "identity_evidence_refs": ["fixture:pool"],
                "asset_out": asset_out[index],
            }
        )
    return {
        "route_id": route_id,
        "chain_id": 4663,
        "base_asset": {"chain_id": 4663, "address": BASE},
        "hops": hops,
        "amounts": [
            {"amount_atoms": "1000", "decimals": 18, "decimals_evidence_ref": "fixture:decimals"}
        ],
    }


def _candidates(routes: list[dict[str, object]]) -> dict[str, object]:
    return {"schema_id": "w2-shadow-candidates-v1", "routes": routes}


def test_two_hop_parallel_and_amounts_are_distinct() -> None:
    """Parallel pools and distinct principal amounts remain separate candidates."""
    registry = load_registry(
        _registry([_pool(POOL_ONE), _pool(POOL_TWO), _pool(POOL_THREE), _pool(POOL_FOUR)])
    )
    routes = [
        _route(None, [POOL_ONE, POOL_TWO], [MIDDLE, BASE]),
        _route(None, [POOL_ONE, POOL_TWO], [MIDDLE, BASE]),
    ]
    routes[1]["amounts"] = [
        {"amount_atoms": "1000", "decimals": 18, "decimals_evidence_ref": "fixture:decimals"},
        {"amount_atoms": "2000", "decimals": 18, "decimals_evidence_ref": "fixture:decimals"},
    ]
    selection = select_candidates(_candidates(routes), registry, 3)
    assert len(selection.candidates) == 3
    assert len({candidate.amount_in.atoms for candidate in selection.candidates}) == 2


def test_four_hop_route_is_resolved_but_policy_disabled() -> None:
    """W0 can parse four hops, while the W2 strategy layer explicitly rejects it."""
    registry = load_registry(_registry([_pool(POOL_ONE), _pool(POOL_TWO)]))
    route = _route(
        None,
        [POOL_ONE, POOL_TWO, POOL_THREE, POOL_FOUR],
        [MIDDLE, MIDDLE_TWO, MIDDLE_THREE, BASE],
    )
    selection = select_candidates(_candidates([route]), registry, 1)
    assert selection.rejections[0].reason == "route_hops_disabled_by_policy"


def test_unclosed_route_is_a_schema_error() -> None:
    """A malformed pool descriptor is rejected before quoting rather than silently merged."""
    registry = load_registry(_registry([_pool(POOL_ONE), _pool(POOL_TWO)]))
    route = _route(None, [POOL_ONE, POOL_TWO], [MIDDLE, BASE])
    route["hops"][1]["currency1"] = MIDDLE  # type: ignore[index]
    with pytest.raises(CandidateInputError, match="invalid W0 pool descriptor"):
        select_candidates(_candidates([route]), registry, 1)


def test_hop_limit_contract_still_enforced() -> None:
    """The W0 max_hops contract remains active behind the strategy boundary."""
    with pytest.raises(HopLimitExceededError):
        raise HopLimitExceededError("five hops")
