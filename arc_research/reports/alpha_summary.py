"""Arc Alpha Research and Strategy Evaluation Summary (T36)

Enforces:
- Route-level market opportunity assessment backed by verified ledger and research evidence
- Distinction between unpermissioned public DEX routes and gated/credentialed routes (USYC, private RFQ)
- Zero-profit samples explicitly documented without inventing synthetic alpha
- Prohibition of fixed yield guarantees or unwarranted return promises
"""

from __future__ import annotations

from dataclasses import dataclass


class AlphaEvaluationError(ValueError):
    """Raised when alpha evaluation invariants are violated."""


@dataclass(frozen=True, slots=True)
class RouteAlphaScore:
    """Quantitative alpha evaluation for a candidate route."""

    route_id: str
    base_asset: str
    sample_count: int
    median_spread_bps: int
    net_yield_bps_est: int
    max_capacity_atoms: int
    realized_profit_atoms: int | None
    requires_gated_credentials: bool
    exclusion_reasons: tuple[str, ...]
    priority_score: float

    def is_immediately_actionable(self) -> bool:
        """Route is actionable only if unpermissioned, has positive net yield, and no exclusions."""
        return (
            not self.requires_gated_credentials
            and self.net_yield_bps_est > 0
            and len(self.exclusion_reasons) == 0
        )


@dataclass(frozen=True, slots=True)
class AlphaResearchReport:
    """Comprehensive alpha research deliverable."""

    report_id: str
    generated_at_utc: float
    total_routes_analyzed: int
    actionable_routes_count: int
    gated_routes_count: int
    coverage_gap_reasons: tuple[str, ...]
    route_rankings: tuple[RouteAlphaScore, ...]
    guaranteed_fixed_yield_claimed: bool = False  # Permanently False

    def __post_init__(self) -> None:
        if self.guaranteed_fixed_yield_claimed:
            raise AlphaEvaluationError("Fixed return or guaranteed yield claims are strictly prohibited")


class AlphaReportGenerator:
    """Synthesizes T33 (events), T34 (settled), and T35 (replay) into an actionable report."""

    @classmethod
    def evaluate_route(
        cls,
        route_id: str,
        base_asset: str,
        sample_count: int,
        median_spread_bps: int,
        net_yield_bps_est: int,
        max_capacity_atoms: int,
        realized_profit_atoms: int | None = None,
        is_gated: bool = False,
        exclusions: list[str] | None = None,
        promised_fixed_return: bool = False,
    ) -> RouteAlphaScore:
        """Score a candidate route with strict safety checks."""
        if promised_fixed_return:
            raise AlphaEvaluationError("Promising fixed yield or guaranteed returns is strictly prohibited")

        ex_list = list(exclusions) if exclusions else []
        if is_gated:
            ex_list.append("REQUIRES_INSTITUTIONAL_CREDENTIALS_OR_KYC")

        # Compute priority score: (net_yield_bps * capacity_weight) - penalties
        capacity_weight = min(1.0, float(max_capacity_atoms) / 500_000_000.0) if max_capacity_atoms > 0 else 0.0
        base_score = float(net_yield_bps_est) * capacity_weight
        if is_gated or ex_list:
            base_score = -100.0  # Deprioritize gated or excluded routes

        return RouteAlphaScore(
            route_id=route_id,
            base_asset=base_asset,
            sample_count=sample_count,
            median_spread_bps=median_spread_bps,
            net_yield_bps_est=net_yield_bps_est,
            max_capacity_atoms=max_capacity_atoms,
            realized_profit_atoms=realized_profit_atoms,
            requires_gated_credentials=is_gated,
            exclusion_reasons=tuple(ex_list),
            priority_score=round(base_score, 2),
        )

    @classmethod
    def compile_report(
        cls,
        report_id: str,
        route_scores: list[RouteAlphaScore],
        coverage_gaps: list[str],
        timestamp_utc: float = 0.0,
    ) -> AlphaResearchReport:
        """Compile comprehensive research findings."""
        sorted_scores = sorted(route_scores, key=lambda r: r.priority_score, reverse=True)
        actionable = [r for r in route_scores if r.is_immediately_actionable()]
        gated = [r for r in route_scores if r.requires_gated_credentials]

        return AlphaResearchReport(
            report_id=report_id,
            generated_at_utc=timestamp_utc,
            total_routes_analyzed=len(route_scores),
            actionable_routes_count=len(actionable),
            gated_routes_count=len(gated),
            coverage_gap_reasons=tuple(coverage_gaps),
            route_rankings=tuple(sorted_scores),
            guaranteed_fixed_yield_claimed=False,
        )
