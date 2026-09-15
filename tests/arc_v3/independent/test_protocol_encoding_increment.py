"""Protocol counterexamples with exact calldata and isolated read-only RPC fixtures.

Adapted for Universal Router encoding under ExecutionPlan architecture.
Maintains exact action sequences and financial topology assertions.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from eth_abi import decode
from web3 import Web3

from arbitrage_contracts.identity import (
    Amount,
    AssetRef,
    FeeModel,
    PoolDescriptor,
    PoolKey,
    TokenKey,
)
from arbitrage_contracts.quote import HopRef, RouteRef
from atomic_execution.encoding import (
    ROBINHOOD_CHAIN_ID,
    ZERO_ADDRESS,
    encode_execution_plan,
)
from atomic_execution.inputs import USDG_ADDRESS_4663, WETH_ADDRESS_4663
from atomic_execution.planning import CANONICAL_UNIVERSAL_ROUTER, ExecutionPlan
from atomic_execution.policy import ExecutionPolicy

USDG: str = USDG_ADDRESS_4663
WETH: str = WETH_ADDRESS_4663
PONS: str = "0x39dBED3a2bd333467115dE45665cC57F813C4571"
ROUTER: str = CANONICAL_UNIVERSAL_ROUTER
PERMIT2: str = "0x000000000022d473030f116ddee9f6b43ac78ba3"
BASES: dict[str, tuple[str, int]] = {"USDG": (USDG, 6), "WETH": (WETH, 18)}

def _get_asset_address(asset: AssetRef) -> str:
    """Extract EVM address from AssetRef."""
    assert asset.token_key is not None
    return asset.token_key.address


def make_plan(protocols: list[str]) -> ExecutionPlan:
    """Construct an ExecutionPlan fixture for protocol encoding counterexamples."""
    tokens = [USDG, WETH]
    if len(protocols) == 3:
        tokens.append(PONS)
    tokens.append(tokens[0])

    usdg_asset = AssetRef.erc20(TokenKey(ROBINHOOD_CHAIN_ID, USDG))
    weth_asset = AssetRef.erc20(TokenKey(ROBINHOOD_CHAIN_ID, WETH))
    pons_asset = AssetRef.erc20(TokenKey(ROBINHOOD_CHAIN_ID, PONS))
    asset_map = {USDG: usdg_asset, WETH: weth_asset, PONS: pons_asset}

    hops: list[HopRef] = []
    for i, dex in enumerate(protocols):
        a_in = asset_map[tokens[i]]
        a_out = asset_map[tokens[i + 1]]
        in_int = int(tokens[i], 16)
        out_int = int(tokens[i + 1], 16)
        c0 = a_in if in_int < out_int else a_out
        c1 = a_out if c0 == a_in else a_in
        direction = "zero_for_one" if c0 == a_in else "one_for_zero"

        is_v4 = "v4" in dex
        protocol_id = "uniswap_v4" if is_v4 else "uniswap_v3"
        venue_kind = "manager" if is_v4 else "factory"
        pool_id_kind = "bytes32" if is_v4 else "address"
        pool_id = ("0x" + str(i + 1) * 64) if is_v4 else ("0x" + str(i + 1) * 40)

        pk = PoolKey(
            chain_id=ROBINHOOD_CHAIN_ID,
            protocol_id=protocol_id,
            pool_id=pool_id,
            pool_id_kind=pool_id_kind,
            venue_address=ROUTER,
            venue_kind=venue_kind,
        )
        desc = PoolDescriptor(
            key=pk,
            currency0=c0,
            currency1=c1,
            fee_model=FeeModel.static(100),
            tick_spacing=1 if is_v4 else None,
            hooks=ZERO_ADDRESS,
        )
        hops.append(
            HopRef(
                pool_key=pk,
                asset_in=a_in,
                asset_out=a_out,
                direction=direction,
                pool_descriptor=desc,
            )
        )

    base = usdg_asset
    route = RouteRef(chain_id=ROBINHOOD_CHAIN_ID, hops=tuple(hops), base_asset=base)
    amount_in = Amount(asset_ref=base, atoms=10_000_000, decimals=6)
    expected_out = Amount(asset_ref=base, atoms=10_100_000, decimals=6)
    min_out = Amount(asset_ref=base, atoms=10_015_000, decimals=6)
    floor_amt = Amount(asset_ref=base, atoms=10_011_000, decimals=6)

    return ExecutionPlan(
        plan_id="plan_counterexample",
        route_ref=route,
        base_asset=base,
        amount_in=amount_in,
        expected_out=expected_out,
        min_amount_out=min_out,
        output_floor=floor_amt,
        policy=ExecutionPolicy(),
        quoter_block=123456,
        deadline=1_700_000_000,
        target_router=ROUTER,
        estimated_gas_usd=Decimal("0.01"),
        gas_atoms=10_000,
        net_atoms=5_000,
        net_profit_usd=Decimal("0.005"),
        trade_amount_usd=Decimal("10.0"),
        base_asset_usd_price=Decimal("1.0"),
    )


def v4_inputs(calldata: str | bytes) -> list[tuple[bytes, list[bytes]]]:
    """Decode Universal Router execute calldata to extract V4 command actions and inputs."""
    if isinstance(calldata, str):
        normalized = calldata[2:] if calldata.startswith(("0x", "0X")) else calldata
        raw_bytes = bytes.fromhex(normalized)
    else:
        raw_bytes = calldata
    selector = raw_bytes[:4]
    if selector == Web3.keccak(text="execute(bytes,bytes[])")[:4]:
        types = ["bytes", "bytes[]"]
    elif selector == Web3.keccak(text="execute(bytes,bytes[],uint256)")[:4]:
        types = ["bytes", "bytes[]", "uint256"]
    else:
        raise ValueError(f"Unexpected router execute selector: {selector.hex()}")
    commands, inputs = decode(types, raw_bytes[4:])[:2]
    return [
        decode(["bytes", "bytes[]"], payload)
        for command, payload in zip(commands, inputs, strict=True)
        if command == 0x10
    ]


@pytest.mark.parametrize("deadline", [None, 1_700_000_000])
def test_same_base_v4_prefunds_principal_and_takes_full_output(deadline: int | None) -> None:
    """Verify pure V4 path prefunds principal (SETTLE) and takes output (TAKE_ALL/TAKE)."""
    candidate = make_plan(["uniswap-v4", "uniswap-v4"])
    encoded = encode_execution_plan(candidate, deadline=deadline)
    actions, params = v4_inputs(encoded.calldata_hex)[0]
    assert actions == bytes([0x0B, 0x07, 0x0F])
    currency, amount, payer = decode(["address", "uint256", "bool"], params[0])
    assert currency.lower() == BASES["USDG"][0].lower()
    assert amount == candidate.amount_in.atoms
    assert payer is True
    token, minimum = decode(["address", "uint256"], params[2])
    assert token.lower() == currency.lower()
    assert minimum == candidate.min_amount_out.atoms > 0


@pytest.mark.parametrize(
    "protocols",
    [
        ["uniswap-v4", "uniswap-v3", "uniswap-v4"],
        ["uniswap-v3", "uniswap-v4", "uniswap-v3"],
    ],
)
@pytest.mark.parametrize("deadline", [None, 1_700_000_000])
def test_mixed_v4_payer_and_recipient_track_router_balance(
    protocols: list[str], deadline: int | None
) -> None:
    """Verify mixed V3/V4 path: intermediate V4 hops settle from router and output to router."""
    candidate = make_plan(protocols)
    encoded = encode_execution_plan(candidate, deadline=deadline)
    blocks = iter(v4_inputs(encoded.calldata_hex))
    for index, protocol in enumerate(protocols):
        if protocol != "uniswap-v4":
            continue
        actions, params = next(blocks)
        assert actions == bytes([0x0B, 0x06, 0x0E])
        currency, amount, payer = decode(["address", "uint256", "bool"], params[0])
        assert currency.lower() == _get_asset_address(candidate.hops[index].asset_in).lower()
        assert payer == (index == 0)
        assert amount == (candidate.amount_in.atoms if index == 0 else 1 << 255)
        output_token, recipient, take_amount = decode(["address", "address", "uint256"], params[2])
        assert output_token.lower() == _get_asset_address(candidate.hops[index].asset_out).lower()
        assert int(recipient, 16) == (1 if index == len(protocols) - 1 else 2)
        assert take_amount == 0

