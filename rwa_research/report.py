"""Markdown and JSONL reporting generators for RWA research observations."""

from __future__ import annotations

from collections.abc import Sequence

from .classify import ResearchClassification
from .codec import to_canonical_json
from .models import RwaResearchRecord


def generate_jsonl_records(records: Sequence[RwaResearchRecord]) -> str:
    """Serialize research records to standard newline-delimited canonical JSON."""
    lines = [to_canonical_json(r) for r in records]
    return "\n".join(lines) + ("\n" if lines else "")


def generate_markdown_report(
    classification: ResearchClassification,
    record: RwaResearchRecord,
) -> str:
    """Generate human-readable Markdown summary report for one research observation."""
    lines: list[str] = [
        f"# RWA Research Observation: {record.record_id}",
        "",
        f"- **Underlier**: `{record.instrument.underlier_id}` ({record.instrument.issuer_id})",
        f"- **Token Address**: `{record.instrument.token_key.address}` (Chain {record.instrument.token_key.chain_id})",
        f"- **As-Of Timestamp**: `{record.as_of_ms}`",
        f"- **Data Mode**: `{record.data_mode}` (Draft: `{record.is_draft}`)",
        "",
        "## 1. Track 1: Same-Token Cross-Pool Candidates",
    ]

    if classification.cross_pool_candidates:
        lines.append("| Buy Pool | Sell Pool | Amount In | Buy Price | Sell Price | Raw Spread | Level |")
        lines.append("|---|---|---|---|---|---|---|")
        for c in classification.cross_pool_candidates:
            lines.append(
                f"| `{c.buy_pool_key.canonical_pool_id[:10]}...` "
                f"| `{c.sell_pool_key.canonical_pool_id[:10]}...` "
                f"| {c.amount_in_atoms} "
                f"| {float(c.buy_effective_price):.4f} "
                f"| {float(c.sell_effective_price):.4f} "
                f"| {float(c.raw_spread_ratio * 100):+.2f}% "
                f"| `{c.evidence_level}` |"
            )
    else:
        lines.append("_No cross-pool arbitrage candidates found among reviewed independent pools._")

    lines.extend(
        [
            "",
            "## 2. Track 2: Equity Basis Research (RESEARCH_ONLY)",
        ]
    )

    if classification.basis_research is not None:
        b = classification.basis_research
        obs_str = f"${float(b.observed_token_price_usd):.2f}" if b.observed_token_price_usd else "N/A"
        basis_str = f"{float(b.signed_basis * 100):+.2f}%" if b.signed_basis is not None else "N/A"
        lines.extend(
            [
                f"- **Reference Price**: `${float(b.reference_price_usd):.2f}`",
                f"- **Observed Token Price**: `{obs_str}`",
                f"- **Signed Basis**: `{basis_str}`",
                f"- **Session**: `{b.session_kind}`",
                f"- **Classification**: `{b.research_category}` (Non-actionable, no mean reversion assumption)",
            ]
        )
    else:
        lines.append("_No equity reference quote attached to this observation._")

    if classification.rejected_reasons:
        lines.extend(
            [
                "",
                "## 3. Rejection & Caution Reasons",
            ]
        )
        for r in classification.rejected_reasons:
            lines.append(f"- {r}")

    lines.append("")
    return "\n".join(lines)
