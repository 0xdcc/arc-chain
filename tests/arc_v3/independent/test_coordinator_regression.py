"""Direct independent regression and boundary tests for research/reservation_coordinator.py.

Frozen regression suite for COORDINATOR-R3/v1:
1. Stale slot with same id/nonce but different payload cannot cancel/sign/unknown/reconcile new slot.
2. Stale slot with identical request/nonce re-reserved is rejected due to fresh generation uuid.
3. Public release_execution_slot with controlled event barrier interleaving fails when concurrent
   cancel+reserve executes, and the new slot's pending state is preserved (atomic TOCTOU defense).
4. First reconcile succeeds, repeat call with identical parameters succeeds idempotently (lossless),
   and conflicting receipt fields (net, gas, hash, status) are strictly rejected.
5. All coordinator write actions use atomic expected identity; forged/tampered slots cannot mutate state.
6. Boundaries: zero/negative base price rejection, exact Fraction ceil +1 floor, explicit one_shot parameter.
"""

from __future__ import annotations

import inspect
import threading
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

from research.reservation_coordinator import (
    CoordinatorError,
    ExecutionCoordinator,
    FloorViolationError,
    ReservationRequest,
    ReservedExecution,
    SlotVerificationError,
    ValuationError,
    ValuationEvidence,
)
from research.reservation_ledger import ExecutionLatched, FundsLedger

CHAIN_ID = 5042
WALLET_1 = "0x" + "11" * 20
WALLET_2 = "0x" + "22" * 20
TX_HASH_1 = "0x" + "aa" * 32
TX_HASH_2 = "0x" + "bb" * 32
BLOCK_HASH_1 = "0x" + "cc" * 32
BLOCK_HASH_2 = "0x" + "dd" * 32
ALLOWED_BASES = frozenset({"USDG"})
LOSS_BUDGET = Decimal("10.0")


def make_ledger(db_path: Path) -> FundsLedger:
    return FundsLedger(db_path, allowed_bases=ALLOWED_BASES, loss_budget_usd=LOSS_BUDGET)


def make_coordinator(
    ledger: FundsLedger,
    valuation: ValuationEvidence | None = None,
) -> ExecutionCoordinator:
    default_val = valuation or ValuationEvidence(
        base_price_usd=Decimal("1.0"),
        gas_usd=Decimal("0.05"),
        slippage_bps=50,
    )
    return ExecutionCoordinator(
        ledger,
        chain_id=CHAIN_ID,
        valuation_provider=lambda _req: default_val,
    )


def test_regression_1_stale_slot_superseded_payload_cannot_sign_cancel_or_reconcile(
    tmp_path: Path,
) -> None:
    """Regression 1: Old slot with same id/nonce but different payload cannot operate on new slot."""
    db_path = tmp_path / "ledger.sqlite"
    ledger = make_ledger(db_path)
    coord = make_coordinator(ledger)

    req1 = ReservationRequest(
        plan_id="plan_aba",
        chain_id=CHAIN_ID,
        wallet_address=WALLET_1,
        base_symbol="USDG",
        decimals=6,
        amount_in_atoms=100_000_000,
        minimum_output_atoms=100_050_001,
        nonce=1,
    )
    slot1 = coord.acquire_execution_slot(req1, one_shot=True)
    coord.release_execution_slot(slot1)
    assert not coord.is_in_flight(WALLET_1)

    req2 = ReservationRequest(
        plan_id="plan_aba",
        chain_id=CHAIN_ID,
        wallet_address=WALLET_1,
        base_symbol="USDG",
        decimals=6,
        amount_in_atoms=200_000_000,
        minimum_output_atoms=200_050_001,
        nonce=1,
    )
    slot2 = coord.acquire_execution_slot(req2, one_shot=True)
    assert coord.is_in_flight(WALLET_1)

    # 1. Stale slot1 cannot sign new slot2
    with pytest.raises((SlotVerificationError, ExecutionLatched)):
        coord.record_signed(slot1, TX_HASH_1)

    # 2. Stale slot1 cannot cancel new slot2
    with pytest.raises((SlotVerificationError, ExecutionLatched)):
        coord.release_execution_slot(slot1)

    # 3. Stale slot1 cannot mark unknown on new slot2
    with pytest.raises((SlotVerificationError, ExecutionLatched)):
        coord.mark_unknown(slot1)

    # 4. Stale slot1 cannot reconcile new slot2
    with pytest.raises((SlotVerificationError, ExecutionLatched)):
        coord.reconcile(
            slot1,
            tx_hash=TX_HASH_1,
            block_hash=BLOCK_HASH_1,
            net_profit_usd=Decimal("0.10"),
            actual_gas_usd=Decimal("0.05"),
            receipt_status=1,
        )

    # Confirm slot2 remains intact in pending state
    pending = ledger.pending_intent(CHAIN_ID, WALLET_1)
    assert pending is not None
    assert pending["state"] == "RESERVED"
    assert pending["payload"]["amount_in_atoms"] == 200_000_000


def test_regression_2_identical_request_and_nonce_rejected_due_to_generation(
    tmp_path: Path,
) -> None:
    """Regression 2: Re-reservation with identical request and nonce generates new generation and rejects stale slot."""
    db_path = tmp_path / "ledger.sqlite"
    ledger = make_ledger(db_path)
    coord = make_coordinator(ledger)

    req = ReservationRequest(
        plan_id="plan_gen",
        chain_id=CHAIN_ID,
        wallet_address=WALLET_1,
        base_symbol="USDG",
        decimals=6,
        amount_in_atoms=100_000_000,
        minimum_output_atoms=100_050_001,
        nonce=1,
    )
    slot1 = coord.acquire_execution_slot(req, one_shot=True)
    coord.release_execution_slot(slot1)
    assert not coord.is_in_flight(WALLET_1)

    slot2 = coord.acquire_execution_slot(req, one_shot=True)
    assert coord.is_in_flight(WALLET_1)

    # slot1 and slot2 have distinct generations
    assert slot1.generation != slot2.generation

    # Stale slot1 cannot sign new reservation slot2 even with identical request parameters
    with pytest.raises((SlotVerificationError, ExecutionLatched)):
        coord.record_signed(slot1, TX_HASH_1)

    # Valid slot2 signs cleanly
    coord.record_signed(slot2, TX_HASH_1)
    pending = ledger.pending_intent(CHAIN_ID, WALLET_1)
    assert pending is not None
    assert pending["state"] == "SIGNED"
    assert pending["hash"] == TX_HASH_1


class InterleavedLedger(FundsLedger):
    """Test ledger hook for controlled deterministic threading synchronization."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.before_cancel_event = threading.Event()
        self.proceed_cancel_event = threading.Event()
        self.interleaving_active = False

    def cancel_before_signing(self, intent_id: str, *args, **kwargs) -> None:
        if self.interleaving_active:
            self.before_cancel_event.set()
            assert self.proceed_cancel_event.wait(timeout=5.0), "Proceed cancel event timed out"
        super().cancel_before_signing(intent_id, *args, **kwargs)


def test_regression_3_controlled_interleaving_release_fails_and_new_pending_preserved(
    tmp_path: Path,
) -> None:
    """Regression 3: In public release call, interleaving another cancel+reserve causes old release to fail, preserving new pending."""
    db_path = tmp_path / "ledger.sqlite"
    ledger = InterleavedLedger(db_path, allowed_bases=ALLOWED_BASES, loss_budget_usd=LOSS_BUDGET)
    coord1 = make_coordinator(ledger)
    coord2 = make_coordinator(ledger)

    req1 = ReservationRequest(
        plan_id="plan_toctou",
        chain_id=CHAIN_ID,
        wallet_address=WALLET_1,
        base_symbol="USDG",
        decimals=6,
        amount_in_atoms=100_000_000,
        minimum_output_atoms=100_050_001,
        nonce=1,
    )
    slot1 = coord1.acquire_execution_slot(req1, one_shot=True)

    thread1_exception: list[Exception] = []

    ledger.interleaving_active = True

    def run_thread1_release() -> None:
        try:
            coord1.release_execution_slot(slot1)
        except Exception as exc:
            thread1_exception.append(exc)

    t1 = threading.Thread(target=run_thread1_release)
    t1.start()

    # Wait until Thread 1 enters cancel_before_signing
    assert ledger.before_cancel_event.wait(timeout=5.0), "Thread 1 did not reach cancellation hook"

    # Thread 2 intervenes: cancels slot1 and acquires slot2 with nonce=2
    ledger.interleaving_active = False
    coord2.release_execution_slot(slot1)
    assert not coord2.is_in_flight(WALLET_1)

    req2 = ReservationRequest(
        plan_id="plan_toctou",
        chain_id=CHAIN_ID,
        wallet_address=WALLET_1,
        base_symbol="USDG",
        decimals=6,
        amount_in_atoms=200_000_000,
        minimum_output_atoms=200_050_001,
        nonce=2,
    )
    slot2 = coord2.acquire_execution_slot(req2, one_shot=True)
    assert coord2.is_in_flight(WALLET_1)

    # Resume Thread 1
    ledger.proceed_cancel_event.set()
    t1.join(timeout=5.0)

    # Thread 1 MUST fail due to expected_identity mismatch
    assert len(thread1_exception) == 1, "Thread 1 release was expected to fail on expected_identity mismatch"
    assert isinstance(thread1_exception[0], (SlotVerificationError, ExecutionLatched))

    # slot2 must NOT have been destroyed by Thread 1
    pending_after = ledger.pending_intent(CHAIN_ID, WALLET_1)
    assert pending_after is not None, "slot2 was destroyed by stale release!"
    assert pending_after["nonce"] == 2
    assert pending_after["payload"]["generation"] == slot2.generation


def test_regression_4_reconcile_repeat_idempotent_success_and_conflict_rejection(
    tmp_path: Path,
) -> None:
    """Regression 4: First reconcile succeeds, repeat identical call succeeds losslessly, conflicting receipts rejected."""
    db_path = tmp_path / "ledger.sqlite"
    ledger = make_ledger(db_path)
    coord = make_coordinator(ledger)

    req = ReservationRequest(
        plan_id="plan_rec",
        chain_id=CHAIN_ID,
        wallet_address=WALLET_1,
        base_symbol="USDG",
        decimals=6,
        amount_in_atoms=10_000_000,
        minimum_output_atoms=10_050_001,
        nonce=1,
    )
    slot = coord.acquire_execution_slot(req, one_shot=True)
    coord.record_signed(slot, TX_HASH_1)

    # 1. First reconcile succeeds
    coord.reconcile(
        slot,
        tx_hash=TX_HASH_1,
        block_hash=BLOCK_HASH_1,
        net_profit_usd=Decimal("0.10"),
        actual_gas_usd=Decimal("0.05"),
        receipt_status=1,
    )

    # 2. Repeat call with identical parameters succeeds idempotently (lossless)
    coord.reconcile(
        slot,
        tx_hash=TX_HASH_1,
        block_hash=BLOCK_HASH_1,
        net_profit_usd=Decimal("0.10"),
        actual_gas_usd=Decimal("0.05"),
        receipt_status=1,
    )

    # 3. Conflicting net_profit_usd rejected
    with pytest.raises(ExecutionLatched, match="Conflicting receipt"):
        coord.reconcile(
            slot,
            tx_hash=TX_HASH_1,
            block_hash=BLOCK_HASH_1,
            net_profit_usd=Decimal("0.20"),
            actual_gas_usd=Decimal("0.05"),
            receipt_status=1,
        )

    # 4. Conflicting actual_gas_usd rejected
    with pytest.raises(ExecutionLatched, match="Conflicting receipt"):
        coord.reconcile(
            slot,
            tx_hash=TX_HASH_1,
            block_hash=BLOCK_HASH_1,
            net_profit_usd=Decimal("0.10"),
            actual_gas_usd=Decimal("0.06"),
            receipt_status=1,
        )

    # 5. Conflicting block_hash / receipt_id rejected
    with pytest.raises(ExecutionLatched, match="Conflicting receipt"):
        coord.reconcile(
            slot,
            tx_hash=TX_HASH_1,
            block_hash=BLOCK_HASH_2,
            net_profit_usd=Decimal("0.10"),
            actual_gas_usd=Decimal("0.05"),
            receipt_status=1,
        )

    # 6. Conflicting tx_hash rejected
    with pytest.raises(ExecutionLatched, match="Receipt does not match persisted intent"):
        coord.reconcile(
            slot,
            tx_hash=TX_HASH_2,
            block_hash=BLOCK_HASH_1,
            net_profit_usd=Decimal("0.10"),
            actual_gas_usd=Decimal("0.05"),
            receipt_status=1,
        )

    # 7. Conflicting receipt_status rejected
    with pytest.raises(ExecutionLatched, match="Conflicting receipt"):
        coord.reconcile(
            slot,
            tx_hash=TX_HASH_1,
            block_hash=BLOCK_HASH_1,
            net_profit_usd=Decimal("-0.05"),
            actual_gas_usd=Decimal("0.05"),
            receipt_status=0,
        )


def test_regression_5_atomic_expected_identity_blocks_tampered_slot_mutations(
    tmp_path: Path,
) -> None:
    """Regression 5: All write actions use atomic expected identity, forged/tampered slots cannot mutate state."""
    db_path = tmp_path / "ledger.sqlite"
    ledger = make_ledger(db_path)
    coord = make_coordinator(ledger)

    req = ReservationRequest(
        plan_id="plan_tamper",
        chain_id=CHAIN_ID,
        wallet_address=WALLET_1,
        base_symbol="USDG",
        decimals=6,
        amount_in_atoms=100_000_000,
        minimum_output_atoms=100_050_001,
        nonce=1,
    )
    slot = coord.acquire_execution_slot(req, one_shot=True)

    # Tampered slot with manipulated amount
    req_tampered = ReservationRequest(
        plan_id="plan_tamper",
        chain_id=CHAIN_ID,
        wallet_address=WALLET_1,
        base_symbol="USDG",
        decimals=6,
        amount_in_atoms=999_999_999,
        minimum_output_atoms=999_999_999,
        nonce=1,
    )
    tampered_slot = ReservedExecution(
        request=req_tampered,
        reserved_loss_usd=slot.reserved_loss_usd,
        generation=slot.generation,
        one_shot=slot.one_shot,
    )

    # 1. Tampered slot cannot sign
    with pytest.raises((SlotVerificationError, ExecutionLatched)):
        coord.record_signed(tampered_slot, TX_HASH_1)

    # Verify intent state has NOT mutated to SIGNED
    pending = ledger.pending_intent(CHAIN_ID, WALLET_1)
    assert pending is not None
    assert pending["state"] == "RESERVED"
    assert pending["hash"] is None

    # 2. Tampered slot cannot release
    with pytest.raises((SlotVerificationError, ExecutionLatched)):
        coord.release_execution_slot(tampered_slot)

    # Verify intent state has NOT been deleted
    assert coord.is_in_flight(WALLET_1)

    # 3. Tampered slot cannot mark unknown
    with pytest.raises((SlotVerificationError, ExecutionLatched)):
        coord.mark_unknown(tampered_slot)

    # 4. Tampered slot cannot reconcile
    with pytest.raises((SlotVerificationError, ExecutionLatched)):
        coord.reconcile(
            tampered_slot,
            tx_hash=TX_HASH_1,
            block_hash=BLOCK_HASH_1,
            net_profit_usd=Decimal("0.10"),
            actual_gas_usd=Decimal("0.05"),
            receipt_status=1,
        )

    # Ensure clean legitimate workflow still operates
    coord.record_signed(slot, TX_HASH_1)
    pending_signed = ledger.pending_intent(CHAIN_ID, WALLET_1)
    assert pending_signed is not None
    assert pending_signed["state"] == "SIGNED"


def test_review_boundary_zero_and_negative_base_price_rejected() -> None:
    """Boundary: base_price_usd <= 0 strictly rejected by ValuationEvidence."""
    with pytest.raises(ValuationError, match="base_price_usd must be strictly positive"):
        ValuationEvidence(
            base_price_usd=Decimal("0"),
            gas_usd=Decimal("0.05"),
            slippage_bps=50,
        )
    with pytest.raises(ValuationError, match="base_price_usd must be strictly positive"):
        ValuationEvidence(
            base_price_usd=Decimal("-1.5"),
            gas_usd=Decimal("0.05"),
            slippage_bps=50,
        )


def test_review_boundary_fraction_gas_ceil_rigid_floor(tmp_path: Path) -> None:
    """Boundary: exact Fraction ceil for recurring rational fractions and +1 atom floor."""
    db_path = tmp_path / "ledger.sqlite"
    ledger = make_ledger(db_path)
    val = ValuationEvidence(
        base_price_usd=Decimal("3.0"),
        gas_usd=Decimal("0.07"),
        slippage_bps=50,
    )
    coord = make_coordinator(ledger, val)

    amount_in = 30_000_000
    expected_gas_atoms = (70_000 + 3 - 1) // 3
    assert expected_gas_atoms == 23334

    req_bad = ReservationRequest(
        plan_id="frac_floor_bad",
        chain_id=CHAIN_ID,
        wallet_address=WALLET_1,
        base_symbol="USDG",
        decimals=6,
        amount_in_atoms=amount_in,
        minimum_output_atoms=amount_in + expected_gas_atoms,
        nonce=1,
    )
    with pytest.raises(FloorViolationError):
        coord.acquire_execution_slot(req_bad, one_shot=True)

    req_good = ReservationRequest(
        plan_id="frac_floor_good",
        chain_id=CHAIN_ID,
        wallet_address=WALLET_1,
        base_symbol="USDG",
        decimals=6,
        amount_in_atoms=amount_in,
        minimum_output_atoms=amount_in + expected_gas_atoms + 1,
        nonce=1,
    )
    slot = coord.acquire_execution_slot(req_good, one_shot=True)
    assert slot.plan_id == "frac_floor_good"


def test_review_boundary_one_shot_signature_requires_explicit_argument() -> None:
    """Boundary: acquire_execution_slot requires explicit one_shot parameter without default."""
    sig = inspect.signature(ExecutionCoordinator.acquire_execution_slot)
    param = sig.parameters.get("one_shot")
    assert param is not None
    assert param.default is inspect.Parameter.empty, "one_shot must not have a default value"
