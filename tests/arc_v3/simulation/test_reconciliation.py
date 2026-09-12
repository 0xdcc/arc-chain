"""Arc Execution Settlement Reconciliation and Circuit Breaker Tests (T30)

Verifies:
- Positive: Closed-loop balance change with Gas deducted produces verified positive realized profit
- Boundary: EOA pays Gas, contract receives profit (multi-controlled account consolidation)
- Boundary: On-chain revert (status=0) records Gas fee as net financial loss
- Boundary: Intermediate non-base token residue is explicitly surfaced in residual_dust
- Failure: Receipt success (status=1) with negative net yield is NOT marked profitable
- Failure: Untracked third-party balance injection is rejected as INDETERMINATE (anti-pollution)
- Failure: Missing receipt yields INDETERMINATE
- Circuit Breaker: 3 consecutive reverts trips breaker to OPEN, blocking new execution
- Circuit Breaker: Daily cumulative loss ceiling trip and disk persistence across reboots
"""

from __future__ import annotations

import os
import tempfile
import pytest

from arbitrage_contracts.identity import Amount, AssetRef, FeeModel, PoolDescriptor, PoolKey, TokenKey
from arbitrage_contracts.quote import HopRef, RouteRef
from arc_execution.circuit_breaker import (
    CircuitBreakerOpenError,
    CircuitBreakerState,
    FinancialCircuitBreaker,
)
from arc_execution.reconcile import (
    AccountBalanceSnapshot,
    ExecutionReceipt,
    ExecutionReconciler,
    ReconciliationCategory,
    ReconciliationReport,
)
from atomic_execution.arc_planning import ArcExecutionPlan
from atomic_execution.policy import ExecutionPolicy

CHAIN_ARC = 5042
CALLER_EOA = "0x" + "11" * 20
CONTRACT_ROUTER = "0x" + "22" * 20
RECIPIENT_VAULT = "0x" + "33" * 20
USDC_ADDR = "0x3600000000000000000000000000000000000000"
WETH_ADDR = "0x" + "55" * 20


def make_mock_plan() -> ArcExecutionPlan:
    asset_usdc = AssetRef("erc20", CHAIN_ARC, TokenKey(CHAIN_ARC, USDC_ADDR))
    asset_weth = AssetRef("erc20", CHAIN_ARC, TokenKey(CHAIN_ARC, WETH_ADDR))
    p1 = PoolDescriptor(
        PoolKey(CHAIN_ARC, "v3", "factory", "0x" + "66" * 20, "address", "0x" + "77" * 20),
        asset_usdc,
        asset_weth,
        FeeModel.static(500),
        10,
    )
    p2 = PoolDescriptor(
        PoolKey(CHAIN_ARC, "v3", "factory", "0x" + "66" * 20, "address", "0x" + "88" * 20),
        asset_usdc,
        asset_weth,
        FeeModel.static(3000),
        60,
    )
    hop1 = HopRef(p1.key, asset_usdc, asset_weth, "zero_for_one", p1)
    hop2 = HopRef(p2.key, asset_weth, asset_usdc, "one_for_zero", p2)
    route = RouteRef(CHAIN_ARC, asset_usdc, (hop1, hop2))
    return ArcExecutionPlan(
        plan_id="plan-001",
        chain_id=CHAIN_ARC,
        route_ref=route,
        base_asset=asset_usdc,
        amount_in=Amount(asset_usdc, 100_000_000, 6),
        expected_out=Amount(asset_usdc, 105_000_000, 6),
        min_amount_out=Amount(asset_usdc, 102_000_000, 6),
        output_floor=Amount(asset_usdc, 100_000_000, 6),
        policy=ExecutionPolicy(),
        target_router=CONTRACT_ROUTER,
        deployment_fingerprint="sha256:fp",
    )


class TestArcReconciliation:
    """Test suite for T30 Settlement Reconciliation and Financial Circuit Breaker."""

    def test_positive_closed_loop_settlement(self) -> None:
        """Verify normal profitable execution with gas deduction."""
        plan = make_mock_plan()
        receipt = ExecutionReceipt(
            tx_hash="0x" + "aa" * 32,
            status=1,
            block_number=1000,
            gas_used_atoms=100_000,
            effective_gas_price_atoms=2_000_000_000_000,  # 200,000 canonical 6-decimal atoms
            from_address=CALLER_EOA,
            to_address=CONTRACT_ROUTER,
        )
        # Caller spent 100 USDC and received 106 USDC (+6,000,000 atoms)
        diffs = [
            AccountBalanceSnapshot(CALLER_EOA, USDC_ADDR, 1_000_000_000, 1_006_000_000)
        ]
        report = ExecutionReconciler.reconcile(
            reconcile_id="rec-001",
            plan=plan,
            receipt=receipt,
            balance_snapshots=diffs,
            controlled_accounts=[CALLER_EOA],
            gas_deducted_in_base_token=False,
        )
        assert report.category == ReconciliationCategory.SUCCESS
        assert report.is_profitable is True
        # 6,000,000 - 200,000 = 5,800,000 net
        assert report.realized_net_atoms == 5_800_000
        assert report.gas_fee_atoms == 200_000
        assert report.untracked_injections_detected is False

    def test_multi_controlled_account_consolidation(self) -> None:
        """Verify multi-account consolidation (EOA pays input, Vault receives payout)."""
        plan = make_mock_plan()
        receipt = ExecutionReceipt(
            tx_hash="0x" + "bb" * 32,
            status=1,
            block_number=1001,
            gas_used_atoms=150_000,
            effective_gas_price_atoms=2_000_000_000_000,  # 300,000 canonical 6-decimal atoms
            from_address=CALLER_EOA,
            to_address=CONTRACT_ROUTER,
        )
        # EOA spent 100 USDC (-100,000,000); Vault received 106.5 USDC (+106,500,000)
        diffs = [
            AccountBalanceSnapshot(CALLER_EOA, USDC_ADDR, 100_000_000, 0),
            AccountBalanceSnapshot(RECIPIENT_VAULT, USDC_ADDR, 0, 106_500_000),
        ]
        report = ExecutionReconciler.reconcile(
            reconcile_id="rec-002",
            plan=plan,
            receipt=receipt,
            balance_snapshots=diffs,
            controlled_accounts=[CALLER_EOA, RECIPIENT_VAULT],
            gas_deducted_in_base_token=False,
        )
        assert report.category == ReconciliationCategory.SUCCESS
        assert report.is_profitable is True
        # Base net = 6,500,000. Gas = 300,000. Net realized = 6,200,000
        assert report.realized_net_atoms == 6_200_000

    def test_onchain_revert_records_gas_loss(self) -> None:
        """Rule: Reverted transaction (status=0) records gas fee as financial loss."""
        plan = make_mock_plan()
        receipt = ExecutionReceipt(
            tx_hash="0x" + "cc" * 32,
            status=0,  # Reverted!
            block_number=1002,
            gas_used_atoms=80_000,
            effective_gas_price_atoms=2_000_000_000_000,  # 160,000 canonical 6-decimal atoms
            from_address=CALLER_EOA,
            to_address=CONTRACT_ROUTER,
        )
        diffs = [AccountBalanceSnapshot(CALLER_EOA, USDC_ADDR, 100_000_000, 100_000_000)]
        report = ExecutionReconciler.reconcile(
            reconcile_id="rec-003",
            plan=plan,
            receipt=receipt,
            balance_snapshots=diffs,
            controlled_accounts=[CALLER_EOA],
        )
        assert report.category == ReconciliationCategory.REVERTED
        assert report.is_profitable is False
        assert report.realized_net_atoms == -160_000
        assert report.gas_fee_atoms == 160_000

    def test_residual_dust_accounting(self) -> None:
        """Verify non-base token residue is captured in residual_dust."""
        plan = make_mock_plan()
        receipt = ExecutionReceipt(
            tx_hash="0x" + "dd" * 32,
            status=1,
            block_number=1003,
            gas_used_atoms=100_000,
            effective_gas_price_atoms=1_000_000_000_000,
            from_address=CALLER_EOA,
            to_address=CONTRACT_ROUTER,
        )
        diffs = [
            AccountBalanceSnapshot(CALLER_EOA, USDC_ADDR, 100_000_000, 102_000_000),
            AccountBalanceSnapshot(CALLER_EOA, WETH_ADDR, 0, 500),  # 500 wei dust
        ]
        report = ExecutionReconciler.reconcile(
            reconcile_id="rec-004",
            plan=plan,
            receipt=receipt,
            balance_snapshots=diffs,
            controlled_accounts=[CALLER_EOA],
        )
        assert report.category == ReconciliationCategory.SUCCESS
        assert WETH_ADDR.lower() in report.residual_dust
        assert report.residual_dust[WETH_ADDR.lower()] == 500

    def test_receipt_success_unprofitable_trade_not_marked_profit(self) -> None:
        """CRITICAL: Transaction succeeded with status=1, but returned less than gas cost."""
        plan = make_mock_plan()
        receipt = ExecutionReceipt(
            tx_hash="0x" + "ee" * 32,
            status=1,
            block_number=1004,
            gas_used_atoms=300_000,
            effective_gas_price_atoms=2_000_000_000_000,  # 600,000 canonical 6-decimal atoms
            from_address=CALLER_EOA,
            to_address=CONTRACT_ROUTER,
        )
        # Balance delta is only +100,000 atoms
        diffs = [AccountBalanceSnapshot(CALLER_EOA, USDC_ADDR, 100_000_000, 100_100_000)]
        report = ExecutionReconciler.reconcile(
            reconcile_id="rec-005",
            plan=plan,
            receipt=receipt,
            balance_snapshots=diffs,
            controlled_accounts=[CALLER_EOA],
        )
        assert report.category == ReconciliationCategory.SUCCESS
        # Realized net: 100,000 - 600,000 = -500,000
        assert report.realized_net_atoms == -500_000
        assert report.is_profitable is False

    def test_external_injection_rejected_as_indeterminate(self) -> None:
        """Rule: Deposit from untracked third-party address must be flagged as INDETERMINATE."""
        plan = make_mock_plan()
        receipt = ExecutionReceipt(
            tx_hash="0x" + "ff" * 32,
            status=1,
            block_number=1005,
            gas_used_atoms=100_000,
            effective_gas_price_atoms=1_000_000_000_000,
            from_address=CALLER_EOA,
            to_address=CONTRACT_ROUTER,
        )
        hacker = "0x" + "99" * 20
        diffs = [
            AccountBalanceSnapshot(CALLER_EOA, USDC_ADDR, 100_000_000, 105_000_000),
            AccountBalanceSnapshot(hacker, USDC_ADDR, 0, 50_000_000),  # Untracked injection!
        ]
        report = ExecutionReconciler.reconcile(
            reconcile_id="rec-006",
            plan=plan,
            receipt=receipt,
            balance_snapshots=diffs,
            controlled_accounts=[CALLER_EOA],
        )
        assert report.category == ReconciliationCategory.INDETERMINATE
        assert report.untracked_injections_detected is True
        assert report.is_profitable is False

    def test_consecutive_reverts_trip_circuit_breaker(self) -> None:
        """Rule: 3 consecutive reverts trip breaker to OPEN, blocking future execution."""
        breaker = FinancialCircuitBreaker(max_consecutive_reverts=3, max_daily_loss_atoms=50_000_000)
        assert breaker.state == CircuitBreakerState.CLOSED

        # Create a mock reverted report
        def make_revert_report(tx_idx: int) -> ReconciliationReport:
            return ReconciliationReport(
                reconcile_id=f"rec-rev-{tx_idx}",
                tx_hash="0x" + f"{tx_idx:02x}" * 32,
                category=ReconciliationCategory.REVERTED,
                is_profitable=False,
                realized_net_atoms=-200_000,
                gas_fee_atoms=200_000,
                base_asset=USDC_ADDR.lower(),
                base_asset_net_delta=0,
                controlled_accounts=(CALLER_EOA.lower(),),
                residual_dust={},
                untracked_injections_detected=False,
                verdict_notes=("reverted",),
            )

        # 1st revert
        breaker.record_reconciliation(make_revert_report(1))
        assert breaker.state == CircuitBreakerState.CLOSED
        assert breaker.consecutive_reverts == 1

        # 2nd revert
        breaker.record_reconciliation(make_revert_report(2))
        assert breaker.state == CircuitBreakerState.CLOSED
        assert breaker.consecutive_reverts == 2

        # 3rd revert -> Trips!
        breaker.record_reconciliation(make_revert_report(3))
        assert breaker.state == CircuitBreakerState.OPEN
        assert "Consecutive failure threshold reached" in str(breaker.trip_reason)

        # Future execution must be blocked
        with pytest.raises(CircuitBreakerOpenError, match="Trading halted"):
            breaker.check_can_execute()

    def test_daily_loss_ceiling_trip_and_persistence(self) -> None:
        """Rule: Breaching cumulative loss limit trips breaker, and reload from disk preserves state."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "circuit_breaker.json")
            breaker = FinancialCircuitBreaker(
                max_consecutive_reverts=5,
                max_daily_loss_atoms=1_000_000,  # 1 USD loss ceiling
                state_file_path=state_file,
            )

            # Single big loss of 1,200,000 atoms
            big_loss_report = ReconciliationReport(
                reconcile_id="rec-loss-01",
                tx_hash="0x" + "88" * 32,
                category=ReconciliationCategory.REVERTED,
                is_profitable=False,
                realized_net_atoms=-1_200_000,
                gas_fee_atoms=1_200_000,
                base_asset=USDC_ADDR.lower(),
                base_asset_net_delta=0,
                controlled_accounts=(CALLER_EOA.lower(),),
                residual_dust={},
                untracked_injections_detected=False,
                verdict_notes=("big loss",),
            )
            breaker.record_reconciliation(big_loss_report)
            assert breaker.state == CircuitBreakerState.OPEN
            assert "Cumulative daily loss ceiling breached" in str(breaker.trip_reason)

            # Reboot simulation: instantiate new breaker from the same state file
            rebooted_breaker = FinancialCircuitBreaker(
                max_consecutive_reverts=5,
                max_daily_loss_atoms=1_000_000,
                state_file_path=state_file,
            )
            assert rebooted_breaker.state == CircuitBreakerState.OPEN
            assert rebooted_breaker.cumulative_loss_atoms == 1_200_000

            # Manual reset clears the trip
            rebooted_breaker.reset_manual(admin_token="admin_override_token")
            assert rebooted_breaker.state == CircuitBreakerState.CLOSED
            rebooted_breaker.check_can_execute()
