"""Comprehensive test suite for M10 execution service and reconciliation.

Covers:
1. Simulation Revert Handling:
   - Returns SIMULATION_FAILED and retains exact revert reason & code on EVM reverts.
   - Distinguishes transport/provider RPC_ERROR from EVM SIMULATION_FAILED.
   - Forbids substituting Quoter queries for Universal Router simulation.
2. Execution Dry-Run Guardrails:
   - Rejects live on-chain broadcast (dry_run=False) with AuthorizationBlockedError.
   - Completes dry-run slot reservation, simulation, and pre-sign release.
3. On-Chain Reconciliation & Accounting:
   - Accurately accounts for gas loss on reverted transactions (status=0).
   - Accurately computes net profit (token delta - actual gas USD) on verified receipts (status=1).
   - Intercepts fake events (dry_run, hash mismatch, non-target token transfers, malformed logs).
   - Handles UNKNOWN receipts (None, timeout) and engages durable execution locks.
4. Static AST Security Audit:
   - Verifies service.py and reconciliation.py contain no plaintext credentials or .env reads.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from web3.exceptions import ContractLogicError

from arbitrage.domain.types import (
    ExecutionPlan,
    PoolIdentity,
    RouteHop,
    TokenAmount,
    TokenIdentity,
)
from execution.coordinator import (
    CrossBaseBlockedError,
    ExecutionCoordinator,
    FlightLockError,
    ReservedExecution,
)
from execution.funds import BASES, TRANSFER_TOPIC, hex_value
from execution.funds_ledger import FundsLedger
from execution.protocols import ROUTER, V3_QUOTER, V4_QUOTER
from execution.reconciliation import (
    ExecutionReconciler,
    ReconciliationResult,
    ReconciliationStatus,
)
from execution.service import (
    AuthorizationBlockedError,
    ExecutionResult,
    ExecutionService,
    SimulationResult,
    SimulationStatus,
    build_router_calldata,
    simulate_plan,
)

# Test constants
TEST_WALLET = "0x" + "11" * 20
TEST_ROUTER = ROUTER
TEST_POOL_V3 = "0x" + "33" * 20
TEST_TX_HASH = "0x" + "aa" * 32
TEST_BLOCK_HASH = "0x" + "bb" * 32

TOKEN_WETH = TokenIdentity(
    chain_id=4663,
    address=BASES["WETH"][0],
    decimals=18,
    symbol="WETH",
)

TOKEN_USDG = TokenIdentity(
    chain_id=4663,
    address=BASES["USDG"][0],
    decimals=6,
    symbol="USDG",
)


def make_test_plan(
    base: str = "WETH",
    amount_in: int = 10**16,
    min_amount_out: int = 10**16 - 1000,
    gas_usd: Decimal = Decimal("0.05"),
    plan_id: str | None = None,
    target_router: str = TEST_ROUTER,
) -> ExecutionPlan:
    """Build a well-formed 2-hop cyclic ExecutionPlan."""
    base_tok = TOKEN_WETH if base == "WETH" else TOKEN_USDG
    alt_tok = TOKEN_USDG if base == "WETH" else TOKEN_WETH

    pool1 = PoolIdentity(
        chain_id=4663,
        protocol="uniswap_v3",
        pool_id=TEST_POOL_V3,
        token0=base_tok.address,
        token1=alt_tok.address,
        fee_bps=5.0,
        tick_spacing=10,
    )
    pool2 = PoolIdentity(
        chain_id=4663,
        protocol="uniswap_v3",
        pool_id="0x" + "44" * 20,
        token0=alt_tok.address,
        token1=base_tok.address,
        fee_bps=5.0,
        tick_spacing=10,
    )

    hop1 = RouteHop(pool=pool1, token_in=base_tok, token_out=alt_tok)
    hop2 = RouteHop(pool=pool2, token_in=alt_tok, token_out=base_tok)

    return ExecutionPlan(
        plan_id=plan_id or f"plan_{base.lower()}_{amount_in}",
        candidate_id="cand_test_01",
        route_type="cyclic",
        base_token=base_tok,
        amount_in=TokenAmount(token=base_tok, atoms=amount_in),
        min_amount_out=TokenAmount(token=base_tok, atoms=min_amount_out),
        hops=(hop1, hop2),
        quoter_block=500000,
        deadline=1800000000,
        estimated_gas_usd=gas_usd,
        target_router=target_router,
    )


@pytest.fixture
def ledger(tmp_path: Path) -> FundsLedger:
    return FundsLedger(tmp_path / "test_m10_ledger.sqlite")


@pytest.fixture
def coordinator(ledger: FundsLedger) -> ExecutionCoordinator:
    return ExecutionCoordinator(ledger=ledger)


@pytest.fixture
def reconciler(coordinator: ExecutionCoordinator) -> ExecutionReconciler:
    return ExecutionReconciler(coordinator=coordinator, default_native_price_usd=Decimal("2500"))


@pytest.fixture
def service(coordinator: ExecutionCoordinator) -> ExecutionService:
    return ExecutionService(coordinator=coordinator, wallet_address=TEST_WALLET)


# ==============================================================================
# 1. Execution Service & Simulation Tests
# ==============================================================================


class TestExecutionServiceSimulation:
    """Test Universal Router simulation via eth_call and error differentiation."""

    def test_simulation_success(self, service: ExecutionService) -> None:
        """Successful eth_call simulation returns SUCCESS with valid calldata."""
        plan = make_test_plan()

        mock_rpc = MagicMock()
        mock_rpc.return_value = "0x0000000000000000000000000000000000000001"

        res = service.simulate_plan(plan, rpc=mock_rpc)
        assert res.is_success is True
        assert res.status == SimulationStatus.SUCCESS
        assert res.calldata is not None
        assert res.calldata.startswith("0x3593564c")  # Universal Router execute selector
        assert res.target_router is not None
        assert res.target_router.lower() == TEST_ROUTER.lower()

    def test_simulation_with_web3_object(self, service: ExecutionService) -> None:
        """Web3 object with eth.call method simulates successfully."""
        plan = make_test_plan()

        class DummyWeb3:
            def __init__(self) -> None:
                self.eth = MagicMock()
                self.eth.call.return_value = bytes.fromhex("00" * 31 + "01")

        dummy_w3 = DummyWeb3()
        res = service.simulate_plan(plan, rpc=dummy_w3)
        assert res.is_success is True
        assert res.status == SimulationStatus.SUCCESS
        assert res.raw_output == "0x" + "00" * 31 + "01"

    def test_simulation_revert_retains_reason(self, service: ExecutionService) -> None:
        """EVM revert correctly returns SIMULATION_FAILED and retains revert message."""
        plan = make_test_plan()

        # ContractLogicError from web3.py
        mock_rpc = MagicMock()
        mock_rpc.side_effect = ContractLogicError("execution reverted: STF")

        res = service.simulate_plan(plan, rpc=mock_rpc)
        assert res.is_success is False
        assert res.status == SimulationStatus.SIMULATION_FAILED
        assert res.is_revert is True
        assert "execution reverted: STF" in (res.revert_reason or "")
        assert res.error_code == 3

    def test_simulation_revert_via_json_rpc_error_dict(self, service: ExecutionService) -> None:
        """Structured JSON-RPC code 3 error is mapped to SIMULATION_FAILED."""
        plan = make_test_plan()

        mock_rpc = MagicMock()
        mock_rpc.return_value = {
            "error": {
                "code": 3,
                "message": "execution reverted: Transaction slippage exceeded",
                "data": "0x08c379a0...",
            }
        }

        res = service.simulate_plan(plan, rpc=mock_rpc)
        assert res.status == SimulationStatus.SIMULATION_FAILED
        assert res.is_revert is True
        assert "Transaction slippage exceeded" in (res.revert_reason or "")
        assert res.error_code == 3

    def test_simulation_rpc_error_distinction(self, service: ExecutionService) -> None:
        """Network/transport errors are strictly segregated into RPC_ERROR."""
        plan = make_test_plan()

        # TimeoutError is a network issue, NOT an EVM revert
        mock_rpc = MagicMock()
        mock_rpc.side_effect = TimeoutError("HTTP request to node timed out after 10s")

        res = service.simulate_plan(plan, rpc=mock_rpc)
        assert res.status == SimulationStatus.RPC_ERROR
        assert res.is_rpc_error is True
        assert res.is_revert is False
        assert res.revert_reason is None
        assert "timed out" in str(res.error_data)

    def test_quoter_simulation_rejection(self, service: ExecutionService) -> None:
        """Simulating against a Quoter address instead of Universal Router is strictly forbidden."""
        plan_v3_quoter = make_test_plan(target_router=V3_QUOTER)
        mock_rpc = MagicMock()

        with pytest.raises(ValueError, match="Target .* is a Quoter contract"):
            service.simulate_plan(plan_v3_quoter, rpc=mock_rpc)

        plan_v4_quoter = make_test_plan(target_router=V4_QUOTER)
        with pytest.raises(ValueError, match="Target .* is a Quoter contract"):
            service.simulate_plan(plan_v4_quoter, rpc=mock_rpc)

    def test_broadcast_dry_run_guardrail(self, service: ExecutionService) -> None:
        """Live broadcast (dry_run=False) is strictly blocked by AuthorizationBlockedError."""
        plan = make_test_plan()
        with pytest.raises(AuthorizationBlockedError, match="Live on-chain broadcast blocked"):
            service.execute_plan(plan, dry_run=False)

    def test_broadcast_dry_run_success(self, service: ExecutionService) -> None:
        """Dry-run execution acquires coordinator slot, simulates, and cancels slot cleanly."""
        plan = make_test_plan()
        mock_rpc = MagicMock(return_value="0x1")

        result = service.execute_plan(plan, dry_run=True, rpc=mock_rpc)
        assert result.success is True
        assert result.dry_run is True
        assert result.simulation.is_success is True
        # Slot must be cleanly released (not in flight)
        assert service.coordinator.is_in_flight(TEST_WALLET) is False


# ==============================================================================
# 2. Execution Reconciler Tests
# ==============================================================================


def _make_transfer_log(
    token_addr: str,
    from_addr: str,
    to_addr: str,
    amount: int,
    log_index: int = 0,
    tx_hash: str = TEST_TX_HASH,
    block_hash: str = TEST_BLOCK_HASH,
) -> dict[str, Any]:
    """Helper to construct a canonical ERC-20 Transfer event log."""
    topic_from = "0x" + "00" * 12 + hex_value(from_addr, 20)[2:]
    topic_to = "0x" + "00" * 12 + hex_value(to_addr, 20)[2:]
    data_hex = "0x" + amount.to_bytes(32, "big").hex()

    return {
        "address": token_addr,
        "topics": [TRANSFER_TOPIC, topic_from, topic_to],
        "data": data_hex,
        "logIndex": log_index,
        "transactionHash": tx_hash,
        "blockHash": block_hash,
        "removed": False,
    }


class TestExecutionReconciliation:
    """Test on-chain receipt auditing, gas accounting, and fraud defense."""

    def test_revert_status_zero_records_gas_loss(
        self, reconciler: ExecutionReconciler, coordinator: ExecutionCoordinator
    ) -> None:
        """Status=0 revert transactions accurately record gas loss and update budget."""
        plan = make_test_plan()
        slot = coordinator.acquire_execution_slot(plan, TEST_WALLET)

        reverted_receipt = {
            "transactionHash": TEST_TX_HASH,
            "blockHash": TEST_BLOCK_HASH,
            "blockNumber": 123456,
            "status": 0,
            "gasUsed": 100_000,
            "effectiveGasPrice": 20 * 10**9,  # 20 Gwei
            "logs": [],
        }

        verdict = reconciler.reconcile(
            receipt=reverted_receipt,
            expected_tx_hash=TEST_TX_HASH,
            wallet=TEST_WALLET,
            coordinator=coordinator,
            reserved=slot,
            native_price_usd=Decimal("2500"),
        )

        assert verdict.status == ReconciliationStatus.REVERTED
        assert verdict.is_reverted is True
        assert verdict.token_delta == 0

        # Gas native: 100,000 * 20e9 / 1e18 = 0.002 ETH
        # Gas USD: 0.002 * 2500 = $5.00 USD
        expected_gas_usd = Decimal("5.00")
        assert verdict.actual_gas_usd == expected_gas_usd
        assert verdict.net_profit_usd == -expected_gas_usd

        # Coordinator ledger must now reflect the $5.00 spent budget
        status = coordinator.ledger.status(4663, TEST_WALLET, "WETH")
        assert status["spent_usd"] == expected_gas_usd

    def test_verified_success_accounting(
        self, reconciler: ExecutionReconciler, coordinator: ExecutionCoordinator
    ) -> None:
        """Status=1 receipt with genuine transfer logs produces VERIFIED and positive net profit."""
        plan = make_test_plan()
        slot = coordinator.acquire_execution_slot(plan, TEST_WALLET)

        token_weth = BASES["WETH"][0]
        amount_in = 10**16  # 0.01 ETH
        amount_out = int(10**16 + 5 * 10**14)  # 0.0105 ETH (+0.0005 ETH profit)

        logs = [
            _make_transfer_log(token_weth, TEST_WALLET, TEST_ROUTER, amount_in, log_index=0),
            _make_transfer_log(token_weth, TEST_ROUTER, TEST_WALLET, amount_out, log_index=1),
        ]

        success_receipt = {
            "transactionHash": TEST_TX_HASH,
            "blockHash": TEST_BLOCK_HASH,
            "blockNumber": 123456,
            "status": 1,
            "gasUsed": 80_000,
            "effectiveGasPrice": 10 * 10**9,  # 10 Gwei
            "logs": logs,
        }

        verdict = reconciler.reconcile(
            receipt=success_receipt,
            expected_tx_hash=TEST_TX_HASH,
            wallet=TEST_WALLET,
            base_symbol="WETH",
            counterparties={TEST_ROUTER},
            coordinator=coordinator,
            reserved=slot,
            native_price_usd=Decimal("2500"),
        )

        assert verdict.status == ReconciliationStatus.VERIFIED
        assert verdict.is_verified is True
        assert verdict.token_delta == 5 * 10**14  # 0.0005 ETH

        # Token delta USD: 0.0005 * 2500 = $1.25 USD
        # Gas USD: 80,000 * 10e9 / 1e18 = 0.0008 ETH * 2500 = $2.00 USD
        # Net profit: 1.25 - 2.00 = -$0.75 USD
        assert verdict.token_delta_usd == Decimal("1.25")
        assert verdict.actual_gas_usd == Decimal("2.00")
        assert verdict.net_profit_usd == Decimal("-0.75")

    def test_fake_event_dry_run_flag_rejected(self, reconciler: ExecutionReconciler) -> None:
        """Event marked with dry_run or test flag is rejected with FAKE_EVENT_REJECTED."""
        fake_event = {
            "dry_run": True,
            "tx_hash": TEST_TX_HASH,
            "base_symbol": "WETH",
        }
        receipt = {"transactionHash": TEST_TX_HASH, "status": 1, "logs": []}

        verdict = reconciler.reconcile(receipt=receipt, event=fake_event)
        assert verdict.status == ReconciliationStatus.FAKE_EVENT_REJECTED
        assert verdict.is_rejected is True
        assert "cannot be reconciled as live" in verdict.reason

    def test_fake_event_hash_mismatch_rejected(self, reconciler: ExecutionReconciler) -> None:
        """Receipt transactionHash differing from expected hash is rejected."""
        wrong_hash = "0x" + "99" * 32
        receipt = {"transactionHash": wrong_hash, "status": 1, "logs": []}

        verdict = reconciler.reconcile(
            receipt=receipt, expected_tx_hash=TEST_TX_HASH, wallet=TEST_WALLET
        )
        assert verdict.status == ReconciliationStatus.FAKE_EVENT_REJECTED
        assert "does not match expected" in verdict.reason

    def test_reverted_with_unexpected_logs_rejected(self, reconciler: ExecutionReconciler) -> None:
        """Reverted receipt claiming status=0 while containing event logs is rejected."""
        anomalous_receipt = {
            "transactionHash": TEST_TX_HASH,
            "status": 0,
            "logs": [{"address": BASES["WETH"][0], "topics": []}],
        }
        verdict = reconciler.reconcile(receipt=anomalous_receipt, expected_tx_hash=TEST_TX_HASH)
        assert verdict.status == ReconciliationStatus.FAKE_EVENT_REJECTED
        assert "contains anomalous event logs" in verdict.reason

    def test_fake_transfer_log_malformed_padding_rejected(
        self, reconciler: ExecutionReconciler
    ) -> None:
        """Transfer log with non-zero high bytes in indexed address topic is rejected."""
        bad_topic = "0x" + "ff" * 12 + hex_value(TEST_WALLET, 20)[2:]
        corrupted_log = {
            "address": BASES["WETH"][0],
            "topics": [TRANSFER_TOPIC, bad_topic, bad_topic],
            "data": "0x01",
            "logIndex": 0,
            "transactionHash": TEST_TX_HASH,
            "removed": False,
        }
        receipt = {
            "transactionHash": TEST_TX_HASH,
            "status": 1,
            "logs": [corrupted_log],
        }

        verdict = reconciler.reconcile(
            receipt=receipt, expected_tx_hash=TEST_TX_HASH, wallet=TEST_WALLET
        )
        assert verdict.status == ReconciliationStatus.FAKE_EVENT_REJECTED
        assert "padding" in verdict.reason

    def test_fake_transfer_log_duplicate_index_rejected(
        self, reconciler: ExecutionReconciler
    ) -> None:
        """Duplicate logIndex within the same receipt is strictly rejected."""
        token_weth = BASES["WETH"][0]
        log1 = _make_transfer_log(token_weth, TEST_WALLET, TEST_ROUTER, 100, log_index=5)
        log2 = _make_transfer_log(token_weth, TEST_ROUTER, TEST_WALLET, 110, log_index=5)

        receipt = {
            "transactionHash": TEST_TX_HASH,
            "status": 1,
            "logs": [log1, log2],
        }

        verdict = reconciler.reconcile(
            receipt=receipt, expected_tx_hash=TEST_TX_HASH, wallet=TEST_WALLET
        )
        assert verdict.status == ReconciliationStatus.FAKE_EVENT_REJECTED
        assert "Duplicate logIndex" in verdict.reason

    def test_non_target_token_transfer_intercepted(self, reconciler: ExecutionReconciler) -> None:
        """Transfer log on foreign non-target token involving wallet is intercepted."""
        fake_token = "0x" + "99" * 20
        foreign_log = _make_transfer_log(
            token_addr=fake_token,
            from_addr=TEST_WALLET,
            to_addr=TEST_ROUTER,
            amount=1000,
            log_index=0,
        )
        receipt = {
            "transactionHash": TEST_TX_HASH,
            "status": 1,
            "logs": [foreign_log],
        }

        verdict = reconciler.reconcile(
            receipt=receipt,
            expected_tx_hash=TEST_TX_HASH,
            wallet=TEST_WALLET,
            base_symbol="WETH",
        )
        assert verdict.status == ReconciliationStatus.FAKE_EVENT_REJECTED
        assert "Non-target token transfer involving wallet intercepted" in verdict.reason

    def test_unauthorized_counterparty_debit_intercepted(
        self, reconciler: ExecutionReconciler
    ) -> None:
        """Token debit to an unauthorized recipient address is rejected."""
        token_weth = BASES["WETH"][0]
        unauthorized_recipient = "0x" + "88" * 20

        log = _make_transfer_log(
            token_weth, TEST_WALLET, unauthorized_recipient, 1000, log_index=0
        )
        receipt = {
            "transactionHash": TEST_TX_HASH,
            "status": 1,
            "logs": [log],
        }

        verdict = reconciler.reconcile(
            receipt=receipt,
            expected_tx_hash=TEST_TX_HASH,
            wallet=TEST_WALLET,
            counterparties={TEST_ROUTER},  # unauthorized_recipient is not in allowed set
        )
        assert verdict.status == ReconciliationStatus.FAKE_EVENT_REJECTED
        assert "Unattributed token debit" in verdict.reason


# ==============================================================================
# 3. Unknown Receipt & Flight Lock Tests
# ==============================================================================


class TestUnknownReceiptAndLocking:
    """Test handling of missing or timeout receipts and cross-base lock hanging."""

    def test_unknown_receipt_none_triggers_latch(
        self, reconciler: ExecutionReconciler, coordinator: ExecutionCoordinator
    ) -> None:
        """Missing receipt (None) returns UNKNOWN and durable latch blocks new orders."""
        plan_weth = make_test_plan(base="WETH")
        slot = coordinator.acquire_execution_slot(plan_weth, TEST_WALLET)

        verdict = reconciler.reconcile(
            receipt=None,
            expected_tx_hash=TEST_TX_HASH,
            coordinator=coordinator,
            reserved=slot,
        )

        assert verdict.status == ReconciliationStatus.UNKNOWN
        assert verdict.is_unknown is True

        # Now durable ledger is in UNKNOWN state: subsequent order on any base is strictly blocked
        plan_usdg = make_test_plan(base="USDG")
        with pytest.raises(CrossBaseBlockedError, match="Cross-base reconciliation blocked"):
            coordinator.acquire_execution_slot(plan_usdg, TEST_WALLET)

    def test_timeout_returns_unknown_and_latches(
        self, reconciler: ExecutionReconciler, coordinator: ExecutionCoordinator
    ) -> None:
        """Timeout flag returns UNKNOWN and blocks subsequent orders."""
        plan = make_test_plan()
        slot = coordinator.acquire_execution_slot(plan, TEST_WALLET)

        verdict = reconciler.reconcile(
            receipt=None,
            timeout=True,
            expected_tx_hash=TEST_TX_HASH,
            coordinator=coordinator,
            reserved=slot,
        )

        assert verdict.status == ReconciliationStatus.UNKNOWN
        assert "timed out" in verdict.reason

        with pytest.raises(CrossBaseBlockedError, match="unresolved intent"):
            coordinator.acquire_execution_slot(plan, TEST_WALLET)

    def test_hold_flight_lock_keeps_in_flight(
        self, reconciler: ExecutionReconciler, coordinator: ExecutionCoordinator
    ) -> None:
        """When hold_flight_lock_on_unknown=True, in-memory flight lock is preserved."""
        plan = make_test_plan()
        slot = coordinator.acquire_execution_slot(plan, TEST_WALLET)

        assert coordinator.is_in_flight(TEST_WALLET) is True

        verdict = reconciler.reconcile(
            receipt=None,
            timeout=True,
            expected_tx_hash=TEST_TX_HASH,
            coordinator=coordinator,
            reserved=slot,
            hold_flight_lock_on_unknown=True,
        )

        assert verdict.status == ReconciliationStatus.UNKNOWN
        # Flight lock is still held in-memory
        assert coordinator.is_in_flight(TEST_WALLET) is True
        with pytest.raises(RuntimeError, match="Flight lock: transaction in-flight"):
            coordinator.acquire_execution_slot(plan, TEST_WALLET)


# ==============================================================================
# 4. Static AST Security Audit Tests
# ==============================================================================


class TestStaticASTAudit:
    """Audit service.py and reconciliation.py for zero-credential isolation."""

    FORBIDDEN_CALLS: set[str] = {"eval", "exec", "compile", "__import__"}
    FORBIDDEN_KEYWORDS: list[str] = [".env", "PRIVATE_KEY", "private_key", "keystore"]
    FORBIDDEN_MODULES: set[str] = {"dotenv"}

    @pytest.mark.parametrize("filename", ["service.py", "reconciliation.py"])
    def test_ast_security_audit(self, filename: str) -> None:
        """Verify file contains no unauthorized modules, calls, or credential keywords."""
        target_file = Path(__file__).resolve().parent.parent / "execution" / filename
        assert target_file.exists(), f"Target file {target_file} not found"

        source = target_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(target_file))

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root_mod = alias.name.split(".")[0]
                    assert root_mod not in self.FORBIDDEN_MODULES, (
                        f"Forbidden import '{alias.name}' in {filename}"
                    )
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                root_mod = mod.split(".")[0]
                assert root_mod not in self.FORBIDDEN_MODULES, (
                    f"Forbidden import-from '{mod}' in {filename}"
                )
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    assert node.func.id not in self.FORBIDDEN_CALLS, (
                        f"Forbidden call '{node.func.id}()' in {filename}"
                    )

        for kw in self.FORBIDDEN_KEYWORDS:
            assert kw not in source, f"Forbidden keyword '{kw}' found in {filename}"
