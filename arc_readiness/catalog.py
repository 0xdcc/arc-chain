"""Arc market catalog registry and eligibility aggregation."""

from __future__ import annotations

from arc_readiness.errors import ArcValidationError
from arc_readiness.models import (
    ArcAssetEligibilityDraft,
    ArcMarketEligibilityDraft,
)


class ArcMarketCatalog:
    """In-memory registry maintaining verified assets and pools on Arc."""

    def __init__(self) -> None:
        self._assets: dict[str, ArcAssetEligibilityDraft] = {}
        self._markets: dict[str, ArcMarketEligibilityDraft] = {}

    @property
    def total_assets(self) -> int:
        return len(self._assets)

    @property
    def total_markets(self) -> int:
        return len(self._markets)

    def register_asset(self, asset: ArcAssetEligibilityDraft) -> None:
        """Register an asset candidate into the catalog."""
        if not isinstance(asset, ArcAssetEligibilityDraft):
            raise ArcValidationError(
                f"Expected ArcAssetEligibilityDraft, got {type(asset).__name__}"
            )
        self._assets[asset.asset_id] = asset

    def register_market(self, market: ArcMarketEligibilityDraft) -> None:
        """Register a market candidate into the catalog, deduping identical pool addresses."""
        if not isinstance(market, ArcMarketEligibilityDraft):
            raise ArcValidationError(
                f"Expected ArcMarketEligibilityDraft, got {type(market).__name__}"
            )
        self._markets[market.market_id] = market

    def get_asset(self, asset_id: str) -> ArcAssetEligibilityDraft | None:
        return self._assets.get(asset_id)

    def get_market(self, market_id: str) -> ArcMarketEligibilityDraft | None:
        return self._markets.get(market_id)

    def get_active_markets(self) -> tuple[ArcMarketEligibilityDraft, ...]:
        """Return only markets that support quoting and are not disqualified."""
        active = [
            m
            for m in self._markets.values()
            if m.can_quote == "supported" and "DEPRECATED_MARKET_VENUE" not in m.reasons
        ]
        return tuple(active)

    def get_catalog_status(self) -> str:
        """Determine aggregate catalog readiness state."""
        if not self._markets:
            return "EMPTY_CATALOG_INPUT"
        active = self.get_active_markets()
        if not active:
            return "NO_ELIGIBLE_MARKET"
        return "MARKETS_AVAILABLE"

    def export_summary(self) -> dict[str, object]:
        """Produce structured JSON-ready catalog diagnostic report."""
        status = self.get_catalog_status()
        active = self.get_active_markets()
        return {
            "status": status,
            "total_assets": len(self._assets),
            "total_markets": len(self._markets),
            "active_markets_count": len(active),
            "active_markets": [m.to_dict() for m in active],
            "all_markets": [m.to_dict() for m in self._markets.values()],
        }
