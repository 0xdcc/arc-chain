"""Independent tests for offline wallet controlled resume and loss budget regression.

Verifies:
1. Controlled resume lifecycle on the same wallet:
   - Legal floor reservation -> signed -> reconcile loss 0.6
   - Paused wallet default rejects new reservations
   - unpause_wallet with explicit non-empty reason succeeds
   - Pre-resume spent (0.6) and remaining budget (0.4) strictly preserved
   - Subsequent gas reservation: 0.5 rejected (exhausts budget), 0.2 allowed
   - Reconcile loss 0.4 exhausts budget (total spent 1.0)
   - unpause_wallet rejected on exhausted budget
   - Reopen/restart on same SQLite DB preserves budget, spent, and resume audit log
2. Gate rejections fail-closed:
   - Pending RESERVED intent rejects resume
   - In-flight UNKNOWN intent rejects resume
   - Corrupted pending chain (pending NULL with SIGNED intent) rejects resume
   - Empty, whitespace, or invalid reason rejected
   - Duplicate resume on active/unpaused wallet rejected
   - Corrupt or negative spent amount rejected
   - Missing/unregistered wallet rejected
3. Concurrency safety:
   - Concurrent unpause race: exactly one succeeds, exactly one audit record written
4. Mode safety:
   - PAUSED base returns to PROBE, never automatically promoted to NORMAL
"""

from __future__ import annotations

import sqlite3
import threading
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

from research.reservation_coordinator import (
    CoordinatorError,
    ExecutionCoordinator,
    ReservationRequest,
    ReservedExecution,
    ValuationEvidence,
)
from research.reservation_ledger import (
    ExecutionLatched,
    FundsLedger,
    LedgerError,
)

WALLET = "0x" + "11" * 20
ROUTER = "0x" + "22" * 20
TX_HASH_1 = "0x" + "aa" * 32
BLOCK_HASH_1 = "0x" + "bb" * 32
TX_HASH_2 = "0x" + "cc" * 32
BLOCK_HASH_2 = "0x" + "dd" * 32

ALLOWED_BASES = frozenset({"USDG", "WETH"})
LOSS_BUDGET = Decimal("1.0")
CHAIN_ID = 5042
D = Decimal


def make_ledger(db_path: Path) -> FundsLedger:
    return FundsLedger(db_path, allowed_bases=ALLOWED_BASES, loss_budget_usd=LOSS_BUDGET)


def make_coordinator(
    ledger: FundsLedger,
    gas_usd: Decimal = D("0.6"),
    price_usd: Decimal = D("1.0"),
    slippage_bps: int = 50,
) -> ExecutionCoordinator:
    def provider(req: ReservationRequest) -> ValuationEvidence:
        return ValuationEvidence(
            base_price_usd=price_usd,
            gas_usd=gas_usd,
            slippage_bps=slippage_bps,
        )

    return ExecutionCoordinator(
        chain_id=CHAIN_ID,
        ledger=ledger,
        valuation_provider=provider,
    )


def make_request(
    plan_id: str,
    nonce: int,
    *,
    gas_usd: Decimal,
    amount_in_atoms: int = 1000,
    decimals: int = 6,
    price_usd: Decimal = D("1.0"),
    wallet: str = WALLET,
) -> ReservationRequest:
    # Rigid floor math: gas_atoms = ceil(gas_usd / price * 10^decimals)
    # min_out = amount_in_atoms + gas_atoms + 1
    gas_usd_frac = Fraction(gas_usd)
    price_frac = Fraction(price_usd)
    gas_token_frac = gas_usd_frac / price_frac
    gas_atoms_frac = gas_token_frac * (10**decimals)
    gas_atoms = (gas_atoms_frac.numerator + gas_atoms_frac.denominator - 1) // gas_atoms_frac.denominator
    min_out = amount_in_atoms + gas_atoms + 1

    return ReservationRequest(
        plan_id=plan_id,
        chain_id=CHAIN_ID,
        wallet_address=wallet,
        base_symbol="USDG",
        decimals=decimals,
        amount_in_atoms=amount_in_atoms,
        minimum_output_atoms=min_out,
        nonce=nonce,
    )


def test_controlled_wallet_resume_lifecycle_and_loss_budget_regression(tmp_path: Path) -> None:
    """End-to-end regression test for controlled wallet resume across losses and restart."""
    db_path = tmp_path / "controlled_resume.sqlite"
    ledger = make_ledger(db_path)
    coord = make_coordinator(ledger, gas_usd=D("0.6"))

    # 1. First reservation with gas_usd = 0.6
    req1 = make_request("plan_01", 1, gas_usd=D("0.6"))
    slot1 = coord.acquire_execution_slot(req1, one_shot=False)
    assert isinstance(slot1, ReservedExecution)
    assert slot1.reserved_loss_usd == D("0.6")

    # 2. Record signed and reconcile loss 0.6 (revert retains gas loss)
    coord.record_signed(slot1, TX_HASH_1)
    coord.reconcile(
        slot1,
        tx_hash=TX_HASH_1,
        block_hash=BLOCK_HASH_1,
        net_profit_usd=D("-0.6"),
        actual_gas_usd=D("0.6"),
        receipt_status=0,
    )

    # 3. Default state after loss is PAUSED: new reservations rejected
    st1 = ledger.status(CHAIN_ID, WALLET, "USDG")
    assert st1["paused"] is True
    assert st1["spent_usd"] == D("0.6")
    assert st1["remaining_usd"] == D("0.4")
    assert st1["mode"] == "PAUSED"

    req_reject = make_request("plan_reject", 2, gas_usd=D("0.6"))
    with pytest.raises(ExecutionLatched, match="[Ww]allet/base pending or paused"):
        coord.acquire_execution_slot(req_reject, one_shot=False)

    # 4. Controlled explicit resume with reason
    reason_msg = "Post-loss offline analysis complete: gas anomaly diagnosed, unpausing for continued simulation"
    coord.unpause_wallet(WALLET, reason=reason_msg)

    # 5. Verify state after resume: spent is strictly preserved, remaining is 0.4, base is PROBE
    st_resumed = ledger.status(CHAIN_ID, WALLET, "USDG")
    assert st_resumed["paused"] is False
    assert st_resumed["spent_usd"] == D("0.6")
    assert st_resumed["remaining_usd"] == D("0.4")
    assert st_resumed["mode"] == "PROBE"

    audits = ledger.resume_audits(CHAIN_ID, WALLET)
    assert len(audits) == 1
    assert audits[0]["chain"] == CHAIN_ID
    assert audits[0]["wallet"] == WALLET
    assert audits[0]["reason"] == reason_msg
    assert audits[0]["spent_before"] == "0.6"
    assert audits[0]["budget_before"] == "1"

    # Reopen the same persisted wallet before testing positive remaining-budget admission.
    del coord
    del ledger
    ledger = make_ledger(db_path)
    coord = make_coordinator(ledger, gas_usd=D("0.6"))
    reopened = ledger.status(CHAIN_ID, WALLET, "USDG")
    assert reopened["spent_usd"] == D("0.6")
    assert reopened["remaining_usd"] == D("0.4")
    assert reopened["paused"] is False
    assert ledger.resume_audits(CHAIN_ID, WALLET) == audits

    # 6. Gas reservation 0.5 is rejected (0.6 spent + 0.5 reserve > 1.0 budget)
    coord_05 = make_coordinator(ledger, gas_usd=D("0.5"))
    req_gas_05 = make_request("plan_05", 2, gas_usd=D("0.5"))
    with pytest.raises(ExecutionLatched, match="[Ll]oss budget exhausted"):
        coord_05.acquire_execution_slot(req_gas_05, one_shot=False)

    # 7. Gas reservation 0.2 is allowed (0.6 spent + 0.2 reserve <= 1.0 budget)
    coord_02 = make_coordinator(ledger, gas_usd=D("0.2"))
    req_gas_02 = make_request("plan_02", 2, gas_usd=D("0.2"))
    slot2 = coord_02.acquire_execution_slot(req_gas_02, one_shot=False)
    assert slot2.reserved_loss_usd == D("0.2")

    # 8. Reconcile loss 0.4: cumulative loss reaches 0.6 + 0.4 = 1.0 (budget fully exhausted)
    coord_02.record_signed(slot2, TX_HASH_2)
    coord_02.reconcile(
        slot2,
        tx_hash=TX_HASH_2,
        block_hash=BLOCK_HASH_2,
        net_profit_usd=D("-0.4"),
        actual_gas_usd=D("0.4"),
        receipt_status=0,
    )

    st_exhausted = ledger.status(CHAIN_ID, WALLET, "USDG")
    assert st_exhausted["paused"] is True
    assert st_exhausted["spent_usd"] == D("1.0")
    assert st_exhausted["remaining_usd"] == D("0")

    # 9. Attempted resume on exhausted budget MUST be rejected fail-closed
    with pytest.raises(ExecutionLatched, match="[Bb]udget exhausted"):
        coord.unpause_wallet(WALLET, reason="Attempt resume on exhausted wallet")

    # Audit log count must remain 1 (rejected resume creates NO audit entry)
    assert len(ledger.resume_audits(CHAIN_ID, WALLET)) == 1

    # 10. Restart persistence: re-open DB with fresh instances, verify budget, spent, and audits
    restarted_ledger = make_ledger(db_path)
    restarted_coord = make_coordinator(restarted_ledger, gas_usd=D("0.2"))

    st_restart = restarted_ledger.status(CHAIN_ID, WALLET, "USDG")
    assert st_restart["paused"] is True
    assert st_restart["spent_usd"] == D("1.0")
    assert st_restart["remaining_usd"] == D("0")

    restarted_audits = restarted_ledger.resume_audits(CHAIN_ID, WALLET)
    assert len(restarted_audits) == 1
    assert restarted_audits[0]["reason"] == reason_msg
    assert restarted_audits[0]["spent_before"] == "0.6"

    # Resume on restarted instance also rejected
    with pytest.raises(ExecutionLatched, match="[Bb]udget exhausted"):
        restarted_coord.unpause_wallet(WALLET, reason="Post-restart resume attempt")


def test_resume_rejected_when_pending_or_unresolved_intent(tmp_path: Path) -> None:
    """Resuming is strictly rejected if wallet has pending, reserved, signed, or unknown intent."""
    db_path = tmp_path / "pending_resume.sqlite"
    ledger = make_ledger(db_path)
    coord = make_coordinator(ledger, gas_usd=D("0.2"))

    # A: Active RESERVED pending intent
    req = make_request("plan_pending", 1, gas_usd=D("0.2"))
    slot = coord.acquire_execution_slot(req, one_shot=False)

    # Force paused=1 directly in DB while intent is pending
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE wallets SET paused=1 WHERE chain=? AND wallet=?", (CHAIN_ID, WALLET))

    with pytest.raises(ExecutionLatched, match="[Pp]ending"):
        coord.unpause_wallet(WALLET, reason="Manual resume while slot pending")

    # B: Intent in SIGNED state
    coord.record_signed(slot, TX_HASH_1)
    with pytest.raises(ExecutionLatched, match="[Pp]ending|[Uu]nresolved"):
        coord.unpause_wallet(WALLET, reason="Manual resume while signed")

    # C: Intent in UNKNOWN state
    coord.mark_unknown(slot)
    with pytest.raises(ExecutionLatched, match="[Pp]ending|[Uu]nresolved"):
        coord.unpause_wallet(WALLET, reason="Manual resume while broadcast unknown")

    # D: Corrupt pending chain: pending is NULL in wallets table, but intents has UNKNOWN intent
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE wallets SET pending=NULL WHERE chain=? AND wallet=?", (CHAIN_ID, WALLET))

    with pytest.raises(ExecutionLatched, match="[Uu]nresolved"):
        coord.unpause_wallet(WALLET, reason="Manual resume with detached unknown intent")

    # Verify zero audit records created across all failed attempts
    assert len(ledger.resume_audits(CHAIN_ID, WALLET)) == 0


def test_resume_rejected_on_invalid_or_empty_reason(tmp_path: Path) -> None:
    """Empty, whitespace-only, or invalid reason strings are rejected without mutating state."""
    db_path = tmp_path / "reason_rejection.sqlite"
    ledger = make_ledger(db_path)
    coord = make_coordinator(ledger, gas_usd=D("0.2"))

    # Create wallet and pause it with remaining budget
    req = make_request("plan_setup", 1, gas_usd=D("0.2"))
    slot = coord.acquire_execution_slot(req, one_shot=False)
    coord.record_signed(slot, TX_HASH_1)
    coord.reconcile(
        slot,
        tx_hash=TX_HASH_1,
        block_hash=BLOCK_HASH_1,
        net_profit_usd=D("-0.2"),
        actual_gas_usd=D("0.2"),
        receipt_status=0,
    )
    assert ledger.status(CHAIN_ID, WALLET, "USDG")["paused"] is True

    # 1. Empty string
    with pytest.raises((CoordinatorError, LedgerError), match="[Rr]eason"):
        coord.unpause_wallet(WALLET, reason="")

    # 2. Whitespace-only string
    with pytest.raises((CoordinatorError, LedgerError), match="[Rr]eason"):
        coord.unpause_wallet(WALLET, reason="   \t\n  ")

    # 3. Direct ledger call with whitespace
    with pytest.raises(LedgerError, match="[Rr]eason"):
        ledger.resume_wallet(CHAIN_ID, WALLET, reason="   ")

    # Wallet must still be paused and zero audits logged
    assert ledger.status(CHAIN_ID, WALLET, "USDG")["paused"] is True
    assert len(ledger.resume_audits(CHAIN_ID, WALLET)) == 0


def test_duplicate_resume_on_active_wallet_rejected(tmp_path: Path) -> None:
    """Duplicate resume on an already unpaused wallet is rejected to prevent duplicate audits."""
    db_path = tmp_path / "dup_resume.sqlite"
    ledger = make_ledger(db_path)
    coord = make_coordinator(ledger, gas_usd=D("0.2"))

    req = make_request("plan_dup", 1, gas_usd=D("0.2"))
    slot = coord.acquire_execution_slot(req, one_shot=False)
    coord.record_signed(slot, TX_HASH_1)
    coord.reconcile(
        slot,
        tx_hash=TX_HASH_1,
        block_hash=BLOCK_HASH_1,
        net_profit_usd=D("-0.2"),
        actual_gas_usd=D("0.2"),
        receipt_status=0,
    )

    # First resume succeeds
    coord.unpause_wallet(WALLET, reason="Valid operator unpause")
    assert ledger.status(CHAIN_ID, WALLET, "USDG")["paused"] is False
    assert len(ledger.resume_audits(CHAIN_ID, WALLET)) == 1

    # Second consecutive resume fails
    with pytest.raises(ExecutionLatched, match="not paused"):
        coord.unpause_wallet(WALLET, reason="Duplicate unpause attempt")

    # Audit records count remains exactly 1
    assert len(ledger.resume_audits(CHAIN_ID, WALLET)) == 1


def test_corrupt_spent_or_missing_wallet_rejected(tmp_path: Path) -> None:
    """Corrupt spent data, negative spent, or missing wallet rejected fail-closed."""
    db_path = tmp_path / "corrupt_spent.sqlite"
    ledger = make_ledger(db_path)
    coord = make_coordinator(ledger, gas_usd=D("0.2"))

    # Missing wallet
    unregistered_wallet = "0x" + "99" * 20
    with pytest.raises(ExecutionLatched, match="not found"):
        coord.unpause_wallet(unregistered_wallet, reason="Resume missing wallet")

    # Setup valid wallet then corrupt spent in DB
    req = make_request("plan_corrupt", 1, gas_usd=D("0.2"))
    slot = coord.acquire_execution_slot(req, one_shot=False)
    coord.record_signed(slot, TX_HASH_1)
    coord.reconcile(
        slot,
        tx_hash=TX_HASH_1,
        block_hash=BLOCK_HASH_1,
        net_profit_usd=D("-0.2"),
        actual_gas_usd=D("0.2"),
        receipt_status=0,
    )

    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE wallets SET spent='invalid_nan' WHERE chain=? AND wallet=?", (CHAIN_ID, WALLET))

    with pytest.raises(LedgerError, match="[Ii]nvalid spent"):
        coord.unpause_wallet(WALLET, reason="Resume with corrupt spent")

    # Negative spent
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE wallets SET spent='-0.5' WHERE chain=? AND wallet=?", (CHAIN_ID, WALLET))

    with pytest.raises(LedgerError, match="[Ss]pent|negative"):
        coord.unpause_wallet(WALLET, reason="Resume with negative spent")

    assert len(ledger.resume_audits(CHAIN_ID, WALLET)) == 0


def test_concurrent_unpause_race_exactly_one_succeeds(tmp_path: Path) -> None:
    """Two concurrent resume calls on a paused wallet: exactly one succeeds and one audit row is logged."""
    db_path = tmp_path / "concurrent_resume.sqlite"
    ledger = make_ledger(db_path)
    coord = make_coordinator(ledger, gas_usd=D("0.2"))

    req = make_request("plan_race", 1, gas_usd=D("0.2"))
    slot = coord.acquire_execution_slot(req, one_shot=False)
    coord.record_signed(slot, TX_HASH_1)
    coord.reconcile(
        slot,
        tx_hash=TX_HASH_1,
        block_hash=BLOCK_HASH_1,
        net_profit_usd=D("-0.2"),
        actual_gas_usd=D("0.2"),
        receipt_status=0,
    )

    barrier = threading.Barrier(2)
    results: list[str] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def do_unpause(worker_id: str) -> None:
        barrier.wait()
        try:
            coord.unpause_wallet(WALLET, reason=f"Concurrent resume from {worker_id}")
            with lock:
                results.append(worker_id)
        except Exception as exc:
            with lock:
                errors.append(exc)

    t1 = threading.Thread(target=do_unpause, args=("t1",))
    t2 = threading.Thread(target=do_unpause, args=("t2",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(results) == 1, f"Expected exactly 1 success, got {results}"
    assert len(errors) == 1, f"Expected exactly 1 error, got {errors}"
    assert isinstance(errors[0], ExecutionLatched)

    audits = ledger.resume_audits(CHAIN_ID, WALLET)
    assert len(audits) == 1
    assert audits[0]["reason"] in ("Concurrent resume from t1", "Concurrent resume from t2")


def test_paused_base_mode_reverts_to_probe_not_normal(tmp_path: Path) -> None:
    """A PAUSED base reverts to PROBE on resume and cannot be promoted to NORMAL without positive receipt."""
    db_path = tmp_path / "mode_safety.sqlite"
    ledger = make_ledger(db_path)
    coord = make_coordinator(ledger, gas_usd=D("0.2"))

    req = make_request("plan_mode", 1, gas_usd=D("0.2"))
    slot = coord.acquire_execution_slot(req, one_shot=False)
    coord.record_signed(slot, TX_HASH_1)
    coord.reconcile(
        slot,
        tx_hash=TX_HASH_1,
        block_hash=BLOCK_HASH_1,
        net_profit_usd=D("-0.2"),
        actual_gas_usd=D("0.2"),
        receipt_status=0,
    )

    assert ledger.status(CHAIN_ID, WALLET, "USDG")["mode"] == "PAUSED"

    coord.unpause_wallet(WALLET, reason="Restore base for probe observation")

    # Base is PROBE
    assert ledger.status(CHAIN_ID, WALLET, "USDG")["mode"] == "PROBE"

    # Direct promote to NORMAL must fail (only RECONCILED_PROFIT can be promoted)
    with pytest.raises(ExecutionLatched, match="Base has no reconciled positive receipt"):
        ledger.promote(CHAIN_ID, WALLET, "USDG")
