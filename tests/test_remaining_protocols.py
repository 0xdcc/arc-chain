"""Protocol counterexamples with exact calldata and isolated read-only RPC fixtures."""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from eth_abi import decode, encode
from web3 import Web3

from core.config import load_safe_config
from execution.funds import BASES, FundsError
from execution.protocols import PERMIT2, ROUTER, RouteVerifier, bps_to_raw, uniswap_fee
from execution.weth_arbitrage_executor import ArbitrageLeg, ArbitragePlan, WethArbitrageExecutor


def test_explicit_fee_units_and_zero_dynamic_fees():
    assert uniswap_fee(100, "uniswap-v3").fee_bps == Decimal(1)
    assert uniswap_fee(3000, "uniswap-v3").fee_bps == Decimal(30)
    assert uniswap_fee(0, "uniswap-v3").fee_bps == 0
    assert bps_to_raw(100) == 10000
    assert bps_to_raw(0) == 0
    with pytest.raises(FundsError):
        uniswap_fee(100, "giga-v3")
    with pytest.raises(FundsError):
        _ = uniswap_fee(0x800000, "uniswap-v4").fee_bps
    with pytest.raises(FundsError):
        bps_to_raw(Decimal(".0001"))


def make_executor():
    return WethArbitrageExecutor(config=load_safe_config(_env_file=None), w3=MagicMock())


def make_plan(protocols):
    tokens = [BASES["USDG"][0], BASES["WETH"][0]]
    if len(protocols) == 3:
        tokens.append("0x" + "55" * 20)
    tokens.append(tokens[0])
    legs = [
        ArbitrageLeg(tokens[i], tokens[i + 1], 100, dex=dex, tick_spacing=1)
        for i, dex in enumerate(protocols)
    ]
    return ArbitragePlan(
        legs,
        10_000_000,
        10_100_000,
        9_900_000,
        10,
        1,
        base_symbol="USDG",
        base_token=tokens[0],
        decimals=6,
    )


def v4_inputs(calldata):
    # The router has distinct two- and three-argument execute overloads.
    selector = bytes.fromhex(calldata[2:10])
    if selector == Web3.keccak(text="execute(bytes,bytes[])")[:4]:
        types = ["bytes", "bytes[]"]
    elif selector == Web3.keccak(text="execute(bytes,bytes[],uint256)")[:4]:
        types = ["bytes", "bytes[]", "uint256"]
    else:
        raise ValueError("Unexpected router execute selector")
    commands, inputs = decode(types, bytes.fromhex(calldata[10:]))[:2]
    return [
        decode(["bytes", "bytes[]"], payload)
        for command, payload in zip(commands, inputs, strict=True)
        if command == 0x10
    ]


@pytest.mark.parametrize("deadline", [None, 1_700_000_000])
def test_same_base_v4_prefunds_principal_and_takes_full_output(deadline):
    candidate = make_plan(["uniswap-v4", "uniswap-v4"])
    result = make_executor().build_swap_calldata(candidate, deadline=deadline)
    actions, params = v4_inputs(result["calldata"])[0]
    assert actions == bytes([0x0B, 0x07, 0x0F])
    currency, amount, payer = decode(["address", "uint256", "bool"], params[0])
    assert currency == BASES["USDG"][0]
    assert amount == candidate.amount_in_weth
    assert payer is True
    token, minimum = decode(["address", "uint256"], params[2])
    assert token == currency
    assert minimum == candidate.amount_out_min > 0


@pytest.mark.parametrize(
    "protocols",
    [
        ["uniswap-v4", "uniswap-v3", "uniswap-v4"],
        ["uniswap-v3", "uniswap-v4", "uniswap-v3"],
    ],
)
@pytest.mark.parametrize("deadline", [None, 1_700_000_000])
def test_mixed_v4_payer_and_recipient_track_router_balance(protocols, deadline):
    candidate = make_plan(protocols)
    blocks = iter(
        v4_inputs(make_executor().build_swap_calldata(candidate, deadline=deadline)["calldata"])
    )
    for index, protocol in enumerate(protocols):
        if protocol != "uniswap-v4":
            continue
        actions, params = next(blocks)
        assert actions == bytes([0x0B, 0x06, 0x0E])
        currency, amount, payer = decode(["address", "uint256", "bool"], params[0])
        assert currency == candidate.legs[index].from_token.lower()
        assert payer == (index == 0)
        assert amount == (candidate.amount_in_weth if index == 0 else 1 << 255)
        output_token, recipient, take_amount = decode(["address", "address", "uint256"], params[2])
        assert output_token == candidate.legs[index].to_token.lower()
        assert int(recipient, 16) == (1 if index == len(protocols) - 1 else 2)
        assert take_amount == 0  # OPEN_DELTA is not a zero swap minOut.


@pytest.mark.parametrize(
    "delegated,expiration,allowed", [(0, 1000, False), (100, 99, False), (100, 101, True)]
)
def test_actual_router_permit2_amount_and_expiration(delegated, expiration, allowed):
    requests = []
    erc_selector = Web3.keccak(text="allowance(address,address)")[:4].hex()
    p2_selector = Web3.keccak(text="allowance(address,address,address)")[:4].hex()
    wallet = "0x" + "11" * 20
    token = BASES["USDG"][0]

    def rpc(method, params):
        requests.append((method, params))
        if method == "eth_getCode":
            return "0x01"
        assert method == "eth_call"
        assert params[1] == "0xa"
        data = params[0]["data"]
        if data[2:10] == erc_selector:
            return "0x" + encode(["uint256"], [1000]).hex()
        assert data[2:10] == p2_selector
        owner, asset, spender = decode(["address"] * 3, bytes.fromhex(data[10:]))
        assert (owner, asset, spender) == (wallet, token, ROUTER)
        return "0x" + encode(["uint160", "uint48", "uint48"], [delegated, expiration, 0]).hex()

    digest = "0x" + Web3.keccak(b"\x01").hex()
    verifier = RouteVerifier(rpc, {ROUTER: digest, PERMIT2: digest})
    result = verifier.allowance(
        wallet, token, ROUTER, 100, {"number": "0xa", "hash": "0x" + "44" * 32, "timestamp": "0x64"}
    )
    assert result["has_allowance"] is allowed
    assert {method for method, _ in requests} == {"eth_call", "eth_getCode"}


def test_fork_identity_cannot_be_relabelled_uniswap_path():
    candidate = make_plan(["giga-v3", "giga-v3"])
    verifier = RouteVerifier(MagicMock(), {})
    with pytest.raises(FundsError):
        verifier.leg_identity(candidate.legs[0], ROUTER, {"number": "0x1"})


def test_manual_fee_unit_is_explicit_never_guessed_from_size():
    executor = make_executor()
    basis_points = executor.plan_two_hop("USDG", 100, 5, amount_usd=10)
    raw = executor.plan_two_hop("USDG", 100, 500, amount_usd=10, fee_unit="raw")
    assert [leg.pool_fee for leg in basis_points.legs] == [10000, 500]
    assert [leg.pool_fee for leg in raw.legs] == [100, 500]
    with pytest.raises(FundsError):
        executor.plan_two_hop("USDG", 100, 500, amount_usd=10, fee_unit="auto")
