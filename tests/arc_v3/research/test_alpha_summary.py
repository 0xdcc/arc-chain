"""Arc Alpha Research and Strategy Evaluation Tests (T36)

Verifies:
- Evaluation and priority ranking of unpermissioned cyclic arbitrage routes
- Gated credentials (USYC / KYC RFQ) are strictly segregated and not marked actionable
- Zero-profit samples are accurately documented without fabricating synthetic alpha
- Prohibition of fixed yield guarantees or return promises (raises AlphaEvaluationError)
- Comprehensive report compilation and gap analysis preservation
"""

from __future__ import annotations

import pytest

from arc_research.reports.alpha_summary import (
    AlphaEvaluationError,
    AlphaReportGenerator,
    AlphaResearchReport,
    RouteAlphaScore,
)


class TestArcAlphaSummary:
    """Test suite for T36 Alpha Research and Strategy Evaluation."""

    def test_unpermissioned_actionable_route_scoring(self) -> None:
        """Verify normal positive route is scored and marked immediately actionable."""
        score = AlphaReportGenerator.evaluate_route(
            route_id="route-v3-usdc-weth",
            base_asset="USDC",
            sample_count=50,
            median_spread_bps=25,
            net_yield_bps_est=12,
            max_capacity_atoms=100_000_000,  # 100 USDC
            realized_profit_atoms=450_000,
            is_gated=False,
        )
        assert score.is_immediately_actionable() is True
        assert score.requires_gated_credentials is False
        assert score.net_yield_bps_est == 12
        assert score.priority_score > 0

    def test_gated_institutional_route_segregation(self) -> None:
        """Rule: Routes requiring KYC / institutional gates (USYC) cannot be marked actionable."""
        score = AlphaReportGenerator.evaluate_route(
            route_id="route-rwa-usyc-usdc",
            base_asset="USDC",
            sample_count=20,
            median_spread_bps=40,
            net_yield_bps_est=28,  # High yield on paper
            max_capacity_atoms=500_000_000,
            is_gated=True,  # Institutional KYC required
        )
        assert score.is_immediately_actionable() is False
        assert score.requires_gated_credentials is True
        assert "REQUIRES_INSTITUTIONAL_CREDENTIALS_OR_KYC" in score.exclusion_reasons
        # Penalized priority score
        assert score.priority_score < 0

    def test_zero_profit_sample_preservation(self) -> None:
        """Rule: Routes with zero or negative net yield are not marked actionable."""
        score = AlphaReportGenerator.evaluate_route(
            route_id="route-thin-pool",
            base_asset="USDC",
            sample_count=10,
            median_spread_bps=5,
            net_yield_bps_est=0,  # 0 bps after fees
            max_capacity_atoms=10_000_000,
            is_gated=False,
        )
        assert score.is_immediately_actionable() is False

    def test_guaranteed_return_claim_prohibited(self) -> None:
        """Rule: Claiming guaranteed returns or fixed yield must raise AlphaEvaluationError."""
        with pytest.raises(AlphaEvaluationError, match="Promising fixed yield or guaranteed returns is strictly prohibited"):
            AlphaReportGenerator.evaluate_route(
                route_id="route-scam",
                base_asset="USDC",
                sample_count=1,
                median_spread_bps=100,
                net_yield_bps_est=80,
                max_capacity_atoms=100_000_000,
                promised_fixed_return=True,  # Prohibited!
            )

    def test_compile_research_report_and_gaps(self) -> None:
        """Verify compiling full alpha research deliverable with gap tracking."""
        r1 = AlphaReportGenerator.evaluate_route("r1", "USDC", 10, 20, 10, 100_000_000)
        r2 = AlphaReportGenerator.evaluate_route("r2", "USDC", 5, 40, 25, 500_000_000, is_gated=True)

        gaps = ["OTC_WALL_WS_RATE_LIMIT", "V4_DYNAMIC_HOOK_COVERAGE"]
        report = AlphaReportGenerator.compile_report(
            report_id="rep-alpha-001",
            route_scores=[r1, r2],
            coverage_gaps=gaps,
            timestamp_utc=1726000000.0,
        )
        assert report.total_routes_analyzed == 2
        assert report.actionable_routes_count == 1
        assert report.gated_routes_count == 1
        assert len(report.coverage_gap_reasons) == 2
        assert report.guaranteed_fixed_yield_claimed is False
        # Actionable route ranked first
        assert report.route_rankings[0].route_id == "r1"
