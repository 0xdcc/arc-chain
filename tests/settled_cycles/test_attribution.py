"""T11-T16 attribution tests for settled-cycle research."""

from __future__ import annotations

import unittest
from decimal import Decimal

from arbitrage_contracts.identity import AssetRef, TokenKey
from research.settled_cycles.attribution import AttributionResult, attribute_cycle
from research.settled_cycles.models import (
    ActionKind,
    AttributionStatus,
    CycleAction,
    EconomicStatus,
    ExecutionCostBreakdown,
    FeeComponentRecord,
    SubjectBalanceDelta,
    TransactionSubjects,
)

CHAIN_ID = 4663
STRATEGY = "0x1111111111111111111111111111111111111111"
PROVIDER = "0x2222222222222222222222222222222222222222"
FUNDER = "0x3333333333333333333333333333333333333333"
BUNDLER = "0x4444444444444444444444444444444444444444"
TOKEN_A = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TOKEN_B = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
NATIVE = "native"
SUBJECTS = TransactionSubjects(
    tx_origin=STRATEGY,
    executor_contract=STRATEGY,
    beneficiary=STRATEGY,
    gas_payer=STRATEGY,
    economic_bearer=STRATEGY,
)


def asset_by_name(asset_name: str) -> AssetRef:
    """Build the two synthetic assets used by the attribution matrix."""
    if asset_name == NATIVE:
        return AssetRef.native(CHAIN_ID, "native")
    return AssetRef.erc20(TokenKey(CHAIN_ID, asset_name))


def action(
    step_id: int,
    action_kind: ActionKind,
    asset_in: str,
    asset_out: str,
    amount_in: int,
    amount_out: int,
    direction: str,
) -> CycleAction:
    """Create a successful action with explicit endpoints and trace evidence."""
    return CycleAction(
        step_id=step_id,
        action_kind=action_kind,
        pool_key=None,
        asset_in=asset_by_name(asset_in),
        asset_out=asset_by_name(asset_out),
        amount_in_atoms=amount_in,
        amount_out_atoms=amount_out,
        direction=direction,
        log_index=step_id - 1,
        trace_address=str(step_id - 1),
        parent_step_id=None,
        execution_status="success",
        evidence_refs=(f"trace:{step_id - 1}",),
    )


def delta(
    subject: str,
    asset_name: str,
    amount: int,
    basis: str = "trace_transfer",
) -> SubjectBalanceDelta:
    """Create a complete subject balance delta."""
    return SubjectBalanceDelta(
        subject_address=subject,
        asset=asset_by_name(asset_name),
        delta_atoms=amount,
        basis=basis,
        completeness="complete",
        reconciliation_diff_atoms=0,
    )


def fee(
    component_id: str,
    *,
    asset_name: str = TOKEN_A,
    amount: int = 0,
    payer: str | None = STRATEGY,
    bearer: str | None = STRATEGY,
    counted_in: str = "none",
    dedup_key: str | None = None,
) -> FeeComponentRecord:
    """Create a unique independent fee component."""
    return FeeComponentRecord(
        component_id=component_id,
        asset=asset_by_name(asset_name),
        amount_atoms=amount,
        payer=payer,
        economic_bearer=bearer,
        source="trace",
        counted_in=counted_in,
        dedup_key=dedup_key or component_id,
    )


def cycle_swaps() -> tuple[CycleAction, ...]:
    """Create a successful A->B->A cycle."""
    return (
        action(1, ActionKind.SWAP, TOKEN_A, TOKEN_B, 1_000, 2_000, f"{STRATEGY}->{STRATEGY}"),
        action(2, ActionKind.SWAP, TOKEN_B, TOKEN_A, 2_000, 1_000, f"{STRATEGY}->{STRATEGY}"),
    )


def closed_deltas(strategy_delta: int) -> tuple[SubjectBalanceDelta, ...]:
    """Create a conservation-consistent two-asset delta vector."""
    return (
        delta(STRATEGY, TOKEN_A, strategy_delta),
        delta(STRATEGY, TOKEN_B, 0),
        delta(PROVIDER, TOKEN_A, -strategy_delta),
        delta(PROVIDER, TOKEN_B, 0),
    )


class AttributionTests(unittest.TestCase):
    """Independent tests locking every W3-D fail-closed defense line."""

    def _assert_net(
        self,
        result: AttributionResult,
        status: AttributionStatus,
        economic: EconomicStatus,
        net: int | None,
        usd: Decimal | None = None,
    ) -> None:
        self.assertEqual(status, result.attribution_status)
        self.assertEqual(economic, result.economic_status)
        self.assertEqual(net, result.attributed_net_atoms)
        self.assertEqual(usd, result.attributed_net_usd)

    def test_positive_zero_and_negative_closed_cycles(self) -> None:
        cases = (
            (20, EconomicStatus.POSITIVE),
            (0, EconomicStatus.NONPOSITIVE),
            (-7, EconomicStatus.NONPOSITIVE),
        )
        for net_value, expected in cases:
            with self.subTest(net_value=net_value):
                result = attribute_cycle(
                    cycle_swaps(),
                    closed_deltas(net_value),
                    SUBJECTS,
                    ExecutionCostBreakdown(()),
                    trace_available=True,
                    strategy_actor=STRATEGY,
                )
                self._assert_net(result, AttributionStatus.FULLY_ATTRIBUTED, expected, net_value)
                self.assertIn("cycle_candidate", result.reasons)
                self.assertEqual((), result.unexplained_flows)

    def test_missing_trace_is_fail_closed(self) -> None:
        result = attribute_cycle(
            cycle_swaps(),
            closed_deltas(100),
            SUBJECTS,
            ExecutionCostBreakdown(()),
            trace_available=False,
            strategy_actor=STRATEGY,
        )
        self._assert_net(
            result,
            AttributionStatus.UNVERIFIED_MISSING_TRACE,
            EconomicStatus.UNKNOWN,
            None,
        )
        self.assertIsNone(result.attributed_asset)
        self.assertIsNone(result.attributed_net_usd)

    def test_external_injection_does_not_mask_loss(self) -> None:
        actions = (
            action(1, ActionKind.TRANSFER, TOKEN_A, TOKEN_A, 15, 15, f"{FUNDER}->{STRATEGY}"),
            *cycle_swaps(),
        )
        subject_deltas = (
            delta(STRATEGY, TOKEN_A, 5),
            delta(FUNDER, TOKEN_A, -15),
            delta(PROVIDER, TOKEN_A, 10),
            delta(STRATEGY, TOKEN_B, 0),
            delta(FUNDER, TOKEN_B, 0),
            delta(PROVIDER, TOKEN_B, 0),
        )
        result = attribute_cycle(
            actions,
            subject_deltas,
            SUBJECTS,
            ExecutionCostBreakdown(()),
            trace_available=True,
            strategy_actor=STRATEGY,
        )
        self._assert_net(
            result,
            AttributionStatus.FULLY_ATTRIBUTED,
            EconomicStatus.NONPOSITIVE,
            -10,
        )
        self.assertIn("external_capital_separated:15", result.reasons)
        self.assertEqual(1, len(result.unexplained_flows))
        self.assertEqual(15, result.unexplained_flows[0].delta_atoms)

    def test_lp_mixture_is_ambiguous(self) -> None:
        actions = (
            action(1, ActionKind.MINT, TOKEN_A, TOKEN_B, 500, 500, f"{STRATEGY}->{PROVIDER}"),
            *cycle_swaps(),
        )
        result = attribute_cycle(
            actions,
            closed_deltas(100),
            SUBJECTS,
            ExecutionCostBreakdown(()),
            trace_available=True,
            strategy_actor=STRATEGY,
        )
        self._assert_net(
            result,
            AttributionStatus.AMBIGUOUS_COMPLEX_TX,
            EconomicStatus.UNKNOWN,
            None,
        )
        self.assertIn("unseparable_complex_action", result.reasons)

    def test_closed_flash_loan_excludes_principal_and_keeps_profit(self) -> None:
        actions = (
            action(1, ActionKind.BORROW, TOKEN_A, TOKEN_A, 1_000, 1_000, f"{PROVIDER}->{STRATEGY}"),
            action(2, ActionKind.REPAY, TOKEN_A, TOKEN_A, 1_000, 1_000, f"{STRATEGY}->{PROVIDER}"),
            *cycle_swaps(),
        )
        subject_deltas = (
            delta(STRATEGY, TOKEN_A, 20),
            delta(STRATEGY, TOKEN_B, 0),
            delta(PROVIDER, TOKEN_A, -20),
            delta(PROVIDER, TOKEN_B, 0),
        )
        result = attribute_cycle(
            actions,
            subject_deltas,
            SUBJECTS,
            ExecutionCostBreakdown(()),
            trace_available=True,
            strategy_actor=STRATEGY,
            flash_loan_providers=frozenset({PROVIDER}),
        )
        self._assert_net(result, AttributionStatus.FULLY_ATTRIBUTED, EconomicStatus.POSITIVE, 20)
        self.assertIn("flash_loan_principal_excluded", result.reasons)

    def test_open_flash_loan_is_partial(self) -> None:
        actions = (
            action(1, ActionKind.BORROW, TOKEN_A, TOKEN_A, 1_000, 1_000, f"{PROVIDER}->{STRATEGY}"),
            action(2, ActionKind.REPAY, TOKEN_A, TOKEN_A, 900, 900, f"{STRATEGY}->{PROVIDER}"),
            *cycle_swaps(),
        )
        result = attribute_cycle(
            actions,
            closed_deltas(1_100),
            SUBJECTS,
            ExecutionCostBreakdown(()),
            trace_available=True,
            strategy_actor=STRATEGY,
            flash_loan_providers=frozenset({PROVIDER}),
        )
        self._assert_net(
            result,
            AttributionStatus.PARTIALLY_ATTRIBUTED,
            EconomicStatus.UNKNOWN,
            None,
        )
        self.assertIn("flash_loan_debt_open:100", result.reasons)

    def test_unidentified_loan_counterparty_is_partial(self) -> None:
        actions = (
            action(1, ActionKind.BORROW, TOKEN_A, TOKEN_A, 1_000, 1_000, f"{PROVIDER}->{STRATEGY}"),
            action(2, ActionKind.REPAY, TOKEN_A, TOKEN_A, 1_000, 1_000, f"{STRATEGY}->{PROVIDER}"),
            *cycle_swaps(),
        )
        result = attribute_cycle(
            actions,
            closed_deltas(0),
            SUBJECTS,
            ExecutionCostBreakdown(()),
            trace_available=True,
            strategy_actor=STRATEGY,
        )
        self._assert_net(
            result,
            AttributionStatus.PARTIALLY_ATTRIBUTED,
            EconomicStatus.UNKNOWN,
            None,
        )
        self.assertIn("flash_loan_debt_unknown", result.reasons)

    def test_duplicate_dedup_key_is_inconsistent(self) -> None:
        costs = ExecutionCostBreakdown(
            (
                fee("fee-one", amount=5, dedup_key="gas"),
                fee("fee-two", amount=5, dedup_key="gas"),
            )
        )
        result = attribute_cycle(
            cycle_swaps(),
            closed_deltas(100),
            SUBJECTS,
            costs,
            trace_available=True,
            strategy_actor=STRATEGY,
        )
        self._assert_net(result, AttributionStatus.INCONSISTENT, EconomicStatus.UNKNOWN, None)
        self.assertIn("dedup_key_conflict:gas", result.reasons)

    def test_tip_counted_in_effective_gas_price_is_not_subtracted(self) -> None:
        costs = ExecutionCostBreakdown(
            (
                fee(
                    "tip",
                    asset_name=NATIVE,
                    amount=8,
                    counted_in="effective_gas_price",
                ),
            )
        )
        result = attribute_cycle(
            cycle_swaps(),
            closed_deltas(100),
            SUBJECTS,
            costs,
            trace_available=True,
            strategy_actor=STRATEGY,
        )
        self._assert_net(result, AttributionStatus.FULLY_ATTRIBUTED, EconomicStatus.POSITIVE, 100)

    def test_payer_bearer_split_and_unknown_bundle_bearer(self) -> None:
        costs = ExecutionCostBreakdown(
            (
                fee(
                    "third-party-gas",
                    asset_name=NATIVE,
                    amount=30,
                    payer=BUNDLER,
                    bearer=PROVIDER,
                ),
                fee("bundle-gas", asset_name=NATIVE, amount=20, payer=BUNDLER, bearer=None),
            )
        )
        result = attribute_cycle(
            cycle_swaps(),
            closed_deltas(100),
            SUBJECTS,
            costs,
            trace_available=True,
            strategy_actor=STRATEGY,
        )
        self._assert_net(
            result,
            AttributionStatus.PARTIALLY_ATTRIBUTED,
            EconomicStatus.UNKNOWN,
            100,
        )
        self.assertIn("fee_borne_by_other_subject", result.reasons)
        self.assertIn("fee_bearer_unknown", result.reasons)

    def test_missing_price_is_null(self) -> None:
        result = attribute_cycle(
            cycle_swaps(),
            closed_deltas(10),
            SUBJECTS,
            ExecutionCostBreakdown(()),
            trace_available=True,
            strategy_actor=STRATEGY,
            price_lookup={TOKEN_B: Decimal("2")},
        )
        self._assert_net(result, AttributionStatus.FULLY_ATTRIBUTED, EconomicStatus.POSITIVE, 10)
        self.assertIn("usd_price_missing", result.reasons)

    def test_decimal_price_is_used(self) -> None:
        result = attribute_cycle(
            cycle_swaps(),
            closed_deltas(10),
            SUBJECTS,
            ExecutionCostBreakdown(()),
            trace_available=True,
            strategy_actor=STRATEGY,
            price_lookup={TOKEN_A: Decimal("0.25")},
        )
        self._assert_net(
            result,
            AttributionStatus.FULLY_ATTRIBUTED,
            EconomicStatus.POSITIVE,
            10,
            Decimal("2.50"),
        )

    def test_nonzero_conservation_is_inconsistent(self) -> None:
        subject_deltas = (
            delta(STRATEGY, TOKEN_A, 100),
            delta(PROVIDER, TOKEN_A, -90),
            delta(STRATEGY, TOKEN_B, 0),
            delta(PROVIDER, TOKEN_B, 0),
        )
        result = attribute_cycle(
            cycle_swaps(),
            subject_deltas,
            SUBJECTS,
            ExecutionCostBreakdown(()),
            trace_available=True,
            strategy_actor=STRATEGY,
        )
        self._assert_net(result, AttributionStatus.INCONSISTENT, EconomicStatus.UNKNOWN, None)
        self.assertIn("conservation_failed:", result.reasons[0])


if __name__ == "__main__":
    unittest.main()
