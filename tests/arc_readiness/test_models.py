"""Unit tests covering arc_readiness domain models, validation, and DTO roundtrips."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from arc_readiness.errors import (
    ArcContractBridgeError,
    ArcDualInterfaceMismatchError,
    ArcEventDeduplicationError,
    ArcMarketIneligibleError,
    ArcNetworkMismatchError,
    ArcReadinessError,
    ArcValidationError,
)
from arc_readiness.models import (
    ARC_SYSTEM_TRANSFER_EMITTER,
    ARC_TESTNET_CHAIN_ID,
    ARC_USDC_ERC20_ADDRESS,
    ArcAssetEligibilityDraft,
    ArcBalanceObservation,
    ArcEventRecordDraft,
    ArcFeeObservation,
    ArcMarketEligibilityDraft,
    ArcNetworkIdentity,
    ArcPermissionStatus,
)


def test_exception_hierarchy() -> None:
    """Verify that all custom errors inherit from ArcReadinessError."""
    assert issubclass(ArcValidationError, (ArcReadinessError, ValueError))
    assert issubclass(ArcNetworkMismatchError, ArcReadinessError)
    assert issubclass(ArcDualInterfaceMismatchError, ArcReadinessError)
    assert issubclass(ArcEventDeduplicationError, ArcReadinessError)
    assert issubclass(ArcMarketIneligibleError, ArcReadinessError)
    assert issubclass(ArcContractBridgeError, ArcReadinessError)


# ==============================================================================
# 1. ArcNetworkIdentity Tests
# ==============================================================================


def test_network_identity_valid_and_roundtrip(sample_network_identity_data: dict[str, Any]) -> None:
    """Verify normal instantiation, immutability, and lossless dict roundtrip."""
    identity = ArcNetworkIdentity.from_dict(sample_network_identity_data)
    assert identity.chain_id == ARC_TESTNET_CHAIN_ID
    assert identity.network_name == "Arc Testnet"
    assert identity.erc20_usdc_address == ARC_USDC_ERC20_ADDRESS
    assert identity.native_decimals == 18
    assert identity.erc20_usdc_decimals == 6

    # Test immutability
    with pytest.raises(dataclasses.FrozenInstanceError):
        identity.chain_id = 9999  # type: ignore[misc]

    # Test roundtrip
    serialized = identity.to_dict()
    assert serialized == sample_network_identity_data
    reconstructed = ArcNetworkIdentity.from_dict(serialized)
    assert reconstructed == identity


@pytest.mark.parametrize(
    "patch_dict,expected_err",
    [
        ({"chain_id": 0}, "chain_id must be a positive integer"),
        ({"chain_id": -1}, "chain_id must be a positive integer"),
        ({"chain_id": True}, "chain_id must be a positive integer"),
        ({"chain_id": "5042002"}, "chain_id must be a positive integer"),
        ({"network_name": ""}, "network_name must be a non-empty string"),
        ({"network_name": 123}, "network_name must be a string"),
        ({"rpc_endpoint": "   "}, "rpc_endpoint must be a non-empty string"),
        ({"native_decimals": 0}, "native_decimals must be a positive integer"),
        (
            {"erc20_usdc_address": "0xinvalid"},
            "erc20_usdc_address must be a valid 42-char hex address",
        ),
        ({"verification_status": "bogus"}, "verification_status must be one of"),
        ({"known_stage": "invalid_stage"}, "known_stage must be one of"),
    ],
)
def test_network_identity_negative_cases(
    sample_network_identity_data: dict[str, Any], patch_dict: dict[str, Any], expected_err: str
) -> None:
    """Verify that invalid inputs raise ArcValidationError fail-closed."""
    payload = dict(sample_network_identity_data)
    payload.update(patch_dict)
    with pytest.raises(ArcValidationError, match=expected_err):
        ArcNetworkIdentity.from_dict(payload)


# ==============================================================================
# 2. ArcBalanceObservation Tests
# ==============================================================================


def test_balance_observation_valid_and_roundtrip(
    sample_balance_observation_data: dict[str, Any],
) -> None:
    """Verify normal instantiation, optional atoms, and roundtrip."""
    obs = ArcBalanceObservation.from_dict(sample_balance_observation_data)
    assert obs.native_atoms == 1000000000000000123
    assert obs.erc20_atoms == 1000000
    assert obs.dust_atoms == 123
    assert obs.verified_consistency is True

    serialized = obs.to_dict()
    assert serialized == sample_balance_observation_data
    assert ArcBalanceObservation.from_dict(serialized) == obs

    # Test None atoms allowed
    obs_none = ArcBalanceObservation(
        account_address="0x" + "22" * 20,
        chain_id=5042002,
        block_number=100,
        block_hash="0x" + "bb" * 32,
        native_atoms=None,
        erc20_atoms=None,
    )
    assert obs_none.native_atoms is None
    assert obs_none.erc20_atoms is None
    assert obs_none.verified_consistency is False


@pytest.mark.parametrize(
    "patch_dict,expected_err",
    [
        ({"account_address": "0x123"}, "account_address must be a valid 42-char hex address"),
        ({"account_address": 12345}, "account_address must be a string"),
        ({"chain_id": -5}, "chain_id must be a positive integer"),
        ({"block_number": -1}, "block_number must be a non-negative integer"),
        ({"block_number": True}, "block_number must be a non-negative integer"),
        ({"block_hash": "0x1234"}, "block_hash must be a valid 66-char hex bytes32"),
        ({"block_hash": "0x" + "zz" * 32}, "block_hash must be a valid 66-char hex bytes32"),
        ({"native_atoms": -10}, "native_atoms must be a non-negative integer"),
        ({"native_atoms": True}, "native_atoms must be a non-negative integer"),
        ({"native_atoms": "1000"}, "native_atoms must be a non-negative integer"),
        ({"erc20_atoms": -1}, "erc20_atoms must be a non-negative integer"),
        ({"dust_atoms": -5}, "dust_atoms must be a non-negative integer"),
        ({"verified_consistency": "yes"}, "verified_consistency must be a bool"),
        (
            {"stale_or_incomplete_reasons": [123]},
            "stale_or_incomplete_reasons\\[0\\] must be a string",
        ),
    ],
)
def test_balance_observation_negative_cases(
    sample_balance_observation_data: dict[str, Any], patch_dict: dict[str, Any], expected_err: str
) -> None:
    """Verify that malformed balance observation records fail-closed."""
    payload = dict(sample_balance_observation_data)
    payload.update(patch_dict)
    with pytest.raises(ArcValidationError, match=expected_err):
        ArcBalanceObservation.from_dict(payload)


# ==============================================================================
# 3. ArcFeeObservation Tests
# ==============================================================================


def test_fee_observation_valid_and_roundtrip() -> None:
    """Verify fee metrics observation and serialization."""
    data = {
        "chain_id": ARC_TESTNET_CHAIN_ID,
        "block_number": 99999,
        "block_hash": "0x" + "33" * 32,
        "gas_used": 21000,
        "effective_gas_price_wei": 21000000000,
        "total_fee_atoms": 441000000000000,
        "base_fee_gwei": 20,
        "priority_fee_gwei": 1,
        "fee_source": "receipt",
        "is_estimate": False,
    }
    fee = ArcFeeObservation.from_dict(data)
    assert fee.gas_used == 21000
    assert fee.total_fee_atoms == 441000000000000
    assert fee.to_dict() == data
    assert ArcFeeObservation.from_dict(fee.to_dict()) == fee

    # Negative checks
    with pytest.raises(ArcValidationError, match="fee_source must be one of"):
        ArcFeeObservation(
            chain_id=ARC_TESTNET_CHAIN_ID,
            block_number=1,
            block_hash="0x" + "44" * 32,
            fee_source="magic_oracle",
        )
    with pytest.raises(ArcValidationError, match="gas_used must be a non-negative integer"):
        ArcFeeObservation(
            chain_id=ARC_TESTNET_CHAIN_ID,
            block_number=1,
            block_hash="0x" + "44" * 32,
            gas_used=-50,
        )


# ==============================================================================
# 4. ArcPermissionStatus Tests
# ==============================================================================


def test_permission_status_valid_and_roundtrip() -> None:
    """Verify permission status DTO behavior."""
    data = {
        "account_address": "0x" + "aa" * 20,
        "target_contract": "0x" + "bb" * 20,
        "erc20_allowance_atoms": 5000000,
        "native_spending_authorized": False,
        "source_ref": "eth_call:allowance",
    }
    perm = ArcPermissionStatus.from_dict(data)
    assert perm.erc20_allowance_atoms == 5000000
    assert perm.native_spending_authorized is False
    assert perm.to_dict() == data
    assert ArcPermissionStatus.from_dict(perm.to_dict()) == perm

    with pytest.raises(
        ArcValidationError, match="erc20_allowance_atoms must be a non-negative integer"
    ):
        ArcPermissionStatus(
            account_address="0x" + "aa" * 20,
            target_contract="0x" + "bb" * 20,
            erc20_allowance_atoms=-100,
        )


# ==============================================================================
# 5. ArcAssetEligibilityDraft & ArcMarketEligibilityDraft Tests
# ==============================================================================


def test_asset_and_market_eligibility_drafts() -> None:
    """Verify asset and market candidate qualification models."""
    # Valid ERC-20 candidate
    asset_data = {
        "asset_id": "arc:eurc",
        "symbol": "EURC",
        "decimals": 6,
        "contract_address": "0x89b50855aa3be2f677cd6303cec089b5f319d72a",
        "interface_kind": "erc20",
        "review_status": "discovered",
        "decimals_status": "verified",
        "is_usdc_native_domain": False,
        "reasons": ["genesis_predeployed"],
    }
    asset = ArcAssetEligibilityDraft.from_dict(asset_data)
    assert asset.symbol == "EURC"
    assert asset.to_dict() == asset_data

    # ERC-20 must have contract_address
    with pytest.raises(ArcValidationError, match="ERC-20 asset must provide contract_address"):
        ArcAssetEligibilityDraft(
            asset_id="test",
            symbol="TEST",
            decimals=6,
            interface_kind="erc20",
            contract_address=None,
        )

    # Native must NOT have contract_address
    with pytest.raises(ArcValidationError, match="Native asset must not provide contract_address"):
        ArcAssetEligibilityDraft(
            asset_id="native:usdc",
            symbol="USDC",
            decimals=18,
            interface_kind="native",
            contract_address="0x" + "11" * 20,
        )

    # Market eligibility
    market_data = {
        "market_id": "arc:pool:usdc_eurc",
        "protocol_id": "uniswap_v3",
        "pool_address": "0x" + "55" * 20,
        "base_asset_id": "arc:usdc",
        "quote_asset_id": "arc:eurc",
        "can_quote": "supported",
        "can_simulate": "unknown",
        "can_atomic_execute": "unknown",
        "reasons": ["verified_depth_gt_0"],
    }
    market = ArcMarketEligibilityDraft.from_dict(market_data)
    assert market.can_quote == "supported"
    assert market.to_dict() == market_data

    # Base and quote assets cannot be identical
    with pytest.raises(
        ArcValidationError, match="Market base and quote assets cannot be identical"
    ):
        ArcMarketEligibilityDraft(
            market_id="invalid",
            protocol_id="uniswap_v3",
            pool_address="0x" + "55" * 20,
            base_asset_id="arc:usdc",
            quote_asset_id="arc:usdc",
        )


# ==============================================================================
# 6. ArcEventRecordDraft Tests
# ==============================================================================


def test_event_record_draft() -> None:
    """Verify event normalization and idempotency key computation."""
    evt_data = {
        "chain_id": ARC_TESTNET_CHAIN_ID,
        "block_number": 500,
        "block_hash": "0x" + "66" * 32,
        "tx_hash": "0x" + "77" * 32,
        "log_index": 3,
        "emitter_address": ARC_SYSTEM_TRANSFER_EMITTER,
        "event_type": "Transfer",
        "from_address": "0x" + "88" * 20,
        "to_address": "0x" + "99" * 20,
        "raw_value_atoms": 5000000000000000000,
        "decimals_view": 18,
        "is_system_emitter": True,
        "idempotency_key": "",
    }
    evt = ArcEventRecordDraft.from_dict(evt_data)
    assert evt.is_system_emitter is True
    # Verify auto-generated idempotency key
    expected_key = (
        f"{ARC_TESTNET_CHAIN_ID}:{evt.block_hash}:{evt.tx_hash}:3:{ARC_SYSTEM_TRANSFER_EMITTER}"
    )
    assert evt.idempotency_key == expected_key

    # Negative check: negative raw value
    with pytest.raises(ArcValidationError, match="raw_value_atoms must be a non-negative integer"):
        ArcEventRecordDraft(
            chain_id=ARC_TESTNET_CHAIN_ID,
            block_number=1,
            block_hash="0x" + "66" * 32,
            tx_hash="0x" + "77" * 32,
            log_index=0,
            emitter_address="0x" + "11" * 20,
            event_type="Transfer",
            from_address="0x" + "22" * 20,
            to_address="0x" + "33" * 20,
            raw_value_atoms=-1,
            decimals_view=18,
        )
