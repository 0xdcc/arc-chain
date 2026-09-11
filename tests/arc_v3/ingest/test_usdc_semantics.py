"""Tests for T10: USDC Dual-Interface Balance Reconciliation, Gas Netting, and Asset Scoping."""

from __future__ import annotations

import pytest

from arc_readiness.balances import (
    SCALE_FACTOR,
    prevent_balance_double_counting,
    reconcile_dual_interface_balance,
    validate_spending_authorization,
)
from arc_readiness.eligibility import (
    ARC_CANONICAL_EURC_ADDRESS,
    evaluate_asset_eligibility,
    evaluate_market_eligibility,
)
from arc_readiness.errors import (
    ArcDualInterfaceMismatchError,
    ArcNetworkMismatchError,
    ArcValidationError,
)
from arc_readiness.fees import (
    apply_single_deduction_netting,
    calculate_receipt_fee_atoms,
)
from arc_readiness.profile_assets import (
    ARC_MAINNET_CHAIN_ID,
    ARC_TESTNET_CHAIN_ID,
    ARC_TESTNET_EURC_ERC20,
    ArcAssetRegistry,
)

_SAMPLE_ADDR = "0x1111111111111111111111111111111111111111"
_SAMPLE_HASH = "0x" + "aa" * 32


class TestUSDCSemanticsAndGasNetting:
    """Test suite for T10 dual-interface USDC rules, single-deduction netting, and asset guards."""

    # 1. Dual-Interface Balance Invariants & Double Counting Rejection
    def test_reconcile_dual_interface_balance_exact_match(self) -> None:
        # Native: 2.000000123456789000 USDC (18 decimals)
        # ERC20: 2.000000 USDC (6 decimals) -> 2_000_000
        # Dust: 123_456_789_000 atoms
        native_atoms = 2_000_000_123_456_789_000
        erc20_atoms = 2_000_000
        expected_dust = 123_456_789_000

        obs = reconcile_dual_interface_balance(
            account=_SAMPLE_ADDR,
            chain_id=5042,
            block_number=100,
            block_hash=_SAMPLE_HASH,
            native_atoms=native_atoms,
            erc20_atoms=erc20_atoms,
            strict_raise=True,
        )
        assert obs.verified_consistency is True
        assert obs.dust_atoms == expected_dust
        assert obs.erc20_atoms == erc20_atoms

    def test_reconcile_dual_interface_dust_boundary_zero_erc20(self) -> None:
        # ERC20 is 0, but Native has 500 atoms of dust (real gas buying power!)
        native_atoms = 500
        erc20_atoms = 0

        obs = reconcile_dual_interface_balance(
            account=_SAMPLE_ADDR,
            chain_id=5042,
            block_number=100,
            block_hash=_SAMPLE_HASH,
            native_atoms=native_atoms,
            erc20_atoms=erc20_atoms,
            strict_raise=True,
        )
        assert obs.verified_consistency is True
        assert obs.dust_atoms == 500
        assert obs.native_atoms == 500

    def test_prevent_balance_double_counting_rejects_summing(self) -> None:
        # Attempting to sum dual-interface balance views is fatal:
        with pytest.raises(ArcValidationError, match="Fatal double-counting violation"):
            prevent_balance_double_counting(native_atoms=10**18, erc20_atoms=10**6)

    def test_spending_authorization_native_isolated_from_allowance(self) -> None:
        # ERC-20 allowance does not grant native spending authorization:
        with pytest.raises(ArcValidationError, match="ERC-20 allowance does not grant native"):
            validate_spending_authorization(
                interface_kind="native",
                has_erc20_allowance=True,
                has_native_authorization=False,
            )

        # Explicit native authorization passes:
        validate_spending_authorization(
            interface_kind="native",
            has_erc20_allowance=False,
            has_native_authorization=True,
        )

    # 2. Single-Deduction Netting & Gas Guarantees
    def test_single_deduction_netting_success(self) -> None:
        amount_in = 1_000_000  # 1 USDC in (atoms)
        quote_out = 1_050_000  # Gross out
        gas_cost = 10_000

        net_profit, output_floor = apply_single_deduction_netting(
            gross_quote_out_atoms=quote_out,
            amount_in_atoms=amount_in,
            gas_cost_atoms=gas_cost,
            dex_fee_already_deducted_in_quoter=True,
            gas_already_deducted=False,
        )
        assert net_profit == 40_000  # 1_050_000 - 1_000_000 - 10_000
        assert output_floor == amount_in + gas_cost + 1

    def test_single_deduction_netting_rejects_double_deduction(self) -> None:
        with pytest.raises(ArcValidationError, match="Double-deduction violation: Gas cost has already been deducted"):
            apply_single_deduction_netting(
                gross_quote_out_atoms=1_050_000,
                amount_in_atoms=1_000_000,
                gas_cost_atoms=10_000,
                gas_already_deducted=True,  # double deduction!
            )

    # 3. Asset Scoping & Self-Pairing Rejection
    def test_testnet_eurc_constant_rejected_on_mainnet_without_audit(self) -> None:
        # On mainnet 5042, EURC testnet constant is NOT automatically verified
        draft = evaluate_asset_eligibility(
            asset_id="arc:eurc",
            symbol="EURC",
            decimals=6,
            interface_kind="erc20",
            chain_id=ARC_MAINNET_CHAIN_ID,
            contract_address=ARC_CANONICAL_EURC_ADDRESS,
            independent_audit_proof=None,
        )
        assert draft.review_status != "verified"
        assert "EURC_TESTNET_CONSTANT_REQUIRES_MAINNET_AUDIT_PROOF" in draft.reasons

    def test_asset_registry_rejects_testnet_assets_on_mainnet(self) -> None:
        registry = ArcAssetRegistry(chain_id=ARC_MAINNET_CHAIN_ID)
        with pytest.raises(ArcNetworkMismatchError, match="Testnet asset constant.*cannot be automatically verified"):
            registry.assert_asset_allowed(ARC_TESTNET_EURC_ERC20, target_chain_id=ARC_MAINNET_CHAIN_ID)

    def test_self_pairing_same_balance_domain_rejected(self) -> None:
        from arc_readiness.models import ARC_USDC_ERC20_ADDRESS
        native_usdc = evaluate_asset_eligibility(
            asset_id="arc:native_usdc",
            symbol="USDC_NATIVE",
            decimals=18,
            interface_kind="native",
            chain_id=ARC_MAINNET_CHAIN_ID,
        )
        erc20_usdc = evaluate_asset_eligibility(
            asset_id="arc:erc20_usdc",
            symbol="USDC",
            decimals=6,
            interface_kind="erc20",
            chain_id=ARC_MAINNET_CHAIN_ID,
            contract_address=ARC_USDC_ERC20_ADDRESS,
        )
        assert native_usdc.is_usdc_native_domain is True
        assert erc20_usdc.is_usdc_native_domain is True

        market = evaluate_market_eligibility(
            market_id="fake_loop_pool",
            protocol_id="uniswap_v3",
            pool_address=_SAMPLE_ADDR,
            asset0=native_usdc,
            asset1=erc20_usdc,
            chain_id=ARC_MAINNET_CHAIN_ID,
        )
        assert market.can_quote == "unsupported"
        assert "SELF_PAIRING_SAME_BALANCE_DOMAIN_REJECTED" in market.reasons
