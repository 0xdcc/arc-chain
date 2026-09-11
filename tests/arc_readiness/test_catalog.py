"""Tests covering ArcMarketCatalog registry operations and C23 status invariants."""

from __future__ import annotations

import pytest

from arc_readiness.catalog import ArcMarketCatalog
from arc_readiness.eligibility import (
    ARC_CANONICAL_EURC_ADDRESS,
    evaluate_asset_eligibility,
    evaluate_market_eligibility,
)
from arc_readiness.errors import ArcValidationError
from arc_readiness.models import ARC_USDC_ERC20_ADDRESS

POOL_ADDR = "0x" + "aa" * 20


def test_catalog_lifecycle_and_c23_status_invariants() -> None:
    """C23: Catalog lifecycle: empty -> no_eligible_market -> markets_available."""
    catalog = ArcMarketCatalog()

    # 1. Empty catalog state
    assert catalog.total_assets == 0
    assert catalog.total_markets == 0
    assert catalog.get_catalog_status() == "EMPTY_CATALOG_INPUT"

    # Register assets
    usdc = evaluate_asset_eligibility(
        asset_id="arc:usdc",
        symbol="USDC",
        decimals=6,
        interface_kind="erc20",
        contract_address=ARC_USDC_ERC20_ADDRESS,
    )
    eurc = evaluate_asset_eligibility(
        asset_id="arc:eurc",
        symbol="EURC",
        decimals=6,
        interface_kind="erc20",
        contract_address=ARC_CANONICAL_EURC_ADDRESS,
    )
    catalog.register_asset(usdc)
    catalog.register_asset(eurc)
    assert catalog.total_assets == 2

    # 2. Register only an unsupported / deprecated market
    deprecated_m = evaluate_market_eligibility(
        market_id="deprecated_pool",
        protocol_id="legacy",
        pool_address=POOL_ADDR,
        base_asset=usdc,
        quote_asset=eurc,
        is_deprecated_venue=True,
    )
    catalog.register_market(deprecated_m)

    # Status must be NO_ELIGIBLE_MARKET (not empty input, but 0 active markets)
    assert catalog.total_markets == 1
    assert len(catalog.get_active_markets()) == 0
    assert catalog.get_catalog_status() == "NO_ELIGIBLE_MARKET"

    # 3. Register an eligible active market
    active_m = evaluate_market_eligibility(
        market_id="active_pool",
        protocol_id="uniswap_v3",
        pool_address="0x" + "bb" * 20,
        base_asset=usdc,
        quote_asset=eurc,
    )
    catalog.register_market(active_m)

    assert catalog.total_markets == 2
    assert len(catalog.get_active_markets()) == 1
    assert catalog.get_catalog_status() == "MARKETS_AVAILABLE"

    # 4. Verify summary export
    summary = catalog.export_summary()
    assert summary["status"] == "MARKETS_AVAILABLE"
    assert summary["total_assets"] == 2
    assert summary["total_markets"] == 2
    assert summary["active_markets_count"] == 1


def test_catalog_type_safety() -> None:
    """Ensure catalog rejects arbitrary objects without proper DTO wrapping."""
    catalog = ArcMarketCatalog()
    with pytest.raises(ArcValidationError, match="Expected ArcAssetEligibilityDraft"):
        catalog.register_asset("not_an_asset")  # type: ignore[arg-type]

    with pytest.raises(ArcValidationError, match="Expected ArcMarketEligibilityDraft"):
        catalog.register_market({"pool": "dict"})  # type: ignore[arg-type]
