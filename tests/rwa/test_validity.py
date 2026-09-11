"""Unit tests for W7 validity, session flags, timing barriers, and eligibility matrix (C09-C15, C21)."""

from __future__ import annotations

import pytest

from arbitrage_contracts import StateVersion, TokenKey
from rwa_research import (
    EligibilityMatrix,
    EquityReference,
    OracleObservation,
    ValidityStatus,
    check_capability_eligibility,
    validate_equity_reference,
    validate_oracle_observation,
)


def test_c09_oracle_paused_treated_as_temporarily_unavailable() -> None:
    """C09: When oracle_paused is True, price feed is marked ORACLE_PAUSED.

    It must NOT be treated as zero price or stale network error.
    """
    as_of_ms = 1700000010000  # 1700000010s
    oracle = OracleObservation(
        round_id=100,
        answer=12550000000,
        started_at_s=1700000000,
        updated_at_s=1700000005,  # 5s age, fresh
        answered_in_round=100,
        multiplier_uint=1050000000000000000,
        oracle_paused=True,
        heartbeat_s=86400,
        session_kind="regular",
    )

    res = validate_oracle_observation(oracle, as_of_ms)
    assert res.is_valid is False
    assert res.status == ValidityStatus.ORACLE_PAUSED
    assert "paused" in res.reasons[0].lower()


def test_c10_staleness_and_unknown_heartbeat_rejection() -> None:
    """C10: Price age exceeding heartbeat is ORACLE_STALE; missing heartbeat is HEARTBEAT_UNKNOWN."""
    as_of_ms = 1700000100000  # 1700000100s
    # 1. Stale price (age = 100s > heartbeat = 60s)
    oracle_stale = OracleObservation(
        round_id=101,
        answer=12550000000,
        started_at_s=1700000000,
        updated_at_s=1700000000,
        answered_in_round=101,
        multiplier_uint=1050000000000000000,
        oracle_paused=False,
        heartbeat_s=60,
        session_kind="regular",
    )
    res_stale = validate_oracle_observation(oracle_stale, as_of_ms)
    assert res_stale.is_valid is False
    assert res_stale.status == ValidityStatus.ORACLE_STALE
    assert res_stale.age_s == 100

    # 2. Unknown heartbeat in regular session -> fail-closed HEARTBEAT_UNKNOWN
    oracle_no_hb = OracleObservation(
        round_id=102,
        answer=12550000000,
        started_at_s=1700000000,
        updated_at_s=1700000090,  # 10s age
        answered_in_round=102,
        multiplier_uint=1050000000000000000,
        oracle_paused=False,
        heartbeat_s=None,
        session_kind="regular",
    )
    res_no_hb = validate_oracle_observation(oracle_no_hb, as_of_ms)
    assert res_no_hb.is_valid is False
    assert res_no_hb.status == ValidityStatus.HEARTBEAT_UNKNOWN

    # 3. Off-hours session holds last price without heartbeat
    oracle_off_hours = OracleObservation(
        round_id=103,
        answer=12550000000,
        started_at_s=1700000000,
        updated_at_s=1700000000,  # 100s age
        answered_in_round=103,
        multiplier_uint=1050000000000000000,
        oracle_paused=False,
        heartbeat_s=None,
        session_kind="off_hours",
    )
    res_off = validate_oracle_observation(oracle_off_hours, as_of_ms)
    assert res_off.is_valid is True
    assert res_off.status == ValidityStatus.OFF_HOURS_HOLDING


def test_c11_pending_multiplier_timeline_activation() -> None:
    """C11: Multiplier is staged with effective_at_s; active multiplier holds until that exact second."""
    eff_at = 1700000050
    oracle = OracleObservation(
        round_id=104,
        answer=12550000000,
        started_at_s=1700000000,
        updated_at_s=1700000010,
        answered_in_round=104,
        multiplier_uint=1000000000000000000,  # 1.0e18 current
        pending_multiplier_uint=10000000000000000000,  # 10.0e18 staged (10:1 split)
        effective_at_s=eff_at,
        oracle_paused=False,
        heartbeat_s=86400,
        session_kind="regular",
    )

    # 1. Before effective_at_s (as_of = 1700000049s) -> must use current multiplier
    res_before = validate_oracle_observation(oracle, 1700000049000)
    assert res_before.is_valid is True
    assert res_before.effective_multiplier_uint == 1000000000000000000

    # 2. At or after effective_at_s (as_of = 1700000050s) -> pending multiplier takes effect
    res_after = validate_oracle_observation(oracle, 1700000050000)
    assert res_after.is_valid is True
    assert res_after.effective_multiplier_uint == 10000000000000000000


def test_c12_session_flags_closed_session_not_tradable_reference() -> None:
    """C12: When market is in closed session (weekend/holiday), mark REFERENCE_CLOSED_SESSION."""
    as_of_ms = 1700000005000
    eq_closed = EquityReference(
        underlier_id="NVDA",
        currency="USD",
        bid_price="120.50",
        ask_price="120.60",
        generated_at_ms=1700000000000,
        received_at_ms=1700000001000,
        session_kind="closed",
    )

    res = validate_equity_reference(eq_closed, as_of_ms)
    assert res.is_valid is False
    assert res.status == ValidityStatus.REFERENCE_CLOSED_SESSION
    assert res.is_market_open is False


def test_c13_future_data_rejection_and_corrupted_round() -> None:
    """C13: updatedAt > as_of rejected as TIMING_FUTURE_DATA; non-positive parameters as CORRUPTED_ROUND."""
    as_of_ms = 1700000000000  # 1700000000s

    # 1. Future timestamp in oracle
    oracle_future = OracleObservation(
        round_id=105,
        answer=12550000000,
        started_at_s=1700000000,
        updated_at_s=1700000005,  # 5s in the future
        answered_in_round=105,
        multiplier_uint=1050000000000000000,
        heartbeat_s=86400,
    )
    res_future = validate_oracle_observation(oracle_future, as_of_ms)
    assert res_future.is_valid is False
    assert res_future.status == ValidityStatus.TIMING_FUTURE_DATA

    # 2. Corrupted round (started_at_s > updated_at_s)
    oracle_corrupt = OracleObservation(
        round_id=106,
        answer=12550000000,
        started_at_s=1700000010,
        updated_at_s=1700000000,  # started after updated
        answered_in_round=106,
        multiplier_uint=1050000000000000000,
    )
    res_corrupt = validate_oracle_observation(oracle_corrupt, 1700000020000)
    assert res_corrupt.is_valid is False
    assert res_corrupt.status == ValidityStatus.CORRUPTED_ROUND


def test_c14_sequencer_down_and_grace_period_protection() -> None:
    """C14: Reject oracle feed when L2 Sequencer is down or in grace period cooldown."""
    as_of_ms = 1700000100000  # 1700000100s

    # 1. Sequencer is down
    oracle_down = OracleObservation(
        round_id=107,
        answer=12550000000,
        started_at_s=1700000000,
        updated_at_s=1700000050,
        answered_in_round=107,
        multiplier_uint=1050000000000000000,
        sequencer_status="down",
    )
    res_down = validate_oracle_observation(oracle_down, as_of_ms)
    assert res_down.is_valid is False
    assert res_down.status == ValidityStatus.SEQUENCER_DOWN

    # 2. Sequencer just restarted, still in 3600s grace period
    # Restarted at 1700000000s, as_of is 1700000100s (only 100s elapsed < 3600s)
    oracle_grace = OracleObservation(
        round_id=108,
        answer=12550000000,
        started_at_s=1700000000,
        updated_at_s=1700000050,
        answered_in_round=108,
        multiplier_uint=1050000000000000000,
        sequencer_status="up",
        sequencer_started_at_s=1700000000,
        grace_period_s=3600,
    )
    res_grace = validate_oracle_observation(oracle_grace, as_of_ms)
    assert res_grace.is_valid is False
    assert res_grace.status == ValidityStatus.SEQUENCER_GRACE_PERIOD_ACTIVE


def test_c15_equity_trading_halt_check() -> None:
    """C15: Underlying equity trading halt flag marks quote as TRADING_HALT."""
    as_of_ms = 1700000005000
    eq_halt = EquityReference(
        underlier_id="GME",
        currency="USD",
        bid_price="22.50",
        ask_price="22.60",
        generated_at_ms=1700000000000,
        received_at_ms=1700000001000,
        session_kind="regular",
        trading_halt=True,
    )

    res = validate_equity_reference(eq_halt, as_of_ms)
    assert res.is_valid is False
    assert res.status == ValidityStatus.TRADING_HALT


def test_c21_multi_dimensional_capability_eligibility() -> None:
    """C21: Actions require explicit verified_true state; unknown/false are strictly blocked."""
    tk = TokenKey(4663, "0x1111111111111111111111111111111111111111")
    caps = {
        "hold": "verified_true",
        "venue_buy": "verified_true",
        "venue_sell": "verified_true",
        "primary_redeem": "verified_false",
        "short_sale": "unknown",
    }
    matrix = EligibilityMatrix(token_key=tk, capabilities=caps, evidence_refs=("kyb:onboarding_v1",))

    # Hold is allowed
    chk_hold = check_capability_eligibility(matrix, "hold")
    assert chk_hold.is_allowed is True
    assert chk_hold.state == "verified_true"

    # Primary redeem is forbidden
    chk_redeem = check_capability_eligibility(matrix, "primary_redeem")
    assert chk_redeem.is_allowed is False
    assert chk_redeem.state == "verified_false"

    # Short sale is unknown (not verified) -> blocked
    chk_short = check_capability_eligibility(matrix, "short_sale")
    assert chk_short.is_allowed is False
    assert chk_short.state == "unknown"

    # Undeclared action (e.g. primary_mint) -> blocked
    chk_mint = check_capability_eligibility(matrix, "primary_mint")
    assert chk_mint.is_allowed is False
    assert chk_mint.state == "unknown"

    # None matrix -> completely blocked
    chk_none = check_capability_eligibility(None, "hold")
    assert chk_none.is_allowed is False
