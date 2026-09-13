"""Arc Output Evidence and State-Diff Verification Tests (T27)

Verifies:
- Positive: Independent balance diff and gas accounting correctly recalculated
- Boundary: Gas included in diff vs deducted separately, distinct recipient vs caller
- Anti-pollution: Injection of funds from untracked third-party accounts is detected and rejected
- Failure: Substituting plan.net_profit without verified evidence raises OutputVerificationError
- Failure: Secondary independent balanceOf call is rejected as invalid post-state proof
- Failure: Trace capability unavailable gracefully falls back to status=OUTPUT_UNVERIFIED, output_verified=False
"""

from __future__ import annotations

import pytest

from arbitrage_contracts.arc_extensions import SimulationEvidenceBridge, SimulationStatus
from arbitrage_contracts.identity import (
    Amount,
    AssetRef,
    FeeModel,
    PoolDescriptor,
    PoolKey,
    TokenKey,
)
from arbitrage_contracts.quote import HopRef, RouteRef
from atomic_execution.arc_output_adapter import ArcOutputAdapter
from atomic_execution.arc_planning import ArcExecutionPlan
from atomic_execution.output_evidence import (
    OutputEvidence,
    OutputEvidenceVerifier,
    OutputVerificationError,
    OutputVerificationStatus,
    StateDiffEntry,
    TraceGasAttribution,
)
from atomic_execution.policy import ExecutionPolicy

CHAIN_ARC = 5042
CALLER = "0x1111111111111111111111111111111111111111"
ROUTER = "0x2222222222222222222222222222222222222222"
RECIPIENT = "0x3333333333333333333333333333333333333333"
USDC_ADDR = "0x3600000000000000000000000000000000000000"
WETH_ADDR = "0x5555555555555555555555555555555555555555"


def make_mock_plan() -> ArcExecutionPlan:
    asset_usdc = AssetRef("erc20", CHAIN_ARC, TokenKey(CHAIN_ARC, USDC_ADDR))
    asset_weth = AssetRef("erc20", CHAIN_ARC, TokenKey(CHAIN_ARC, WETH_ADDR))
    p1 = PoolDescriptor(
        PoolKey(CHAIN_ARC, "v3", "factory", "0x6666666666666666666666666666666666666666", "address", "0x7777777777777777777777777777777777777777"),
        asset_usdc,
        asset_weth,
        FeeModel.static(500),
        10,
    )
    p2 = PoolDescriptor(
        PoolKey(CHAIN_ARC, "v3", "factory", "0x6666666666666666666666666666666666666666", "address", "0x8888888888888888888888888888888888888888"),
        asset_usdc,
        asset_weth,
        FeeModel.static(3000),
        60,
    )
    hop1 = HopRef(p1.key, asset_usdc, asset_weth, "zero_for_one", p1)
    hop2 = HopRef(p2.key, asset_weth, asset_usdc, "one_for_zero", p2)
    route = RouteRef(CHAIN_ARC, asset_usdc, (hop1, hop2))
    amount_in = Amount(asset_usdc, 100_000_000, 6)
    expected_out = Amount(asset_usdc, 105_000_000, 6)
    min_amount_out = Amount(asset_usdc, 102_000_000, 6)
    output_floor = Amount(asset_usdc, 101_000_000, 6)
    return ArcExecutionPlan(
        plan_id="plan-001",
        chain_id=CHAIN_ARC,
        route_ref=route,
        base_asset=asset_usdc,
        amount_in=amount_in,
        expected_out=expected_out,
        min_amount_out=min_amount_out,
        output_floor=output_floor,
        policy=ExecutionPolicy(),
        target_router=ROUTER,
        deployment_fingerprint="sha256:fingerprint",
    )


class TestArcOutputEvidence:
    """Test suite for T27 Output Evidence and Verification Invariants."""

    def test_positive_state_diff_and_gas_recalculation(self) -> None:
        """Verify state diff and separate gas accounting produces exact verified net profit."""
        plan = make_mock_plan()
        # Caller spent 100 USDC and received 106 USDC -> balance_after - balance_before = +6 USDC
        diffs = [
            StateDiffEntry(
                account=CALLER,
                token_address=USDC_ADDR,
                balance_before=1_000_000_000,
                balance_after=1_006_000_000,
            )
        ]
        # Gas attribution uses Arc native USDC 18-decimal units; 50,000 * 2e12 -> 100,000 canonical 6-decimal atoms.
        gas_attr = TraceGasAttribution(
            gas_payer=CALLER,
            gas_used_atoms=50_000,
            effective_gas_price_atoms=2_000_000_000_000,
            gas_included_in_diff=False,  # deducted separately
        )

        evidence = OutputEvidenceVerifier.verify_from_trace(
            evidence_id="evi-01",
            plan_id=plan.plan_id,
            caller_address=CALLER,
            router_address=ROUTER,
            recipient_address=CALLER,
            base_token_address=USDC_ADDR,
            block_number=1000,
            state_hash="0x" + "aa" * 32,
            trace_available=True,
            is_independent_poll=False,
            diffs=diffs,
            gas_attribution=gas_attr,
        )

        assert evidence.is_verified is True
        assert evidence.status == OutputVerificationStatus.VERIFIED
        # 6,000_000 - 100_000 = 5,900,000
        assert evidence.verified_net_atoms == 6_000_000 - 100_000

        # Adapt to SimulationEvidenceBridge
        bridge = ArcOutputAdapter.adapt_with_evidence(
            call_succeeded=True,
            plan=plan,
            evidence=evidence,
        )
        assert bridge.call_succeeded is True
        assert bridge.output_verified is True
        assert bridge.status == SimulationStatus.CALL_SUCCEEDED
        assert bridge.net_output_atoms == 5_900_000

    def test_distinct_recipient_balance_change(self) -> None:
        """Verify that when profit is routed to a distinct recipient, recipient's diff is certified."""
        plan = make_mock_plan()
        diffs = [
            # Caller spent 100 USDC
            StateDiffEntry(
                account=CALLER,
                token_address=USDC_ADDR,
                balance_before=100_000_000,
                balance_after=0,
            ),
            # Recipient received 106 USDC
            StateDiffEntry(
                account=RECIPIENT,
                token_address=USDC_ADDR,
                balance_before=0,
                balance_after=106_000_000,
            ),
        ]
        evidence = OutputEvidenceVerifier.verify_from_trace(
            evidence_id="evi-02",
            plan_id=plan.plan_id,
            caller_address=CALLER,
            router_address=ROUTER,
            recipient_address=RECIPIENT,
            base_token_address=USDC_ADDR,
            block_number=1001,
            state_hash="0x" + "bb" * 32,
            trace_available=True,
            is_independent_poll=False,
            diffs=diffs,
            gas_attribution=TraceGasAttribution(
                gas_payer=CALLER,
                gas_used_atoms=0,
                effective_gas_price_atoms=0,
                gas_included_in_diff=False,
            ),
        )
        assert evidence.is_verified is True
        # Net strategy profit is recipient's +106M plus caller's -100M = +6M
        assert evidence.verified_net_atoms == 6_000_000

    def test_independent_balance_poll_rejected(self) -> None:
        """Rule: Calling balanceOf secondary query is rejected as post-state evidence."""
        plan = make_mock_plan()
        diffs = [
            StateDiffEntry(account=CALLER, token_address=USDC_ADDR, balance_before=0, balance_after=105_000_000)
        ]
        evidence = OutputEvidenceVerifier.verify_from_trace(
            evidence_id="evi-03",
            plan_id=plan.plan_id,
            caller_address=CALLER,
            router_address=ROUTER,
            recipient_address=CALLER,
            base_token_address=USDC_ADDR,
            block_number=1002,
            state_hash="0x" + "cc" * 32,
            trace_available=True,
            is_independent_poll=True,  # Disallowed secondary poll
            diffs=diffs,
        )
        assert evidence.is_verified is False
        assert evidence.status == OutputVerificationStatus.INDEPENDENT_POLL_REJECTED
        assert evidence.verified_net_atoms is None

        bridge = ArcOutputAdapter.adapt_with_evidence(call_succeeded=True, plan=plan, evidence=evidence)
        assert bridge.output_verified is False
        assert bridge.status == SimulationStatus.OUTPUT_UNVERIFIED
        assert bridge.net_output_atoms is None

    def test_state_pollution_detected_and_rejected(self) -> None:
        """Rule: Injections from unknown third-party accounts must fail closed."""
        plan = make_mock_plan()
        hacker_addr = "0x9999999999999999999999999999999999999999"
        diffs = [
            StateDiffEntry(account=CALLER, token_address=USDC_ADDR, balance_before=0, balance_after=105_000_000),
            StateDiffEntry(account=hacker_addr, token_address=USDC_ADDR, balance_before=0, balance_after=50_000_000),
        ]
        evidence = OutputEvidenceVerifier.verify_from_trace(
            evidence_id="evi-04",
            plan_id=plan.plan_id,
            caller_address=CALLER,
            router_address=ROUTER,
            recipient_address=CALLER,
            base_token_address=USDC_ADDR,
            block_number=1003,
            state_hash="0x" + "dd" * 32,
            trace_available=True,
            is_independent_poll=False,
            diffs=diffs,
        )
        assert evidence.is_verified is False
        assert evidence.status == OutputVerificationStatus.STATE_POLLUTION_DETECTED

    def test_prohibit_plan_expected_profit_substitution(self) -> None:
        """Rule: Attempting to assert verified profit without OutputEvidence raises OutputVerificationError."""
        plan = make_mock_plan()
        with pytest.raises(OutputVerificationError, match="Substituting plan expected profit"):
            ArcOutputAdapter.enforce_no_plan_substitution(
                plan=plan,
                claimed_verified_atoms=5_000_000,
                evidence=None,
            )

    def test_trace_unavailable_falls_back_to_unverified(self) -> None:
        """Rule: If trace backend is unavailable, output_verified remains False."""
        plan = make_mock_plan()
        evidence = OutputEvidenceVerifier.verify_from_trace(
            evidence_id="evi-05",
            plan_id=plan.plan_id,
            caller_address=CALLER,
            router_address=ROUTER,
            recipient_address=CALLER,
            base_token_address=USDC_ADDR,
            block_number=1004,
            state_hash="0x" + "ee" * 32,
            trace_available=False,  # Unavailable
            is_independent_poll=False,
            diffs=[StateDiffEntry(account=CALLER, token_address=USDC_ADDR, balance_before=0, balance_after=100)],
        )
        assert evidence.is_verified is False
        assert evidence.status == OutputVerificationStatus.TRACE_UNAVAILABLE

        bridge = ArcOutputAdapter.adapt_with_evidence(call_succeeded=True, plan=plan, evidence=evidence)
        assert bridge.output_verified is False
        assert bridge.status == SimulationStatus.OUTPUT_UNVERIFIED
        assert bridge.net_output_atoms is None
