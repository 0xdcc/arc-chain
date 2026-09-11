"""Hermes Reporting & Opportunity Aggregation Engine (T39)

Generates human-readable Markdown summaries and machine-readable JSON metrics.
Enforces:
- Strict 4-tier outcome separation: ESTIMATED / SIMULATED / OUTPUT_VERIFIED / REALIZED
- Transparent display of input parameters, state references, and cost breakdowns
- Zero fabrication of real execution profits in offline research environments
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OpportunityReportSummary:
    """Consolidated summary metrics across a batch of opportunities."""

    total_observed: int
    positive_estimated_count: int
    unprofitable_count: int
    simulated_count: int
    output_verified_count: int
    realized_profits_atoms: int  # Strictly 0 in offline research mode
    candidate_sha: str
    generated_at: float


def generate_opportunity_json_summary(
    records: list[dict[str, Any]],
    candidate_sha: str,
) -> dict[str, Any]:
    """Generate structured JSON metrics for Hermes desktop consumption."""
    total = len(records)
    positive = 0
    unprofitable = 0
    simulated = 0
    verified = 0

    items_summary = []
    for r in records:
        net_atoms = r.get("net_profit_atoms", 0)
        status = r.get("status", "unknown")
        sim_status = r.get("simulation_status")

        if net_atoms > 0:
            positive += 1
        else:
            unprofitable += 1

        if sim_status:
            simulated += 1
            if r.get("output_verified"):
                verified += 1

        items_summary.append(
            {
                "observation_id": r.get("observation_id"),
                "cycle": r.get("cycle_summary"),
                "net_profit_atoms": net_atoms,
                "tier": "ESTIMATED" if not sim_status else ("OUTPUT_VERIFIED" if r.get("output_verified") else "SIMULATED"),
                "as_of_block": r.get("as_of_block"),
            }
        )

    return {
        "summary": {
            "total_observed": total,
            "positive_estimated": positive,
            "unprofitable": unprofitable,
            "simulated": simulated,
            "output_verified": verified,
            "realized_profit": 0,  # strictly 0 in research
            "candidate_sha": candidate_sha,
            "generated_at": time.time(),
        },
        "items": items_summary,
    }


def generate_opportunity_markdown_report(
    records: list[dict[str, Any]],
    candidate_sha: str,
) -> str:
    """Generate clean GitHub-flavored Markdown report for Hermes chat and desktop UI."""
    data = generate_opportunity_json_summary(records, candidate_sha)
    s = data["summary"]

    lines = [
        "# Arc Chain Opportunity & Research Report",
        "",
        f"- **Candidate SHA**: `{candidate_sha}`",
        f"- **Generated At**: `{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(s['generated_at']))} UTC`",
        f"- **Total Observed**: {s['total_observed']}",
        f"- **Positive Estimated (> 0)**: {s['positive_estimated']}",
        f"- **Simulated**: {s['simulated']}",
        f"- **Output Verified**: {s['output_verified']}",
        "- **Realized Profits**: `0 ATOMS (OFFLINE_RESEARCH_MODE)`",
        "",
        "## 1. Outcome Classification Tiers",
        "| Tier | Definition | Current Batch Count |",
        "|---|---|---|",
        f"| `ESTIMATED` | CLMM discrete math quote output minus fees | {s['positive_estimated']} |",
        f"| `SIMULATED` | Contract eth_call execution attempted | {s['simulated']} |",
        f"| `OUTPUT_VERIFIED` | State change verified with non-empty output | {s['output_verified']} |",
        "| `REALIZED` | On-chain balance settlement profit | 0 (Blocked) |",
        "",
        "## 2. Sample Opportunities",
    ]

    if not data["items"]:
        lines.append("*No opportunities observed in this batch.*")
    else:
        lines.append("| Observation ID | Route / Cycle | Net Output (atoms) | Tier | As Of Block |")
        lines.append("|---|---|---|---|---|")
        for item in data["items"][:10]:
            lines.append(
                f"| `{item['observation_id'][:12]}...` | {item['cycle']} | {item['net_profit_atoms']} | `{item['tier']}` | {item['as_of_block']} |"
            )

    lines.extend(
        [
            "",
            "## 3. Disclaimers & Safety Enforcements",
            "- Pre-execution quotes represent theoretical math opportunities and do not guarantee live execution.",
            "- Real funds trading and mutating transactions remain strictly disabled.",
        ]
    )

    return "\n".join(lines) + "\n"
