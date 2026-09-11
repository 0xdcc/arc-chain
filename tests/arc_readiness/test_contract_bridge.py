"""Tests covering contract bridging from Arc models to arbitrage_contracts (C26)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arbitrage_contracts.identity import AssetRef, TokenKey
from arc_readiness.contract_bridge import (
    bridge_balance_observation_to_amounts,
    bridge_to_public_amount,
    bridge_to_public_asset_ref,
    bridge_to_public_pool_key,
    bridge_to_public_state_version,
)
from arc_readiness.eligibility import (
    evaluate_asset_eligibility,
    evaluate_market_eligibility,
)
from arc_readiness.errors import ArcContractBridgeError
from arc_readiness.models import (
    ARC_TESTNET_CHAIN_ID,
    ARC_USDC_ERC20_ADDRESS,
    ArcBalanceObservation,
)

BLOCK_HASH = "0x" + "aa" * 32
POOL_ADDR = "0x" + "bb" * 20


def test_c26_bridge_assets_to_public_contracts() -> None:
    """C26: Validates mapping Arc asset drafts to public AssetRef and TokenKey."""
    native_draft = evaluate_asset_eligibility(
        asset_id="arc:usdc_native",
        symbol="USDC",
        decimals=18,
        interface_kind="native",
    )
    erc20_draft = evaluate_asset_eligibility(
        asset_id="arc:usdc_erc20",
        symbol="USDC",
        decimals=6,
        interface_kind="erc20",
        contract_address=ARC_USDC_ERC20_ADDRESS,
    )

    pub_native = bridge_to_public_asset_ref(native_draft, ARC_TESTNET_CHAIN_ID)
    pub_erc20 = bridge_to_public_asset_ref(erc20_draft, ARC_TESTNET_CHAIN_ID)

    assert pub_native.interface_kind == "native"
    assert pub_native.chain_id == ARC_TESTNET_CHAIN_ID
    assert pub_native.native_identifier == "USDC"
    assert pub_native.token_key is None
    assert pub_native.balance_domain_id == f"arc:{ARC_TESTNET_CHAIN_ID}:usdc_canonical"

    assert pub_erc20.interface_kind == "erc20"
    assert pub_erc20.chain_id == ARC_TESTNET_CHAIN_ID
    assert isinstance(pub_erc20.token_key, TokenKey)
    assert pub_erc20.token_key.address == ARC_USDC_ERC20_ADDRESS
    assert pub_erc20.balance_domain_id == f"arc:{ARC_TESTNET_CHAIN_ID}:usdc_canonical"

    # Crucial domain equality: both views share the same balance domain ID
    assert pub_native.balance_domain_id == pub_erc20.balance_domain_id


def test_c26_bridge_state_version_mandatory_l1() -> None:
    """C26: Arc StateVersion mandatory block_domain='l1' enforcement."""
    state = bridge_to_public_state_version(
        chain_id=ARC_TESTNET_CHAIN_ID,
        block_number=1000,
        block_hash=BLOCK_HASH,
        received_at_ms=1757500000000,
    )
    assert state.chain_id == ARC_TESTNET_CHAIN_ID
    assert state.block_number == 1000
    assert state.block_domain == "l1", "Arc StateVersion must strictly be l1"
    assert state.finality == "unknown"


def test_c26_bridge_pool_key() -> None:
    """C26: Bridge market draft to public PoolKey."""
    native_draft = evaluate_asset_eligibility(
        asset_id="arc:usdc_native",
        symbol="USDC",
        decimals=18,
        interface_kind="native",
    )
    eurc_draft = evaluate_asset_eligibility(
        asset_id="arc:eurc",
        symbol="EURC",
        decimals=6,
        interface_kind="erc20",
        contract_address="0x" + "33" * 20,
    )
    market_draft = evaluate_market_eligibility(
        market_id="v3_pool",
        protocol_id="uniswap_v3",
        pool_address=POOL_ADDR,
        base_asset=native_draft,
        quote_asset=eurc_draft,
    )
    pool_key = bridge_to_public_pool_key(market_draft, ARC_TESTNET_CHAIN_ID)
    assert pool_key.chain_id == ARC_TESTNET_CHAIN_ID
    assert pool_key.protocol_id == "uniswap_v3"
    assert pool_key.venue_address == POOL_ADDR
    assert pool_key.pool_id == POOL_ADDR


def test_c26_bridge_balance_observation_to_amounts() -> None:
    """C26: Convert ArcBalanceObservation into public Amount dictionary."""
    native_draft = evaluate_asset_eligibility(
        asset_id="arc:usdc_native",
        symbol="USDC",
        decimals=18,
        interface_kind="native",
    )
    erc20_draft = evaluate_asset_eligibility(
        asset_id="arc:usdc_erc20",
        symbol="USDC",
        decimals=6,
        interface_kind="erc20",
        contract_address=ARC_USDC_ERC20_ADDRESS,
    )
    obs = ArcBalanceObservation(
        account_address="0x" + "11" * 20,
        chain_id=ARC_TESTNET_CHAIN_ID,
        block_number=500,
        block_hash=BLOCK_HASH,
        native_atoms=10**18,
        erc20_atoms=10**6,
    )
    amounts = bridge_balance_observation_to_amounts(obs, native_draft, erc20_draft)
    assert amounts["native"] is not None
    assert amounts["native"].atoms == 10**18
    assert amounts["erc20"] is not None
    assert amounts["erc20"].atoms == 10**6


def test_c26_missing_contract_address_raises() -> None:
    """C26: Attempting to bridge ERC20 draft without address raises ArcContractBridgeError."""
    # Construct invalid draft manually via __post_init__ bypass simulation
    from arc_readiness.models import ArcAssetEligibilityDraft

    invalid_erc20 = object.__new__(ArcAssetEligibilityDraft)
    object.__setattr__(invalid_erc20, "asset_id", "bad")
    object.__setattr__(invalid_erc20, "symbol", "BAD")
    object.__setattr__(invalid_erc20, "decimals", 18)
    object.__setattr__(invalid_erc20, "contract_address", None)
    object.__setattr__(invalid_erc20, "interface_kind", "erc20")
    object.__setattr__(invalid_erc20, "is_usdc_native_domain", False)

    with pytest.raises(ArcContractBridgeError, match="lacks contract address"):
        bridge_to_public_asset_ref(invalid_erc20, ARC_TESTNET_CHAIN_ID)


def test_fixture_driven_bridge_vectors() -> None:
    """Verify test cases from bridge_vectors.json fixture."""
    fixture_path = (
        Path(__file__).resolve().parents[1] / "fixtures" / "arc_readiness" / "bridge_vectors.json"
    )
    data = json.loads(fixture_path.read_text(encoding="utf-8"))

    state_vec = next(v for v in data["vectors"] if v["id"] == "C26_public_state_version")
    state = bridge_to_public_state_version(
        chain_id=data["chain_id"],
        block_number=state_vec["block_number"],
        block_hash=state_vec["block_hash"],
        received_at_ms=1000,
    )
    assert state.block_domain == state_vec["expected_block_domain"]
