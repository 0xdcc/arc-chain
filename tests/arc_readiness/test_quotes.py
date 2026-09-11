"""Tests covering single-hop bidirectional quoting, zero-output truncation, and capability promotion (C24, C25, C27)."""

from __future__ import annotations

import pytest

from arbitrage_contracts.identity import Amount, AssetRef, PoolKey, TokenKey
from arc_readiness.errors import ArcValidationError
from arc_readiness.models import ARC_TESTNET_CHAIN_ID, ARC_USDC_ERC20_ADDRESS
from arc_readiness.quotes import (
    SingleHopQuoteRequest,
    SingleHopQuoteResult,
    execute_bidirectional_single_hop_quotes,
)

BLOCK_HASH = "0x" + "aa" * 32
POOL_KEY = PoolKey(
    chain_id=ARC_TESTNET_CHAIN_ID,
    protocol_id="uniswap_v3",
    venue_kind="factory",
    venue_address="0x" + "bb" * 20,
    pool_id_kind="address",
    pool_id="0x" + "bb" * 20,
)
ASSET0 = AssetRef.erc20(TokenKey(ARC_TESTNET_CHAIN_ID, ARC_USDC_ERC20_ADDRESS))
ASSET1 = AssetRef.erc20(TokenKey(ARC_TESTNET_CHAIN_ID, "0x" + "33" * 20))


# ==============================================================================
# C24: Bidirectional Independent Quoting & Reverse Inversion Sabotage
# ==============================================================================


def test_c24_bidirectional_independent_quoting() -> None:
    """C24: zero_for_one and one_for_zero are called independently; no mathematical inverse shortcut."""
    calls_made: list[tuple[str, int]] = []

    def mock_quoter(direction: str, amount_in: Amount) -> tuple[int, int | None]:
        calls_made.append((direction, amount_in.atoms))
        if direction == "zero_for_one":
            return (980_000, 100_000)  # 0.98 EURC out for 1 USDC in
        elif direction == "one_for_zero":
            return (1_010_000, 100_000)  # 1.01 USDC out for 1 EURC in
        raise ValueError(f"Unknown direction: {direction}")

    amt_in0 = Amount(asset_ref=ASSET0, atoms=1_000_000, decimals=6)
    amt_in1 = Amount(asset_ref=ASSET1, atoms=1_000_000, decimals=6)

    res_0_to_1, res_1_to_0 = execute_bidirectional_single_hop_quotes(
        pool_key=POOL_KEY,
        asset0=ASSET0,
        asset1=ASSET1,
        amount_in0=amt_in0,
        amount_in1=amt_in1,
        block_number=100,
        block_hash=BLOCK_HASH,
        quoter_fn=mock_quoter,
    )

    # Both directions must be independently called
    assert len(calls_made) == 2
    assert calls_made[0] == ("zero_for_one", 1_000_000)
    assert calls_made[1] == ("one_for_zero", 1_000_000)

    assert res_0_to_1.amount_out is not None and res_0_to_1.amount_out.atoms == 980_000
    assert res_1_to_0.amount_out is not None and res_1_to_0.amount_out.atoms == 1_010_000

    # Sabotage check: reverse price is NOT an inverse fraction of forward price
    # (Forward effective price = 0.98, reverse effective price = 1.01 != 1 / 0.98)
    assert res_1_to_0.amount_out.atoms != int(1_000_000 * (1_000_000 / 980_000))


# ==============================================================================
# C25: Zero-Output Truncation & Revert Safety
# ==============================================================================


def test_c25_zero_output_truncated_to_none() -> None:
    """C25: Zero output or liquidity exhaustion sets amount_out=None and status='exhausted'."""

    def mock_quoter(direction: str, amount_in: Amount) -> tuple[int, int | None]:
        if direction == "zero_for_one":
            return (0, 50_000)  # 0 output returned (exhausted depth)
        raise RuntimeError("simulated_revert")

    amt_in0 = Amount(asset_ref=ASSET0, atoms=1_000_000, decimals=6)
    amt_in1 = Amount(asset_ref=ASSET1, atoms=1_000_000, decimals=6)

    res_0_to_1, res_1_to_0 = execute_bidirectional_single_hop_quotes(
        pool_key=POOL_KEY,
        asset0=ASSET0,
        asset1=ASSET1,
        amount_in0=amt_in0,
        amount_in1=amt_in1,
        block_number=101,
        block_hash=BLOCK_HASH,
        quoter_fn=mock_quoter,
    )

    # 0 output -> exhausted
    assert res_0_to_1.amount_out is None
    assert res_0_to_1.status == "exhausted"
    assert res_0_to_1.can_quote == "unsupported"

    # revert -> reverted
    assert res_1_to_0.amount_out is None
    assert res_1_to_0.status == "reverted"
    assert res_1_to_0.can_quote == "unsupported"


# ==============================================================================
# C27: Capability Promotion Bounds
# ==============================================================================


def test_c27_capability_promotion_bounds() -> None:
    """C27: Quote success ONLY promotes can_quote='supported'; can_simulate remains unknown, atomic unsupported."""

    def mock_quoter(direction: str, amount_in: Amount) -> tuple[int, int | None]:
        return (1_000_000, 80_000)

    amt_in0 = Amount(asset_ref=ASSET0, atoms=1_000_000, decimals=6)
    amt_in1 = Amount(asset_ref=ASSET1, atoms=1_000_000, decimals=6)

    res_0_to_1, _ = execute_bidirectional_single_hop_quotes(
        pool_key=POOL_KEY,
        asset0=ASSET0,
        asset1=ASSET1,
        amount_in0=amt_in0,
        amount_in1=amt_in1,
        block_number=102,
        block_hash=BLOCK_HASH,
        quoter_fn=mock_quoter,
    )

    assert res_0_to_1.can_quote == "supported"
    # Strict bounds: NEVER claim simulation or atomic execution capability from a single-hop quote
    assert res_0_to_1.can_simulate == "unknown"
    assert res_0_to_1.can_atomic_execute == "unsupported"


def test_single_hop_model_validation_failures() -> None:
    """Ensure invalid QuoteRequest and QuoteResult combinations fail-closed."""
    # Invalid direction
    with pytest.raises(ArcValidationError, match="Invalid direction"):
        SingleHopQuoteRequest(
            quote_id="q1",
            pool_key=POOL_KEY,
            direction="three_way_cycle",  # type: ignore[arg-type]
            asset_in=ASSET0,
            asset_out=ASSET1,
            amount_in=Amount(asset_ref=ASSET0, atoms=100, decimals=6),
            block_number=1,
            block_hash=BLOCK_HASH,
        )

    # Negative amount_in
    with pytest.raises(ArcValidationError, match="amount_in must be positive"):
        SingleHopQuoteRequest(
            quote_id="q1",
            pool_key=POOL_KEY,
            direction="zero_for_one",
            asset_in=ASSET0,
            asset_out=ASSET1,
            amount_in=Amount(asset_ref=ASSET0, atoms=0, decimals=6),
            block_number=1,
            block_hash=BLOCK_HASH,
        )
