"""Admission boundary counter-examples for coordinator and ledger identity repairs.

Validates 4 single-variable public interface counter-examples:
1. Slot tampering via dataclasses.replace (amount / floor / generation) rejected by _verify_slot_payload_consistency.
2. Sparse / incomplete payload slot rejected by atomic row identity verification in ledger.
3. External dict mutation on slot does not corrupt slot generation and is rejected upon execution.
4. Public reconcile identity precedence and single-variable receipt conflict rejection.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from research.reservation_coordinator import (
    CoordinatorError,
    ExecutionCoordinator,
    ReservationRequest,
    ReservedExecution,
    SlotVerificationError,
    ValuationEvidence,
)
from research.reservation_ledger import ExecutionLatched, FundsLedger

CHAIN_ID = 5042
WALLET_1 = "0x" + "11" * 20
TX_HASH_1 = "0x" + "aa" * 32
BLOCK_HASH_1 = "0x" + "cc" * 32
ALLOWED_BASES = frozenset({"USDG"})
LOSS_BUDGET = Decimal("10.0")


def make_ledger(db_path: Path) -> FundsLedger:
    return FundsLedger(db_path, allowed_bases=ALLOWED_BASES, loss_budget_usd=LOSS_BUDGET)


def make_coordinator(ledger: FundsLedger) -> ExecutionCoordinator:
    val = ValuationEvidence(
        base_price_usd=Decimal("1.0"),
        gas_usd=Decimal("0.05"),
        slippage_bps=50,
    )
    return ExecutionCoordinator(
        ledger,
        chain_id=CHAIN_ID,
        valuation_provider=lambda _req: val,
    )


def test_counterexample_1_dataclasses_replace_tampering_rejected(tmp_path: Path) -> None:
    """Counter-example 1: Single-variable slot mutation via dataclasses.replace is rejected in __post_init__."""
    db_path = tmp_path / "ledger.sqlite"
    ledger = make_ledger(db_path)
    coord = make_coordinator(ledger)

    req = ReservationRequest(
        plan_id="plan_ce1",
        chain_id=CHAIN_ID,
        wallet_address=WALLET_1,
        base_symbol="USDG",
        decimals=6,
        amount_in_atoms=100_000_000,
        minimum_output_atoms=100_050_001,
        nonce=1,
    )
    slot = coord.acquire_execution_slot(req, one_shot=True)

    # 1A: Only generation tampered
    with pytest.raises(SlotVerificationError, match="Slot payload generation mismatch"):
        dataclasses.replace(slot, generation="forged_gen_123")

    # 1B: Only amount_in_atoms tampered
    tampered_req_amount = dataclasses.replace(req, amount_in_atoms=200_000_000)
    with pytest.raises(SlotVerificationError, match="Slot payload amount_in_atoms mismatch"):
        dataclasses.replace(slot, request=tampered_req_amount)

    # 1C: Only minimum_output_atoms (floor) tampered
    tampered_req_floor = dataclasses.replace(req, minimum_output_atoms=999_999_999)
    with pytest.raises(SlotVerificationError, match="Slot payload minimum_output_atoms mismatch"):
        dataclasses.replace(slot, request=tampered_req_floor)


def test_counterexample_2_sparse_payload_rejected_by_atomic_identity(tmp_path: Path) -> None:
    """Counter-example 2: Payload missing fields cannot match row accidentally; rejected atomically."""
    db_path = tmp_path / "ledger.sqlite"
    ledger = make_ledger(db_path)
    coord = make_coordinator(ledger)

    req = ReservationRequest(
        plan_id="plan_ce2",
        chain_id=CHAIN_ID,
        wallet_address=WALLET_1,
        base_symbol="USDG",
        decimals=6,
        amount_in_atoms=100_000_000,
        minimum_output_atoms=100_050_001,
        nonce=1,
    )
    slot = coord.acquire_execution_slot(req, one_shot=True)

    # Synthesize a slot whose payload is missing gas_atoms and gas_usd fields
    assert slot.payload is not None
    sparse_payload = {
        k: v for k, v in slot.payload.items() if k not in ("gas_atoms", "gas_usd", "base_price_usd")
    }
    sparse_slot = ReservedExecution(
        request=req,
        reserved_loss_usd=slot.reserved_loss_usd,
        generation=slot.generation,
        one_shot=slot.one_shot,
        payload=sparse_payload,
    )

    # Attempting to sign with sparse slot must be rejected
    with pytest.raises((SlotVerificationError, ExecutionLatched), match="Intent payload mismatch"):
        coord.record_signed(sparse_slot, TX_HASH_1)

    # Assert pending state remains RESERVED, not SIGNED
    pending = ledger.pending_intent(CHAIN_ID, WALLET_1)
    assert pending is not None
    assert pending["state"] == "RESERVED"


def test_counterexample_3_external_dict_mutation_cannot_masquerade(tmp_path: Path) -> None:
    """Counter-example 3: Mutating external dict cannot alter slot.generation or mutate another generation."""
    db_path = tmp_path / "ledger.sqlite"
    ledger = make_ledger(db_path)
    coord = make_coordinator(ledger)

    req1 = ReservationRequest(
        plan_id="plan_ce3",
        chain_id=CHAIN_ID,
        wallet_address=WALLET_1,
        base_symbol="USDG",
        decimals=6,
        amount_in_atoms=100_000_000,
        minimum_output_atoms=100_050_001,
        nonce=1,
    )
    slot1 = coord.acquire_execution_slot(req1, one_shot=True)

    # Mutate slot1's internal payload dictionary
    assert slot1.payload is not None
    orig_gen = slot1.generation
    slot1.payload["generation"] = "corrupted_generation_uuid"

    # Slot 1's frozen generation attribute is immutable
    assert slot1.generation == orig_gen

    # Attempting to sign with corrupted payload dict fails consistency verification
    with pytest.raises(SlotVerificationError, match="Slot payload generation mismatch"):
        coord.record_signed(slot1, TX_HASH_1)

    # Restore slot1's generation in payload dict, and release it cleanly
    slot1.payload["generation"] = orig_gen
    coord.release_execution_slot(slot1)

    # Acquire a second slot; verify slot2 is independent and unaffected
    req2 = ReservationRequest(
        plan_id="plan_ce3_gen2",
        chain_id=CHAIN_ID,
        wallet_address=WALLET_1,
        base_symbol="USDG",
        decimals=6,
        amount_in_atoms=100_000_000,
        minimum_output_atoms=100_050_001,
        nonce=2,
    )
    slot2 = coord.acquire_execution_slot(req2, one_shot=True)
    assert slot2.generation != orig_gen
    assert slot2.payload is not None
    assert slot2.payload["generation"] == slot2.generation


def test_counterexample_4_reconcile_identity_precedence_and_single_variable_conflict(
    tmp_path: Path,
) -> None:
    """Counter-example 4: Reconcile verifies identity before hash; single-variable conflict is rejected."""
    db_path = tmp_path / "ledger.sqlite"
    ledger = make_ledger(db_path)
    coord = make_coordinator(ledger)

    req = ReservationRequest(
        plan_id="plan_ce4",
        chain_id=CHAIN_ID,
        wallet_address=WALLET_1,
        base_symbol="USDG",
        decimals=6,
        amount_in_atoms=100_000_000,
        minimum_output_atoms=100_050_001,
        nonce=1,
    )
    slot = coord.acquire_execution_slot(req, one_shot=True)
    coord.record_signed(slot, TX_HASH_1)

    # 4A: Tampered slot (different nonce) presented to reconcile fails on identity
    tampered_req = dataclasses.replace(req, nonce=99)
    assert slot.payload is not None
    tampered_payload = dict(slot.payload)
    tampered_payload["nonce"] = 99
    tampered_slot = ReservedExecution(
        request=tampered_req,
        reserved_loss_usd=slot.reserved_loss_usd,
        generation=slot.generation,
        one_shot=slot.one_shot,
        payload=tampered_payload,
    )

    with pytest.raises((SlotVerificationError, ExecutionLatched), match="Slot nonce mismatch|Intent nonce mismatch"):
        coord.reconcile(
            tampered_slot,
            tx_hash=TX_HASH_1,
            block_hash=BLOCK_HASH_1,
            net_profit_usd=Decimal("0.10"),
            actual_gas_usd=Decimal("0.05"),
            receipt_status=1,
        )

    # Intent is still SIGNED in ledger
    with sqlite3.connect(db_path) as conn:
        state = conn.execute("SELECT state FROM intents WHERE id=?", (req.plan_id,)).fetchone()[0]
        assert state == "SIGNED"

    # 4B: Valid reconcile succeeds losslessly
    coord.reconcile(
        slot,
        tx_hash=TX_HASH_1,
        block_hash=BLOCK_HASH_1,
        net_profit_usd=Decimal("0.10"),
        actual_gas_usd=Decimal("0.05"),
        receipt_status=1,
    )
    with sqlite3.connect(db_path) as conn:
        state = conn.execute("SELECT state FROM intents WHERE id=?", (req.plan_id,)).fetchone()[0]
        assert state == "RECONCILED"

    # Repeat reconcile with identical arguments is idempotent
    coord.reconcile(
        slot,
        tx_hash=TX_HASH_1,
        block_hash=BLOCK_HASH_1,
        net_profit_usd=Decimal("0.10"),
        actual_gas_usd=Decimal("0.05"),
        receipt_status=1,
    )

    # 4C: Single-variable conflict on net_profit_usd rejected
    with pytest.raises(ExecutionLatched, match="Conflicting receipt"):
        coord.reconcile(
            slot,
            tx_hash=TX_HASH_1,
            block_hash=BLOCK_HASH_1,
            net_profit_usd=Decimal("0.11"),
            actual_gas_usd=Decimal("0.05"),
            receipt_status=1,
        )
