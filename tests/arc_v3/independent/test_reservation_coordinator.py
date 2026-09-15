"""Direct independent tests for research/reservation_coordinator.py.

Verifies:
- Real SQLite concurrency and persistent latching
- Explicit valuation evidence and strictly checked typed fields
- Hard notional limit <= 500 USD (Fraction math)
- Rigid principal + gas + 1 atom floor gate (zero token loss)
- No fallback for missing/wrong hashes or gas
- Fake slot spoofing detection across wallets, nonces, and intents
- Cancellation forbidden after signing or unknown broadcast
- AST zero-float compliance
"""

from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from research.reservation_coordinator import (
    AmountLimitExceededError,
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

WALLET_1 = "0x" + "11" * 20
WALLET_2 = "0x" + "22" * 20
TX_HASH_1 = "0x" + "aa" * 32
TX_HASH_2 = "0x" + "bb" * 32
BLOCK_HASH_1 = "0x" + "cc" * 32

ALLOWED_BASES = frozenset({"USDG", "WETH"})
LOSS_BUDGET = Decimal("10.0")
CHAIN_ID = 5042


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


def make_valid_request(
    plan_id: str = "plan_1",
    wallet: str = WALLET_1,
    base: str = "USDG",
    nonce: int = 1,
    amount_in: int = 100_000_000,  # 100 USDG (6 decimals)
    gas_usd: Decimal = Decimal("0.05"),  # 50,000 atoms gas at $1.00
    extra_profit_atoms: int = 1000,
) -> tuple[ReservationRequest, ValuationEvidence]:
    # gas_atoms = ceil(0.05 / 1.0 * 10**6) = 50,000
    # floor = amount_in + gas_atoms + 1
    # minimum_output_atoms = 100_000_000 + 50_000 + 1 + extra
    val = ValuationEvidence(
        base_price_usd=Decimal("1.0"),
        gas_usd=gas_usd,
        slippage_bps=50,
    )
    gas_atoms = 50_000
    min_out = amount_in + gas_atoms + 1 + extra_profit_atoms
    req = ReservationRequest(
        plan_id=plan_id,
        chain_id=CHAIN_ID,
        wallet_address=wallet,
        base_symbol=base,
        decimals=6,
        amount_in_atoms=amount_in,
        minimum_output_atoms=min_out,
        nonce=nonce,
    )
    return req, val


def test_legal_floor_reservation_cancel_and_re_reserve(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.sqlite"
    ledger = make_ledger(db_path)
    req, val = make_valid_request(plan_id="intent_floor_1", nonce=1)
    coord = make_coordinator(ledger, val)

    # 1. Acquire slot
    assert not coord.is_in_flight(WALLET_1, "USDG")
    slot = coord.acquire_execution_slot(req, one_shot=True)
    assert isinstance(slot, ReservedExecution)
    assert slot.plan_id == "intent_floor_1"
    assert slot.reserved_loss_usd == Decimal("0.05")
    assert coord.is_in_flight(WALLET_1, "USDG")
    assert coord.is_in_flight(WALLET_1)

    # 2. Cancel slot before signing
    coord.release_execution_slot(slot)
    assert not coord.is_in_flight(WALLET_1, "USDG")
    assert not coord.is_in_flight(WALLET_1)

    # 3. Re-reserve with next intent
    req2, val2 = make_valid_request(plan_id="intent_floor_2", nonce=2)
    slot2 = coord.acquire_execution_slot(req2, one_shot=True)
    assert slot2.plan_id == "intent_floor_2"
    assert coord.is_in_flight(WALLET_1, "USDG")


def test_two_instances_competing_only_one_wins(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.sqlite"
    ledger_a = make_ledger(db_path)
    ledger_b = make_ledger(db_path)

    req_a, val_a = make_valid_request(plan_id="race_a", nonce=10)
    req_b, val_b = make_valid_request(plan_id="race_b", nonce=10)

    coord_a = make_coordinator(ledger_a, val_a)
    coord_b = make_coordinator(ledger_b, val_b)

    results: list[tuple[str, Any]] = []

    def try_acquire(coord: ExecutionCoordinator, req: ReservationRequest, label: str) -> None:
        try:
            slot = coord.acquire_execution_slot(req, one_shot=True)
            results.append((label, slot))
        except Exception as exc:
            results.append((label, exc))

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(try_acquire, coord_a, req_a, "A")
        f2 = executor.submit(try_acquire, coord_b, req_b, "B")
        f1.result()
        f2.result()

    successes = [res for label, res in results if isinstance(res, ReservedExecution)]
    failures = [res for label, res in results if isinstance(res, ExecutionLatched)]

    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], ExecutionLatched)


def test_unknown_state_across_base_retains_lock(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.sqlite"
    ledger = make_ledger(db_path)
    req, val = make_valid_request(plan_id="intent_unk", base="USDG", nonce=1)
    coord = make_coordinator(ledger, val)

    slot = coord.acquire_execution_slot(req, one_shot=True)
    coord.record_signed(slot, TX_HASH_1)
    coord.mark_unknown(slot)

    # Reopening on a new coordinator instance across base
    restarted_ledger = make_ledger(db_path)
    coord_restarted = make_coordinator(restarted_ledger)

    # Wallet is locked across ALL bases
    assert coord_restarted.is_in_flight(WALLET_1, "USDG")
    assert coord_restarted.is_in_flight(WALLET_1, "WETH")

    # Attempting to acquire on WETH must fail with ExecutionLatched
    gas_atoms_weth = 50_000_000_000_000_000  # 0.05 * 10**18
    req_weth = ReservationRequest(
        plan_id="intent_weth",
        chain_id=CHAIN_ID,
        wallet_address=WALLET_1,
        base_symbol="WETH",
        decimals=18,
        amount_in_atoms=10**18,
        minimum_output_atoms=10**18 + gas_atoms_weth + 1 + 1000,
        nonce=2,
    )
    with pytest.raises(ExecutionLatched):
        coord_restarted.acquire_execution_slot(req_weth, one_shot=True)

    # Releasing UNKNOWN slot via cancel_before_signing must be rejected
    with pytest.raises(ExecutionLatched):
        coord_restarted.release_execution_slot(slot)


def test_invalid_inputs_rejected_and_no_intent_persisted(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.sqlite"
    ledger = make_ledger(db_path)
    coord = make_coordinator(ledger)

    # 1. Missing / invalid price in valuation evidence
    bad_bool: Any = True
    bad_float: Any = 100.5
    bad_valuations = [
        # Bool masquerading as Decimal
        lambda: ValuationEvidence(base_price_usd=bad_bool, gas_usd=Decimal("0.05"), slippage_bps=50),
        # Non-finite price
        lambda: ValuationEvidence(base_price_usd=Decimal("NaN"), gas_usd=Decimal("0.05"), slippage_bps=50),
        lambda: ValuationEvidence(base_price_usd=Decimal("Infinity"), gas_usd=Decimal("0.05"), slippage_bps=50),
        # Gas <= 0
        lambda: ValuationEvidence(base_price_usd=Decimal("1.0"), gas_usd=Decimal("0"), slippage_bps=50),
        lambda: ValuationEvidence(base_price_usd=Decimal("1.0"), gas_usd=Decimal("-0.01"), slippage_bps=50),
        # Non-finite gas
        lambda: ValuationEvidence(base_price_usd=Decimal("1.0"), gas_usd=Decimal("NaN"), slippage_bps=50),
        # Slippage bool or out of bounds
        lambda: ValuationEvidence(base_price_usd=Decimal("1.0"), gas_usd=Decimal("0.05"), slippage_bps=bad_bool),
        lambda: ValuationEvidence(base_price_usd=Decimal("1.0"), gas_usd=Decimal("0.05"), slippage_bps=0),
        lambda: ValuationEvidence(base_price_usd=Decimal("1.0"), gas_usd=Decimal("0.05"), slippage_bps=501),
    ]

    for val_fn in bad_valuations:
        with pytest.raises(ValuationError):
            val_fn()
        # Verify no intent persisted
        assert not coord.is_in_flight(WALLET_1)
        assert ledger.pending_intent(CHAIN_ID, WALLET_1) is None

    # 2. Bool and invalid types in ReservationRequest
    bad_requests = [
        # Bool masquerading as int in amount_in_atoms
        lambda: ReservationRequest("p", CHAIN_ID, WALLET_1, "USDG", 6, bad_bool, 100, 1),
        # Bool masquerading as int in minimum_output_atoms
        lambda: ReservationRequest("p", CHAIN_ID, WALLET_1, "USDG", 6, 100, bad_bool, 1),
        # Bool masquerading as int in decimals
        lambda: ReservationRequest("p", CHAIN_ID, WALLET_1, "USDG", bad_bool, 100, 200, 1),
        # Bool masquerading as int in nonce
        lambda: ReservationRequest("p", CHAIN_ID, WALLET_1, "USDG", 6, 100, 200, bad_bool),
        # Decimals out of bounds (> 36)
        lambda: ReservationRequest("p", CHAIN_ID, WALLET_1, "USDG", 37, 100, 200, 1),
        # Decimals negative
        lambda: ReservationRequest("p", CHAIN_ID, WALLET_1, "USDG", -1, 100, 200, 1),
        # Float passed as atom quantity
        lambda: ReservationRequest("p", CHAIN_ID, WALLET_1, "USDG", 6, bad_float, 200, 1),
        # Empty plan_id
        lambda: ReservationRequest("", CHAIN_ID, WALLET_1, "USDG", 6, 100, 200, 1),
        # Invalid wallet address length / chars
        lambda: ReservationRequest("p", CHAIN_ID, "0x1234", "USDG", 6, 100, 200, 1),
    ]

    for req_fn in bad_requests:
        with pytest.raises((CoordinatorError, ValueError)):
            req_fn()
        assert not coord.is_in_flight(WALLET_1)
        assert ledger.pending_intent(CHAIN_ID, WALLET_1) is None


def test_notional_boundary_500_usd(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.sqlite"
    ledger = make_ledger(db_path)

    # Base price = 2.0 USD, decimals = 6
    val = ValuationEvidence(
        base_price_usd=Decimal("2.0"),
        gas_usd=Decimal("0.10"),
        slippage_bps=50,
    )
    coord = make_coordinator(ledger, val)

    # 1. Exactly 500 USD: 250 tokens * $2.0 = $500.00 -> ALLOWED
    # gas_atoms = ceil(0.10 / 2.0 * 10**6) = 50,000
    # floor = 250_000_000 + 50_000 + 1 = 250_050_001
    amount_500 = 250_000_000
    req_500 = ReservationRequest(
        plan_id="boundary_500",
        chain_id=CHAIN_ID,
        wallet_address=WALLET_1,
        base_symbol="USDG",
        decimals=6,
        amount_in_atoms=amount_500,
        minimum_output_atoms=amount_500 + 50_000 + 10,
        nonce=1,
    )
    slot = coord.acquire_execution_slot(req_500, one_shot=True)
    assert slot.plan_id == "boundary_500"
    coord.release_execution_slot(slot)

    # 2. Exceeding 500 USD by just 1 atom: 250_000_001 atoms * $2.0 = $500.000002 -> REJECTED
    req_exceed = ReservationRequest(
        plan_id="boundary_exceed",
        chain_id=CHAIN_ID,
        wallet_address=WALLET_1,
        base_symbol="USDG",
        decimals=6,
        amount_in_atoms=250_000_001,
        minimum_output_atoms=250_000_001 + 50_000 + 10,
        nonce=2,
    )
    with pytest.raises(AmountLimitExceededError):
        coord.acquire_execution_slot(req_exceed, one_shot=True)

    assert not coord.is_in_flight(WALLET_1)
    assert ledger.pending_intent(CHAIN_ID, WALLET_1) is None


def test_floor_violation_rejected_no_loss_leak(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.sqlite"
    ledger = make_ledger(db_path)

    # price = 1.0 USD, gas = 0.10 USD (100,000 atoms at 6 decimals)
    val = ValuationEvidence(
        base_price_usd=Decimal("1.0"),
        gas_usd=Decimal("0.10"),
        slippage_bps=50,
    )
    coord = make_coordinator(ledger, val)

    amount_in = 10_000_000  # 10 USDG
    gas_atoms = 100_000
    # required_floor = 10_000_000 + 100_000 + 1 = 10_100_001

    # Case A: exactly missing the +1 atom profit
    req_missing_1 = ReservationRequest(
        plan_id="floor_a",
        chain_id=CHAIN_ID,
        wallet_address=WALLET_1,
        base_symbol="USDG",
        decimals=6,
        amount_in_atoms=amount_in,
        minimum_output_atoms=amount_in + gas_atoms,  # 10_100_000
        nonce=1,
    )
    with pytest.raises(FloorViolationError):
        coord.acquire_execution_slot(req_missing_1, one_shot=True)

    # Case B: break-even principal, zero gas coverage
    req_breakeven = ReservationRequest(
        plan_id="floor_b",
        chain_id=CHAIN_ID,
        wallet_address=WALLET_1,
        base_symbol="USDG",
        decimals=6,
        amount_in_atoms=amount_in,
        minimum_output_atoms=amount_in,  # 10_000_000
        nonce=1,
    )
    with pytest.raises(FloorViolationError):
        coord.acquire_execution_slot(req_breakeven, one_shot=True)

    # Case C: legacy negative floor (token loss: 9.5 USD out for 10.0 USD in)
    req_loss = ReservationRequest(
        plan_id="floor_loss",
        chain_id=CHAIN_ID,
        wallet_address=WALLET_1,
        base_symbol="USDG",
        decimals=6,
        amount_in_atoms=amount_in,
        minimum_output_atoms=9_500_000,
        nonce=1,
    )
    with pytest.raises(FloorViolationError):
        coord.acquire_execution_slot(req_loss, one_shot=True)

    assert not coord.is_in_flight(WALLET_1)
    assert ledger.pending_intent(CHAIN_ID, WALLET_1) is None


def test_missing_or_wrong_hash_or_gas_rejection_no_fallback(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.sqlite"
    ledger = make_ledger(db_path)
    req, val = make_valid_request()
    coord = make_coordinator(ledger, val)

    slot = coord.acquire_execution_slot(req, one_shot=True)
    coord.record_signed(slot, TX_HASH_1)

    # 1. Wrong tx_hash
    with pytest.raises(ExecutionLatched, match="Receipt does not match persisted intent"):
        coord.reconcile(
            slot,
            tx_hash=TX_HASH_2,
            block_hash=BLOCK_HASH_1,
            net_profit_usd=Decimal("0.5"),
            actual_gas_usd=Decimal("0.05"),
            receipt_status=1,
        )

    # 2. Empty tx_hash
    with pytest.raises(CoordinatorError):
        coord.reconcile(
            slot,
            tx_hash="",
            block_hash=BLOCK_HASH_1,
            net_profit_usd=Decimal("0.5"),
            actual_gas_usd=Decimal("0.05"),
            receipt_status=1,
        )

    # 3. Non-finite gas
    with pytest.raises(CoordinatorError):
        coord.reconcile(
            slot,
            tx_hash=TX_HASH_1,
            block_hash=BLOCK_HASH_1,
            net_profit_usd=Decimal("0.5"),
            actual_gas_usd=Decimal("NaN"),
            receipt_status=1,
        )

    # 4. Negative gas
    with pytest.raises(CoordinatorError):
        coord.reconcile(
            slot,
            tx_hash=TX_HASH_1,
            block_hash=BLOCK_HASH_1,
            net_profit_usd=Decimal("0.5"),
            actual_gas_usd=Decimal("-0.01"),
            receipt_status=1,
        )

    # 5. Invalid receipt status (not 0 or 1)
    with pytest.raises(CoordinatorError):
        coord.reconcile(
            slot,
            tx_hash=TX_HASH_1,
            block_hash=BLOCK_HASH_1,
            net_profit_usd=Decimal("0.5"),
            actual_gas_usd=Decimal("0.05"),
            receipt_status=2,
        )

    # 6. Valid reconcile succeeds with no fallback used
    coord.reconcile(
        slot,
        tx_hash=TX_HASH_1,
        block_hash=BLOCK_HASH_1,
        net_profit_usd=Decimal("0.5"),
        actual_gas_usd=Decimal("0.05"),
        receipt_status=1,
    )
    assert not coord.is_in_flight(WALLET_1)


def test_fake_slot_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.sqlite"
    ledger = make_ledger(db_path)
    req, val = make_valid_request(plan_id="real_plan", nonce=1)
    coord = make_coordinator(ledger, val)

    slot = coord.acquire_execution_slot(req, one_shot=True)

    # Construct fake slots attempting cross-slot manipulation
    # Fake 1: spoofed wallet address
    req_fake_wallet = ReservationRequest(
        plan_id="real_plan",
        chain_id=CHAIN_ID,
        wallet_address=WALLET_2,
        base_symbol="USDG",
        decimals=6,
        amount_in_atoms=req.amount_in_atoms,
        minimum_output_atoms=req.minimum_output_atoms,
        nonce=1,
    )
    fake_slot_1 = ReservedExecution(request=req_fake_wallet, reserved_loss_usd=slot.reserved_loss_usd)

    with pytest.raises(SlotVerificationError):
        coord.record_signed(fake_slot_1, TX_HASH_1)

    with pytest.raises(SlotVerificationError):
        coord.release_execution_slot(fake_slot_1)

    with pytest.raises(SlotVerificationError):
        coord.reconcile(
            fake_slot_1,
            tx_hash=TX_HASH_1,
            block_hash=BLOCK_HASH_1,
            net_profit_usd=Decimal("0.1"),
            actual_gas_usd=Decimal("0.05"),
            receipt_status=1,
        )

    # Fake 2: spoofed nonce
    req_fake_nonce = ReservationRequest(
        plan_id="real_plan",
        chain_id=CHAIN_ID,
        wallet_address=WALLET_1,
        base_symbol="USDG",
        decimals=6,
        amount_in_atoms=req.amount_in_atoms,
        minimum_output_atoms=req.minimum_output_atoms,
        nonce=99,
    )
    fake_slot_2 = ReservedExecution(request=req_fake_nonce, reserved_loss_usd=slot.reserved_loss_usd)
    with pytest.raises(SlotVerificationError):
        coord.record_signed(fake_slot_2, TX_HASH_1)

    # Fake 3: spoofed plan_id
    req_fake_id = ReservationRequest(
        plan_id="fake_plan",
        chain_id=CHAIN_ID,
        wallet_address=WALLET_1,
        base_symbol="USDG",
        decimals=6,
        amount_in_atoms=req.amount_in_atoms,
        minimum_output_atoms=req.minimum_output_atoms,
        nonce=1,
    )
    fake_slot_3 = ReservedExecution(request=req_fake_id, reserved_loss_usd=slot.reserved_loss_usd)
    with pytest.raises(SlotVerificationError):
        coord.record_signed(fake_slot_3, TX_HASH_1)

    # Fake 4: spoofed reserve amount
    fake_slot_4 = ReservedExecution(request=req, reserved_loss_usd=Decimal("9.99"))
    with pytest.raises(SlotVerificationError):
        coord.record_signed(fake_slot_4, TX_HASH_1)

    # Real slot remains fully operational
    coord.record_signed(slot, TX_HASH_1)
    coord.reconcile(
        slot,
        tx_hash=TX_HASH_1,
        block_hash=BLOCK_HASH_1,
        net_profit_usd=Decimal("0.10"),
        actual_gas_usd=Decimal("0.05"),
        receipt_status=1,
    )
    assert not coord.is_in_flight(WALLET_1)


def test_cancel_after_sign_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.sqlite"
    ledger = make_ledger(db_path)
    req, val = make_valid_request()
    coord = make_coordinator(ledger, val)

    slot = coord.acquire_execution_slot(req, one_shot=True)
    coord.record_signed(slot, TX_HASH_1)

    # Cannot cancel after signed
    with pytest.raises(ExecutionLatched, match="Cannot cancel a potentially broadcast intent"):
        coord.release_execution_slot(slot)

    # Still in flight
    assert coord.is_in_flight(WALLET_1)


def test_zero_float_ast_audit() -> None:
    """Rigid AST scan asserting zero float literals and zero float() calls."""
    import research.reservation_coordinator as module

    assert module.__file__ is not None
    source_path = Path(module.__file__).resolve()
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))

    float_constants: list[ast.Constant] = []
    float_calls: list[ast.Call] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, float):
            float_constants.append(node)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "float":
                float_calls.append(node)

    assert not float_constants, f"Detected float literals in reservation_coordinator: {float_constants}"
    assert not float_calls, f"Detected float() calls in reservation_coordinator: {float_calls}"
