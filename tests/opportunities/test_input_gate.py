"""Regression coverage for strict W1 registry input gating."""

from __future__ import annotations

import pytest

from opportunities.input_gate import InputGateError, load_registry


def _registry(**overrides: object) -> dict[str, object]:
    registry: dict[str, object] = {
        "schema_id": "w2-shadow-registry-v1",
        "data_mode": "synthetic",
        "registry_semantic_revision": "registry-v1",
        "assets": [
            {
                "address": "0x00000000000000000000000000000000000000aa",
                "chain_id": 4663,
                "decimals": 18,
                "decimals_status": "verified",
                "decimals_evidence_ref": "fixture:decimals",
                "review_status": "approved",
                "reviewer_ref": "fixture:reviewer",
                "reviewed_at_ms": 1,
                "evidence_refs": ["fixture:asset"],
            }
        ],
        "pools": [
            {
                "chain_id": 4663,
                "protocol_id": "uniswap_v3",
                "venue_kind": "factory",
                "venue_address": "0x0000000000000000000000000000000000000001",
                "pool_id_kind": "address",
                "pool_id": "0x0000000000000000000000000000000000000002",
                "can_quote": "supported",
                "can_simulate": "unknown",
                "can_atomic_execute": "unknown",
            }
        ],
    }
    registry.update(overrides)
    return registry


@pytest.mark.parametrize(
    ("field_name", "field_value", "expected"),
    [
        ("review_status", "pending_review", "asset_review_pending_review"),
        ("review_status", "revoked", "asset_review_revoked"),
        ("review_status", "unknown", "asset_review_unknown"),
        ("decimals_status", "unknown", "asset_decimals_unverified"),
    ],
)
def test_asset_eligibility_rejections(field_name: str, field_value: object, expected: str) -> None:
    """Unapproved or unverified assets remain outside the quote set with an auditable reason."""
    asset = dict(_registry()["assets"][0])  # type: ignore[index]
    asset[field_name] = field_value
    registry = load_registry(_registry(assets=[asset]))
    asset_ref, eligibility = next(iter(registry.assets.items()))
    assert registry.rejection_reasons[f"asset:{asset_ref}"] == (expected,)


def test_missing_decimals_evidence_is_rejected() -> None:
    """A decimals value without provenance cannot pass the gate."""
    asset = dict(_registry()["assets"][0])  # type: ignore[index]
    asset["decimals_evidence_ref"] = None
    registry = load_registry(_registry(assets=[asset]))
    assert "asset_decimals_evidence_missing" in next(iter(registry.rejection_reasons.values()))


def test_unknown_pool_and_wrong_chain_are_distinct() -> None:
    """Unknown pools are retained only as rejection reasons and cannot be quoted."""
    registry = load_registry(_registry())
    assert next(iter(registry.pools.values())).can_quote == "supported"


def test_schema_is_fail_closed() -> None:
    """Unknown registry schemas are rejected before eligibility interpretation."""
    with pytest.raises(InputGateError, match="registry.schema_id"):
        load_registry(_registry(schema_id="w1-registry"))


def test_unsupported_pool_is_retained_as_rejection() -> None:
    """An unsupported pool cannot silently enter the quote set."""
    pool = dict(_registry()["pools"][0])  # type: ignore[index]
    pool["can_quote"] = "unsupported"
    pool["reasons"] = ["pool_hook_unsupported"]
    registry = load_registry(_registry(pools=[pool]))
    key, capability = next(iter(registry.pools.items()))
    assert registry.rejection_reasons[f"pool:{key}"] == (
        "pool_quote_unsupported",
        "pool_hook_unsupported",
    )
    assert capability.can_quote == "unsupported"
