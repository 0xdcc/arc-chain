"""Standardized security audit event models and event sinks for dex-sniper-engine.

Architecture Constraints:
1. Observability Only: Security audit events are strictly for observability,
   monitoring, and forensics. They are NEVER authoritative for wallet balance,
   funds ledger, or trade execution state.
2. Invariant Enforcement: `is_authoritative_funds` MUST strictly be False.
3. Mode Isolation: Strictly differentiates 'live', 'historical_replay', and
   'synthetic' modes. Non-live modes are explicitly tagged to prevent spoofing.
4. Zero External Dependencies: Pure Python standard library + domain data contracts.
   Zero network sockets, zero subprocess creation, zero side-effects.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from research.market_data.types import (
    CandidateRoute,
    ExecutionPlan,
    QuoteResult,
    QuoteStatus,
    to_dict,
)

SCHEMA_VERSION: str = "1.0.0"
AUTHORITATIVE_FUNDS_DISCLAIMER: str = "OBSERVABILITY_ONLY: NOT AUTHORITATIVE FOR FUNDS"


class ExecutionMode(StrEnum):
    """Execution mode categorization for audit trails and anti-counterfeiting."""

    LIVE = "live"
    HISTORICAL_REPLAY = "historical_replay"
    SYNTHETIC = "synthetic"


ALLOWED_MODES: tuple[str, ...] = tuple(m.value for m in ExecutionMode)


class AuditEventType(StrEnum):
    """Standardized event classification for security and operational monitoring."""

    OPPORTUNITY_DETECTED = "opportunity_detected"
    QUOTE_QUERY = "quote_query"
    QUOTE_RESULT = "quote_result"
    QUOTE_FAILED = "quote_failed"
    PLAN_GENERATED = "plan_generated"
    SECURITY_BLOCKED = "security_blocked"
    EXECUTION_SUBMITTED = "execution_submitted"
    EXECUTION_CONFIRMED = "execution_confirmed"
    EXECUTION_REVERTED = "execution_reverted"
    REPLAY_COMPLETED = "replay_completed"
    CUSTOM_AUDIT = "custom_audit"


class AuditSeverity(StrEnum):
    """Severity levels for audit events."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


def normalize_mode(mode: str | ExecutionMode | None) -> str:
    """Validate and normalize execution mode.

    Args:
        mode: Mode string, ExecutionMode enum, or None (defaults to 'live').

    Returns:
        Normalized mode string in ('live', 'historical_replay', 'synthetic').

    Raises:
        ValueError: If mode is not one of the allowed execution modes.
    """
    if mode is None:
        return ExecutionMode.LIVE.value
    val = str(mode.value if isinstance(mode, ExecutionMode) else mode).strip().lower()
    if val not in ALLOWED_MODES:
        raise ValueError(f"Invalid execution mode '{mode}'. Must be one of {ALLOWED_MODES}.")
    return val


@dataclass(frozen=True)
class SecurityAuditEvent:
    """Standardized security and audit trail event.

    Safety Invariant:
    `is_authoritative_funds` MUST strictly be False. Security audit events are
    purely observational and must never be treated as the financial ledger of record.
    """

    event_id: str
    event_type: str
    schema_version: str = SCHEMA_VERSION
    timestamp: float = field(default_factory=time.time)
    mode: str = ExecutionMode.LIVE.value
    is_authoritative_funds: bool = False
    authority_disclaimer: str = AUTHORITATIVE_FUNDS_DISCLAIMER
    severity: str = AuditSeverity.INFO.value
    payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not self.event_id.strip():
            raise ValueError("event_id must be a non-empty string")

        if not isinstance(self.schema_version, str) or not self.schema_version.strip():
            raise ValueError("schema_version must be a non-empty string")

        # Invariant Red Line: Observability Only
        if self.is_authoritative_funds is not False:
            raise ValueError(
                "SecurityAuditEvent invariant violation: is_authoritative_funds must strictly be False. "
                "Events are strictly for observability and audit, never authoritative for funds or balance."
            )

        norm_mode = normalize_mode(self.mode)
        object.__setattr__(self, "mode", norm_mode)

        # Normalize event_type
        if hasattr(self.event_type, "value"):
            object.__setattr__(self, "event_type", str(self.event_type.value))
        else:
            if not isinstance(self.event_type, str) or not self.event_type.strip():
                raise ValueError("event_type must be a non-empty string")
            object.__setattr__(self, "event_type", self.event_type.strip())

        # Normalize severity
        if hasattr(self.severity, "value"):
            object.__setattr__(self, "severity", str(self.severity.value))
        else:
            sev_str = str(self.severity).upper().strip()
            object.__setattr__(self, "severity", sev_str)

        # Enforce disclaimer
        if self.authority_disclaimer != AUTHORITATIVE_FUNDS_DISCLAIMER:
            object.__setattr__(self, "authority_disclaimer", AUTHORITATIVE_FUNDS_DISCLAIMER)

    def to_dict(self) -> dict[str, Any]:
        """Convert audit event to a standard JSON-serializable dictionary."""
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "mode": self.mode,
            "is_authoritative_funds": self.is_authoritative_funds,
            "authority_disclaimer": self.authority_disclaimer,
            "severity": self.severity,
            "payload": to_dict(self.payload),
            "metadata": to_dict(self.metadata),
        }

    def to_json(self, indent: int | None = None) -> str:
        """Serialize event to a formatted JSON string."""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, default=str)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SecurityAuditEvent:
        """Construct SecurityAuditEvent from standard schema dictionary."""
        return cls(
            event_id=str(data["event_id"]),
            event_type=str(data["event_type"]),
            schema_version=str(data.get("schema_version", SCHEMA_VERSION)),
            timestamp=float(data.get("timestamp", time.time())),
            mode=str(data.get("mode", ExecutionMode.LIVE.value)),
            is_authoritative_funds=bool(data.get("is_authoritative_funds", False)),
            authority_disclaimer=str(
                data.get("authority_disclaimer", AUTHORITATIVE_FUNDS_DISCLAIMER)
            ),
            severity=str(data.get("severity", AuditSeverity.INFO.value)),
            payload=dict(data.get("payload", {})),
            metadata=dict(data.get("metadata", {})),
        )

    @classmethod
    def from_json(cls, text: str) -> SecurityAuditEvent:
        """Parse SecurityAuditEvent from JSON string."""
        return cls.from_dict(json.loads(text))


def _generate_event_id(prefix: str = "evt") -> str:
    """Generate deterministic-length random event ID."""
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def create_quote_audit_event(
    quote: QuoteResult,
    route: CandidateRoute | None = None,
    mode: str | ExecutionMode | None = None,
    metadata: dict[str, Any] | None = None,
) -> SecurityAuditEvent:
    """Construct an audit event from a QuoteResult with error classification.

    Args:
        quote: Quoting result from domain layer.
        route: Optional candidate route evaluated.
        mode: Explicit mode override; defaults to quote.quote_mode if unspecified.
        metadata: Optional additional metadata context.

    Returns:
        Structured SecurityAuditEvent.
    """
    resolved_mode = normalize_mode(mode if mode is not None else quote.quote_mode)

    is_success = quote.status == QuoteStatus.QUOTED
    event_type = (
        AuditEventType.QUOTE_RESULT.value if is_success else AuditEventType.QUOTE_FAILED.value
    )

    severity = (
        AuditSeverity.INFO.value
        if is_success
        else (
            AuditSeverity.WARNING.value
            if quote.status == QuoteStatus.CONTRACT_REVERT
            else AuditSeverity.ERROR.value
        )
    )

    amount_in_dict = quote.amount_in.to_dict()
    amount_out_dict = quote.amount_out.to_dict() if quote.amount_out is not None else None

    # Lossless profit calculation without floating-point pollution
    delta_decimal: str | None = None
    if quote.delta_atoms is not None:
        delta_dec = Decimal(quote.delta_atoms) / (Decimal(10) ** quote.amount_in.token.decimals)
        delta_decimal = str(delta_dec)

    payload: dict[str, Any] = {
        "status": quote.status.value,
        "is_success": is_success,
        "amount_in": amount_in_dict,
        "amount_out": amount_out_dict,
        "delta_atoms": quote.delta_atoms,
        "delta_decimal": delta_decimal,
        "gas_estimate": quote.gas_estimate,
        "error_code": quote.error_code,
        "error_message": quote.error_message,
        "raw_revert_data": quote.raw_revert_data,
        "block_number": quote.block_number,
        "quote_mode": resolved_mode,
    }

    if route is not None:
        payload["candidate_id"] = route.candidate_id
        payload["route_type"] = route.route_type
        payload["base_token"] = route.base_token.symbol
        payload["hop_count"] = len(route.hops)
        payload["observed_gross_bps"] = route.observed_gross_bps

    return SecurityAuditEvent(
        event_id=_generate_event_id("quote"),
        event_type=event_type,
        schema_version=SCHEMA_VERSION,
        mode=resolved_mode,
        severity=severity,
        payload=payload,
        metadata=dict(metadata or {}),
    )


def create_execution_plan_audit_event(
    plan: ExecutionPlan,
    mode: str | ExecutionMode = ExecutionMode.LIVE.value,
    metadata: dict[str, Any] | None = None,
) -> SecurityAuditEvent:
    """Construct an audit event for a generated execution plan.

    Args:
        plan: Execution plan ready for dispatch.
        mode: Execution mode.
        metadata: Optional metadata.

    Returns:
        Structured SecurityAuditEvent.
    """
    resolved_mode = normalize_mode(mode)
    payload = {
        "plan_id": plan.plan_id,
        "candidate_id": plan.candidate_id,
        "route_type": plan.route_type,
        "base_token": plan.base_token.symbol,
        "amount_in": plan.amount_in.to_dict(),
        "min_amount_out": plan.min_amount_out.to_dict(),
        "hop_count": len(plan.hops),
        "quoter_block": plan.quoter_block,
        "deadline": plan.deadline,
        "estimated_gas_usd": str(plan.estimated_gas_usd),
        "target_router": plan.target_router,
    }

    return SecurityAuditEvent(
        event_id=_generate_event_id("plan"),
        event_type=AuditEventType.PLAN_GENERATED.value,
        schema_version=SCHEMA_VERSION,
        mode=resolved_mode,
        severity=AuditSeverity.INFO.value,
        payload=payload,
        metadata=dict(metadata or {}),
    )


def create_candidate_route_audit_event(
    route: CandidateRoute,
    mode: str | ExecutionMode = ExecutionMode.LIVE.value,
    metadata: dict[str, Any] | None = None,
) -> SecurityAuditEvent:
    """Construct an audit event for a discovered arbitrage opportunity.

    Args:
        route: Discovered closed candidate route.
        mode: Execution mode.
        metadata: Optional metadata.

    Returns:
        Structured SecurityAuditEvent.
    """
    resolved_mode = normalize_mode(mode)
    hops_summary = [
        {
            "hop_index": idx,
            "pool_id": h.pool.pool_id,
            "protocol": h.pool.protocol,
            "fee_bps": h.pool.fee_bps,
            "token_in": h.token_in.symbol,
            "token_out": h.token_out.symbol,
        }
        for idx, h in enumerate(route.hops)
    ]

    payload = {
        "candidate_id": route.candidate_id,
        "route_type": route.route_type,
        "base_token": route.base_token.symbol,
        "hop_count": len(route.hops),
        "hops": hops_summary,
        "observed_gross_bps": route.observed_gross_bps,
        "snapshot_block": route.snapshot_block,
    }

    return SecurityAuditEvent(
        event_id=_generate_event_id("route"),
        event_type=AuditEventType.OPPORTUNITY_DETECTED.value,
        schema_version=SCHEMA_VERSION,
        mode=resolved_mode,
        severity=AuditSeverity.INFO.value,
        payload=payload,
        metadata=dict(metadata or {}),
    )


def create_security_blocked_audit_event(
    rule_name: str,
    reason: str,
    details: dict[str, Any] | None = None,
    mode: str | ExecutionMode = ExecutionMode.LIVE.value,
    severity: str | AuditSeverity = AuditSeverity.CRITICAL.value,
    metadata: dict[str, Any] | None = None,
) -> SecurityAuditEvent:
    """Construct an audit event when a security guardrail blocks an action.

    Args:
        rule_name: Name of the safety rule triggered.
        reason: Plain-text explanation of the rejection.
        details: Diagnostic details and rejected parameters.
        mode: Execution mode.
        severity: Severity level (defaults to CRITICAL).
        metadata: Optional metadata.

    Returns:
        Structured SecurityAuditEvent.
    """
    resolved_mode = normalize_mode(mode)
    payload = {
        "rule_name": rule_name,
        "reason": reason,
        "details": details or {},
    }

    return SecurityAuditEvent(
        event_id=_generate_event_id("risk"),
        event_type=AuditEventType.SECURITY_BLOCKED.value,
        schema_version=SCHEMA_VERSION,
        mode=resolved_mode,
        severity=str(severity.value if isinstance(severity, AuditSeverity) else severity),
        payload=payload,
        metadata=dict(metadata or {}),
    )


def create_report_audit_event(
    report: Any,
    mode: str | ExecutionMode | None = None,
    metadata: dict[str, Any] | None = None,
) -> SecurityAuditEvent:
    """Construct an audit event from an ArbitrageReport.

    Args:
        report: Arbitrage report to audit.
        mode: Execution mode override (defaults to report.mode).
        metadata: Optional metadata.

    Returns:
        Structured SecurityAuditEvent.
    """
    report_mode = getattr(report, "mode", ExecutionMode.LIVE.value)
    resolved_mode = normalize_mode(mode if mode is not None else report_mode)

    is_executed = getattr(report, "executed", False)
    has_error = bool(getattr(report, "error_message", None))

    event_type = (
        AuditEventType.EXECUTION_CONFIRMED.value
        if is_executed and not has_error
        else (
            AuditEventType.EXECUTION_REVERTED.value
            if has_error
            else AuditEventType.CUSTOM_AUDIT.value
        )
    )

    severity = AuditSeverity.ERROR.value if has_error else AuditSeverity.INFO.value

    payload = {
        "report_id": getattr(report, "report_id", _generate_event_id("rep")),
        "executed": is_executed,
        "tx_hash": getattr(report, "tx_hash", None),
        "error_message": getattr(report, "error_message", None),
        "has_route": getattr(report, "route", None) is not None,
        "has_quote": getattr(report, "quote", None) is not None,
        "has_plan": getattr(report, "plan", None) is not None,
    }

    return SecurityAuditEvent(
        event_id=_generate_event_id("rep_evt"),
        event_type=event_type,
        schema_version=SCHEMA_VERSION,
        mode=resolved_mode,
        severity=severity,
        payload=payload,
        metadata=dict(metadata or {}),
    )


class InMemoryEventSink:
    """Thread-safe, side-effect-free in-memory audit log collector.

    Used for testing, inspection, and local log buffering without external I/O.
    """

    def __init__(self) -> None:
        self._events: list[SecurityAuditEvent] = []

    def append(self, event: SecurityAuditEvent) -> None:
        """Store an event in the in-memory buffer."""
        if not isinstance(event, SecurityAuditEvent):
            raise TypeError(f"Expected SecurityAuditEvent, got {type(event).__name__}")
        self._events.append(event)

    @property
    def events(self) -> list[SecurityAuditEvent]:
        """Return a snapshot copy of all recorded events."""
        return list(self._events)

    def filter(
        self,
        event_type: str | None = None,
        mode: str | None = None,
        severity: str | None = None,
    ) -> list[SecurityAuditEvent]:
        """Filter stored events by type, mode, or severity."""
        res = self._events
        if event_type is not None:
            res = [e for e in res if e.event_type == event_type]
        if mode is not None:
            res = [e for e in res if e.mode == mode]
        if severity is not None:
            res = [e for e in res if e.severity == severity]
        return list(res)

    def clear(self) -> None:
        """Clear all stored events."""
        self._events.clear()

    def __len__(self) -> int:
        return len(self._events)
