"""Boundary and tamper fail-closed tests for research/reservation_ledger.py.

Carries forward review boundary tests covering:
- Corrupted SQLite state and tampering fail-closed
- Revert receipt gas matching and reconciliation guards
- Path validation and leaf symlink rejection
"""

from __future__ import annotations

import decimal
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from research.reservation_ledger import (
    ExecutionLatched,
    FundsLedger,
    LedgerError,
)

WALLET = "0x" + "11" * 20
ROUTER = "0x" + "22" * 20
TX = "0x" + "33" * 32
BLOCK = "0x" + "44" * 32
ALLOWED_BASES = frozenset({"USDG", "WETH"})
DEFAULT_LOSS_BUDGET = Decimal("1")
D = Decimal


def make_ledger(
    db_path: Path,
    *,
    bases: frozenset[str] = ALLOWED_BASES,
    budget: Decimal = DEFAULT_LOSS_BUDGET,
) -> FundsLedger:
    return FundsLedger(db_path, allowed_bases=bases, loss_budget_usd=budget)


def reserve(
    ledger: FundsLedger,
    intent: str = "first",
    base: str = "USDG",
    nonce: int = 1,
    loss: Decimal = D(1),
) -> None:
    ledger.reserve(
        intent_id=intent,
        chain=4663,
        wallet=WALLET,
        base=base,
        nonce=nonce,
        payload={"router": ROUTER, "pool": "0x55" * 20, "amount_in": 100},
        worst_loss_usd=loss,
        running=True,
        auto_execute=True,
        one_shot=True,
    )


def test_database_corruption_and_tampering_fail_closed(tmp_path: Path) -> None:
    """Tampering with persistent state triggers fail-closed error branches."""
    db_path = tmp_path / "corrupt.sqlite"
    ledger = make_ledger(db_path)
    reserve(ledger, intent="tamper_seed", nonce=1, loss=Decimal("0.5"))

    # 1. Negative spent in SQLite -> LedgerError on status, ExecutionLatched on reserve
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE wallets SET spent='-0.5'")

    with pytest.raises(LedgerError, match="Corrupt negative loss ledger"):
        ledger.status(4663, WALLET, "USDG")

    # 2. NaN spent in SQLite -> LedgerError
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE wallets SET spent='NaN'")

    with pytest.raises(LedgerError, match="must be finite"):
        ledger.status(4663, WALLET, "USDG")

    # 3. Ghost pending intent in wallets pointing to nonexistent row -> LedgerError
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE wallets SET spent='0', pending='ghost_id'")

    with pytest.raises(LedgerError, match="Pending wallet has no reserved intent"):
        ledger.status(4663, WALLET, "USDG")

    with pytest.raises(LedgerError, match="Corrupt pending intent"):
        ledger.pending_intent(4663, WALLET)

    # 4. Non-positive reserve in intents -> LedgerError
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE wallets SET pending='tamper_seed'")
        db.execute("UPDATE intents SET reserve='-0.1' WHERE id='tamper_seed'")

    with pytest.raises(LedgerError, match="strictly positive"):
        ledger.status(4663, WALLET, "USDG")


def test_revert_receipt_exact_gas_matching_and_reconciliation_guards(tmp_path: Path) -> None:
    """Reconcile validates gas matching on revert, receipt_status bounds, and signed state."""
    db_path = tmp_path / "revert_guard.sqlite"
    ledger = make_ledger(db_path)
    reserve(ledger, intent="rev_intent", nonce=1)

    # Reconciling an unsigned intent (state='RESERVED') is rejected
    with pytest.raises(ExecutionLatched, match="Receipt does not match persisted intent"):
        ledger.reconcile(
            "rev_intent",
            tx_hash=TX,
            block_hash=BLOCK,
            net_profit_usd=Decimal("-0.25"),
            actual_gas_usd=Decimal("0.25"),
            receipt_status=0,
        )

    ledger.mark_signed("rev_intent", TX)

    # Revert (receipt_status=0) with mismatched net profit != -gas is rejected
    with pytest.raises(LedgerError, match="Revert must retain exactly its gas loss"):
        ledger.reconcile(
            "rev_intent",
            tx_hash=TX,
            block_hash=BLOCK,
            net_profit_usd=Decimal("-0.20"),
            actual_gas_usd=Decimal("0.25"),
            receipt_status=0,
        )

    # Reconciling with invalid receipt_status not in (0, 1) is rejected
    with pytest.raises(LedgerError, match="Unknown receipt status"):
        ledger.reconcile(
            "rev_intent",
            tx_hash=TX,
            block_hash=BLOCK,
            net_profit_usd=Decimal("-0.25"),
            actual_gas_usd=Decimal("0.25"),
            receipt_status=2,
        )

    # Valid revert with exact net == -gas succeeds and pauses wallet
    ledger.reconcile(
        "rev_intent",
        tx_hash=TX,
        block_hash=BLOCK,
        net_profit_usd=Decimal("-0.25"),
        actual_gas_usd=Decimal("0.25"),
        receipt_status=0,
    )
    st = ledger.status(4663, WALLET, "USDG")
    assert st["spent_usd"] == Decimal("0.25")
    assert st["paused"] is True
    assert st["mode"] == "PAUSED"


def test_path_validation_and_leaf_symlink_rejection(tmp_path: Path) -> None:
    """Path validation rejects symlinks at leaf, non-file existing paths, and relative paths."""
    real_db = tmp_path / "real.sqlite"
    real_db.touch()

    symlink_db = tmp_path / "symlink.sqlite"
    symlink_db.symlink_to(real_db)

    # Leaf symlink rejected
    with pytest.raises(LedgerError, match="absolute regular runtime path"):
        FundsLedger(symlink_db, allowed_bases=ALLOWED_BASES, loss_budget_usd=Decimal("1"))

    # Directory existing at path rejected
    sub_dir = tmp_path / "dir_target"
    sub_dir.mkdir()
    with pytest.raises(LedgerError, match="absolute regular runtime path"):
        FundsLedger(sub_dir, allowed_bases=ALLOWED_BASES, loss_budget_usd=Decimal("1"))

    # Non-existent parent directory rejected
    missing_parent = tmp_path / "non_existent_dir" / "ledger.sqlite"
    with pytest.raises(LedgerError, match="absolute regular runtime path"):
        FundsLedger(missing_parent, allowed_bases=ALLOWED_BASES, loss_budget_usd=Decimal("1"))
