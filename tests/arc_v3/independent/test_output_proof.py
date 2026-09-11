"""Independent Verification Test Suite for Arc Output Proof & State-Diff Certifications (T46 / G2).

Audits and proves:
1. Secondary independent balanceOf polling is strictly rejected as post-state proof.
2. Trace backend unavailability fails-closed to OUTPUT_UNVERIFIED with None net atoms.
3. Unrelated external balance injections fail-closed as STATE_POLLUTION_DETECTED.
4. Separate caller vs recipient accounts: total strategy net delta accurately attributed.
5. Gas attribution: fee deducted once if not already reflected in native token diff.
6. Absolute decoupling: plan.expected_out or net_profit is NEVER substituted for verified trace proof.
7. Revert fail-closed: execution revert locks output_verified to False.
"""

from __future__ import annotations

import pytest

from arbitrage_contracts.arc_extensions import SimulationStatus
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
    OutputEvidenceVerifier,
    OutputVerificationStatus,
    StateDiffEntry,
    TraceGasAttribution,
)
from atomic_execution.policy import ExecutionPolicy

CHAIN_ARC = 5042
CALLER = "0x1111111111111111111111111111111111111111"
ROUTER = "0x2222222222222222222222222222222222222222"
RECIPIENT = "0x3333333333333333333333333333333333333333"
USDC_ADDR = "0x4444444444444444444444444444444444444444"
WETH_ADDR = "0x5555555555555555555555555555555555555555"


def _make_mock_plan() -> ArcExecutionPlan:
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
    output_floor = Amount(asset_usdc, 100_100_000, 6)
    policy = ExecutionPolicy()
    return ArcExecutionPlan(
        plan_id="plan_t46_proof",
        chain_id=CHAIN_ARC,
        route_ref=route,
        base_asset=asset_usdc,
        amount_in=amount_in,
        expected_out=expected_out,
        min_amount_out=output_floor,
        output_floor=output_floor,
        policy=policy,
        target_router=ROUTER,
        deployment_fingerprint="fp_test_t46",
    )


class TestOutputEvidenceVerification:
    """Verifies invariant adherence inside OutputEvidenceVerifier."""

    STATE_HASH = "0x" + "aa" * 32

    def test_independent_balanceof_poll_rejected(self) -> None:
        # Polling balanceOf separately must be rejected
        evidence = OutputEvidenceVerifier.verify_from_trace(
            evidence_id="ev_poll",
            plan_id="p1",
            caller_address=CALLER,
            router_address=ROUTER,
            recipient_address=RECIPIENT,
            base_token_address=USDC_ADDR,
            block_number=1000,
            state_hash=self.STATE_HASH,
            trace_available=True,
            is_independent_poll=True,  # Disallowed polling
            diffs=[
                StateDiffEntry(CALLER, USDC_ADDR, 1000, 0),
                StateDiffEntry(RECIPIENT, USDC_ADDR, 0, 1050),
            ],
        )

        assert evidence.is_verified is False
        assert evidence.status == OutputVerificationStatus.INDEPENDENT_POLL_REJECTED
        assert evidence.verified_net_atoms is None
        assert "Independent balanceOf poll rejected" in (evidence.rejection_reason or "")

    def test_trace_unavailable_fails_closed(self) -> None:
        evidence = OutputEvidenceVerifier.verify_from_trace(
            evidence_id="ev_no_trace",
            plan_id="p1",
            caller_address=CALLER,
            router_address=ROUTER,
            recipient_address=RECIPIENT,
            base_token_address=USDC_ADDR,
            block_number=1000,
            state_hash=self.STATE_HASH,
            trace_available=False,  # Trace capability unavailable
            is_independent_poll=False,
            diffs=[],
        )

        assert evidence.is_verified is False
        assert evidence.status == OutputVerificationStatus.TRACE_UNAVAILABLE
        assert evidence.verified_net_atoms is None

    def test_empty_diffs_fails_closed(self) -> None:
        evidence = OutputEvidenceVerifier.verify_from_trace(
            evidence_id="ev_empty",
            plan_id="p1",
            caller_address=CALLER,
            router_address=ROUTER,
            recipient_address=RECIPIENT,
            base_token_address=USDC_ADDR,
            block_number=1000,
            state_hash=self.STATE_HASH,
            trace_available=True,
            is_independent_poll=False,
            diffs=[],  # Empty diffs
        )

        assert evidence.is_verified is False
        assert evidence.status == OutputVerificationStatus.INSUFFICIENT_EVIDENCE
        assert evidence.verified_net_atoms is None

    def test_untracked_account_pollution_rejected(self) -> None:
        unrelated_bot = "0x" + "99" * 20
        evidence = OutputEvidenceVerifier.verify_from_trace(
            evidence_id="ev_pollute",
            plan_id="p1",
            caller_address=CALLER,
            router_address=ROUTER,
            recipient_address=RECIPIENT,
            base_token_address=USDC_ADDR,
            block_number=1000,
            state_hash=self.STATE_HASH,
            trace_available=True,
            is_independent_poll=False,
            diffs=[
                StateDiffEntry(CALLER, USDC_ADDR, 1000, 0),
                StateDiffEntry(RECIPIENT, USDC_ADDR, 0, 1050),
                StateDiffEntry(unrelated_bot, USDC_ADDR, 0, 500),  # Injected balance!
            ],
        )

        assert evidence.is_verified is False
        assert evidence.status == OutputVerificationStatus.STATE_POLLUTION_DETECTED
        assert evidence.verified_net_atoms is None
        assert "State pollution detected" in (evidence.rejection_reason or "")

    def test_distinct_caller_and_recipient_net_attribution(self) -> None:
        # Caller pays 1000 USDC, beneficiary receives 1050 USDC
        # Gas paid: 5 USDC (not included in diff)
        gas_attr = TraceGasAttribution(
            gas_payer=CALLER,
            gas_used_atoms=50_000,
            effective_gas_price_atoms=100,  # 5_000_000 atoms gas = 5 USDC
            gas_included_in_diff=False,
        )

        evidence = OutputEvidenceVerifier.verify_from_trace(
            evidence_id="ev_valid",
            plan_id="p1",
            caller_address=CALLER,
            router_address=ROUTER,
            recipient_address=RECIPIENT,
            base_token_address=USDC_ADDR,
            block_number=1000,
            state_hash=self.STATE_HASH,
            trace_available=True,
            is_independent_poll=False,
            diffs=[
                StateDiffEntry(CALLER, USDC_ADDR, 1000, 0),  # -1000
                StateDiffEntry(RECIPIENT, USDC_ADDR, 0, 1050),  # +1050
            ],
            gas_attribution=gas_attr,
        )

        assert evidence.is_verified is True
        assert evidence.status == OutputVerificationStatus.VERIFIED
        # Net = (+1050 - 1000) - 5 = 45 atoms
        assert evidence.verified_net_atoms == (1050 - 1000) - gas_attr.gas_fee_atoms


class TestArcOutputAdapterDecoupling:
    """Verifies that ArcOutputAdapter never substitutes hypotheses for verified proof."""

    def test_unverified_simulation_call_succeeded_blocks_output_verified(self) -> None:
        plan = _make_mock_plan()

        # EVM call returned 0x, but no trace evidence could certify the balance change
        bridge = ArcOutputAdapter.adapt_with_evidence(
            call_succeeded=True,
            plan=plan,
            evidence=None,  # No evidence
            raw_gas_used=120_000,
        )

        assert bridge.call_succeeded is True
        assert bridge.output_verified is False
        assert bridge.status == SimulationStatus.OUTPUT_UNVERIFIED
        assert bridge.net_output_atoms is None  # CRUCIAL: never defaults to plan.expected_out!

    def test_rejected_evidence_blocks_profit_elevation(self) -> None:
        plan = _make_mock_plan()
        unverified_evidence = OutputEvidenceVerifier.verify_from_trace(
            evidence_id="ev_fail",
            plan_id=plan.plan_id,
            caller_address=CALLER,
            router_address=ROUTER,
            recipient_address=RECIPIENT,
            base_token_address=USDC_ADDR,
            block_number=1000,
            state_hash="0x" + "11" * 32,
            trace_available=False,
            is_independent_poll=False,
            diffs=[],
        )

        bridge = ArcOutputAdapter.adapt_with_evidence(
            call_succeeded=True,
            plan=plan,
            evidence=unverified_evidence,
            raw_gas_used=100_000,
        )

        assert bridge.call_succeeded is True
        assert bridge.output_verified is False
        assert bridge.status == SimulationStatus.OUTPUT_UNVERIFIED
        assert bridge.net_output_atoms is None

    def test_reverted_call_fails_closed(self) -> None:
        plan = _make_mock_plan()
        bridge = ArcOutputAdapter.adapt_with_evidence(
            call_succeeded=False,
            plan=plan,
            evidence=None,
            revert_reason="INSUFFICIENT_OUTPUT_AMOUNT",
        )

        assert bridge.call_succeeded is False
        assert bridge.output_verified is False
        assert bridge.status == SimulationStatus.CONTRACT_REVERT
        assert bridge.net_output_atoms is None
        assert bridge.execution_revert_reason == "INSUFFICIENT_OUTPUT_AMOUNT"

    def test_certified_evidence_elevates_verified_net_profit(self) -> None:
        plan = _make_mock_plan()
        verified_evidence = OutputEvidenceVerifier.verify_from_trace(
            evidence_id="ev_cert",
            plan_id=plan.plan_id,
            caller_address=CALLER,
            router_address=ROUTER,
            recipient_address=CALLER,
            base_token_address=USDC_ADDR,
            block_number=1000,
            state_hash="0x" + "11" * 32,
            trace_available=True,
            is_independent_poll=False,
            diffs=[
                StateDiffEntry(CALLER, USDC_ADDR, 100_000_000, 105_000_000),
            ],
            gas_attribution=TraceGasAttribution(
                gas_payer=CALLER,
                gas_used_atoms=100_000,
                effective_gas_price_atoms=10,  # 1_000_000 fee
                gas_included_in_diff=False,
            ),
        )

        bridge = ArcOutputAdapter.adapt_with_evidence(
            call_succeeded=True,
            plan=plan,
            evidence=verified_evidence,
            raw_gas_used=100_000,
        )

        assert bridge.call_succeeded is True
        assert bridge.output_verified is True
        assert bridge.status == SimulationStatus.CALL_SUCCEEDED
        # Net = (105_000_000 - 100_000_000) - 1_000_000 = 4_000_000
        assert bridge.net_output_atoms == 4_000_000
