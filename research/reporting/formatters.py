"""Unified report formatters, anti-spoof watermarking, and safe notification sinks.

Architecture Constraints:
1. Mode Isolation & Anti-Counterfeiting:
   Strictly separates 'live', 'historical_replay', and 'synthetic' modes. Replay
   and synthetic results are prominently stamped with anti-spoof watermarks and
   warnings. Under NO circumstances can non-live data be presented as real profit.
2. Field Fault-Tolerance:
   Handles unquoted paths, missing amount_out, None delta_atoms, and contract reverts
   gracefully. Never raises unhandled exceptions, and NEVER fabricates artificial numbers.
3. Observability Purity:
   Reports and events are strictly non-authoritative for funds balances.
4. Physical Side-Effect Isolation:
   External notification sinks are hard-blocked during non-live modes (replay/synthetic)
   or when not explicitly enabled. Real subprocess spawning and shell execution
   are strictly prohibited.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from research.market_data.types import (
    CandidateRoute,
    ExecutionPlan,
    QuoteResult,
    QuoteStatus,
    TokenAmount,
    to_dict,
)
from research.reporting.events import (
    AUTHORITATIVE_FUNDS_DISCLAIMER,
    SCHEMA_VERSION,
    ExecutionMode,
    SecurityAuditEvent,
    normalize_mode,
)

# Prominent anti-spoof watermarks for non-live modes
WATERMARK_REPLAY: str = "HISTORICAL_REPLAY_SIMULATION_NOT_REAL_PROFIT"
WATERMARK_HISTORICAL_REPLAY: str = WATERMARK_REPLAY
WATERMARK_SYNTHETIC: str = "SYNTHETIC_TEST_RUN_NOT_REAL_PROFIT"
WATERMARK_LIVE: str = "LIVE_ONCHAIN_EXECUTION"


@dataclass(frozen=True)
class ArbitrageReport:
    """Consolidated execution and opportunity report for an arbitrage cycle."""

    report_id: str
    mode: str = ExecutionMode.LIVE.value
    created_at: float = field(default_factory=time.time)
    schema_version: str = SCHEMA_VERSION
    route: CandidateRoute | None = None
    quote: QuoteResult | None = None
    plan: ExecutionPlan | None = None
    executed: bool = False
    tx_hash: str | None = None
    error_message: str | None = None
    notes: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.report_id, str) or not self.report_id.strip():
            raise ValueError("report_id must be a non-empty string")

        norm_mode = normalize_mode(self.mode)
        object.__setattr__(self, "mode", norm_mode)

        if not isinstance(self.schema_version, str) or not self.schema_version.strip():
            raise ValueError("schema_version must be a non-empty string")

    @property
    def is_live(self) -> bool:
        """Indicate whether report reflects actual live production execution."""
        return self.mode == ExecutionMode.LIVE.value


# ==============================================================================
# Helper Formatting Utilities (Fault-Tolerant, Lossless)
# ==============================================================================


def format_token_amount(amount: TokenAmount | None) -> str:
    """Format TokenAmount into human-readable string without float loss.

    Returns 'N/A' if amount is None, preventing exceptions.
    """
    if amount is None:
        return "N/A"
    try:
        dec = amount.to_decimal()
        return f"{dec} {amount.token.symbol} ({amount.atoms} atoms)"
    except Exception:
        return f"Unknown TokenAmount ({getattr(amount, 'atoms', 'N/A')} atoms)"


def format_delta_profit(
    amount_in: TokenAmount | None,
    amount_out: TokenAmount | None,
    delta_atoms: int | None,
    mode: str = ExecutionMode.LIVE.value,
) -> str:
    """Format net arbitrage profit/loss with mode isolation watermarks.

    Safety Rules:
    - Never fabricates numbers if delta_atoms is None.
    - Never masquerades non-live replay/synthetic profits as real money.
    """
    if delta_atoms is None or amount_in is None:
        return "N/A"

    # Base token decimal scaling
    decimals = amount_in.token.decimals
    symbol = amount_in.token.symbol
    delta_dec = Decimal(delta_atoms) / (Decimal(10) ** decimals)
    sign = "+" if delta_atoms > 0 else ""

    base_str = f"{sign}{delta_dec} {symbol} ({sign}{delta_atoms} atoms)"

    # Mode isolation watermark
    norm_mode = normalize_mode(mode)
    if norm_mode == ExecutionMode.HISTORICAL_REPLAY.value:
        return f"{base_str} [REPLAY SIMULATED - NOT REAL MONEY]"
    elif norm_mode == ExecutionMode.SYNTHETIC.value:
        return f"{base_str} [SYNTHETIC TEST - NOT REAL MONEY]"
    return base_str


def _get_anti_counterfeit_payload(mode: str) -> dict[str, Any]:
    """Generate tamper-evident mode metadata dictionary."""
    norm_mode = normalize_mode(mode)
    is_live = norm_mode == ExecutionMode.LIVE.value
    if norm_mode == ExecutionMode.HISTORICAL_REPLAY.value:
        watermark = WATERMARK_REPLAY
        funds_impact = "ZERO (SIMULATION)"
        disclaimer = "NON-LIVE HISTORICAL REPLAY: DOES NOT REPRESENT REAL PROFIT OR LEDGER BALANCES"
    elif norm_mode == ExecutionMode.SYNTHETIC.value:
        watermark = WATERMARK_SYNTHETIC
        funds_impact = "ZERO (MOCK)"
        disclaimer = "NON-LIVE SYNTHETIC DATA: DOES NOT REPRESENT REAL MARKET EXECUTION"
    else:
        watermark = WATERMARK_LIVE
        funds_impact = "NON_AUTHORITATIVE_OBSERVABILITY"
        disclaimer = AUTHORITATIVE_FUNDS_DISCLAIMER

    return {
        "is_live": is_live,
        "mode": norm_mode,
        "watermark": watermark,
        "funds_impact": funds_impact,
        "disclaimer": disclaimer,
    }


# ==============================================================================
# Structured JSON Formatters
# ==============================================================================


def format_quote_dict(
    quote: QuoteResult, mode: str | ExecutionMode | None = None
) -> dict[str, Any]:
    """Convert QuoteResult into a structured dictionary with mode protection."""
    resolved_mode = normalize_mode(mode if mode is not None else quote.quote_mode)
    anti_spoof = _get_anti_counterfeit_payload(resolved_mode)

    delta_decimal: str | None = None
    if quote.delta_atoms is not None:
        delta_dec = Decimal(quote.delta_atoms) / (Decimal(10) ** quote.amount_in.token.decimals)
        delta_decimal = str(delta_dec)

    return {
        "schema_version": SCHEMA_VERSION,
        "mode": resolved_mode,
        "is_live": anti_spoof["is_live"],
        "is_authoritative_funds": False,
        "anti_counterfeit": anti_spoof,
        "status": quote.status.value,
        "is_quoted": quote.status == QuoteStatus.QUOTED,
        "amount_in": quote.amount_in.to_dict(),
        "amount_out": quote.amount_out.to_dict() if quote.amount_out is not None else None,
        "delta_atoms": quote.delta_atoms,
        "delta_decimal": delta_decimal,
        "gas_estimate": quote.gas_estimate,
        "error_code": quote.error_code,
        "error_message": quote.error_message,
        "raw_revert_data": quote.raw_revert_data,
        "block_number": quote.block_number,
    }


def format_quote_json(
    quote: QuoteResult, mode: str | ExecutionMode | None = None, indent: int = 2
) -> str:
    """Export QuoteResult to formatted JSON string."""
    return json.dumps(format_quote_dict(quote, mode=mode), ensure_ascii=False, indent=indent)


def format_route_dict(route: CandidateRoute) -> dict[str, Any]:
    """Convert CandidateRoute into a structured dictionary."""
    hops_data = []
    for idx, h in enumerate(route.hops):
        hops_data.append(
            {
                "hop_index": idx,
                "protocol": h.pool.protocol,
                "pool_id": h.pool.pool_id,
                "fee_bps": h.pool.fee_bps,
                "token_in": h.token_in.symbol,
                "token_in_address": h.token_in.address,
                "token_out": h.token_out.symbol,
                "token_out_address": h.token_out.address,
            }
        )

    return {
        "candidate_id": route.candidate_id,
        "route_type": route.route_type,
        "base_token": route.base_token.to_dict(),
        "hop_count": len(route.hops),
        "hops": hops_data,
        "observed_gross_bps": route.observed_gross_bps,
        "snapshot_block": route.snapshot_block,
        "created_at": route.created_at,
    }


def format_route_json(route: CandidateRoute, indent: int = 2) -> str:
    """Export CandidateRoute to formatted JSON string."""
    return json.dumps(format_route_dict(route), ensure_ascii=False, indent=indent)


def format_plan_dict(plan: ExecutionPlan) -> dict[str, Any]:
    """Convert ExecutionPlan into a structured dictionary."""
    return {
        "plan_id": plan.plan_id,
        "candidate_id": plan.candidate_id,
        "route_type": plan.route_type,
        "base_token": plan.base_token.to_dict(),
        "amount_in": plan.amount_in.to_dict(),
        "min_amount_out": plan.min_amount_out.to_dict(),
        "hop_count": len(plan.hops),
        "quoter_block": plan.quoter_block,
        "deadline": plan.deadline,
        "estimated_gas_usd": str(plan.estimated_gas_usd),
        "target_router": plan.target_router,
    }


def format_plan_json(plan: ExecutionPlan, indent: int = 2) -> str:
    """Export ExecutionPlan to formatted JSON string."""
    return json.dumps(format_plan_dict(plan), ensure_ascii=False, indent=indent)


def format_report_dict(report: ArbitrageReport) -> dict[str, Any]:
    """Export ArbitrageReport to structured dictionary."""
    anti_spoof = _get_anti_counterfeit_payload(report.mode)

    return {
        "report_id": report.report_id,
        "schema_version": report.schema_version,
        "created_at": report.created_at,
        "mode": report.mode,
        "is_live": report.is_live,
        "is_authoritative_funds": False,
        "anti_counterfeit": anti_spoof,
        "executed": report.executed,
        "tx_hash": report.tx_hash,
        "error_message": report.error_message,
        "notes": report.notes,
        "route": format_route_dict(report.route) if report.route is not None else None,
        "quote": format_quote_dict(report.quote, mode=report.mode)
        if report.quote is not None
        else None,
        "plan": format_plan_dict(report.plan) if report.plan is not None else None,
        "metadata": to_dict(report.metadata),
    }


def format_report_json(report: ArbitrageReport, indent: int = 2) -> str:
    """Export ArbitrageReport to formatted JSON string."""
    return json.dumps(format_report_dict(report), ensure_ascii=False, indent=indent)


# ==============================================================================
# Plain Text Summary Formatters
# ==============================================================================


def format_quote_text(quote: QuoteResult, mode: str | ExecutionMode | None = None) -> str:
    """Render human-readable text summary of QuoteResult."""
    resolved_mode = normalize_mode(mode if mode is not None else quote.quote_mode)

    lines: list[str] = []
    if resolved_mode == ExecutionMode.HISTORICAL_REPLAY.value:
        lines.append("=== QUOTE RESULT [MODE: HISTORICAL_REPLAY - NOT REAL PROFIT] ===")
    elif resolved_mode == ExecutionMode.SYNTHETIC.value:
        lines.append("=== QUOTE RESULT [MODE: SYNTHETIC - NOT REAL PROFIT] ===")
    else:
        lines.append("=== QUOTE RESULT [MODE: LIVE] ===")

    lines.append(f"Status: {quote.status.value}")
    lines.append(f"Amount In: {format_token_amount(quote.amount_in)}")

    if quote.status == QuoteStatus.QUOTED:
        lines.append(f"Amount Out: {format_token_amount(quote.amount_out)}")
        profit_str = format_delta_profit(
            quote.amount_in, quote.amount_out, quote.delta_atoms, mode=resolved_mode
        )
        lines.append(f"Net Profit: {profit_str}")
        if quote.gas_estimate is not None:
            lines.append(f"Gas Estimate: {quote.gas_estimate}")
    else:
        lines.append("Amount Out: N/A (Quote Failed)")
        lines.append("Net Profit: N/A")
        if quote.error_message:
            lines.append(f"Error: {quote.error_message}")
        if quote.error_code is not None:
            lines.append(f"Error Code: {quote.error_code}")
        if quote.raw_revert_data:
            lines.append(f"Revert Data: {quote.raw_revert_data}")

    if quote.block_number is not None:
        lines.append(f"Block: {quote.block_number}")

    lines.append(f"Authority: {AUTHORITATIVE_FUNDS_DISCLAIMER}")
    return "\n".join(lines)


def format_route_text(route: CandidateRoute) -> str:
    """Render human-readable text summary of CandidateRoute."""
    lines = [
        f"=== CANDIDATE ROUTE [{route.candidate_id}] ===",
        f"Type: {route.route_type}",
        f"Base Token: {route.base_token.symbol} ({route.base_token.address})",
        f"Observed Gross: {route.observed_gross_bps:.2f} bps",
        f"Snapshot Block: {route.snapshot_block}",
        f"Hops ({len(route.hops)}):",
    ]
    for idx, h in enumerate(route.hops, 1):
        lines.append(
            f"  {idx}. {h.token_in.symbol} -> {h.token_out.symbol} "
            f"via {h.pool.protocol} ({h.pool.pool_id[:10]}..., fee: {h.pool.fee_bps} bps)"
        )
    return "\n".join(lines)


def format_plan_text(plan: ExecutionPlan) -> str:
    """Render human-readable text summary of ExecutionPlan."""
    lines = [
        f"=== EXECUTION PLAN [{plan.plan_id}] ===",
        f"Candidate: {plan.candidate_id} ({plan.route_type})",
        f"Router: {plan.target_router}",
        f"Amount In: {format_token_amount(plan.amount_in)}",
        f"Min Amount Out: {format_token_amount(plan.min_amount_out)}",
        f"Estimated Gas: ${plan.estimated_gas_usd}",
        f"Block / Deadline: {plan.quoter_block} / {plan.deadline}",
    ]
    return "\n".join(lines)


def format_report_text(report: ArbitrageReport) -> str:
    """Render comprehensive plain-text summary of ArbitrageReport."""
    lines: list[str] = []

    if report.mode == ExecutionMode.HISTORICAL_REPLAY.value:
        lines.append("****************************************************************")
        lines.append("*** HISTORICAL REPLAY REPORT - SIMULATION ONLY (NOT REAL PROFIT) ***")
        lines.append("****************************************************************")
    elif report.mode == ExecutionMode.SYNTHETIC.value:
        lines.append("****************************************************************")
        lines.append("*** SYNTHETIC TEST REPORT - MOCK DATA ONLY (NOT REAL PROFIT)    ***")
        lines.append("****************************************************************")
    else:
        lines.append("================================================================")
        lines.append("=== ARBITRAGE EXECUTION REPORT [LIVE]                        ===")
        lines.append("================================================================")

    lines.append(f"Report ID: {report.report_id}")
    lines.append(f"Mode: {report.mode} (Live: {report.is_live})")
    lines.append(f"Executed: {report.executed}")
    if report.tx_hash:
        lines.append(f"Tx Hash: {report.tx_hash}")
    if report.error_message:
        lines.append(f"Failure Reason: {report.error_message}")
    if report.notes:
        lines.append(f"Notes: {report.notes}")

    if report.route is not None:
        lines.append("")
        lines.append(format_route_text(report.route))

    if report.quote is not None:
        lines.append("")
        lines.append(format_quote_text(report.quote, mode=report.mode))

    if report.plan is not None:
        lines.append("")
        lines.append(format_plan_text(report.plan))

    lines.append("")
    lines.append(f"Disclaimer: {AUTHORITATIVE_FUNDS_DISCLAIMER}")
    return "\n".join(lines)


# ==============================================================================
# Markdown Card Formatters
# ==============================================================================


def format_quote_markdown(quote: QuoteResult, mode: str | ExecutionMode | None = None) -> str:
    """Render rich Markdown card for QuoteResult."""
    resolved_mode = normalize_mode(mode if mode is not None else quote.quote_mode)

    parts: list[str] = []

    # Anti-counterfeiting banners
    if resolved_mode == ExecutionMode.HISTORICAL_REPLAY.value:
        parts.append(
            "> ⚠️ **HISTORICAL REPLAY SIMULATION — NOT REAL PROFIT**\n"
            "> *This quote is from an offline replay. No real funds were moved or earned.*"
        )
    elif resolved_mode == ExecutionMode.SYNTHETIC.value:
        parts.append(
            "> 🧪 **SYNTHETIC TEST RUN — NOT REAL PROFIT**\n"
            "> *This quote is synthetic test data. No real funds were moved or earned.*"
        )

    status_icon = "✅" if quote.status == QuoteStatus.QUOTED else "❌"
    parts.append(f"### {status_icon} Quote Status: `{quote.status.value}`")

    # Details table
    rows = [
        "| Field | Value |",
        "| :--- | :--- |",
        f"| **Mode** | `{resolved_mode}` |",
        f"| **Amount In** | {format_token_amount(quote.amount_in)} |",
    ]

    if quote.status == QuoteStatus.QUOTED:
        rows.append(f"| **Amount Out** | {format_token_amount(quote.amount_out)} |")
        profit_str = format_delta_profit(
            quote.amount_in, quote.amount_out, quote.delta_atoms, mode=resolved_mode
        )
        rows.append(f"| **Net Profit** | **{profit_str}** |")
        if quote.gas_estimate is not None:
            rows.append(f"| **Gas Estimate** | `{quote.gas_estimate}` units |")
    else:
        rows.append("| **Amount Out** | *N/A (Failed to Quote)* |")
        rows.append("| **Net Profit** | *N/A* |")
        if quote.error_message:
            rows.append(f"| **Error Message** | `{quote.error_message}` |")
        if quote.error_code is not None:
            rows.append(f"| **Error Code** | `{quote.error_code}` |")
        if quote.raw_revert_data:
            rows.append(f"| **Revert Data** | `{quote.raw_revert_data[:32]}...` |")

    if quote.block_number is not None:
        rows.append(f"| **Block Number** | `{quote.block_number}` |")

    parts.append("\n".join(rows))
    parts.append(f"\n*Notice: {AUTHORITATIVE_FUNDS_DISCLAIMER}*")
    return "\n\n".join(parts)


def format_route_markdown(route: CandidateRoute) -> str:
    """Render Markdown card for CandidateRoute."""
    parts = [
        f"### 🔀 Candidate Route: `{route.candidate_id}`",
        f"- **Type**: `{route.route_type}`",
        f"- **Base Token**: `{route.base_token.symbol}` (`{route.base_token.address}`)",
        f"- **Observed Spread**: **{route.observed_gross_bps:.2f} bps**",
        f"- **Snapshot Block**: `{route.snapshot_block}`",
        "",
        "| # | Protocol | Pool ID | Leg | Fee (bps) |",
        "| :- | :--- | :--- | :--- | :- |",
    ]
    for idx, h in enumerate(route.hops, 1):
        leg_str = f"`{h.token_in.symbol}` ➔ `{h.token_out.symbol}`"
        parts.append(
            f"| {idx} | `{h.pool.protocol}` | `{h.pool.pool_id[:10]}...` | {leg_str} | {h.pool.fee_bps} |"
        )
    return "\n".join(parts)


def format_plan_markdown(plan: ExecutionPlan) -> str:
    """Render Markdown card for ExecutionPlan."""
    rows = [
        f"### 📋 Execution Plan: `{plan.plan_id}`",
        "| Field | Value |",
        "| :--- | :--- |",
        f"| **Candidate ID** | `{plan.candidate_id}` |",
        f"| **Router** | `{plan.target_router}` |",
        f"| **Amount In** | {format_token_amount(plan.amount_in)} |",
        f"| **Min Amount Out** | **{format_token_amount(plan.min_amount_out)}** (Slippage Protected) |",
        f"| **Estimated Gas** | `${plan.estimated_gas_usd}` |",
        f"| **Block / Deadline** | `{plan.quoter_block}` / `{plan.deadline}` |",
    ]
    return "\n".join(rows)


def format_report_markdown(report: ArbitrageReport) -> str:
    """Render full Markdown card for ArbitrageReport."""
    sections: list[str] = []

    # Banner
    if report.mode == ExecutionMode.HISTORICAL_REPLAY.value:
        sections.append(
            "> ⚠️ **HISTORICAL REPLAY REPORT (SIMULATION — NOT REAL PROFIT)**\n"
            "> This is an offline backtest / historical replay report. "
            "No real transactions or balance updates occurred."
        )
    elif report.mode == ExecutionMode.SYNTHETIC.value:
        sections.append(
            "> 🧪 **SYNTHETIC TEST REPORT (MOCK DATA — NOT REAL PROFIT)**\n"
            "> This report was generated from synthetic test fixtures. "
            "It does not represent actual market conditions."
        )
    else:
        sections.append(f"## ⚡ Arbitrage Report: `{report.report_id}`")

    # Overview table
    overview_rows = [
        "| Parameter | Value |",
        "| :--- | :--- |",
        f"| **Report ID** | `{report.report_id}` |",
        f"| **Mode** | `{report.mode}` (Live: `{report.is_live}`) |",
        f"| **Executed** | `{'Yes' if report.executed else 'No'}` |",
    ]
    if report.tx_hash:
        overview_rows.append(f"| **Transaction** | `{report.tx_hash}` |")
    if report.error_message:
        overview_rows.append(f"| **Failure** | ❌ `{report.error_message}` |")
    if report.notes:
        overview_rows.append(f"| **Notes** | {report.notes} |")

    sections.append("\n".join(overview_rows))

    if report.route is not None:
        sections.append(format_route_markdown(report.route))

    if report.quote is not None:
        sections.append(format_quote_markdown(report.quote, mode=report.mode))

    if report.plan is not None:
        sections.append(format_plan_markdown(report.plan))

    sections.append(f"\n*Notice: {AUTHORITATIVE_FUNDS_DISCLAIMER}*")
    return "\n\n".join(sections)


# ==============================================================================
# Side-Effect Isolated Notification Dispatcher
# ==============================================================================


@dataclass(frozen=True)
class DispatchResult:
    """Result of attempting to dispatch an event or report."""

    dispatched: bool
    mode: str
    sinks_notified: tuple[str, ...]
    blocked_reason: str | None = None


class InMemoryReportSink:
    """In-memory collector for reports and formatted payloads."""

    def __init__(self) -> None:
        self._reports: list[ArbitrageReport | SecurityAuditEvent | QuoteResult | CandidateRoute | ExecutionPlan | dict[str, Any]] = []

    def append(self, item: ArbitrageReport | SecurityAuditEvent | QuoteResult | CandidateRoute | ExecutionPlan | dict[str, Any]) -> None:
        """Store item in the in-memory sink."""
        self._reports.append(item)

    @property
    def reports(self) -> list[ArbitrageReport | SecurityAuditEvent | QuoteResult | CandidateRoute | ExecutionPlan | dict[str, Any]]:
        """Return snapshot copy of items."""
        return list(self._reports)

    def clear(self) -> None:
        """Clear all stored items."""
        self._reports.clear()

    def __len__(self) -> int:
        return len(self._reports)


class SafeNotifier:
    """Side-effect-isolated notification manager for reports and audit events.

    Security Rules:
    1. Zero Subprocess Spawning: Subprocesses, shell calls, and os.system are
       architecturally prohibited.
    2. Replay & Synthetic Isolation: When mode is 'historical_replay' or 'synthetic',
       external sinks (webhooks, alerts) are strictly blocked to prevent false alarms.
    3. Safe Defaults: Notification is disabled by default (`enabled=False`). When
       disabled, dispatch is completely silent.
    4. Error Containment: Individual sink failures do not crash the caller.
    """

    def __init__(
        self,
        enabled: bool = False,
        in_memory_sink: InMemoryReportSink | None = None,
        external_sinks: dict[str, Callable[[Any], None]] | None = None,
    ) -> None:
        self.enabled = enabled
        self.in_memory_sink = in_memory_sink
        self._external_sinks: dict[str, Callable[[Any], None]] = dict(external_sinks or {})

    def register_sink(self, name: str, sink: Callable[[Any], None]) -> None:
        """Register an external notification sink."""
        if not callable(sink):
            raise TypeError("Sink must be a callable")
        self._external_sinks[name] = sink

    def dispatch(
        self,
        item: ArbitrageReport | SecurityAuditEvent | QuoteResult | CandidateRoute | ExecutionPlan | dict[str, Any],
        mode: str | None = None,
    ) -> DispatchResult:
        """Safely dispatch report or event to configured sinks.

        Enforces mode isolation: external sinks are hard-blocked for non-live modes.
        """
        # Resolve mode
        resolved_mode = ExecutionMode.LIVE.value
        if mode is not None:
            resolved_mode = normalize_mode(mode)
        elif hasattr(item, "mode"):
            resolved_mode = normalize_mode(item.mode)
        elif isinstance(item, dict) and "mode" in item:
            resolved_mode = normalize_mode(item["mode"])

        # Record to in-memory sink whenever present
        if self.in_memory_sink is not None:
            self.in_memory_sink.append(item)

        # Check enabled
        if not self.enabled:
            return DispatchResult(
                dispatched=False,
                mode=resolved_mode,
                sinks_notified=(),
                blocked_reason="Notifier is unconfigured / disabled (enabled=False)",
            )

        # Mode isolation guardrail
        if resolved_mode != ExecutionMode.LIVE.value:
            return DispatchResult(
                dispatched=False,
                mode=resolved_mode,
                sinks_notified=(),
                blocked_reason=(
                    f"External notification blocked: mode '{resolved_mode}' is non-live. "
                    "Replay and synthetic events are isolated to prevent false alerts."
                ),
            )

        # Dispatch to external sinks safely
        notified: list[str] = []
        for sink_name, sink_fn in self._external_sinks.items():
            try:
                sink_fn(item)
                notified.append(sink_name)
            except Exception:
                # Error isolation: single sink failure does not break the chain
                pass

        return DispatchResult(
            dispatched=len(notified) > 0,
            mode=resolved_mode,
            sinks_notified=tuple(notified),
            blocked_reason=None if notified else "No external sinks succeeded",
        )
