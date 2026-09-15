"""Direct independent tests for research/reservation_ledger.py.

Carries forward legacy cross-base durable locking, irreversible loss budget,
and receipt idempotence contracts with explicit configuration and no live funds.
"""

from __future__ import annotations

import decimal
import threading
from decimal import Decimal
from pathlib import Path

import pytest

from research.reservation_ledger import ExecutionLatched, FundsLedger, LedgerError

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
    one_shot: bool = False,
) -> None:
    ledger.reserve(
        intent_id=intent,
        chain=4663,
        wallet=WALLET,
        base=base,
        nonce=nonce,
        payload={"to": ROUTER, "data": "0x1234"},
        worst_loss_usd=loss,
        running=True,
        auto_execute=True,
        one_shot=one_shot,
    )


def reconcile(ledger: FundsLedger, net: Decimal = D("-.6")) -> None:
    ledger.mark_signed("first", TX)
    ledger.reconcile(
        "first",
        tx_hash=TX,
        block_hash=BLOCK,
        net_profit_usd=net,
        actual_gas_usd=D(".25"),
        receipt_status=1,
    )


def test_pending_survives_restart_and_blocks_other_base(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.sqlite"
    ledger = make_ledger(db_path)
    reserve(ledger)
    ledger.mark_signed("first", TX)
    ledger.mark_unknown("first")
    restarted = make_ledger(db_path)
    with pytest.raises(ExecutionLatched):
        reserve(restarted, "second", "WETH", 2)
    with pytest.raises(ExecutionLatched):
        restarted.cancel_before_signing("first")
    assert restarted.status(4663, WALLET, "WETH")["pending"] == "first"


def test_loss_is_wallet_global_durable_and_duplicate_receipt_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.sqlite"
    ledger = make_ledger(db_path)
    reserve(ledger)
    reconcile(ledger)
    ledger.reconcile(
        "first",
        tx_hash=TX,
        block_hash=BLOCK,
        net_profit_usd=D("-.6"),
        actual_gas_usd=D(".25"),
        receipt_status=1,
    )
    restarted = make_ledger(db_path)
    status = restarted.status(4663, WALLET, "WETH")
    assert status["remaining_usd"] == D(".4")
    assert status["paused"] is True
    with pytest.raises(ExecutionLatched):
        reserve(restarted, "second", "WETH", 2, D(".5"))


def test_positive_receipt_needs_explicit_upgrade_for_only_its_base(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.sqlite"
    ledger = make_ledger(db_path)
    reserve(ledger)
    reconcile(ledger, D(".1"))
    with pytest.raises(ExecutionLatched):
        reserve(ledger, "second", nonce=2)
    ledger.promote(4663, WALLET, "USDG")
    assert ledger.status(4663, WALLET, "USDG")["mode"] == "NORMAL"
    assert ledger.status(4663, WALLET, "WETH")["mode"] == "PROBE"
    reserve(ledger, "second", nonce=2)


def test_one_shot_profit_still_latches_and_presign_failure_can_cancel(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.sqlite"
    ledger = make_ledger(db_path)
    reserve(ledger)
    ledger.cancel_before_signing("first")
    reserve(ledger, one_shot=True)
    reconcile(ledger, D(1))
    with pytest.raises(ExecutionLatched):
        ledger.promote(4663, WALLET, "USDG")


def test_budget_is_reserved_before_any_broadcast_and_visible_across_bases(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.sqlite"
    ledger = make_ledger(db_path)
    reserve(ledger, loss=D(".6"))
    other_base = make_ledger(db_path).status(4663, WALLET, "WETH")
    assert other_base["spent_usd"] == 0
    assert other_base["reserved_usd"] == D(".6")
    assert other_base["remaining_usd"] == D(".4")
    with pytest.raises(ExecutionLatched):
        reserve(ledger, "second", "WETH", 2, D(".5"))


def test_idempotence_compares_decimal_value_and_receipt_status(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.sqlite"
    ledger = make_ledger(db_path)
    reserve(ledger)
    reconcile(ledger, D("-.25"))
    ledger.reconcile(
        "first",
        tx_hash=TX,
        block_hash=BLOCK,
        net_profit_usd=D("-.2500"),
        actual_gas_usd=D(".2500"),
        receipt_status=1,
    )
    assert ledger.status(4663, WALLET, "USDG")["spent_usd"] == D(".25")
    with pytest.raises(ExecutionLatched, match="Conflicting"):
        ledger.reconcile(
            "first",
            tx_hash=TX,
            block_hash=BLOCK,
            net_profit_usd=D("-.25"),
            actual_gas_usd=D(".25"),
            receipt_status=0,
        )


def test_normal_mode_does_not_get_an_extra_wallet_loss_budget(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.sqlite"
    ledger = make_ledger(db_path)
    reserve(ledger)
    reconcile(ledger, D(".1"))
    ledger.promote(4663, WALLET, "USDG")
    assert ledger.status(4663, WALLET, "USDG")["mode"] == "NORMAL"
    with pytest.raises(ExecutionLatched, match="Wallet-global loss budget"):
        reserve(ledger, "normal", "USDG", 2, D("1.01"))
    assert ledger.status(4663, WALLET, "WETH")["remaining_usd"] == D(1)


def test_cancel_after_signed_or_unknown_is_rejected(tmp_path: Path) -> None:
    """Cannot cancel reservation once signed or marked unknown."""
    db_path = tmp_path / "ledger.sqlite"
    ledger = make_ledger(db_path)
    reserve(ledger, intent="tx_signed", nonce=1)
    ledger.mark_signed("tx_signed", TX)
    with pytest.raises(ExecutionLatched, match="Cannot cancel"):
        ledger.cancel_before_signing("tx_signed")
    ledger.mark_unknown("tx_signed")
    with pytest.raises(ExecutionLatched, match="Cannot cancel"):
        ledger.cancel_before_signing("tx_signed")


def test_conflicting_receipt_net_or_gas_or_status_rejected(tmp_path: Path) -> None:
    """Reconcile rejects conflicting net profit or gas for the same receipt."""
    db_path = tmp_path / "ledger.sqlite"
    ledger = make_ledger(db_path)
    reserve(ledger, intent="c1", nonce=1)
    ledger.mark_signed("c1", TX)
    ledger.reconcile(
        "c1",
        tx_hash=TX,
        block_hash=BLOCK,
        net_profit_usd=D("-.5"),
        actual_gas_usd=D(".1"),
        receipt_status=1,
    )
    with pytest.raises(ExecutionLatched, match="Conflicting receipt"):
        ledger.reconcile(
            "c1",
            tx_hash=TX,
            block_hash=BLOCK,
            net_profit_usd=D("-.4"),
            actual_gas_usd=D(".1"),
            receipt_status=1,
        )
    with pytest.raises(ExecutionLatched, match="Conflicting receipt"):
        ledger.reconcile(
            "c1",
            tx_hash=TX,
            block_hash=BLOCK,
            net_profit_usd=D("-.5"),
            actual_gas_usd=D(".2"),
            receipt_status=1,
        )


def test_low_decimal_context_boundary_precision(tmp_path: Path) -> None:
    """Fraction arithmetic is unaffected by degraded decimal context precision."""
    db_path = tmp_path / "ledger.sqlite"
    with decimal.localcontext() as ctx:
        ctx.prec = 1
        ledger = make_ledger(db_path, budget=Decimal("1.00"))
        reserve(ledger, intent="prec1", nonce=1, loss=Decimal("0.05"))
        st = ledger.status(4663, WALLET, "USDG")
        assert st["remaining_usd"] == Decimal("0.95")
        ledger.mark_signed("prec1", TX)
        ledger.reconcile(
            "prec1",
            tx_hash=TX,
            block_hash=BLOCK,
            net_profit_usd=Decimal("-0.035"),
            actual_gas_usd=Decimal("0.035"),
            receipt_status=1,
        )
        st2 = ledger.status(4663, WALLET, "USDG")
        assert st2["spent_usd"] == Decimal("0.035")
        assert st2["remaining_usd"] == Decimal("0.965")


def test_concurrent_reservation_single_winner(tmp_path: Path) -> None:
    """Two instances competing on the same wallet serialize via BEGIN IMMEDIATE."""
    db_path = tmp_path / "ledger.sqlite"
    l1 = make_ledger(db_path)
    l2 = make_ledger(db_path)

    winners: list[str] = []
    latched_errors: list[ExecutionLatched] = []

    def attempt_reserve(inst: FundsLedger, intent_id: str, nonce: int) -> None:
        try:
            reserve(inst, intent=intent_id, nonce=nonce)
            winners.append(intent_id)
        except ExecutionLatched as exc:
            latched_errors.append(exc)

    t1 = threading.Thread(target=attempt_reserve, args=(l1, "race_1", 1))
    t2 = threading.Thread(target=attempt_reserve, args=(l2, "race_2", 2))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(winners) == 1
    assert len(latched_errors) == 1


def test_constructor_and_input_validation(tmp_path: Path) -> None:
    """Explicit parameters must be validated strictly; no implicit defaults."""
    # Non-absolute path
    with pytest.raises(LedgerError, match="absolute"):
        FundsLedger(Path("relative_path.sqlite"), allowed_bases=ALLOWED_BASES, loss_budget_usd=D(1))

    # Empty bases
    with pytest.raises(LedgerError, match="allowed_bases"):
        FundsLedger(tmp_path / "db.sqlite", allowed_bases=frozenset(), loss_budget_usd=D(1))

    # Non-Decimal / bool budget
    with pytest.raises(LedgerError, match="loss_budget_usd"):
        FundsLedger(tmp_path / "db.sqlite", allowed_bases=ALLOWED_BASES, loss_budget_usd=True)  # type: ignore

    with pytest.raises(LedgerError, match="loss_budget_usd"):
        FundsLedger(tmp_path / "db.sqlite", allowed_bases=ALLOWED_BASES, loss_budget_usd=D("-1"))

    # Negative gas rejection
    ledger = make_ledger(tmp_path / "val.sqlite")
    reserve(ledger, intent="val1")
    ledger.mark_signed("val1", TX)
    with pytest.raises(LedgerError, match="cannot be negative"):
        ledger.reconcile(
            "val1",
            tx_hash=TX,
            block_hash=BLOCK,
            net_profit_usd=D("-.1"),
            actual_gas_usd=D("-.1"),
            receipt_status=1,
        )
