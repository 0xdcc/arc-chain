"""Arc Execution Authorization and Security Policy Tests (T28)

Verifies:
- Authorized requests are admitted and dispatched strictly to the mock execution sink
- Atomic budget reservation prevents overdrafts beyond authorized limits
- Expiration check fails closed once past expiry timestamp
- Hard cap <= 500 USD (500,000,000 atoms) is strictly enforced
- Zero or negative min_output is strictly rejected
- Offline research constraint: offline_mock_only=False fails closed immediately
"""

from __future__ import annotations

import pytest

from arbitrage_contracts.identity import (
    Amount,
    AssetRef,
    FeeModel,
    PoolDescriptor,
    PoolKey,
    TokenKey,
)
from arbitrage_contracts.quote import HopRef, RouteRef
from arc_execution.authorization import (
    AuthorizationBudgetExceededError,
    AuthorizationError,
    AuthorizationExpiredError,
    ExecutionAuthorizationCard,
)
from arc_execution.policy import ArcRestrictedPolicyEngine
from atomic_execution.arc_planning import ArcExecutionPlan
from atomic_execution.policy import ExecutionPolicy

CHAIN_ARC = 5042
WALLET = "0x" + "11" * 20
ROUTER = "0x" + "22" * 20
USDC_ADDR = "0x" + "33" * 20
WETH_ADDR = "0x" + "44" * 20
CONFIG_HASH = "sha256:" + "aa" * 32


def make_mock_plan(amount_atoms: int = 100_000_000, min_out_atoms: int = 102_000_000) -> ArcExecutionPlan:
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
        plan_id="plan-001",
        chain_id=CHAIN_ARC,
        route_ref=route,
        base_asset=asset_usdc,
        amount_in=Amount(asset_usdc, amount_atoms, 6),
        expected_out=Amount(asset_usdc, min_out_atoms + 1_000_000, 6),
        min_amount_out=Amount(asset_usdc, min_out_atoms, 6),
        output_floor=Amount(asset_usdc, amount_atoms, 6),
        policy=ExecutionPolicy(),
        target_router=ROUTER,
        deployment_fingerprint="sha256:fp",
    )


class TestArcAuthorizationPolicy:
    """Test suite for T28 Execution Authorization and Policy Enforcements."""

    def test_authorized_plan_dispatched_to_mock_sink(self) -> None:
        card = ExecutionAuthorizationCard(
            auth_id="card-001",
            wallet_address=WALLET,
            target_router=ROUTER,
            chain_id=CHAIN_ARC,
            config_hash=CONFIG_HASH,
            expires_at_utc=2000.0,
            total_budget_atoms=500_000_000,
        )
        engine = ArcRestrictedPolicyEngine(card)
        plan = make_mock_plan(amount_atoms=100_000_000, min_out_atoms=102_000_000)

        record = engine.evaluate_and_dispatch(
            plan=plan,
            caller_wallet=WALLET,
            config_hash=CONFIG_HASH,
            now_utc=1000.0,
        )
        assert record.is_delivered_to_live_network is False
        assert record.status == "DELIVERED_TO_MOCK_SINK"
        assert engine.card.spent_budget_atoms == 100_000_000
        assert engine.card.remaining_budget_atoms == 400_000_000

    def test_budget_exhaustion_fails_closed(self) -> None:
        """Rule: Attempting to spend beyond total authorized budget raises AuthorizationBudgetExceededError."""
        card = ExecutionAuthorizationCard(
            auth_id="card-002",
            wallet_address=WALLET,
            target_router=ROUTER,
            chain_id=CHAIN_ARC,
            config_hash=CONFIG_HASH,
            expires_at_utc=2000.0,
            total_budget_atoms=150_000_000,  # 150 USDC budget
        )
        engine = ArcRestrictedPolicyEngine(card)

        # 1st request for 100 USDC -> succeeds
        engine.evaluate_and_dispatch(make_mock_plan(amount_atoms=100_000_000), WALLET, CONFIG_HASH, 1000.0)
        assert engine.card.remaining_budget_atoms == 50_000_000

        # 2nd request for 60 USDC -> exceeds 50 USDC remaining -> fails closed
        with pytest.raises(AuthorizationBudgetExceededError, match="exceeds remaining budget"):
            engine.evaluate_and_dispatch(make_mock_plan(amount_atoms=60_000_000), WALLET, CONFIG_HASH, 1000.0)

    def test_expired_card_fails_closed(self) -> None:
        """Rule: Expired authorization card raises AuthorizationExpiredError."""
        card = ExecutionAuthorizationCard(
            auth_id="card-003",
            wallet_address=WALLET,
            target_router=ROUTER,
            chain_id=CHAIN_ARC,
            config_hash=CONFIG_HASH,
            expires_at_utc=1000.0,
            total_budget_atoms=500_000_000,
        )
        engine = ArcRestrictedPolicyEngine(card)
        with pytest.raises(AuthorizationExpiredError, match="expired"):
            engine.evaluate_and_dispatch(make_mock_plan(), WALLET, CONFIG_HASH, now_utc=1001.0)

    def test_hard_cap_500_usd_enforced(self) -> None:
        """Rule: Single transaction strictly cannot exceed 500 USD (500,000,000 atoms)."""
        card = ExecutionAuthorizationCard(
            auth_id="card-004",
            wallet_address=WALLET,
            target_router=ROUTER,
            chain_id=CHAIN_ARC,
            config_hash=CONFIG_HASH,
            expires_at_utc=2000.0,
            total_budget_atoms=1_000_000_000,  # 1000 USDC total budget
        )
        engine = ArcRestrictedPolicyEngine(card)
        # Attempt single trade of 501 USDC (501,000,000 atoms)
        with pytest.raises(AuthorizationError, match="exceeds single tx hard cap"):
            engine.evaluate_and_dispatch(make_mock_plan(amount_atoms=501_000_000), WALLET, CONFIG_HASH, 1000.0)

    def test_zero_or_negative_min_out_rejected(self) -> None:
        """Rule: min_output_atoms must be strictly positive."""
        card = ExecutionAuthorizationCard(
            auth_id="card-005",
            wallet_address=WALLET,
            target_router=ROUTER,
            chain_id=CHAIN_ARC,
            config_hash=CONFIG_HASH,
            expires_at_utc=2000.0,
            total_budget_atoms=500_000_000,
        )
        engine = ArcRestrictedPolicyEngine(card)
        with pytest.raises(AuthorizationError, match="min_output_atoms must be strictly positive"):
            engine.evaluate_and_dispatch(make_mock_plan(min_out_atoms=0), WALLET, CONFIG_HASH, 1000.0)

    def test_live_network_flag_locked_out(self) -> None:
        """Rule: Creating card with offline_mock_only=False must fail closed immediately."""
        with pytest.raises(AuthorizationError, match="offline_mock_only must be True"):
            ExecutionAuthorizationCard(
                auth_id="card-006",
                wallet_address=WALLET,
                target_router=ROUTER,
                chain_id=CHAIN_ARC,
                config_hash=CONFIG_HASH,
                expires_at_utc=2000.0,
                total_budget_atoms=500_000_000,
                offline_mock_only=False,  # Prohibited!
            )
