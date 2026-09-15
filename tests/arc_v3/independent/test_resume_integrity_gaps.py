"""Independent tests verifying controlled resume rejection capability against:
1. Illegal intent state ('INVALID') with pending NULL
2. Illegal non-boolean paused value (paused=2)
3. Ledger loss inconsistency (spent tampered from 0.6 to 0.1)
Along with a genuine passing control test for legitimate normal resume.

When run against candidate code without the integrity guard checks, the three
regression tests fail because the candidate accepts them instead of rejecting,
producing an exit code 1 red light.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from research.reservation_coordinator import (
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
TX_HASH_1 = "0x" + "aa" * 32
BLOCK_HASH_1 = "0x" + "bb" * 32
ALLOWED_BASES = frozenset({"USDG", "WETH"})
LOSS_BUDGET = Decimal("1.0")
CHAIN_ID = 5042
D = Decimal


def make_ledger(db_path: Path) -> FundsLedger:
    return FundsLedger(db_path, allowed_bases=ALLOWED_BASES, loss_budget_usd=LOSS_BUDGET)


def make_coordinator(ledger: FundsLedger) -> ExecutionCoordinator:
    def provider(req: ReservationRequest) -> ValuationEvidence:
        return ValuationEvidence(
            base_price_usd=D("1.0"),
            gas_usd=D("0.6"),
            slippage_bps=50,
        )

    return ExecutionCoordinator(
        chain_id=CHAIN_ID,
        ledger=ledger,
        valuation_provider=provider,
    )


def make_request(plan_id: str, nonce: int) -> ReservationRequest:
    # Rigid floor math: gas_atoms = ceil(gas_usd / price * 10^decimals)
    # min_out = amount_in_atoms + gas_atoms + 1
    amount_in_atoms = 1000
    decimals = 6
    gas_atoms = 600000
    min_out = amount_in_atoms + gas_atoms + 1

    return ReservationRequest(
        plan_id=plan_id,
        chain_id=CHAIN_ID,
        wallet_address=WALLET,
        base_symbol="USDG",
        decimals=decimals,
        amount_in_atoms=amount_in_atoms,
        minimum_output_atoms=min_out,
        nonce=nonce,
    )


def setup_paused_wallet(db_path: Path) -> tuple[FundsLedger, ExecutionCoordinator]:
    ledger = make_ledger(db_path)
    coord = make_coordinator(ledger)

    req = make_request("plan_01", 1)
    slot = coord.acquire_execution_slot(req, one_shot=False)
    assert isinstance(slot, ReservedExecution)
    coord.record_signed(slot, TX_HASH_1)
    coord.reconcile(
        slot,
        tx_hash=TX_HASH_1,
        block_hash=BLOCK_HASH_1,
        net_profit_usd=D("-0.6"),
        actual_gas_usd=D("0.6"),
        receipt_status=0,
    )

    # Verify standard pre-condition: wallet is paused, spent=0.6, pending=None
    st = ledger.status(CHAIN_ID, WALLET, "USDG")
    assert st["paused"] is True
    assert st["spent_usd"] == D("0.6")
    assert st["remaining_usd"] == D("0.4")
    assert st["mode"] == "PAUSED"
    assert len(ledger.resume_audits(CHAIN_ID, WALLET)) == 0

    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT spent, paused, pending FROM wallets WHERE chain=? AND wallet=?",
            (CHAIN_ID, WALLET),
        ).fetchone()
        assert row[0] == "0.6"
        assert row[1] == 1
        assert row[2] is None

    return ledger, coord


def test_control_normal_resume_succeeds(tmp_path: Path) -> None:
    """Control case: Legitimate normal resume must genuinely pass."""
    db_path = tmp_path / "control_normal.sqlite"
    ledger, coord = setup_paused_wallet(db_path)

    reason = "Offline manual diagnosis complete: legitimate control unpause"
    coord.unpause_wallet(WALLET, reason=reason)

    # Verification: wallet resumed, mode PROBE, spent preserved, 1 audit row recorded
    st = ledger.status(CHAIN_ID, WALLET, "USDG")
    assert st["paused"] is False
    assert st["spent_usd"] == D("0.6")
    assert st["remaining_usd"] == D("0.4")
    assert st["mode"] == "PROBE"

    audits = ledger.resume_audits(CHAIN_ID, WALLET)
    assert len(audits) == 1
    assert audits[0]["reason"] == reason
    assert audits[0]["spent_before"] == "0.6"
    assert audits[0]["budget_before"] == "1"


def test_regression_rejects_illegal_intent_state(tmp_path: Path) -> None:
    """Regression probe 1: Reject resume when intent has illegal state ('INVALID') with pending NULL.

    Vulnerability under test: resume_wallet only checks state IN ('RESERVED', 'SIGNED', 'UNKNOWN')
    and ignores invalid/corrupt intent states, allowing unpause on corrupted state.
    Specification requirement: Must reject fail-closed, write 0 audit rows, and keep wallet paused.
    """
    db_path = tmp_path / "illegal_intent_state.sqlite"
    ledger, coord = setup_paused_wallet(db_path)

    # Single-variable mutation: set intent.state='INVALID', keeping pending NULL
    with sqlite3.connect(str(db_path)) as conn:
        count = conn.execute(
            "UPDATE intents SET state='INVALID' WHERE chain=? AND wallet=?",
            (CHAIN_ID, WALLET),
        ).rowcount
        assert count == 1
        conn.commit()

    # The integrity rule requires resume to be REJECTED fail-closed.
    # Current candidate allows this, so expecting rejection will FAIL (RED LIGHT).
    with pytest.raises((ExecutionLatched, LedgerError), match=r"(?i)invalid|corrupt|unresolved|state"):
        coord.unpause_wallet(WALLET, reason="Attempt resume with corrupted intent state")

    # Ensure fail-closed invariants: audit rows zero, wallet remains paused
    assert len(ledger.resume_audits(CHAIN_ID, WALLET)) == 0
    with sqlite3.connect(str(db_path)) as conn:
        p = conn.execute(
            "SELECT paused FROM wallets WHERE chain=? AND wallet=?",
            (CHAIN_ID, WALLET),
        ).fetchone()[0]
        assert p != 0


def test_regression_rejects_illegal_paused_value(tmp_path: Path) -> None:
    """Regression probe 2: Reject resume when paused column has illegal non-boolean value (paused=2).

    Vulnerability under test: resume_wallet evaluates bool(account['paused']), which treats 2 as True,
    and unconditionally resets paused to 0, accepting an out-of-domain corrupted flag.
    Specification requirement: Must reject fail-closed, write 0 audit rows, and keep paused != 0.
    """
    db_path = tmp_path / "illegal_paused_value.sqlite"
    ledger, coord = setup_paused_wallet(db_path)

    # Single-variable mutation: set paused=2 (all else legal)
    with sqlite3.connect(str(db_path)) as conn:
        count = conn.execute(
            "UPDATE wallets SET paused=2 WHERE chain=? AND wallet=?",
            (CHAIN_ID, WALLET),
        ).rowcount
        assert count == 1
        conn.commit()

    # The integrity rule requires resume to be REJECTED fail-closed.
    # Current candidate accepts bool(2) == True, so expecting rejection will FAIL (RED LIGHT).
    with pytest.raises((ExecutionLatched, LedgerError), match=r"(?i)invalid|corrupt|paused|boolean"):
        coord.unpause_wallet(WALLET, reason="Attempt resume with illegal paused=2")

    # Ensure fail-closed invariants: audit rows zero, paused not mutated to 0
    assert len(ledger.resume_audits(CHAIN_ID, WALLET)) == 0
    with sqlite3.connect(str(db_path)) as conn:
        p = conn.execute(
            "SELECT paused FROM wallets WHERE chain=? AND wallet=?",
            (CHAIN_ID, WALLET),
        ).fetchone()[0]
        assert p != 0


def test_regression_rejects_loss_inconsistency(tmp_path: Path) -> None:
    """Regression probe 3: Reject resume when wallet spent is inconsistent with intent loss records.

    Vulnerability under test: resume_wallet does not reconcile wallet.spent against settled intents/receipts;
    it accepts arbitrary spent as long as spent >= 0 and spent < budget, allowing resume with tampered loss.
    Specification requirement: Must reject fail-closed, write 0 audit rows, and keep wallet paused.
    """
    db_path = tmp_path / "loss_inconsistency.sqlite"
    ledger, coord = setup_paused_wallet(db_path)

    # Single-variable mutation: tamper spent from 0.6 to 0.1 (all else legal)
    with sqlite3.connect(str(db_path)) as conn:
        count = conn.execute(
            "UPDATE wallets SET spent='0.1' WHERE chain=? AND wallet=?",
            (CHAIN_ID, WALLET),
        ).rowcount
        assert count == 1
        conn.commit()

    # The integrity rule requires resume to be REJECTED fail-closed due to ledger inconsistency.
    # Current candidate accepts spent='0.1' without checking intents, so expecting rejection will FAIL (RED LIGHT).
    with pytest.raises((ExecutionLatched, LedgerError), match=r"(?i)inconsistent|mismatch|reconcil|spent"):
        coord.unpause_wallet(WALLET, reason="Attempt resume with inconsistent spent amount")

    # Ensure fail-closed invariants: audit rows zero, wallet remains paused
    assert len(ledger.resume_audits(CHAIN_ID, WALLET)) == 0
    with sqlite3.connect(str(db_path)) as conn:
        p = conn.execute(
            "SELECT paused FROM wallets WHERE chain=? AND wallet=?",
            (CHAIN_ID, WALLET),
        ).fetchone()[0]
        assert p == 1
