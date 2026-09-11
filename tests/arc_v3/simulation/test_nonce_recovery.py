"""Arc Nonce Journal, State Machine, and Crash Recovery Tests (T29)

Verifies:
- Linear 7-stage state progression: candidate -> preflight -> authorized -> intent -> pending -> reconcile -> completed
- Single in-flight invariant: attempting to issue or record a new transaction while one is pending raises InFlightCollisionError
- Crash recovery: journal reload recovers unconfirmed pending transaction and restores PENDING_BROADCAST stage
- Monotonic nonce progression: nonces strictly increment by 1
- Timeout does not create a new nonce: unconfirmed transaction blocks subsequent dispatch until reconciled
"""

from __future__ import annotations

import os
import tempfile
import pytest

from arbitrage_contracts.identity import Amount, AssetRef, FeeModel, PoolDescriptor, PoolKey, TokenKey
from arbitrage_contracts.quote import HopRef, RouteRef
from arc_execution.authorization import ExecutionAuthorizationCard
from arc_execution.nonce_journal import (
    ArcExecutionStateMachine,
    ExecutionStage,
    InFlightCollisionError,
    NonceJournal,
    StateTransitionError,
)
from atomic_execution.arc_planning import ArcExecutionPlan
from atomic_execution.policy import ExecutionPolicy

CHAIN_ARC = 5042
WALLET = "0x" + "11" * 20
ROUTER = "0x" + "22" * 20
USDC_ADDR = "0x" + "33" * 20
WETH_ADDR = "0x" + "44" * 20
CONFIG_HASH = "sha256:" + "bb" * 32


def make_mock_plan(plan_id: str = "plan-001") -> ArcExecutionPlan:
    asset_usdc = AssetRef("erc20", CHAIN_ARC, TokenKey(CHAIN_ARC, USDC_ADDR))
    asset_weth = AssetRef("erc20", CHAIN_ARC, TokenKey(CHAIN_ARC, WETH_ADDR))
    p1 = PoolDescriptor(
        PoolKey(CHAIN_ARC, "v3", "factory", "0x" + "55" * 20, "address", "0x" + "66" * 20),
        asset_usdc,
        asset_weth,
        FeeModel.static(500),
        10,
    )
    p2 = PoolDescriptor(
        PoolKey(CHAIN_ARC, "v3", "factory", "0x" + "55" * 20, "address", "0x" + "77" * 20),
        asset_usdc,
        asset_weth,
        FeeModel.static(3000),
        60,
    )
    hop1 = HopRef(p1.key, asset_usdc, asset_weth, "zero_for_one", p1)
    hop2 = HopRef(p2.key, asset_weth, asset_usdc, "one_for_zero", p2)
    route = RouteRef(CHAIN_ARC, asset_usdc, (hop1, hop2))
    return ArcExecutionPlan(
        plan_id=plan_id,
        chain_id=CHAIN_ARC,
        route_ref=route,
        base_asset=asset_usdc,
        amount_in=Amount(asset_usdc, 100_000_000, 6),
        expected_out=Amount(asset_usdc, 105_000_000, 6),
        min_amount_out=Amount(asset_usdc, 102_000_000, 6),
        output_floor=Amount(asset_usdc, 100_000_000, 6),
        policy=ExecutionPolicy(),
        target_router=ROUTER,
        deployment_fingerprint="sha256:fp",
    )


class TestArcNonceRecovery:
    """Test suite for T29 Nonce Journal, State Machine, and Crash Recovery."""

    def test_complete_state_machine_lifecycle(self) -> None:
        """Verify seamless progression through all 7 stages."""
        with tempfile.TemporaryDirectory() as tmpdir:
            journal_path = os.path.join(tmpdir, "nonce_journal.jsonl")
            journal = NonceJournal(journal_path, WALLET)
            card = ExecutionAuthorizationCard(
                auth_id="card-001",
                wallet_address=WALLET,
                target_router=ROUTER,
                chain_id=CHAIN_ARC,
                config_hash=CONFIG_HASH,
                expires_at_utc=2000.0,
                total_budget_atoms=500_000_000,
            )
            sm = ArcExecutionStateMachine(journal, card)

            plan = make_mock_plan("p-1")
            # 1. CANDIDATE
            ctx = sm.transition_candidate(plan)
            assert ctx.stage == ExecutionStage.CANDIDATE

            # 2. PREFLIGHT
            ctx = sm.transition_preflight(passed_checks=True)
            assert ctx.stage == ExecutionStage.PREFLIGHT

            # 3. AUTHORIZED
            ctx = sm.transition_authorized(CONFIG_HASH)
            assert ctx.stage == ExecutionStage.AUTHORIZED

            # 4. INTENT_RECORDED
            ctx = sm.record_intent_and_reserve_nonce("int-1", ROUTER, 100_000_000, "0x" + "aa" * 32)
            assert ctx.stage == ExecutionStage.INTENT_RECORDED
            assert ctx.intent is not None
            assert ctx.intent.nonce == 0

            # 5. PENDING_BROADCAST
            ctx = sm.mark_broadcast_pending()
            assert ctx.stage == ExecutionStage.PENDING_BROADCAST

            # 6. RECONCILE
            ctx = sm.transition_reconcile()
            assert ctx.stage == ExecutionStage.RECONCILE

            # 7. COMPLETED
            ctx = sm.finalize_reconciled(success=True)
            assert ctx.stage == ExecutionStage.COMPLETED
            assert sm.can_accept_new_plan() is True

            # Next plan gets nonce 1
            plan2 = make_mock_plan("p-2")
            sm.transition_candidate(plan2)
            sm.transition_preflight(True)
            sm.transition_authorized(CONFIG_HASH)
            ctx2 = sm.record_intent_and_reserve_nonce("int-2", ROUTER, 100_000_000, "0x" + "bb" * 32)
            assert ctx2.intent is not None
            assert ctx2.intent.nonce == 1

    def test_in_flight_collision_prevents_concurrent_order(self) -> None:
        """Rule: One in-flight transaction locks out new candidate admissions."""
        with tempfile.TemporaryDirectory() as tmpdir:
            journal_path = os.path.join(tmpdir, "nonce_journal.jsonl")
            journal = NonceJournal(journal_path, WALLET)
            card = ExecutionAuthorizationCard("card-002", WALLET, ROUTER, CHAIN_ARC, CONFIG_HASH, 2000.0, 500_000_000)
            sm = ArcExecutionStateMachine(journal, card)

            plan1 = make_mock_plan("p-1")
            sm.transition_candidate(plan1)
            sm.transition_preflight(True)
            sm.transition_authorized(CONFIG_HASH)
            sm.record_intent_and_reserve_nonce("int-1", ROUTER, 100_000_000, "0x" + "aa" * 32)

            # While int-1 is in-flight (stage=INTENT_RECORDED), try to admit plan2
            plan2 = make_mock_plan("p-2")
            with pytest.raises(InFlightCollisionError, match="Cannot admit new plan"):
                sm.transition_candidate(plan2)

    def test_crash_recovery_restores_pending_intent(self) -> None:
        """Rule: System crash during broadcast restores PENDING_BROADCAST stage upon restart."""
        with tempfile.TemporaryDirectory() as tmpdir:
            journal_path = os.path.join(tmpdir, "nonce_journal.jsonl")
            card = ExecutionAuthorizationCard("card-003", WALLET, ROUTER, CHAIN_ARC, CONFIG_HASH, 2000.0, 500_000_000)

            # Instance 1: advances to PENDING_BROADCAST then simulated crash (process exit)
            journal1 = NonceJournal(journal_path, WALLET)
            sm1 = ArcExecutionStateMachine(journal1, card)
            sm1.transition_candidate(make_mock_plan("p-crash"))
            sm1.transition_preflight(True)
            sm1.transition_authorized(CONFIG_HASH)
            sm1.record_intent_and_reserve_nonce("int-crash", ROUTER, 100_000_000, "0x" + "cc" * 32)
            sm1.mark_broadcast_pending()

            # Instance 2 (Reboot): reload journal from disk
            journal2 = NonceJournal(journal_path, WALLET)
            sm2 = ArcExecutionStateMachine(journal2, card)

            # Assert state was restored
            assert sm2.current_context is not None
            assert sm2.current_context.plan_id == "p-crash"
            assert sm2.current_context.stage == ExecutionStage.PENDING_BROADCAST
            assert sm2.can_accept_new_plan() is False

            # Must reconcile first before accepting any new plan
            sm2.transition_reconcile()
            sm2.finalize_reconciled(success=True)
            assert sm2.can_accept_new_plan() is True
