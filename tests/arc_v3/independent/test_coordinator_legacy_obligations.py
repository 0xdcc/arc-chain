"""Tests for legacy coordinator obligations ported to Arc v3.

Ported from legacy tests/test_funds_coordinator.py (COORDINATOR-COVERAGE/v1):
1. test_unknown_blocks_same_base_as_well:
   Acquire slot with one_shot=True -> record_signed -> mark_unknown ->
   attempting reservation on same base with fresh plan_id/nonce raises ExecutionLatched;
   pending intent, hash, and latch state remain unchanged.
2. test_min_amount_out_zero_rejected:
   ReservationRequest with minimum_output_atoms=0 is rejected with ValueError
   (specifically CoordinatorError, which subclasses LedgerError and ValueError).
3. test_unsupported_base_symbol_rejected:
   ReservationRequest with unsupported base ('DOGE') is rejected by acquire_execution_slot
   with CoordinatorError, and no intent is persisted in the ledger.
4. TestStaticASTAudit:
   Static AST audit of research/reservation_coordinator.py enforcing forbidden modules,
   forbidden Name calls, and sensitive credential keywords.

These 4 incremental tests address the 4 undisputed gap obligations from
/tmp/arc-funds-coordinator-obligations/matrix-corrected.json.
Legacy specification conflicts (negative output floors, wallet unpausing,
and False in-flight assertions during UNKNOWN) remain preserved as-is.
"""

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import pytest

from research.reservation_coordinator import (
    CoordinatorError,
    ReservationRequest,
)
from research.reservation_ledger import ExecutionLatched
from tests.arc_v3.independent.test_reservation_coordinator import (
    CHAIN_ID,
    TX_HASH_1,
    WALLET_1,
    make_coordinator,
    make_ledger,
    make_valid_request,
)


def test_unknown_blocks_same_base_as_well(tmp_path: Path) -> None:
    """An UNKNOWN intent on a base symbol strictly blocks subsequent trades on the same base.

    Ported from legacy tests/test_funds_coordinator.py:215-227.
    Flow:
    1. acquire_execution_slot with one_shot=True
    2. record_signed
    3. mark_unknown
    4. Subsequent reservation on the same base (fresh plan_id and nonce)
       must raise ExecutionLatched.
    5. Original pending intent, hash, and state remain unchanged.
    """
    db_path = tmp_path / "ledger.sqlite"
    ledger = make_ledger(db_path)
    req1, val1 = make_valid_request(plan_id="usdg_unk_01", base="USDG", nonce=1)
    coord = make_coordinator(ledger, val1)

    slot = coord.acquire_execution_slot(req1, one_shot=True)
    coord.record_signed(slot, TX_HASH_1)
    coord.mark_unknown(slot)

    # Capture pending state before attempting second acquisition
    pending_before = ledger.pending_intent(req1.chain_id, WALLET_1)
    assert pending_before is not None
    assert pending_before["id"] == req1.plan_id
    assert pending_before["hash"] == TX_HASH_1
    assert pending_before["state"] == "UNKNOWN"
    assert pending_before["base"] == "USDG"
    assert pending_before["nonce"] == req1.nonce

    # Verify coordinator reports in-flight
    assert coord.is_in_flight(WALLET_1, "USDG") is True
    assert coord.is_in_flight(WALLET_1) is True

    # Subsequent reservation on the same base with fresh plan_id and incremented nonce
    req2, val2 = make_valid_request(plan_id="usdg_unk_02", base="USDG", nonce=2)
    with pytest.raises(ExecutionLatched, match=r"Wallet/base pending or paused"):
        coord.acquire_execution_slot(req2, one_shot=True)

    # Verify that original pending intent, hash, and state are completely unchanged
    pending_after = ledger.pending_intent(req1.chain_id, WALLET_1)
    assert pending_after is not None
    assert pending_after["id"] == pending_before["id"] == req1.plan_id
    assert pending_after["hash"] == pending_before["hash"] == TX_HASH_1
    assert pending_after["state"] == pending_before["state"] == "UNKNOWN"
    assert pending_after["base"] == pending_before["base"] == "USDG"
    assert pending_after["nonce"] == pending_before["nonce"] == req1.nonce
    assert pending_after == pending_before

    # Verify latching and state preservation persist across coordinator re-instantiation
    restarted_ledger = make_ledger(db_path)
    coord_restarted = make_coordinator(restarted_ledger)
    assert coord_restarted.is_in_flight(WALLET_1, "USDG") is True
    with pytest.raises(ExecutionLatched, match=r"Wallet/base pending or paused"):
        coord_restarted.acquire_execution_slot(req2, one_shot=True)

    pending_restarted = restarted_ledger.pending_intent(req1.chain_id, WALLET_1)
    assert pending_restarted == pending_before


def test_min_amount_out_zero_rejected() -> None:
    """ReservationRequest with minimum_output_atoms=0 is strictly rejected with ValueError.

    Ported from legacy tests/test_funds_coordinator.py:448-463.
    Verifies that minimum_output_atoms <= 0 is forbidden to prevent MEV sandwich attacks
    and floor violations. Preserves the legacy ValueError assertion intent.
    CoordinatorError is a subclass of LedgerError, which is a subclass of ValueError.
    """
    # 1. Direct construction rejection
    with pytest.raises(ValueError, match=r"minimum_output_atoms must be strictly positive") as exc_info:
        ReservationRequest(
            plan_id="zero_min_out_plan",
            chain_id=CHAIN_ID,
            wallet_address=WALLET_1,
            base_symbol="USDG",
            decimals=6,
            amount_in_atoms=100_000_000,
            minimum_output_atoms=0,
            nonce=1,
        )
    assert isinstance(exc_info.value, CoordinatorError)

    # 2. Rejection via dataclasses.replace on a valid request
    valid_req, _ = make_valid_request(plan_id="valid_plan", base="USDG", nonce=1)
    with pytest.raises(ValueError, match=r"minimum_output_atoms must be strictly positive") as exc_info2:
        replace(valid_req, minimum_output_atoms=0)
    assert isinstance(exc_info2.value, CoordinatorError)

    # 3. Rejection of negative values via dataclasses.replace
    with pytest.raises(ValueError, match=r"minimum_output_atoms must be strictly positive") as exc_info3:
        replace(valid_req, minimum_output_atoms=-1)
    assert isinstance(exc_info3.value, CoordinatorError)


def test_unsupported_base_symbol_rejected(tmp_path: Path) -> None:
    """Reservation request with an unsupported base symbol (e.g. 'DOGE') is rejected by coordinator.

    Ported from legacy tests/test_funds_coordinator.py:465-479.
    Verifies that tokens outside ledger allowed bases (WETH, USDG) raise CoordinatorError
    during acquire_execution_slot and no intent is persisted in the ledger.
    """
    db_path = tmp_path / "ledger.sqlite"
    ledger = make_ledger(db_path)
    coord = make_coordinator(ledger)

    # DOGE is a valid string/format at ReservationRequest level, but unsupported by ledger
    doge_req = ReservationRequest(
        plan_id="doge_plan_01",
        chain_id=CHAIN_ID,
        wallet_address=WALLET_1,
        base_symbol="DOGE",
        decimals=8,
        amount_in_atoms=1000,
        # Synthetic $0.05 gas at $1/base and 8 decimals; isolate only base admission.
        minimum_output_atoms=1000 + 5_000_000 + 1,
        nonce=1,
    )

    with pytest.raises(CoordinatorError, match=r"Base symbol 'DOGE' not in ledger allowed bases"):
        coord.acquire_execution_slot(doge_req, one_shot=True)

    # Ensure no intent was persisted in ledger and wallet is not in-flight
    assert ledger.pending_intent(doge_req.chain_id, WALLET_1) is None
    assert coord.is_in_flight(WALLET_1) is False
    assert coord.is_in_flight(WALLET_1, "USDG") is False

    # Also verify that query for unsupported base in is_in_flight raises CoordinatorError
    with pytest.raises(CoordinatorError, match=r"Unsupported base: DOGE"):
        coord.is_in_flight(WALLET_1, "DOGE")

    # Check database directly: zero rows in intents table
    with ledger._transaction() as db:
        intent_count = db.execute("SELECT count(*) FROM intents").fetchone()[0]
        assert intent_count == 0


class TestStaticASTAudit:
    """Security audit of reservation_coordinator.py AST for hardcoded keys, forbidden modules, and calls.

    Ported from legacy tests/test_funds_coordinator.py:485-547.
    Preserves the original AST Name call, module import, and sensitive keyword inspection obligations.
    NOTE: Verifies static AST Name calls and import statements; does not claim to be a full
    call-chain or dynamic reachability security proof.
    """

    FORBIDDEN_MODULES: set[str] = {
        "subprocess",
        "socket",
        "requests",
        "urllib",
        "http",
        "pickle",
        "os.system",
    }

    FORBIDDEN_CALLS: set[str] = {
        "eval",
        "exec",
        "compile",
        "__import__",
    }

    FORBIDDEN_KEYWORDS: list[str] = [
        ".env",
        "PRIVATE_KEY",
        "private_key",
        "keystore",
    ]

    def test_ast_security_audit(self) -> None:
        """Verify research/reservation_coordinator.py contains no unauthorized calls or plaintext credentials."""
        target_file = Path(__file__).resolve().parents[3] / "research" / "reservation_coordinator.py"
        assert target_file.exists(), f"{target_file} not found"

        source = target_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(target_file))

        for node in ast.walk(tree):
            # 1. Check import x
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root_mod = alias.name.split(".")[0]
                    assert root_mod not in self.FORBIDDEN_MODULES, (
                        f"Forbidden import '{alias.name}' found in reservation_coordinator.py"
                    )

            # 2. Check from x import y
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                root_mod = mod.split(".")[0]
                assert root_mod not in self.FORBIDDEN_MODULES, (
                    f"Forbidden import-from '{mod}' found in reservation_coordinator.py"
                )

            # 3. Check forbidden function calls
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    assert node.func.id not in self.FORBIDDEN_CALLS, (
                        f"Forbidden call '{node.func.id}()' found in reservation_coordinator.py"
                    )

        # 4. Check sensitive keywords
        for kw in self.FORBIDDEN_KEYWORDS:
            assert kw not in source, f"Sensitive keyword '{kw}' found in reservation_coordinator.py"
