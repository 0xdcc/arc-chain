"""Independent Verification Test Suite for Arc Dual-Interface Accounting Semantics (T46 / G2).

Audits and proves:
1. Dual-interface USDC balance reconciliation (18d native vs 6d ERC-20, dust tracking).
2. Strict prohibition of balance double-counting (summing native and ERC-20 atoms).
3. One-sided balance observation semantics (native derives ERC-20+dust, ERC-20 leaves dust unknown).
4. Dual-emitted EIP-7708 system transfer vs ERC-20 transfer deduplication.
5. Preservation of distinct multiple transfers in the same transaction.
6. Gas fee receipt calculation in 18d native USDC atoms.
7. Single-deduction netting guard (prevents double gas deduction and unhandled DEX fee).
8. Native spending authority physical decoupling from ERC-20 allowance (no allowance bypass).
9. Cross-network profile isolation (mainnet 5042 vs testnet 5042002).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from arc_readiness.balances import (
    SCALE_FACTOR,
    calculate_native_bounds_from_erc20,
    check_spending_permission,
    prevent_balance_double_counting,
    reconcile_dual_interface_balance,
    validate_spending_authorization,
)
from arc_readiness.errors import (
    ArcDualInterfaceMismatchError,
    ArcNetworkMismatchError,
    ArcValidationError,
)
from arc_readiness.events import (
    ArcEventDeduplicationError,
    ArcEventJournal,
    deduplicate_transaction_events,
)
from arc_readiness.fees import (
    apply_single_deduction_netting,
    calculate_receipt_fee_atoms,
)
from arc_readiness.models import (
    ArcEventRecordDraft,
    ArcPermissionStatus,
)
from arc_readiness.network import (
    ARC_MAINNET_CHAIN_ID,
    ARC_TESTNET_CHAIN_ID,
)
from arc_readiness.profiles import (
    assert_venue_profile_isolation,
    get_mainnet_profile,
    get_testnet_profile,
)


class TestDualInterfaceBalanceReconciliation:
    """Verifies dual-interface 18d native vs 6d ERC-20 reconciliation and anti-double-counting."""

    ACCOUNT = "0x" + "11" * 20
    BLOCK_HASH = "0x" + "aa" * 32

    def test_consistent_dual_balances_verified(self) -> None:
        # 12.345678 USDC = 12,345,678 * 10^12 atoms native, plus 999_999 dust
        erc20 = 12_345_678
        dust = 999_999
        native = (erc20 * SCALE_FACTOR) + dust

        obs = reconcile_dual_interface_balance(
            account=self.ACCOUNT,
            chain_id=ARC_MAINNET_CHAIN_ID,
            block_number=1000,
            block_hash=self.BLOCK_HASH,
            native_atoms=native,
            erc20_atoms=erc20,
            strict_raise=True,
        )

        assert obs.verified_consistency is True
        assert obs.native_atoms == native
        assert obs.erc20_atoms == erc20
        assert obs.dust_atoms == dust
        assert len(obs.stale_or_incomplete_reasons) == 0

    def test_inconsistent_dual_balances_fails_closed(self) -> None:
        erc20 = 12_345_678
        native = (erc20 * SCALE_FACTOR)  # exact match
        corrupted_erc20 = erc20 + 1

        # Strict raise mode must throw ArcDualInterfaceMismatchError
        with pytest.raises(ArcDualInterfaceMismatchError, match="Dual-interface balance mismatch"):
            reconcile_dual_interface_balance(
                account=self.ACCOUNT,
                chain_id=ARC_MAINNET_CHAIN_ID,
                block_number=1000,
                block_hash=self.BLOCK_HASH,
                native_atoms=native,
                erc20_atoms=corrupted_erc20,
                strict_raise=True,
            )

        # Non-strict mode must flag verified_consistency as False
        obs = reconcile_dual_interface_balance(
            account=self.ACCOUNT,
            chain_id=ARC_MAINNET_CHAIN_ID,
            block_number=1000,
            block_hash=self.BLOCK_HASH,
            native_atoms=native,
            erc20_atoms=corrupted_erc20,
            strict_raise=False,
        )
        assert obs.verified_consistency is False
        assert any("mismatch" in r.lower() for r in obs.stale_or_incomplete_reasons)

    def test_double_counting_prevention_fails_closed(self) -> None:
        native = 10 * SCALE_FACTOR
        erc20 = 10
        with pytest.raises(ArcValidationError, match="Fatal double-counting violation"):
            prevent_balance_double_counting(native, erc20)

    def test_native_only_observation_derives_erc20_and_dust(self) -> None:
        dust = 456_789
        native = (5 * SCALE_FACTOR) + dust

        obs = reconcile_dual_interface_balance(
            account=self.ACCOUNT,
            chain_id=ARC_MAINNET_CHAIN_ID,
            block_number=1000,
            block_hash=self.BLOCK_HASH,
            native_atoms=native,
            erc20_atoms=None,
        )
        assert obs.verified_consistency is True
        assert obs.dust_atoms == dust
        assert obs.native_atoms == native
        assert obs.erc20_atoms is None
        assert "derived_erc20_from_native" in obs.stale_or_incomplete_reasons

    def test_erc20_only_observation_leaves_dust_unknown(self) -> None:
        # Crucial anti-assumption: When only 6d ERC-20 is known, dust CANNOT be assumed 0!
        erc20 = 5_000_000

        obs = reconcile_dual_interface_balance(
            account=self.ACCOUNT,
            chain_id=ARC_MAINNET_CHAIN_ID,
            block_number=1000,
            block_hash=self.BLOCK_HASH,
            native_atoms=None,
            erc20_atoms=erc20,
        )
        assert obs.verified_consistency is True
        assert obs.dust_atoms is None  # Must NOT assume 0
        assert "bounded_native_from_erc20" in obs.stale_or_incomplete_reasons

        # Verify bounds calculation
        min_n, max_n = calculate_native_bounds_from_erc20(erc20)
        assert min_n == 5_000_000 * SCALE_FACTOR
        assert max_n == (5_000_001 * SCALE_FACTOR) - 1

    def test_spending_permission_native_vs_erc20(self) -> None:
        target = "0x" + "22" * 20
        # Native balance allows direct execution without approval
        status_native = check_spending_permission(
            account=self.ACCOUNT,
            target=target,
            required_atoms=1000,
            is_native=True,
            permission_status=ArcPermissionStatus(
                account_address=self.ACCOUNT,
                target_contract=target,
                erc20_allowance_atoms=None,
                native_spending_authorized=True,
            ),
        )
        assert status_native is True

        # ERC20 transfer with insufficient allowance fails closed
        status_erc20_fail = check_spending_permission(
            account=self.ACCOUNT,
            target=target,
            required_atoms=1000,
            is_native=False,
            permission_status=ArcPermissionStatus(
                account_address=self.ACCOUNT,
                target_contract=target,
                erc20_allowance_atoms=500,  # only 500 allowed
                native_spending_authorized=False,
            ),
        )
        assert status_erc20_fail is False

    def test_native_spending_authorization_rejects_erc20_allowance_bypass(self) -> None:
        # Crucial security test: ERC-20 allowance MUST NOT be used to bypass native authorization
        with pytest.raises(ArcValidationError, match="ERC-20 allowance does not grant native"):
            validate_spending_authorization(
                interface_kind="native",
                has_erc20_allowance=True,
                has_native_authorization=False,
            )


class TestDualEmittedEventDeduplication:
    """Verifies collapsing of dual-emitted EIP-7708 system vs ERC-20 transfer events."""

    TX_HASH = "0x" + "99" * 32
    SENDER = "0x" + "aa" * 20
    RECEIVER = "0x" + "bb" * 20

    def test_collapses_matched_system_and_erc20_pair(self) -> None:
        # 10 USDC transfer emits:
        # 1. ERC-20 contract log (6-decimal, 10_000_000)
        # 2. System EIP-7708 log (18-decimal, 10_000_000 * 10^12)
        erc20_event = ArcEventRecordDraft(
            chain_id=ARC_MAINNET_CHAIN_ID,
            block_number=500,
            block_hash="0x" + "11" * 32,
            tx_hash=self.TX_HASH,
            log_index=10,
            emitter_address="0x" + "33" * 20,
            event_type="Transfer",
            from_address=self.SENDER,
            to_address=self.RECEIVER,
            raw_value_atoms=10_000_000,
            decimals_view=6,
            is_system_emitter=False,
        )
        system_event = ArcEventRecordDraft(
            chain_id=ARC_MAINNET_CHAIN_ID,
            block_number=500,
            block_hash="0x" + "11" * 32,
            tx_hash=self.TX_HASH,
            log_index=11,
            emitter_address="0x" + "00" * 20,  # System address
            event_type="Transfer",
            from_address=self.SENDER,
            to_address=self.RECEIVER,
            raw_value_atoms=10_000_000 * SCALE_FACTOR,
            decimals_view=18,
            is_system_emitter=True,
        )

        deduped = deduplicate_transaction_events([erc20_event, system_event])
        # Must collapse to single 18-decimal system event
        assert len(deduped) == 1
        assert deduped[0].is_system_emitter is True
        assert deduped[0].raw_value_atoms == 10_000_000 * SCALE_FACTOR
        assert deduped[0].decimals_view == 18

    def test_preserves_distinct_multiple_transfers_same_tx(self) -> None:
        # Two distinct transfers to same receiver in single tx:
        # transfer 1: 5 USDC
        # transfer 2: 7 USDC
        sys1 = ArcEventRecordDraft(
            chain_id=ARC_MAINNET_CHAIN_ID,
            block_number=500,
            block_hash="0x" + "11" * 32,
            tx_hash=self.TX_HASH,
            log_index=1,
            emitter_address="0x" + "00" * 20,
            event_type="Transfer",
            from_address=self.SENDER,
            to_address=self.RECEIVER,
            raw_value_atoms=5_000_000 * SCALE_FACTOR,
            decimals_view=18,
            is_system_emitter=True,
        )
        sys2 = ArcEventRecordDraft(
            chain_id=ARC_MAINNET_CHAIN_ID,
            block_number=500,
            block_hash="0x" + "11" * 32,
            tx_hash=self.TX_HASH,
            log_index=2,
            emitter_address="0x" + "00" * 20,
            event_type="Transfer",
            from_address=self.SENDER,
            to_address=self.RECEIVER,
            raw_value_atoms=7_000_000 * SCALE_FACTOR,
            decimals_view=18,
            is_system_emitter=True,
        )
        erc1 = ArcEventRecordDraft(
            chain_id=ARC_MAINNET_CHAIN_ID,
            block_number=500,
            block_hash="0x" + "11" * 32,
            tx_hash=self.TX_HASH,
            log_index=3,
            emitter_address="0x" + "33" * 20,
            event_type="Transfer",
            from_address=self.SENDER,
            to_address=self.RECEIVER,
            raw_value_atoms=5_000_000,
            decimals_view=6,
            is_system_emitter=False,
        )
        erc2 = ArcEventRecordDraft(
            chain_id=ARC_MAINNET_CHAIN_ID,
            block_number=500,
            block_hash="0x" + "11" * 32,
            tx_hash=self.TX_HASH,
            log_index=4,
            emitter_address="0x" + "33" * 20,
            event_type="Transfer",
            from_address=self.SENDER,
            to_address=self.RECEIVER,
            raw_value_atoms=7_000_000,
            decimals_view=6,
            is_system_emitter=False,
        )

        deduped = deduplicate_transaction_events([sys1, sys2, erc1, erc2])
        assert len(deduped) == 2
        values = sorted([d.raw_value_atoms for d in deduped])
        assert values == [5_000_000 * SCALE_FACTOR, 7_000_000 * SCALE_FACTOR]

    def test_event_journal_rejects_duplicate_idempotency_key(self) -> None:
        journal = ArcEventJournal()
        evt = ArcEventRecordDraft(
            chain_id=ARC_MAINNET_CHAIN_ID,
            block_number=500,
            block_hash="0x" + "11" * 32,
            tx_hash=self.TX_HASH,
            log_index=10,
            emitter_address="0x" + "33" * 20,
            event_type="Transfer",
            from_address=self.SENDER,
            to_address=self.RECEIVER,
            raw_value_atoms=1000,
            decimals_view=6,
            is_system_emitter=False,
        )
        assert journal.record_event(evt, strict_raise=True) is True
        # Exact same event re-recorded
        assert journal.record_event(evt, strict_raise=False) is False
        with pytest.raises(ArcEventDeduplicationError, match="Duplicate event rejected"):
            journal.record_event(evt, strict_raise=True)


class TestArcGasAndFeeAccounting:
    """Verifies fee accounting in 18d native atoms and single-deduction netting."""

    BLOCK_HASH = "0x" + "bb" * 32

    def test_receipt_fee_atoms_calculation(self) -> None:
        gas_used = 150_000
        effective_gas_price = 2_000_000_000  # 2 Gwei

        fee_obs = calculate_receipt_fee_atoms(
            gas_used=gas_used,
            effective_gas_price_wei=effective_gas_price,
            chain_id=ARC_MAINNET_CHAIN_ID,
            block_number=2000,
            block_hash=self.BLOCK_HASH,
            base_fee_gwei=1,
            priority_fee_gwei=1,
        )

        expected_atoms = 150_000 * 2_000_000_000
        assert fee_obs.total_fee_atoms == expected_atoms
        assert fee_obs.fee_source == "receipt"
        assert fee_obs.is_estimate is False

    def test_single_deduction_netting_success(self) -> None:
        gross_out = 105 * SCALE_FACTOR  # 105 USDC
        amount_in = 100 * SCALE_FACTOR  # 100 USDC
        gas_cost = int(Decimal("0.05") * Decimal(SCALE_FACTOR))  # 0.05 USDC gas

        net_profit, output_floor = apply_single_deduction_netting(
            gross_quote_out_atoms=gross_out,
            amount_in_atoms=amount_in,
            gas_cost_atoms=gas_cost,
            dex_fee_already_deducted_in_quoter=True,
            gas_already_deducted=False,
        )

        # Net profit = 105 - 100 - 0.05 = 4.95 USDC in atoms
        assert net_profit == (5 * SCALE_FACTOR) - gas_cost
        # Output floor enforces principal + gas + 1 atom (> 0 profit condition)
        assert output_floor == amount_in + gas_cost + 1

    def test_double_gas_deduction_rejected(self) -> None:
        with pytest.raises(ArcValidationError, match="Double-deduction violation"):
            apply_single_deduction_netting(
                gross_quote_out_atoms=1000,
                amount_in_atoms=900,
                gas_cost_atoms=50,
                dex_fee_already_deducted_in_quoter=True,
                gas_already_deducted=True,  # Prohibited double deduction
            )

    def test_unhandled_dex_fee_rejected(self) -> None:
        with pytest.raises(ArcValidationError, match="Unsupported fee configuration"):
            apply_single_deduction_netting(
                gross_quote_out_atoms=1000,
                amount_in_atoms=900,
                gas_cost_atoms=50,
                dex_fee_already_deducted_in_quoter=False,  # Quoter did not account for fee
                gas_already_deducted=False,
            )


class TestNetworkProfileAndVenueIsolation:
    """Verifies network profile parameters and mainnet vs testnet isolation."""

    def test_mainnet_and_testnet_chain_ids(self) -> None:
        mainnet = get_mainnet_profile()
        testnet = get_testnet_profile()

        assert mainnet.chain_id == ARC_MAINNET_CHAIN_ID == 5042
        assert testnet.chain_id == ARC_TESTNET_CHAIN_ID == 5042002
        assert mainnet.chain_id != testnet.chain_id

    def test_cross_network_mixing_detected_and_rejected(self) -> None:
        # Passing mismatched chain IDs to venue isolation assertion
        with pytest.raises(ArcNetworkMismatchError, match="Cross-network venue isolation violation"):
            assert_venue_profile_isolation(
                venue_address="0x" + "44" * 20,
                chain_id_a=ARC_MAINNET_CHAIN_ID,
                chain_id_b=ARC_TESTNET_CHAIN_ID,
            )
