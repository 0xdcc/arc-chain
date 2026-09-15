"""Arbitrage reporting, event models, and safe notification sinks.

Provides:
1. Standardized, versioned security audit events (`SecurityAuditEvent`, `AuditEventType`).
2. Mode-isolated, anti-counterfeit report formatters (JSON, text summary, Markdown cards).
3. Field fault-tolerant rendering for unquoted, reverted, or partial arbitrage records.
4. Physically isolated notification dispatchers (`SafeNotifier`, `InMemoryReportSink`).

Architecture Invariants:
- Zero fund tampering: strictly non-authoritative for balance or ledger state.
- Mode isolation: replay and synthetic data are prominently watermarked and disclaimed.
- Zero subprocess / socket side-effects.
"""

from research.reporting.events import (
    ALLOWED_MODES,
    AUTHORITATIVE_FUNDS_DISCLAIMER,
    SCHEMA_VERSION,
    AuditEventType,
    AuditSeverity,
    ExecutionMode,
    InMemoryEventSink,
    SecurityAuditEvent,
    create_candidate_route_audit_event,
    create_execution_plan_audit_event,
    create_quote_audit_event,
    create_report_audit_event,
    create_security_blocked_audit_event,
    normalize_mode,
)
from research.reporting.formatters import (
    WATERMARK_HISTORICAL_REPLAY,
    WATERMARK_LIVE,
    WATERMARK_SYNTHETIC,
    ArbitrageReport,
    DispatchResult,
    InMemoryReportSink,
    SafeNotifier,
    format_delta_profit,
    format_plan_dict,
    format_plan_json,
    format_plan_markdown,
    format_plan_text,
    format_quote_dict,
    format_quote_json,
    format_quote_markdown,
    format_quote_text,
    format_report_dict,
    format_report_json,
    format_report_markdown,
    format_report_text,
    format_route_dict,
    format_route_json,
    format_route_markdown,
    format_route_text,
    format_token_amount,
)

# Aliases for convenience
WATERMARK_REPLAY = WATERMARK_HISTORICAL_REPLAY

__all__ = [
    "ALLOWED_MODES",
    "AUTHORITATIVE_FUNDS_DISCLAIMER",
    "SCHEMA_VERSION",
    "AuditEventType",
    "AuditSeverity",
    "ArbitrageReport",
    "DispatchResult",
    "ExecutionMode",
    "InMemoryEventSink",
    "InMemoryReportSink",
    "SafeNotifier",
    "SecurityAuditEvent",
    "WATERMARK_HISTORICAL_REPLAY",
    "WATERMARK_LIVE",
    "WATERMARK_REPLAY",
    "WATERMARK_SYNTHETIC",
    "create_candidate_route_audit_event",
    "create_execution_plan_audit_event",
    "create_quote_audit_event",
    "create_report_audit_event",
    "create_security_blocked_audit_event",
    "format_delta_profit",
    "format_plan_dict",
    "format_plan_json",
    "format_plan_markdown",
    "format_plan_text",
    "format_quote_dict",
    "format_quote_json",
    "format_quote_markdown",
    "format_quote_text",
    "format_report_dict",
    "format_report_json",
    "format_report_markdown",
    "format_report_text",
    "format_route_dict",
    "format_route_json",
    "format_route_markdown",
    "format_route_text",
    "format_token_amount",
    "normalize_mode",
]
