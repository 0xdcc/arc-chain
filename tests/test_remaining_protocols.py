"""Protocol counterexamples with exact calldata and isolated read-only RPC fixtures.

Reconnected to merged production interfaces:
- research.market_data.fee_units (explicit fee units, bps_to_raw, FeeUnitError, fee_to_raw)
- arc_readiness.permit2 (check_permit2_allowance read-only mock)
- atomic_execution.encoding (encode_execution_plan, UnsupportedProtocolError)
- tests.arc_v3.independent.test_protocol_encoding_increment (make_plan, v4_inputs, helpers)
- tests.arc_v3.independent.test_protocol_whitelist_independent (_replace_hop_protocol)
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from eth_abi import decode, encode
from web3 import Web3

from arc_readiness.permit2 import CANONICAL_PERMIT2, check_permit2_allowance
from atomic_execution.encoding import (
    UnsupportedProtocolError,
    encode_execution_plan,
)
from atomic_execution.planning import CANONICAL_UNIVERSAL_ROUTER
from research.market_data.fee_units import (
    FeeUnitError,
    bps_to_raw,
    fee_to_raw,
    uniswap_fee,
)
from tests.arc_v3.independent.test_protocol_encoding_increment import (
    BASES,
    _get_asset_address,
    make_plan,
    v4_inputs,
)
from tests.arc_v3.independent.test_protocol_whitelist_independent import (
    _replace_hop_protocol,
)

ROUTER: str = CANONICAL_UNIVERSAL_ROUTER
PERMIT2: str = CANONICAL_PERMIT2


def test_explicit_fee_units_and_zero_dynamic_fees() -> None:
    """Validate explicit fee unit conversion and zero dynamic fee rejections."""
    assert uniswap_fee(100, "uniswap-v3").fee_bps == Decimal(1)
    assert uniswap_fee(3000, "uniswap-v3").fee_bps == Decimal(30)
    assert uniswap_fee(0, "uniswap-v3").fee_bps == 0
    assert bps_to_raw(100) == 10000
    assert bps_to_raw(0) == 0
    with pytest.raises(FeeUnitError):
        uniswap_fee(100, "giga-v3")
    with pytest.raises(FeeUnitError):
        _ = uniswap_fee(0x800000, "uniswap-v4").fee_bps
    with pytest.raises(FeeUnitError):
        bps_to_raw(Decimal(".0001"))


@pytest.mark.parametrize("deadline", [None, 1_700_000_000])
def test_same_base_v4_prefunds_principal_and_takes_full_output(
    deadline: int | None,
) -> None:
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


@pytest.mark.parametrize(
    "delegated,expiration,allowed", [(0, 1000, False), (100, 99, False), (100, 101, True)]
)
def test_actual_router_permit2_amount_and_expiration(
    delegated: int, expiration: int, allowed: bool
) -> None:
    """Verify Permit2 amount and expiration conditions via read-only mock transport."""
    requests: list[tuple[str, Any]] = []
    erc_selector = Web3.keccak(text="allowance(address,address)")[:4].hex()
    p2_selector = Web3.keccak(text="allowance(address,address,address)")[:4].hex()
    wallet = "0x" + "11" * 20
    token = BASES["USDG"][0]

    def rpc(method: str, params: Any) -> Any:
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
        assert (owner.lower(), asset.lower(), spender.lower()) == (
            wallet.lower(),
            token.lower(),
            ROUTER.lower(),
        )
        return "0x" + encode(["uint160", "uint48", "uint48"], [delegated, expiration, 0]).hex()

    digest = "0x" + Web3.keccak(b"\x01").hex()
    result = check_permit2_allowance(
        transport=rpc,
        owner=wallet,
        token=token,
        spender=ROUTER,
        permit2_address=PERMIT2,
        requested_amount=100,
        block_context={"number": "0xa", "hash": "0x" + "44" * 32, "timestamp": "0x64"},
        check_erc20=True,
        check_code=True,
        expected_digests={ROUTER: digest, PERMIT2: digest},
    )
    assert result["has_allowance"] is allowed
    assert {method for method, _ in requests} == {"eth_call", "eth_getCode"}


def test_fork_identity_cannot_be_relabelled_uniswap_path() -> None:
    """Reject fork label 'giga-v3' in execution plan encoding."""
    base_plan = make_plan(["uniswap-v3", "uniswap-v3"])
    candidate = _replace_hop_protocol(base_plan, 0, "giga-v3")
    with pytest.raises(UnsupportedProtocolError) as exc_info:
        encode_execution_plan(candidate)
    assert "giga-v3" in str(exc_info.value)
    assert "unknown protocol" in str(exc_info.value) or "unsupported protocol" in str(exc_info.value)


def test_manual_fee_unit_is_explicit_never_guessed_from_size() -> None:
    """Manual fee units must be explicit ('bps' or 'ppm'); 'auto' guessing is prohibited.

    Transformation boundary:
    - Legacy plan_two_hop is not restored.
    - Explicit fee_to_raw maps inputs without guessing.
    - 'raw' corresponds to 'ppm' (parts per million) in canonical DEX terminology (1 bps = 100 ppm).
    """
    basis_points = [fee_to_raw(100, fee_unit="bps"), fee_to_raw(5, fee_unit="bps")]
    raw = [fee_to_raw(100, fee_unit="ppm"), fee_to_raw(500, fee_unit="ppm")]
    assert basis_points == [10000, 500]
    assert raw == [100, 500]
    with pytest.raises(FeeUnitError):
        _ = fee_to_raw(100, fee_unit="auto")
    with pytest.raises(FeeUnitError):
        _ = fee_to_raw(500, fee_unit="auto")
