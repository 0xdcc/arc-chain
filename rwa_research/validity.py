"""Validity, session, timing barriers, and eligibility verification kernel for RWA research."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .models import EligibilityMatrix, EquityReference, OracleObservation


class ValidityStatus(StrEnum):
    """Normalized validity status classifications."""

    VALID = "valid"
    ORACLE_PAUSED = "oracle_paused"
    ORACLE_STALE = "oracle_stale"
    HEARTBEAT_UNKNOWN = "heartbeat_unknown"
    OFF_HOURS_HOLDING = "off_hours_holding"
    SEQUENCER_DOWN = "sequencer_down"
    SEQUENCER_GRACE_PERIOD_ACTIVE = "sequencer_grace_period_active"
    CORRUPTED_ROUND = "corrupted_round"
    TIMING_FUTURE_DATA = "timing_future_data"
    REFERENCE_CLOSED_SESSION = "reference_closed_session"
    REFERENCE_CACHE_STALE = "reference_cache_stale"
    TRADING_HALT = "trading_halt"
    CORPORATE_ACTION_PENDING = "corporate_action_pending"
    CAPABILITY_UNAUTHORIZED = "capability_unauthorized"


@dataclass(frozen=True, slots=True)
class OracleValidityResult:
    """Detailed validation output for an on-chain oracle observation."""

    is_valid: bool
    status: str
    effective_multiplier_uint: int
    age_s: int
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EquityValidityResult:
    """Detailed validation output for an equity reference quote."""

    is_valid: bool
    status: str
    session_kind: str
    is_market_open: bool
    age_s: int
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CapabilityCheckResult:
    """Result of verifying whether a given trading or operational capability is authorized."""

    action: str
    is_allowed: bool
    state: str
    evidence_refs: tuple[str, ...]
    reasons: tuple[str, ...]


def validate_oracle_observation(
    oracle: OracleObservation,
    as_of_ms: int,
    *,
    max_staleness_s: int | None = None,
) -> OracleValidityResult:
    """Validate on-chain oracle freshness, pause flags, and sequencer uptime (C09-C11, C13-C14)."""
    as_of_s = as_of_ms // 1000

    # C13: Timing barrier - future data rejection (cannot look into the future during replay)
    if oracle.updated_at_s > as_of_s or oracle.started_at_s > as_of_s:
        return OracleValidityResult(
            is_valid=False,
            status=ValidityStatus.TIMING_FUTURE_DATA,
            effective_multiplier_uint=oracle.multiplier_uint,
            age_s=as_of_s - oracle.updated_at_s,
            reasons=(f"Oracle updated_at ({oracle.updated_at_s}) is in the future relative to as_of ({as_of_s})",),
        )

    # C13: Corrupted or incomplete round detection
    if (
        oracle.updated_at_s <= 0
        or oracle.answer <= 0
        or oracle.started_at_s > oracle.updated_at_s
        or oracle.answered_in_round < oracle.round_id
    ):
        return OracleValidityResult(
            is_valid=False,
            status=ValidityStatus.CORRUPTED_ROUND,
            effective_multiplier_uint=oracle.multiplier_uint,
            age_s=max(0, as_of_s - oracle.updated_at_s),
            reasons=("Oracle round parameters are inconsistent or non-positive",),
        )

    # C14: L2 Sequencer uptime and grace period check
    if oracle.sequencer_status is not None and oracle.sequencer_status.lower() != "up":
        return OracleValidityResult(
            is_valid=False,
            status=ValidityStatus.SEQUENCER_DOWN,
            effective_multiplier_uint=oracle.multiplier_uint,
            age_s=as_of_s - oracle.updated_at_s,
            reasons=(f"L2 Sequencer is down (status={oracle.sequencer_status!r})",),
        )

    if oracle.sequencer_started_at_s is not None and oracle.grace_period_s is not None:
        elapsed_since_up = as_of_s - oracle.sequencer_started_at_s
        if elapsed_since_up < oracle.grace_period_s:
            return OracleValidityResult(
                is_valid=False,
                status=ValidityStatus.SEQUENCER_GRACE_PERIOD_ACTIVE,
                effective_multiplier_uint=oracle.multiplier_uint,
                age_s=as_of_s - oracle.updated_at_s,
                reasons=(
                    f"Sequencer grace period active ({elapsed_since_up}s < {oracle.grace_period_s}s)",
                ),
            )

    # C09: Corporate actions oracle pause flag
    if oracle.oracle_paused is True:
        return OracleValidityResult(
            is_valid=False,
            status=ValidityStatus.ORACLE_PAUSED,
            effective_multiplier_uint=oracle.multiplier_uint,
            age_s=as_of_s - oracle.updated_at_s,
            reasons=("Price feed is paused on-chain during corporate action",),
        )

    # C11: Pending multiplier timeline resolution
    effective_mult = oracle.multiplier_uint
    mult_reasons: list[str] = []
    if oracle.pending_multiplier_uint is not None and oracle.effective_at_s is not None:
        if as_of_s < oracle.effective_at_s:
            # Active multiplier remains current; pending multiplier is staged for future
            effective_mult = oracle.multiplier_uint
        else:
            # Effective timestamp has arrived; pending multiplier should be active
            effective_mult = oracle.pending_multiplier_uint
            mult_reasons.append("Pending multiplier activated at effective_at_s")

    # C10: Staleness and heartbeat check
    age_s = as_of_s - oracle.updated_at_s
    threshold_s = oracle.heartbeat_s if oracle.heartbeat_s is not None else max_staleness_s

    # Off-hours tokenized equity feeds hold the last published price without heartbeat
    if oracle.session_kind in ("off_hours", "weekend", "closed"):
        return OracleValidityResult(
            is_valid=True,
            status=ValidityStatus.OFF_HOURS_HOLDING,
            effective_multiplier_uint=effective_mult,
            age_s=age_s,
            reasons=tuple(mult_reasons + ["Off-hours session holds last price; feed has no heartbeat during off-hours"]),
        )

    if threshold_s is None:
        return OracleValidityResult(
            is_valid=False,
            status=ValidityStatus.HEARTBEAT_UNKNOWN,
            effective_multiplier_uint=effective_mult,
            age_s=age_s,
            reasons=tuple(mult_reasons + ["Feed heartbeat is unknown; cannot verify staleness threshold"]),
        )

    if age_s > threshold_s:
        return OracleValidityResult(
            is_valid=False,
            status=ValidityStatus.ORACLE_STALE,
            effective_multiplier_uint=effective_mult,
            age_s=age_s,
            reasons=tuple(mult_reasons + [f"Price age ({age_s}s) exceeds heartbeat threshold ({threshold_s}s)"]),
        )

    return OracleValidityResult(
        is_valid=True,
        status=ValidityStatus.VALID,
        effective_multiplier_uint=effective_mult,
        age_s=age_s,
        reasons=tuple(mult_reasons),
    )


def validate_equity_reference(
    equity_ref: EquityReference,
    as_of_ms: int,
    *,
    max_cache_age_s: int = 60,
) -> EquityValidityResult:
    """Validate equity reference quote freshness, trading halts, and market sessions (C12, C13, C15)."""
    as_of_s = as_of_ms // 1000
    gen_s = equity_ref.generated_at_ms // 1000

    # C13: Timing future data rejection
    if equity_ref.generated_at_ms > as_of_ms or equity_ref.received_at_ms > as_of_ms:
        return EquityValidityResult(
            is_valid=False,
            status=ValidityStatus.TIMING_FUTURE_DATA,
            session_kind=equity_ref.session_kind,
            is_market_open=False,
            age_s=as_of_s - gen_s,
            reasons=("Equity quote generated_at or received_at is in the future relative to as_of",),
        )

    # C15: Trading halt check
    if equity_ref.trading_halt is True:
        return EquityValidityResult(
            is_valid=False,
            status=ValidityStatus.TRADING_HALT,
            session_kind=equity_ref.session_kind,
            is_market_open=False,
            age_s=as_of_s - gen_s,
            reasons=("Underlying equity has an active trading halt",),
        )

    # C12: Market session check
    session = equity_ref.session_kind.lower()
    is_open = session in ("regular", "all_day", "24_5", "overnight", "extended")
    if session in ("closed", "weekend", "holiday"):
        return EquityValidityResult(
            is_valid=False,
            status=ValidityStatus.REFERENCE_CLOSED_SESSION,
            session_kind=equity_ref.session_kind,
            is_market_open=False,
            age_s=as_of_s - gen_s,
            reasons=(f"Underlying equity market is closed (session={equity_ref.session_kind!r})",),
        )

    # REST cache freshness check
    age_s = as_of_s - gen_s
    if age_s > max_cache_age_s:
        return EquityValidityResult(
            is_valid=False,
            status=ValidityStatus.REFERENCE_CACHE_STALE,
            session_kind=equity_ref.session_kind,
            is_market_open=is_open,
            age_s=age_s,
            reasons=(f"Equity quote cache age ({age_s}s) exceeds max allowed ({max_cache_age_s}s)",),
        )

    return EquityValidityResult(
        is_valid=True,
        status=ValidityStatus.VALID,
        session_kind=equity_ref.session_kind,
        is_market_open=is_open,
        age_s=age_s,
        reasons=(),
    )


def check_capability_eligibility(
    matrix: EligibilityMatrix | None,
    action: str,
) -> CapabilityCheckResult:
    """Check whether a specific action is explicitly authorized (C21)."""
    if matrix is None:
        return CapabilityCheckResult(
            action=action,
            is_allowed=False,
            state="unknown",
            evidence_refs=(),
            reasons=("No eligibility matrix provided for token",),
        )

    state = matrix.capabilities.get(action, "unknown").lower()
    if state == "verified_true":
        return CapabilityCheckResult(
            action=action,
            is_allowed=True,
            state="verified_true",
            evidence_refs=matrix.evidence_refs,
            reasons=(),
        )

    reason = f"Capability {action!r} is not verified (status={state!r})"
    return CapabilityCheckResult(
        action=action,
        is_allowed=False,
        state=state,
        evidence_refs=matrix.evidence_refs,
        reasons=(reason,),
    )
