"""Test suite for execution/coordinator.py (ExecutionCoordinator).

Covers:
1. Single Flight Lock (单飞锁): Concurrent requests strictly blocked.
2. Cross-Base Reconciliation Blocking (跨本位未知对账阻断): WETH UNKNOWN blocks subsequent USDG.
3. Budget Reservation and Exhaustion (预算预留与耗尽): Rejects new orders when cumulative loss reaches limit.
4. Probe 1U Circuit Breaker (探路 1U 限制): Probe loss >= 1.0 USD triggers circuit breaker.
5. Ledger Restart Recovery (账本重启恢复): SQLite state persists spent budget across restarts.
6. Gate 5 Safety Validations (代币与金额校验): > $500 hard cap and min_amount_out == 0 prohibitions.
7. Static AST Audit: No plaintext keys, no unauthorized dynamic calls or dangerous modules.
"""

from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from arbitrage.domain.types import (
    CandidateRoute,
    ExecutionPlan,
    PoolIdentity,
    RouteHop,
    TokenAmount,
    TokenIdentity,
)
from core.wallet_guard import ExcessiveAmountError
from execution.coordinator import (
    CrossBaseBlockedError,
    ExecutionCoordinator,
    FlightLockError,
    ProbeCircuitBreakerError,
    ReservedExecution,
)
from execution.funds import BASES, FundsError
from execution.funds_ledger import ExecutionLatched, FundsLedger

# Fixed canonical addresses
TEST_WALLET = "0x" + "11" * 20
ROUTER_ADDR = "0x" + "22" * 20
POOL_ADDR = "0x" + "33" * 20
POOL_V4_ADDR = "0x" + "44" * 32
TX_HASH = "0x" + "55" * 32
BLOCK_HASH = "0x" + "66" * 32

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
    base: str = "USDG",
    amount_in: int = 10_000_000,
    min_amount_out: int = 9_950_000,
    gas_usd: Decimal = Decimal("0.05"),
    plan_id: str | None = None,
) -> ExecutionPlan:
    """Construct a well-formed 2-hop cyclic ExecutionPlan."""
    base_tok = TOKEN_USDG if base == "USDG" else TOKEN_WETH
    alt_tok = TOKEN_WETH if base == "USDG" else TOKEN_USDG

    pool1 = PoolIdentity(
        chain_id=4663,
        protocol="uniswap_v3",
        pool_id=POOL_ADDR,
        token0=base_tok.address,
        token1=alt_tok.address,
        fee_bps=5.0,
        tick_spacing=10,
    )
    pool2 = PoolIdentity(
        chain_id=4663,
        protocol="uniswap_v4",
        pool_id=POOL_V4_ADDR,
        token0=alt_tok.address,
        token1=base_tok.address,
        fee_bps=5.0,
        tick_spacing=10,
        hooks="0x0000000000000000000000000000000000000000",
    )

    hop1 = RouteHop(pool=pool1, token_in=base_tok, token_out=alt_tok)
    hop2 = RouteHop(pool=pool2, token_in=alt_tok, token_out=base_tok)

    return ExecutionPlan(
        plan_id=plan_id or f"plan_{base.lower()}_{amount_in}",
        candidate_id="cand_test_01",
        route_type="two_hop_spread",
        base_token=base_tok,
        amount_in=TokenAmount(token=base_tok, atoms=amount_in),
        min_amount_out=TokenAmount(token=base_tok, atoms=min_amount_out),
        hops=(hop1, hop2),
        quoter_block=100000,
        deadline=int(1700000000 + 120),
        estimated_gas_usd=gas_usd,
        target_router=ROUTER_ADDR,
    )


@pytest.fixture
def ledger(tmp_path: Path) -> FundsLedger:
    return FundsLedger(tmp_path / "funds_ledger.sqlite")


@pytest.fixture
def coordinator(ledger: FundsLedger) -> ExecutionCoordinator:
    return ExecutionCoordinator(ledger=ledger)


# ==============================================================================
# 1. Flight Lock Tests (单飞锁)
# ==============================================================================


class TestFlightLock:
    """Test single-flight mutual exclusion locking behavior."""

    def test_in_flight_locks_subsequent_request(self, coordinator: ExecutionCoordinator) -> None:
        """While a transaction is in-flight, any subsequent request is strictly blocked."""
        plan1 = make_test_plan(plan_id="flight_01")
        plan2 = make_test_plan(plan_id="flight_02")

        slot = coordinator.acquire_execution_slot(plan1, TEST_WALLET)
        assert isinstance(slot, ReservedExecution)
        assert coordinator.is_in_flight(TEST_WALLET) is True

        # Second attempt for the same wallet must raise flight lock error
        with pytest.raises(RuntimeError, match="Flight lock: transaction in-flight"):
            coordinator.acquire_execution_slot(plan2, TEST_WALLET)

        # Release the slot (canceling before broadcast)
        coordinator.release_execution_slot(slot)
        assert coordinator.is_in_flight(TEST_WALLET) is False

        # Now subsequent request succeeds
        slot2 = coordinator.acquire_execution_slot(plan2, TEST_WALLET)
        assert slot2.intent_id == "flight_02"
        coordinator.release_execution_slot(slot2)

    def test_concurrent_requests_strictly_intercepted(
        self, coordinator: ExecutionCoordinator
    ) -> None:
        """Concurrent multi-threaded slot acquisition permits exactly one success."""
        num_threads = 6
        plans = [make_test_plan(plan_id=f"concurrent_{i}") for i in range(num_threads)]
        successes: list[ReservedExecution] = []
        failures: list[Exception] = []

        def try_acquire(p: ExecutionPlan) -> None:
            try:
                slot = coordinator.acquire_execution_slot(p, TEST_WALLET)
                successes.append(slot)
            except Exception as e:
                failures.append(e)

        with ThreadPoolExecutor(max_workers=num_threads) as pool:
            futures = [pool.submit(try_acquire, plans[i]) for i in range(num_threads)]
            for f in futures:
                f.result()

        assert len(successes) == 1, f"Expected exactly 1 success, got {len(successes)}"
        assert len(failures) == num_threads - 1
        for exc in failures:
            assert isinstance(exc, RuntimeError)
            assert "Flight lock: transaction in-flight" in str(exc)

        # Clean up the single acquired slot
        coordinator.release_execution_slot(successes[0])
        assert coordinator.is_in_flight(TEST_WALLET) is False


# ==============================================================================
# 2. Cross-Base Reconciliation Blocking Tests (跨本位未知对账阻断)
# ==============================================================================


class TestCrossBaseBlocking:
    """Test that UNKNOWN status on one base strictly blocks all other bases."""

    def test_weth_unknown_blocks_subsequent_usdg(self, coordinator: ExecutionCoordinator) -> None:
        """A WETH trade resulting in UNKNOWN status blocks subsequent USDG trades."""
        plan_weth = make_test_plan(base="WETH", amount_in=10**16, min_amount_out=10**16 - 1000)
        plan_usdg = make_test_plan(base="USDG", amount_in=10_000_000, min_amount_out=9_900_000)

        # Acquire and release WETH slot with UNKNOWN status
        slot_weth = coordinator.acquire_execution_slot(plan_weth, TEST_WALLET)
        coordinator.release_execution_slot(
            slot_weth,
            {"status": "UNKNOWN", "tx_hash": TX_HASH},
        )

        # In-flight lock in memory is released, but ledger has durable UNKNOWN intent
        assert coordinator.is_in_flight(TEST_WALLET) is False

        # Attempting USDG trade must be strictly blocked by cross-base check
        with pytest.raises(
            (CrossBaseBlockedError, RuntimeError),
            match=r"(?i)cross-base|unknown|reconciliation|blocked",
        ):
            coordinator.acquire_execution_slot(plan_usdg, TEST_WALLET)

    def test_unknown_blocks_same_base_as_well(self, coordinator: ExecutionCoordinator) -> None:
        """An UNKNOWN status also blocks further trades on the same base."""
        plan1 = make_test_plan(base="USDG", plan_id="usdg_unk_01")
        plan2 = make_test_plan(base="USDG", plan_id="usdg_unk_02")

        slot = coordinator.acquire_execution_slot(plan1, TEST_WALLET)
        coordinator.release_execution_slot(slot, {"status": "UNKNOWN", "tx_hash": TX_HASH})

        with pytest.raises(
            (CrossBaseBlockedError, RuntimeError),
            match=r"(?i)cross-base|unknown|reconciliation|blocked",
        ):
            coordinator.acquire_execution_slot(plan2, TEST_WALLET)


# ==============================================================================
# 3. Budget Reservation and Exhaustion Tests (预算预留与耗尽)
# ==============================================================================


class TestBudgetReservation:
    """Test budget calculation, deduction, and exhaustion."""

    def test_cumulative_loss_exhaustion(self, coordinator: ExecutionCoordinator) -> None:
        """Cumulative realized losses deduct remaining budget until new orders are rejected."""
        # Initial status: spent = 0, remaining = 1.0
        status = coordinator.ledger.status(4663, TEST_WALLET, "USDG")
        assert status["spent_usd"] == Decimal("0")
        assert status["remaining_usd"] == Decimal("1.0")

        # Plan 1: reserve 0.6 USD loss (gas 0.1, token slippage loss 0.5)
        # amount_in = 10 USDG (10_000_000 atoms), min_out = 9.5 USDG (9_500_000 atoms) -> slippage loss = 0.5 USD
        plan1 = make_test_plan(
            base="USDG",
            amount_in=10_000_000,
            min_amount_out=9_500_000,
            gas_usd=Decimal("0.1"),
            plan_id="budget_plan_01",
        )
        slot1 = coordinator.acquire_execution_slot(plan1, TEST_WALLET)
        assert slot1.worst_loss_usd == Decimal("0.6")

        # Settle slot 1 with 0.6 USD net loss
        coordinator.release_execution_slot(
            slot1,
            {
                "status": 1,
                "tx_hash": TX_HASH,
                "block_hash": BLOCK_HASH,
                "net_profit_usd": Decimal("-0.6"),
                "actual_gas_usd": Decimal("0.1"),
            },
        )

        status_after_1 = coordinator.ledger.status(4663, TEST_WALLET, "USDG")
        assert status_after_1["spent_usd"] == Decimal("0.6")
        assert status_after_1["remaining_usd"] == Decimal("0.4")

        # Unpause wallet to test remaining budget logic
        coordinator.unpause_wallet(TEST_WALLET)

        # Plan 2 demands 0.5 USD loss (0.4 gas + 0.1 slippage) -> exceeds remaining 0.4 USD
        plan2 = make_test_plan(
            base="USDG",
            amount_in=10_000_000,
            min_amount_out=9_900_000,
            gas_usd=Decimal("0.4"),
            plan_id="budget_plan_02",
        )
        with pytest.raises(ExecutionLatched, match="Budget exhausted"):
            coordinator.acquire_execution_slot(plan2, TEST_WALLET)

        # Plan 3 demands 0.2 USD loss -> within remaining 0.4 USD
        plan3 = make_test_plan(
            base="USDG",
            amount_in=10_000_000,
            min_amount_out=9_900_000,
            gas_usd=Decimal("0.1"),
            plan_id="budget_plan_03",
        )
        slot3 = coordinator.acquire_execution_slot(plan3, TEST_WALLET)
        assert slot3.worst_loss_usd == Decimal("0.2")

        # Settle slot 3 with 0.4 USD loss -> cumulative spent hits 1.0 USD
        coordinator.release_execution_slot(
            slot3,
            {
                "status": 1,
                "tx_hash": TX_HASH,
                "block_hash": BLOCK_HASH,
                "net_profit_usd": Decimal("-0.4"),
                "actual_gas_usd": Decimal("0.1"),
            },
        )

        status_after_3 = coordinator.ledger.status(4663, TEST_WALLET, "USDG")
        assert status_after_3["spent_usd"] == Decimal("1.0")
        assert status_after_3["remaining_usd"] == Decimal("0.0")

        coordinator.unpause_wallet(TEST_WALLET)

        # Any further trade is strictly rejected
        plan4 = make_test_plan(gas_usd=Decimal("0.01"), plan_id="budget_plan_04")
        with pytest.raises(ExecutionLatched):
            coordinator.acquire_execution_slot(plan4, TEST_WALLET)


# ==============================================================================
# 4. Probe 1U Limit Circuit Breaker Tests (探路 1U 限制)
# ==============================================================================


class TestProbeCircuitBreaker:
    """Test probe mode 1.0 USD cumulative loss circuit breaker."""

    def test_probe_loss_exceeding_1_usd_triggers_circuit_breaker(
        self, coordinator: ExecutionCoordinator
    ) -> None:
        """When cumulative probe loss reaches 1.0 USD, circuit breaker trips and rejects."""
        plan1 = make_test_plan(
            base="USDG",
            amount_in=10_000_000,
            min_amount_out=9_200_000,
            gas_usd=Decimal("0.2"),
            plan_id="probe_plan_01",
        )
        slot1 = coordinator.acquire_execution_slot(plan1, TEST_WALLET, is_probe=True)

        # Settle slot 1 with exactly 1.0 USD loss
        coordinator.release_execution_slot(
            slot1,
            {
                "status": 1,
                "tx_hash": TX_HASH,
                "block_hash": BLOCK_HASH,
                "net_profit_usd": Decimal("-1.0"),
                "actual_gas_usd": Decimal("0.2"),
            },
        )

        # Unpause wallet so it is not rejected merely by paused flag
        coordinator.unpause_wallet(TEST_WALLET)

        # New probe attempt must trigger circuit breaker
        plan2 = make_test_plan(gas_usd=Decimal("0.01"), plan_id="probe_plan_02")
        with pytest.raises(
            (ProbeCircuitBreakerError, RuntimeError),
            match=r"(?i)probe.*circuit breaker|cumulative loss",
        ):
            coordinator.acquire_execution_slot(plan2, TEST_WALLET, is_probe=True)


# ==============================================================================
# 5. Ledger Restart Recovery Tests (账本重启恢复)
# ==============================================================================


class TestLedgerRestartRecovery:
    """Test SQLite durable persistence across application / coordinator restart."""

    def test_restarted_coordinator_preserves_loss_and_remaining_budget(
        self, tmp_path: Path
    ) -> None:
        """Restarting coordinator and ledger retains consumed budget without counter reset."""
        db_path = tmp_path / "durable_funds.sqlite"

        # Session 1: Run coordinator and incur 0.6 USD loss
        ledger1 = FundsLedger(db_path)
        coord1 = ExecutionCoordinator(ledger=ledger1)

        plan1 = make_test_plan(
            base="USDG",
            amount_in=10_000_000,
            min_amount_out=9_500_000,
            gas_usd=Decimal("0.1"),
            plan_id="restart_plan_01",
        )
        slot1 = coord1.acquire_execution_slot(plan1, TEST_WALLET)
        coord1.release_execution_slot(
            slot1,
            {
                "status": 1,
                "tx_hash": TX_HASH,
                "block_hash": BLOCK_HASH,
                "net_profit_usd": Decimal("-0.6"),
                "actual_gas_usd": Decimal("0.1"),
            },
        )
        coord1.unpause_wallet(TEST_WALLET)

        # Session 2: Fresh instances pointing to the exact same SQLite database
        del coord1
        del ledger1

        ledger2 = FundsLedger(db_path)
        coord2 = ExecutionCoordinator(ledger=ledger2)

        status = ledger2.status(4663, TEST_WALLET, "USDG")
        assert status["spent_usd"] == Decimal("0.6")
        assert status["remaining_usd"] == Decimal("0.4")

        # Over-budget trade (demanding 0.5 USD) must still be rejected
        plan2 = make_test_plan(
            base="USDG",
            amount_in=10_000_000,
            min_amount_out=9_900_000,
            gas_usd=Decimal("0.4"),
            plan_id="restart_plan_02",
        )
        with pytest.raises(ExecutionLatched, match="Budget exhausted"):
            coord2.acquire_execution_slot(plan2, TEST_WALLET)


# ==============================================================================
# 6. Gate 5 Safety Validations (代币与金额校验)
# ==============================================================================


class TestGate5SafetyValidations:
    """Test nominal amount upper bounds, zero-min-out prohibition, and token validation."""

    def test_amount_exceeding_500_usd_rejected(self, coordinator: ExecutionCoordinator) -> None:
        """Trade nominal size exceeding 500 USD is strictly rejected."""
        # 501 USDG = 501_000_000 atoms
        oversized_plan = make_test_plan(
            base="USDG",
            amount_in=501_000_000,
            min_amount_out=500_000_000,
            plan_id="oversized_plan",
        )
        with pytest.raises(ExcessiveAmountError, match=r"exceeds safety limit"):
            coordinator.acquire_execution_slot(oversized_plan, TEST_WALLET)

    def test_min_amount_out_zero_rejected(self, coordinator: ExecutionCoordinator) -> None:
        """min_amount_out <= 0 is strictly forbidden to prevent MEV sandwich attacks."""

        # ExecutionPlan __post_init__ prevents min_amount_out.atoms <= 0,
        # but test mock/bypass to verify coordinator Gate 5 defense in depth
        class MockBypassPlan:
            base_symbol = "USDG"
            decimals = 6
            amount_in_weth = 10_000_000
            amount_out_min = 0  # Forbidden
            amount_usd = Decimal("10")
            estimated_gas_usd = Decimal("0.05")
            plan_id = "zero_min_out_plan"

        with pytest.raises(ValueError, match=r"min_amount_out must be > 0"):
            coordinator.acquire_execution_slot(MockBypassPlan(), TEST_WALLET)

    def test_unsupported_base_symbol_rejected(self, coordinator: ExecutionCoordinator) -> None:
        """Tokens outside BASES (WETH, USDG) are rejected."""

        class MockInvalidTokenPlan:
            base_symbol = "DOGE"
            decimals = 8
            amount_in_weth = 1000
            amount_out_min = 900
            amount_usd = Decimal("10")
            estimated_gas_usd = Decimal("0.05")
            plan_id = "invalid_token_plan"

        with pytest.raises(FundsError, match=r"Unsupported base symbol"):
            coordinator.acquire_execution_slot(MockInvalidTokenPlan(), TEST_WALLET)


# ==============================================================================
# 7. Static AST Audit (静态 AST 审计测试)
# ==============================================================================


class TestStaticASTAudit:
    """Security audit of coordinator.py AST for hardcoded keys, forbidden modules, and calls."""

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
        """Verify execution/coordinator.py contains no unauthorized calls or plaintext credentials."""
        target_file = Path(__file__).resolve().parent.parent / "execution" / "coordinator.py"
        assert target_file.exists(), f"{target_file} not found"

        source = target_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(target_file))

        for node in ast.walk(tree):
            # 1. Check import x
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root_mod = alias.name.split(".")[0]
                    assert root_mod not in self.FORBIDDEN_MODULES, (
                        f"Forbidden import '{alias.name}' found in coordinator.py"
                    )

            # 2. Check from x import y
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                root_mod = mod.split(".")[0]
                assert root_mod not in self.FORBIDDEN_MODULES, (
                    f"Forbidden import-from '{mod}' found in coordinator.py"
                )

            # 3. Check forbidden function calls
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    assert node.func.id not in self.FORBIDDEN_CALLS, (
                        f"Forbidden call '{node.func.id}()' found in coordinator.py"
                    )

        # 4. Check sensitive keywords
        for kw in self.FORBIDDEN_KEYWORDS:
            assert kw not in source, f"Sensitive keyword '{kw}' found in coordinator.py"
