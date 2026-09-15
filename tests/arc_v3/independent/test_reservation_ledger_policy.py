"""Policy regression tests for FundsLedger persistent binding and admission immutability.

Verifies:
1. Same-config reopen and numerical equivalence (e.g. Decimal("1") vs Decimal("1.0")).
2. Divergent budget and divergent bases reopen rejected with LedgerError.
3. Concurrent first initialization with divergent configs: exactly one succeeds.
4. Existing ledger data without metadata rejected with LedgerError.
5. Corrupt metadata fails closed on subsequent operations with no new intent created.
6. Low Decimal context precision maintains exact values without rounding.
7. Instance policy attributes are immutable and tamper-resistant.
"""

from __future__ import annotations

import decimal
import json
import sqlite3
import threading
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
D = Decimal


def make_ledger(
    db_path: Path,
    *,
    bases: frozenset[str] = ALLOWED_BASES,
    budget: Decimal = D("1"),
) -> FundsLedger:
    return FundsLedger(db_path, allowed_bases=bases, loss_budget_usd=budget)


def reserve_intent(
    ledger: FundsLedger,
    intent: str = "first",
    base: str = "USDG",
    nonce: int = 1,
    loss: Decimal = D("0.5"),
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


def test_same_config_reopen_and_numerical_equivalence(tmp_path: Path) -> None:
    """Same configuration reopen and numerical equivalence (Decimal('1') vs '1.0') succeed."""
    db_path = tmp_path / "equiv.sqlite"
    inst1 = FundsLedger(db_path, allowed_bases=frozenset({"USDG"}), loss_budget_usd=D("1.0"))
    reserve_intent(inst1, intent="intent_1", loss=D("0.3"))

    # Reopen with identical config
    inst2 = FundsLedger(db_path, allowed_bases=frozenset({"USDG"}), loss_budget_usd=D("1.0"))
    st2 = inst2.status(4663, WALLET, "USDG")
    assert st2["reserved_usd"] == D("0.3")
    assert st2["remaining_usd"] == D("0.7")

    # Reopen with numerically equivalent budget (1 vs 1.0 vs 1.000)
    inst3 = FundsLedger(db_path, allowed_bases=frozenset({"USDG"}), loss_budget_usd=D("1"))
    st3 = inst3.status(4663, WALLET, "USDG")
    assert st3["reserved_usd"] == D("0.3")
    assert st3["remaining_usd"] == D("0.7")

    inst4 = FundsLedger(db_path, allowed_bases=frozenset({"USDG"}), loss_budget_usd=D("1.000"))
    st4 = inst4.status(4663, WALLET, "USDG")
    assert st4["reserved_usd"] == D("0.3")
    assert st4["remaining_usd"] == D("0.7")


def test_reopen_divergent_budget_rejected(tmp_path: Path) -> None:
    """Reopening an existing database with modified loss budget is rejected with LedgerError."""
    db_path = tmp_path / "budget_mismatch.sqlite"
    FundsLedger(db_path, allowed_bases=frozenset({"USDG"}), loss_budget_usd=D("1.0"))

    # Budget expansion rejected
    with pytest.raises(LedgerError, match="[Pp]olicy|[Bb]udget"):
        FundsLedger(db_path, allowed_bases=frozenset({"USDG"}), loss_budget_usd=D("100.0"))

    # Budget reduction rejected (no automatic migration)
    with pytest.raises(LedgerError, match="[Pp]olicy|[Bb]udget"):
        FundsLedger(db_path, allowed_bases=frozenset({"USDG"}), loss_budget_usd=D("0.5"))

    # Subtle difference rejected
    with pytest.raises(LedgerError, match="[Pp]olicy|[Bb]udget"):
        FundsLedger(db_path, allowed_bases=frozenset({"USDG"}), loss_budget_usd=D("1.000000000000000001"))


def test_reopen_divergent_bases_rejected(tmp_path: Path) -> None:
    """Reopening an existing database with modified allowed_bases is rejected with LedgerError."""
    db_path = tmp_path / "bases_mismatch.sqlite"
    FundsLedger(db_path, allowed_bases=frozenset({"USDG"}), loss_budget_usd=D("1.0"))

    # Base expansion rejected
    with pytest.raises(LedgerError, match="[Pp]olicy|[Bb]ase"):
        FundsLedger(db_path, allowed_bases=frozenset({"USDG", "WETH"}), loss_budget_usd=D("1.0"))

    # Disjoint bases rejected
    with pytest.raises(LedgerError, match="[Pp]olicy|[Bb]ase"):
        FundsLedger(db_path, allowed_bases=frozenset({"WETH"}), loss_budget_usd=D("1.0"))

    # Base subset rejected (no automatic migration)
    db_path_two = tmp_path / "two_bases.sqlite"
    FundsLedger(db_path_two, allowed_bases=frozenset({"USDG", "WETH"}), loss_budget_usd=D("1.0"))
    with pytest.raises(LedgerError, match="[Pp]olicy|[Bb]ase"):
        FundsLedger(db_path_two, allowed_bases=frozenset({"USDG"}), loss_budget_usd=D("1.0"))


def test_concurrent_first_initialization_divergence_race(tmp_path: Path) -> None:
    """Two concurrent instances with divergent configs racing on empty DB: exactly one wins."""
    db_path = tmp_path / "race_init.sqlite"
    barrier = threading.Barrier(2)
    results: list[str] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def init_inst1() -> None:
        barrier.wait()
        try:
            FundsLedger(db_path, allowed_bases=frozenset({"USDG"}), loss_budget_usd=D("1.0"))
            with lock:
                results.append("inst1")
        except Exception as exc:
            with lock:
                errors.append(exc)

    def init_inst2() -> None:
        barrier.wait()
        try:
            FundsLedger(db_path, allowed_bases=frozenset({"USDG", "WETH"}), loss_budget_usd=D("100.0"))
            with lock:
                results.append("inst2")
        except Exception as exc:
            with lock:
                errors.append(exc)

    t1 = threading.Thread(target=init_inst1)
    t2 = threading.Thread(target=init_inst2)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Exactly one succeeded and one failed with LedgerError
    assert len(results) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], LedgerError)


def test_existing_data_without_metadata_rejected(tmp_path: Path) -> None:
    """Existing database tables with data but missing policy metadata are rejected fail-closed."""
    db_path_wallets = tmp_path / "legacy_wallets.sqlite"
    with sqlite3.connect(db_path_wallets) as db:
        db.execute(
            "CREATE TABLE wallets (chain INTEGER, wallet TEXT, spent TEXT DEFAULT '0', "
            "paused INTEGER DEFAULT 0, pending TEXT, PRIMARY KEY (chain, wallet))"
        )
        db.execute("INSERT INTO wallets (chain, wallet, spent) VALUES (4663, ?, '0')", (WALLET,))

    with pytest.raises(LedgerError, match="[Pp]olicy|[Mm]etadata"):
        FundsLedger(db_path_wallets, allowed_bases=ALLOWED_BASES, loss_budget_usd=D("1.0"))

    db_path_intents = tmp_path / "legacy_intents.sqlite"
    with sqlite3.connect(db_path_intents) as db:
        db.execute(
            "CREATE TABLE intents (id TEXT PRIMARY KEY, chain INTEGER, wallet TEXT, base TEXT, "
            "nonce INTEGER, payload TEXT, reserve TEXT, state TEXT, one_shot INTEGER)"
        )
        db.execute(
            "INSERT INTO intents VALUES ('legacy_1', 4663, ?, 'USDG', 1, '{}', '0.5', 'RESERVED', 1)",
            (WALLET,),
        )

    with pytest.raises(LedgerError, match="[Pp]olicy|[Mm]etadata"):
        FundsLedger(db_path_intents, allowed_bases=ALLOWED_BASES, loss_budget_usd=D("1.0"))


def test_corrupt_metadata_fails_closed_no_new_intent(tmp_path: Path) -> None:
    """Corrupted metadata rejects subsequent operations fail-closed without modifying state."""
    db_path = tmp_path / "corrupt_meta.sqlite"
    ledger = FundsLedger(db_path, allowed_bases=frozenset({"USDG"}), loss_budget_usd=D("1.0"))
    reserve_intent(ledger, intent="intent_seed", nonce=1, loss=D("0.2"))

    def check_intents_count(expected: int = 1) -> None:
        with sqlite3.connect(db_path) as db:
            count = db.execute("SELECT COUNT(*) FROM intents").fetchone()[0]
            assert count == expected

    # 1. Empty metadata table
    with sqlite3.connect(db_path) as db:
        db.execute("DELETE FROM metadata")

    with pytest.raises(LedgerError):
        ledger.status(4663, WALLET, "USDG")

    with pytest.raises(LedgerError):
        reserve_intent(ledger, intent="intent_after_del", nonce=2, loss=D("0.2"))
    check_intents_count(1)

    # 2. Corrupt schema_version
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO metadata (id, schema_version, allowed_bases, loss_budget_usd) VALUES (1, ?, ?, ?)",
            (999, '["USDG"]', "1"),
        )

    with pytest.raises(LedgerError, match="[Ss]chema|[Vv]ersion"):
        ledger.status(4663, WALLET, "USDG")
    with pytest.raises(LedgerError, match="[Ss]chema|[Vv]ersion"):
        reserve_intent(ledger, intent="intent_bad_schema", nonce=2, loss=D("0.2"))
    check_intents_count(1)

    # 3. Corrupt allowed_bases JSON
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE metadata SET schema_version=1, allowed_bases='not valid json'")

    with pytest.raises(LedgerError):
        ledger.status(4663, WALLET, "USDG")
    with pytest.raises(LedgerError):
        reserve_intent(ledger, intent="intent_bad_json", nonce=2, loss=D("0.2"))
    check_intents_count(1)

    # 4. Non-canonical / duplicate allowed_bases
    with sqlite3.connect(db_path) as db:
        db.execute('UPDATE metadata SET allowed_bases=\'["USDG","USDG"]\'')

    with pytest.raises(LedgerError):
        ledger.status(4663, WALLET, "USDG")
    with pytest.raises(LedgerError):
        reserve_intent(ledger, intent="intent_dup_bases", nonce=2, loss=D("0.2"))
    check_intents_count(1)

    # 5. Corrupt loss_budget_usd (NaN or negative)
    with sqlite3.connect(db_path) as db:
        db.execute('UPDATE metadata SET allowed_bases=\'["USDG"]\', loss_budget_usd=\'NaN\'')

    with pytest.raises(LedgerError):
        ledger.status(4663, WALLET, "USDG")
    with pytest.raises(LedgerError):
        reserve_intent(ledger, intent="intent_nan_budget", nonce=2, loss=D("0.2"))
    check_intents_count(1)

    # 6. Dropped metadata table
    with sqlite3.connect(db_path) as db:
        db.execute("DROP TABLE metadata")

    with pytest.raises(LedgerError):
        ledger.status(4663, WALLET, "USDG")
    with pytest.raises(LedgerError):
        reserve_intent(ledger, intent="intent_no_table", nonce=2, loss=D("0.2"))
    check_intents_count(1)


def test_low_decimal_context_precision_maintained(tmp_path: Path) -> None:
    """Decimal context precision does not truncate or distort exact ledger balances."""
    db_path = tmp_path / "low_prec.sqlite"

    with decimal.localcontext() as ctx:
        ctx.prec = 2
        # Initialize ledger with high-precision decimal under prec=2
        budget = D("1.23456")
        ledger = FundsLedger(db_path, allowed_bases=frozenset({"USDG"}), loss_budget_usd=budget)
        assert ledger.loss_budget_usd == budget

        # Reopen under prec=2 succeeds
        reopened = FundsLedger(db_path, allowed_bases=frozenset({"USDG"}), loss_budget_usd=budget)
        assert reopened.loss_budget_usd == budget

        # Reserve exact amount
        reserve_intent(reopened, intent="prec_intent", nonce=1, loss=D("0.12345"))
        st = reopened.status(4663, WALLET, "USDG")
        assert st["reserved_usd"] == D("0.12345")
        assert st["remaining_usd"] == D("1.11111")


def test_instance_policy_attributes_immutable_and_tamper_resistant(tmp_path: Path) -> None:
    """External modification of policy attributes is blocked or causes fail-closed rejection."""
    db_path = tmp_path / "immutable.sqlite"
    ledger = FundsLedger(db_path, allowed_bases=frozenset({"USDG"}), loss_budget_usd=D("1.0"))

    # Direct attribute assignment is blocked by read-only properties
    with pytest.raises(AttributeError):
        ledger.allowed_bases = frozenset({"USDG", "WETH"})  # type: ignore

    with pytest.raises(AttributeError):
        ledger.loss_budget_usd = D("100.0")  # type: ignore

    # Forcible tampering of private attributes triggers fail-closed on next operation
    object.__setattr__(ledger, "_budget_fraction", decimal.Decimal("100"))
    with pytest.raises(LedgerError):
        ledger.status(4663, WALLET, "USDG")
