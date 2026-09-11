"""Comprehensive unit and regression test suite for W5-D Universal Router encoding and verification."""

from __future__ import annotations

import json
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from eth_abi import encode as abi_encode
from eth_typing import HexStr
from uniswap_universal_router_decoder import RouterCodec
from web3 import Web3

from arbitrage_contracts.identity import (
    Amount,
    AssetRef,
    FeeModel,
    PoolDescriptor,
    PoolKey,
    TokenKey,
)
from arbitrage_contracts.quote import (
    HopRef,
    QuoteEvidence,
    QuoteStatus,
    RouteRef,
)
from atomic_execution.encoding import (
    ADDRESS_THIS,
    COMMAND_V3_SWAP_EXACT_IN,
    COMMAND_V4_SWAP,
    EXECUTE_SELECTOR_WITH_DEADLINE,
    FLAG_ALLOW_REVERT,
    MSG_SENDER,
    ROBINHOOD_CHAIN_ID,
    ZERO_ADDRESS,
    CommandSecurityError,
    DualVerificationError,
    EncodedCalldata,
    EncodingError,
    EncodingMode,
    UnsupportedChainError,
    UnsupportedProtocolError,
    compute_calldata_sha256,
    encode_execution_plan,
    resolve_v4_pool_key,
    validate_commands_security,
    verify_dual_decoded_calldata,
)
from atomic_execution.inputs import USDG_ADDRESS_4663, WETH_ADDRESS_4663
from atomic_execution.models import DraftSimulationEvidence
from atomic_execution.planning import (
    CANONICAL_UNIVERSAL_ROUTER,
    ExecutionPlan,
)
from atomic_execution.policy import ExecutionPolicy

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "atomic_execution" / "v1"

TEST_PONS_ADDRESS = "0x39dBED3a2bd333467115dE45665cC57F813C4571"
TEST_POOL_1 = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TEST_POOL_2 = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
TEST_POOL_3 = "0xcccccccccccccccccccccccccccccccccccccccc"


def _make_asset(address: str, chain_id: int = ROBINHOOD_CHAIN_ID) -> AssetRef:
    return AssetRef.erc20(TokenKey(chain_id, address))


def _make_pool_key(
    pool_id: str,
    protocol_id: str = "uniswap_v3",
    chain_id: int = ROBINHOOD_CHAIN_ID,
) -> PoolKey:
    venue_kind = "manager" if "v4" in protocol_id else "factory"
    pool_id_kind = "bytes32" if "v4" in protocol_id else "address"
    formatted_pool_id = (
        ("0x" + "1" * 64) if pool_id_kind == "bytes32" and not pool_id.startswith("0x") else pool_id
    )
    if pool_id_kind == "bytes32" and len(formatted_pool_id) != 66:
        formatted_pool_id = "0x" + "a" * 64
    return PoolKey(
        chain_id=chain_id,
        protocol_id=protocol_id,
        pool_id=formatted_pool_id,
        pool_id_kind=pool_id_kind,
        venue_address="0x1f7d7550b1b028f7571e69a784071f0205fd2efa",
        venue_kind=venue_kind,
    )


_pool_counter = 0


def _get_next_pool_id(is_bytes32: bool = False) -> str:
    global _pool_counter
    _pool_counter += 1
    hex_str = f"{_pool_counter:x}"
    if is_bytes32:
        return "0x" + hex_str.zfill(64)
    return "0x" + hex_str.zfill(40)


def _make_v3_hop(
    asset_in: AssetRef,
    asset_out: AssetRef,
    fee_raw: int = 500,
    pool_id: str | None = None,
    chain_id: int = ROBINHOOD_CHAIN_ID,
) -> HopRef:
    resolved_id = pool_id if pool_id is not None else _get_next_pool_id(is_bytes32=False)
    pk = _make_pool_key(resolved_id, protocol_id="uniswap_v3", chain_id=chain_id)
    assert asset_in.token_key is not None
    assert asset_out.token_key is not None
    c0 = (
        asset_in
        if int(asset_in.token_key.address, 16) < int(asset_out.token_key.address, 16)
        else asset_out
    )
    c1 = asset_out if c0 == asset_in else asset_in
    direction = "zero_for_one" if c0 == asset_in else "one_for_zero"
    desc = PoolDescriptor(
        key=pk,
        currency0=c0,
        currency1=c1,
        fee_model=FeeModel.static(fee_raw),
        hooks=ZERO_ADDRESS,
    )
    return HopRef(
        pool_key=pk,
        asset_in=asset_in,
        asset_out=asset_out,
        direction=direction,
        pool_descriptor=desc,
    )


def _make_v4_hop(
    asset_in: AssetRef,
    asset_out: AssetRef,
    fee_raw: int = 500,
    tick_spacing: int = 10,
    hooks: str = ZERO_ADDRESS,
    fee_model: FeeModel | None = None,
    pool_id: str | None = None,
    chain_id: int = ROBINHOOD_CHAIN_ID,
) -> HopRef:
    resolved_id = pool_id if pool_id is not None else _get_next_pool_id(is_bytes32=True)
    pk = _make_pool_key(resolved_id, protocol_id="uniswap_v4", chain_id=chain_id)
    assert asset_in.token_key is not None
    assert asset_out.token_key is not None
    c0 = (
        asset_in
        if int(asset_in.token_key.address, 16) < int(asset_out.token_key.address, 16)
        else asset_out
    )
    c1 = asset_out if c0 == asset_in else asset_in
    direction = "zero_for_one" if c0 == asset_in else "one_for_zero"
    effective_fee_model = fee_model if fee_model is not None else FeeModel.static(fee_raw)
    desc = PoolDescriptor(
        key=pk,
        currency0=c0,
        currency1=c1,
        fee_model=effective_fee_model,
        tick_spacing=tick_spacing,
        hooks=hooks,
    )
    return HopRef(
        pool_key=pk,
        asset_in=asset_in,
        asset_out=asset_out,
        direction=direction,
        pool_descriptor=desc,
    )


def _make_plan(
    hops: Sequence[HopRef],
    base_asset: AssetRef | None = None,
    amount_in_atoms: int = 100_000_000_000_000_000,
    expected_out_atoms: int = 100_500_000_000_000_000,
    min_amount_out_atoms: int = 100_100_000_000_000_000,
    deadline: int = 1700000120,
    chain_id: int = ROBINHOOD_CHAIN_ID,
    plan_id: str = "test_plan_001",
) -> ExecutionPlan:
    resolved_base = base_asset if base_asset is not None else hops[0].asset_in
    route = RouteRef(chain_id=chain_id, hops=tuple(hops), base_asset=resolved_base)
    amt_in = Amount(asset_ref=resolved_base, atoms=amount_in_atoms, decimals=18)
    exp_out = Amount(asset_ref=resolved_base, atoms=expected_out_atoms, decimals=18)
    min_out = Amount(asset_ref=resolved_base, atoms=min_amount_out_atoms, decimals=18)
    floor_amt = Amount(asset_ref=resolved_base, atoms=min_amount_out_atoms, decimals=18)

    return ExecutionPlan(
        plan_id=plan_id,
        route_ref=route,
        base_asset=resolved_base,
        amount_in=amt_in,
        expected_out=exp_out,
        min_amount_out=min_out,
        output_floor=floor_amt,
        policy=ExecutionPolicy(),
        quoter_block=123456,
        deadline=deadline,
        target_router=CANONICAL_UNIVERSAL_ROUTER,
        estimated_gas_usd=Decimal("0.10"),
        gas_atoms=40000000000000,
        net_atoms=min_amount_out_atoms - amount_in_atoms - 40000000000000,
        net_profit_usd=Decimal("0.25"),
        trade_amount_usd=Decimal("250.0"),
        base_asset_usd_price=Decimal("2500.0"),
    )


# ---------------------------------------------------------------------------
# Fixture Integrity and Parity Tests
# ---------------------------------------------------------------------------


def test_fixtures_encoding_cases_file_integrity() -> None:
    fixture_path = FIXTURES_DIR / "encoding-cases.jsonl"
    assert fixture_path.exists(), f"Missing fixture file: {fixture_path}"

    cases: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    with open(fixture_path, encoding="utf-8") as fixture_file:
        for line_num, line in enumerate(fixture_file, 1):
            stripped = line.strip()
            if not stripped:
                continue
            case = json.loads(stripped)
            case_id = str(case["case_id"])
            assert case_id not in seen_ids, f"Duplicate case_id {case_id} on line {line_num}"
            seen_ids.add(case_id)
            assert case["category"] in ("C10", "C11", "C12")
            cases.append(case)

    assert len(cases) >= 25, f"Expected at least 25 fixture cases, found {len(cases)}"


def test_fixtures_encoding_cases_execution() -> None:
    fixture_path = FIXTURES_DIR / "encoding-cases.jsonl"
    with open(fixture_path, encoding="utf-8") as fixture_file:
        for line in fixture_file:
            stripped = line.strip()
            if not stripped:
                continue
            case = json.loads(stripped)
            category = case["category"]
            assert category in ("C10", "C11", "C12")
            assert "description" in case
            assert "expected" in case


# ---------------------------------------------------------------------------
# C10 Tests: Determinism, Pure Paths, Mixed Assembly, and Parameter Sensitivity
# ---------------------------------------------------------------------------


def test_c10_v3_pure_2hop_calldata_assembly() -> None:
    weth = _make_asset(WETH_ADDRESS_4663)
    usdg = _make_asset(USDG_ADDRESS_4663)
    hop1 = _make_v3_hop(weth, usdg, fee_raw=500, pool_id=TEST_POOL_1)
    hop2 = _make_v3_hop(usdg, weth, fee_raw=500, pool_id=TEST_POOL_2)
    plan = _make_plan([hop1, hop2], base_asset=weth)

    encoded = encode_execution_plan(plan)
    assert encoded.commands_hex == "0x00"
    assert encoded.commands_count == 1
    assert encoded.calldata_hex.startswith(EXECUTE_SELECTOR_WITH_DEADLINE)
    assert len(encoded.calldata_sha256) == 64
    assert encoded.encoding_mode == EncodingMode.PURE_PATH
    assert encoded.dual_verified is True
    assert encoded.status == "ENCODED"
    assert encoded.can_atomic_execute is False


def test_c10_v3_pure_3hop_calldata_assembly() -> None:
    weth = _make_asset(WETH_ADDRESS_4663)
    usdg = _make_asset(USDG_ADDRESS_4663)
    pons = _make_asset(TEST_PONS_ADDRESS)
    hop1 = _make_v3_hop(weth, usdg, fee_raw=500, pool_id=TEST_POOL_1)
    hop2 = _make_v3_hop(usdg, pons, fee_raw=3000, pool_id=TEST_POOL_2)
    hop3 = _make_v3_hop(pons, weth, fee_raw=3000, pool_id=TEST_POOL_3)
    plan = _make_plan([hop1, hop2, hop3], base_asset=weth)

    encoded = encode_execution_plan(plan)
    assert encoded.commands_hex == "0x00"
    assert encoded.commands_count == 1
    assert encoded.encoding_mode == EncodingMode.PURE_PATH
    assert encoded.dual_verified is True


def test_c10_v4_pure_2hop_calldata_assembly() -> None:
    weth = _make_asset(WETH_ADDRESS_4663)
    usdg = _make_asset(USDG_ADDRESS_4663)
    hop1 = _make_v4_hop(weth, usdg, fee_raw=500, tick_spacing=10, pool_id="0x" + "1" * 64)
    hop2 = _make_v4_hop(usdg, weth, fee_raw=500, tick_spacing=10, pool_id="0x" + "2" * 64)
    plan = _make_plan([hop1, hop2], base_asset=weth)

    encoded = encode_execution_plan(plan)
    assert encoded.commands_hex == "0x10"
    assert encoded.commands_count == 1
    assert encoded.encoding_mode == EncodingMode.PURE_PATH
    assert encoded.dual_verified is True


def test_c10_v4_pure_3hop_calldata_assembly() -> None:
    weth = _make_asset(WETH_ADDRESS_4663)
    usdg = _make_asset(USDG_ADDRESS_4663)
    pons = _make_asset(TEST_PONS_ADDRESS)
    hop1 = _make_v4_hop(weth, usdg, fee_raw=500, tick_spacing=10, pool_id="0x" + "1" * 64)
    hop2 = _make_v4_hop(usdg, pons, fee_raw=3000, tick_spacing=60, pool_id="0x" + "2" * 64)
    hop3 = _make_v4_hop(pons, weth, fee_raw=3000, tick_spacing=60, pool_id="0x" + "3" * 64)
    plan = _make_plan([hop1, hop2, hop3], base_asset=weth)

    encoded = encode_execution_plan(plan)
    assert encoded.commands_hex == "0x10"
    assert encoded.commands_count == 1
    assert encoded.encoding_mode == EncodingMode.PURE_PATH
    assert encoded.dual_verified is True


def test_c10_mixed_2hop_v3_v4_assembly() -> None:
    weth = _make_asset(WETH_ADDRESS_4663)
    usdg = _make_asset(USDG_ADDRESS_4663)
    hop1 = _make_v3_hop(weth, usdg, fee_raw=500, pool_id=TEST_POOL_1)
    hop2 = _make_v4_hop(usdg, weth, fee_raw=500, tick_spacing=10, pool_id="0x" + "2" * 64)
    plan = _make_plan([hop1, hop2], base_asset=weth)

    encoded = encode_execution_plan(plan)
    assert encoded.commands_hex == "0x0010"
    assert encoded.commands_count == 2
    assert encoded.encoding_mode == EncodingMode.SEQUENTIAL
    assert encoded.dual_verified is True


def test_c10_mixed_2hop_v4_v3_assembly() -> None:
    weth = _make_asset(WETH_ADDRESS_4663)
    usdg = _make_asset(USDG_ADDRESS_4663)
    hop1 = _make_v4_hop(weth, usdg, fee_raw=500, tick_spacing=10, pool_id="0x" + "1" * 64)
    hop2 = _make_v3_hop(usdg, weth, fee_raw=500, pool_id=TEST_POOL_2)
    plan = _make_plan([hop1, hop2], base_asset=weth)

    encoded = encode_execution_plan(plan)
    assert encoded.commands_hex == "0x1000"
    assert encoded.commands_count == 2
    assert encoded.encoding_mode == EncodingMode.SEQUENTIAL
    assert encoded.dual_verified is True


def test_c10_mixed_3hop_v3_v4_v3_assembly() -> None:
    weth = _make_asset(WETH_ADDRESS_4663)
    usdg = _make_asset(USDG_ADDRESS_4663)
    pons = _make_asset(TEST_PONS_ADDRESS)
    hop1 = _make_v3_hop(weth, usdg, fee_raw=500, pool_id=TEST_POOL_1)
    hop2 = _make_v4_hop(usdg, pons, fee_raw=3000, tick_spacing=60, pool_id="0x" + "2" * 64)
    hop3 = _make_v3_hop(pons, weth, fee_raw=3000, pool_id=TEST_POOL_3)
    plan = _make_plan([hop1, hop2, hop3], base_asset=weth)

    encoded = encode_execution_plan(plan)
    assert encoded.commands_hex == "0x001000"
    assert encoded.commands_count == 3
    assert encoded.encoding_mode == EncodingMode.SEQUENTIAL
    assert encoded.dual_verified is True


def test_c10_mixed_3hop_v4_v3_v4_assembly() -> None:
    weth = _make_asset(WETH_ADDRESS_4663)
    usdg = _make_asset(USDG_ADDRESS_4663)
    pons = _make_asset(TEST_PONS_ADDRESS)
    hop1 = _make_v4_hop(weth, usdg, fee_raw=500, tick_spacing=10, pool_id="0x" + "1" * 64)
    hop2 = _make_v3_hop(usdg, pons, fee_raw=3000, pool_id=TEST_POOL_2)
    hop3 = _make_v4_hop(pons, weth, fee_raw=3000, tick_spacing=60, pool_id="0x" + "3" * 64)
    plan = _make_plan([hop1, hop2, hop3], base_asset=weth)

    encoded = encode_execution_plan(plan)
    assert encoded.commands_hex == "0x100010"
    assert encoded.commands_count == 3
    assert encoded.encoding_mode == EncodingMode.SEQUENTIAL
    assert encoded.dual_verified is True


def test_c10_force_sequential_option() -> None:
    weth = _make_asset(WETH_ADDRESS_4663)
    usdg = _make_asset(USDG_ADDRESS_4663)
    hop1 = _make_v3_hop(weth, usdg, fee_raw=500, pool_id=TEST_POOL_1)
    hop2 = _make_v3_hop(usdg, weth, fee_raw=500, pool_id=TEST_POOL_2)
    plan = _make_plan([hop1, hop2], base_asset=weth)

    encoded = encode_execution_plan(plan, force_sequential=True)
    assert encoded.commands_hex == "0x0000"
    assert encoded.commands_count == 2
    assert encoded.encoding_mode == EncodingMode.SEQUENTIAL
    assert encoded.dual_verified is True


def test_c10_sensitivity_min_amount_out() -> None:
    weth = _make_asset(WETH_ADDRESS_4663)
    usdg = _make_asset(USDG_ADDRESS_4663)
    hop1 = _make_v3_hop(weth, usdg, fee_raw=500, pool_id=TEST_POOL_1)
    hop2 = _make_v3_hop(usdg, weth, fee_raw=500, pool_id=TEST_POOL_2)

    plan1 = _make_plan([hop1, hop2], min_amount_out_atoms=100_100_000_000_000_000)
    plan2 = _make_plan([hop1, hop2], min_amount_out_atoms=100_200_000_000_000_000)

    enc1 = encode_execution_plan(plan1)
    enc2 = encode_execution_plan(plan2)
    assert enc1.calldata_sha256 != enc2.calldata_sha256


def test_c10_sensitivity_deadline() -> None:
    weth = _make_asset(WETH_ADDRESS_4663)
    usdg = _make_asset(USDG_ADDRESS_4663)
    hop1 = _make_v3_hop(weth, usdg, fee_raw=500, pool_id=TEST_POOL_1)
    hop2 = _make_v3_hop(usdg, weth, fee_raw=500, pool_id=TEST_POOL_2)
    plan = _make_plan([hop1, hop2], deadline=1700000100)

    enc1 = encode_execution_plan(plan, deadline=1700000100)
    enc2 = encode_execution_plan(plan, deadline=1700000200)
    assert enc1.calldata_sha256 != enc2.calldata_sha256


def test_c10_sensitivity_recipient() -> None:
    weth = _make_asset(WETH_ADDRESS_4663)
    usdg = _make_asset(USDG_ADDRESS_4663)
    hop1 = _make_v3_hop(weth, usdg, fee_raw=500, pool_id=TEST_POOL_1)
    hop2 = _make_v3_hop(usdg, weth, fee_raw=500, pool_id=TEST_POOL_2)
    plan = _make_plan([hop1, hop2])

    custom_a = "0x1111111111111111111111111111111111111111"
    custom_b = "0x2222222222222222222222222222222222222222"

    enc_default = encode_execution_plan(plan)
    enc_a = encode_execution_plan(plan, recipient=custom_a, allowed_recipients={custom_a})
    enc_b = encode_execution_plan(plan, recipient=custom_b, allowed_recipients={custom_b})

    assert enc_default.calldata_sha256 != enc_a.calldata_sha256
    assert enc_a.calldata_sha256 != enc_b.calldata_sha256


def test_c10_sensitivity_hop_to_address() -> None:
    weth = _make_asset(WETH_ADDRESS_4663)
    usdg = _make_asset(USDG_ADDRESS_4663)
    pons = _make_asset(TEST_PONS_ADDRESS)

    hop1_to_usdg = _make_v3_hop(weth, usdg, fee_raw=500, pool_id=TEST_POOL_1)
    hop1_to_pons = _make_v3_hop(weth, pons, fee_raw=500, pool_id=TEST_POOL_1)
    hop2_from_usdg = _make_v3_hop(usdg, weth, fee_raw=500, pool_id=TEST_POOL_2)
    hop2_from_pons = _make_v3_hop(pons, weth, fee_raw=500, pool_id=TEST_POOL_2)

    plan1 = _make_plan([hop1_to_usdg, hop2_from_usdg])
    plan2 = _make_plan([hop1_to_pons, hop2_from_pons])

    enc1 = encode_execution_plan(plan1)
    enc2 = encode_execution_plan(plan2)
    assert enc1.calldata_sha256 != enc2.calldata_sha256


def test_c10_sensitivity_pool_fee() -> None:
    weth = _make_asset(WETH_ADDRESS_4663)
    usdg = _make_asset(USDG_ADDRESS_4663)
    hop1_fee500 = _make_v3_hop(weth, usdg, fee_raw=500, pool_id=TEST_POOL_1)
    hop1_fee3000 = _make_v3_hop(weth, usdg, fee_raw=3000, pool_id=TEST_POOL_1)
    hop2 = _make_v3_hop(usdg, weth, fee_raw=500, pool_id=TEST_POOL_2)

    plan1 = _make_plan([hop1_fee500, hop2])
    plan2 = _make_plan([hop1_fee3000, hop2])

    enc1 = encode_execution_plan(plan1)
    enc2 = encode_execution_plan(plan2)
    assert enc1.calldata_sha256 != enc2.calldata_sha256


def test_c10_dual_decoded_field_by_field_equality() -> None:
    weth = _make_asset(WETH_ADDRESS_4663)
    usdg = _make_asset(USDG_ADDRESS_4663)
    hop1 = _make_v3_hop(weth, usdg, fee_raw=500, pool_id=TEST_POOL_1)
    hop2 = _make_v3_hop(usdg, weth, fee_raw=500, pool_id=TEST_POOL_2)
    plan = _make_plan(
        [hop1, hop2], amount_in_atoms=123456789, min_amount_out_atoms=120000000, deadline=1700000999
    )

    encoded = encode_execution_plan(plan)
    ver = verify_dual_decoded_calldata(encoded.calldata_hex, plan)
    assert ver.is_valid is True
    assert ver.function_name == "execute"
    assert ver.amount_in == 123456789
    assert ver.min_amount_out == 120000000
    assert ver.deadline == 1700000999
    assert ver.final_recipient.lower() == MSG_SENDER.lower()
    assert ver.allow_revert_detected is False


def test_c10_dual_decoded_deadline_mismatch_fails() -> None:
    weth = _make_asset(WETH_ADDRESS_4663)
    usdg = _make_asset(USDG_ADDRESS_4663)
    hop1 = _make_v3_hop(weth, usdg, fee_raw=500, pool_id=TEST_POOL_1)
    hop2 = _make_v3_hop(usdg, weth, fee_raw=500, pool_id=TEST_POOL_2)
    plan = _make_plan([hop1, hop2], deadline=1700000100)

    encoded = encode_execution_plan(plan, deadline=1700000100)
    with pytest.raises(DualVerificationError, match="Decoded deadline"):
        verify_dual_decoded_calldata(encoded.calldata_hex, plan, expected_deadline=1700000200)


# ---------------------------------------------------------------------------
# C11 Tests: Command Bit Defense, Recipient Constraints, and Paired Actions
# ---------------------------------------------------------------------------


def test_c11_allow_revert_bit7_strictly_unset_on_all_commands() -> None:
    weth = _make_asset(WETH_ADDRESS_4663)
    usdg = _make_asset(USDG_ADDRESS_4663)
    pons = _make_asset(TEST_PONS_ADDRESS)

    hop_v3_1 = _make_v3_hop(weth, usdg, fee_raw=500)
    hop_v4_2 = _make_v4_hop(usdg, pons, fee_raw=3000, tick_spacing=60)
    hop_v3_3 = _make_v3_hop(pons, weth, fee_raw=3000)

    plan = _make_plan([hop_v3_1, hop_v4_2, hop_v3_3])
    encoded = encode_execution_plan(plan)

    raw_commands = bytes.fromhex(encoded.commands_hex[2:])
    for command_byte in raw_commands:
        assert (command_byte & FLAG_ALLOW_REVERT) == 0


def test_c11_validate_commands_security_catches_bit7() -> None:
    with pytest.raises(CommandSecurityError, match="bit 7.*allow_revert"):
        validate_commands_security(b"\x80")

    with pytest.raises(CommandSecurityError, match="bit 7.*allow_revert"):
        validate_commands_security(b"\x90")


def test_c11_validate_commands_security_catches_empty() -> None:
    with pytest.raises(CommandSecurityError, match="cannot be empty"):
        validate_commands_security(b"")


def test_c11_dual_verification_detects_bit7_tampering() -> None:
    weth = _make_asset(WETH_ADDRESS_4663)
    usdg = _make_asset(USDG_ADDRESS_4663)
    hop1 = _make_v3_hop(weth, usdg, fee_raw=500)
    hop2 = _make_v3_hop(usdg, weth, fee_raw=500)
    plan = _make_plan([hop1, hop2])
    encoded = encode_execution_plan(plan)

    # Tamper commands to set bit 7 on command 0x00 -> 0x80
    codec = RouterCodec()
    fct_name, decoded_input = codec.decode.function_input(cast(HexStr, encoded.calldata_hex))
    tampered_commands = bytes([decoded_input["commands"][0] | FLAG_ALLOW_REVERT])

    # Re-encode calldata with tampered command byte
    types = ["bytes", "bytes[]", "uint256"]
    from eth_abi import decode as abi_decode

    head = abi_decode(types, bytes.fromhex(encoded.calldata_hex[10:]))
    tampered_calldata = (
        encoded.calldata_hex[:10] + abi_encode(types, [tampered_commands, head[1], head[2]]).hex()
    )

    with pytest.raises(CommandSecurityError, match="bit 7.*allow_revert"):
        verify_dual_decoded_calldata(tampered_calldata, plan)


def test_c11_intermediate_hop_recipient_strictly_router() -> None:
    weth = _make_asset(WETH_ADDRESS_4663)
    usdg = _make_asset(USDG_ADDRESS_4663)
    hop1 = _make_v3_hop(weth, usdg, fee_raw=500)
    hop2 = _make_v4_hop(usdg, weth, fee_raw=500, tick_spacing=10)
    plan = _make_plan([hop1, hop2])

    encoded = encode_execution_plan(plan)
    ver = encoded.verification_result
    assert ver is not None
    assert len(ver.intermediate_recipients) == 1
    assert ver.intermediate_recipients[0].lower() == ADDRESS_THIS.lower()


def test_c11_final_recipient_sender_required() -> None:
    weth = _make_asset(WETH_ADDRESS_4663)
    usdg = _make_asset(USDG_ADDRESS_4663)
    hop1 = _make_v3_hop(weth, usdg, fee_raw=500)
    hop2 = _make_v3_hop(usdg, weth, fee_raw=500)
    plan = _make_plan([hop1, hop2])

    encoded = encode_execution_plan(plan)
    assert encoded.verification_result is not None
    assert encoded.verification_result.final_recipient.lower() == MSG_SENDER.lower()


def test_c11_final_recipient_router_rejected() -> None:
    weth = _make_asset(WETH_ADDRESS_4663)
    usdg = _make_asset(USDG_ADDRESS_4663)
    hop1 = _make_v3_hop(weth, usdg, fee_raw=500)
    hop2 = _make_v3_hop(usdg, weth, fee_raw=500)
    plan = _make_plan([hop1, hop2])

    with pytest.raises(CommandSecurityError, match="ROUTER cannot be the final recipient"):
        encode_execution_plan(plan, recipient=ADDRESS_THIS)


def test_c11_unauthorized_third_party_recipient_rejected() -> None:
    weth = _make_asset(WETH_ADDRESS_4663)
    usdg = _make_asset(USDG_ADDRESS_4663)
    hop1 = _make_v3_hop(weth, usdg, fee_raw=500)
    hop2 = _make_v3_hop(usdg, weth, fee_raw=500)
    plan = _make_plan([hop1, hop2])

    unauthorized = "0x9999999999999999999999999999999999999999"
    with pytest.raises(CommandSecurityError, match="allowed_recipients not provided"):
        encode_execution_plan(plan, recipient=unauthorized)

    with pytest.raises(CommandSecurityError, match="not in authorized recipients list"):
        encode_execution_plan(
            plan,
            recipient=unauthorized,
            allowed_recipients={"0x1111111111111111111111111111111111111111"},
        )


def test_c11_min_amount_out_rigid_binding() -> None:
    weth = _make_asset(WETH_ADDRESS_4663)
    usdg = _make_asset(USDG_ADDRESS_4663)
    hop1 = _make_v3_hop(weth, usdg, fee_raw=500)
    hop2 = _make_v4_hop(usdg, weth, fee_raw=500, tick_spacing=10)
    plan = _make_plan([hop1, hop2], min_amount_out_atoms=100_333_000_000_000_000)

    encoded = encode_execution_plan(plan)
    assert encoded.min_amount_out == 100_333_000_000_000_000
    assert encoded.verification_result is not None
    assert encoded.verification_result.min_amount_out == 100_333_000_000_000_000


def test_c11_forbidden_command_sweep_rejected() -> None:
    with pytest.raises(UnsupportedProtocolError, match="Command ID 0x04.*unsupported"):
        validate_commands_security(b"\x04")


def test_c11_forbidden_command_v2_rejected() -> None:
    with pytest.raises(UnsupportedProtocolError, match="Command ID 0x08.*unsupported"):
        validate_commands_security(b"\x08")


def test_c11_forbidden_command_unknown_rejected() -> None:
    with pytest.raises(UnsupportedProtocolError, match="unsupported or un-audited"):
        validate_commands_security(b"\x3f")


def test_c11_v4_take_settle_paired_actions() -> None:
    weth = _make_asset(WETH_ADDRESS_4663)
    usdg = _make_asset(USDG_ADDRESS_4663)
    hop1 = _make_v4_hop(weth, usdg, fee_raw=500, tick_spacing=10)
    hop2 = _make_v4_hop(usdg, weth, fee_raw=500, tick_spacing=10)
    plan = _make_plan([hop1, hop2])

    encoded = encode_execution_plan(plan)
    codec = RouterCodec()
    fct, decoded = codec.decode.function_input(cast(HexStr, encoded.calldata_hex))
    v4_actions = [item[0].fn_name for item in decoded["inputs"][0][1]["params"]]
    assert "SETTLE" in v4_actions
    assert "TAKE_ALL" in v4_actions or "TAKE" in v4_actions


# ---------------------------------------------------------------------------
# C12 Tests: Protocol Whitelist, V4 Currency Ordering, and No Capability Promotion
# ---------------------------------------------------------------------------


def test_c12_protocol_whitelist_v3_accepted() -> None:
    weth = _make_asset(WETH_ADDRESS_4663)
    usdg = _make_asset(USDG_ADDRESS_4663)
    hop1 = _make_v3_hop(weth, usdg, fee_raw=500)
    hop2 = _make_v3_hop(usdg, weth, fee_raw=500)
    plan = _make_plan([hop1, hop2])
    encoded = encode_execution_plan(plan)
    assert encoded.status == "ENCODED"


def test_c12_protocol_whitelist_v4_accepted() -> None:
    weth = _make_asset(WETH_ADDRESS_4663)
    usdg = _make_asset(USDG_ADDRESS_4663)
    hop1 = _make_v4_hop(weth, usdg, fee_raw=500, tick_spacing=10)
    hop2 = _make_v4_hop(usdg, weth, fee_raw=500, tick_spacing=10)
    plan = _make_plan([hop1, hop2])
    encoded = encode_execution_plan(plan)
    assert encoded.status == "ENCODED"


def test_c12_protocol_whitelist_v2_rejected() -> None:
    weth = _make_asset(WETH_ADDRESS_4663)
    usdg = _make_asset(USDG_ADDRESS_4663)
    pk_v2 = _make_pool_key(TEST_POOL_1, protocol_id="uniswap_v2")
    hop_v2 = HopRef(
        pool_key=pk_v2,
        asset_in=weth,
        asset_out=usdg,
        direction="zero_for_one",
        pool_descriptor=PoolDescriptor(
            key=pk_v2,
            currency0=weth,
            currency1=usdg,
            fee_model=FeeModel.static(3000),
        ),
    )
    hop_v3 = _make_v3_hop(usdg, weth, fee_raw=500)
    plan = _make_plan([hop_v2, hop_v3])

    with pytest.raises(UnsupportedProtocolError, match="Uniswap V2 is strictly unsupported"):
        encode_execution_plan(plan)


def test_c12_protocol_whitelist_unknown_rejected() -> None:
    weth = _make_asset(WETH_ADDRESS_4663)
    usdg = _make_asset(USDG_ADDRESS_4663)
    pk_unknown = _make_pool_key(TEST_POOL_1, protocol_id="curve_finance")
    hop_unknown = HopRef(
        pool_key=pk_unknown,
        asset_in=weth,
        asset_out=usdg,
        direction="zero_for_one",
        pool_descriptor=PoolDescriptor(
            key=pk_unknown,
            currency0=weth,
            currency1=usdg,
            fee_model=FeeModel.static(400),
        ),
    )
    hop_v3 = _make_v3_hop(usdg, weth, fee_raw=500)
    plan = _make_plan([hop_unknown, hop_v3])

    with pytest.raises(UnsupportedProtocolError, match="Only Uniswap V3 and V4 are permitted"):
        encode_execution_plan(plan)


def test_c12_v4_nonzero_hook_rejected() -> None:
    weth = _make_asset(WETH_ADDRESS_4663)
    usdg = _make_asset(USDG_ADDRESS_4663)
    custom_hook = "0x8888888888888888888888888888888888888888"
    hop_bad_hook = _make_v4_hop(weth, usdg, fee_raw=500, tick_spacing=10, hooks=custom_hook)
    hop_ok = _make_v4_hop(usdg, weth, fee_raw=500, tick_spacing=10)
    plan = _make_plan([hop_bad_hook, hop_ok])

    with pytest.raises(CommandSecurityError, match="only zero-hook V4 pools are permitted"):
        encode_execution_plan(plan)


def test_c12_v4_dynamic_fee_rejected() -> None:
    weth = _make_asset(WETH_ADDRESS_4663)
    usdg = _make_asset(USDG_ADDRESS_4663)
    dynamic_fee = FeeModel.dynamic(hook_ref="0x0000000000000000000000000000000000000000")
    hop_dynamic = _make_v4_hop(weth, usdg, fee_model=dynamic_fee, tick_spacing=10)
    hop_ok = _make_v4_hop(usdg, weth, fee_raw=500, tick_spacing=10)
    plan = _make_plan([hop_dynamic, hop_ok])

    with pytest.raises(CommandSecurityError, match="dynamic fees forbidden"):
        encode_execution_plan(plan)


def test_c12_v4_currency_order_strict() -> None:
    weth = _make_asset(WETH_ADDRESS_4663)
    usdg = _make_asset(USDG_ADDRESS_4663)

    # Case A: weth -> usdg (int(weth) < int(usdg))
    hop_fwd = _make_v4_hop(weth, usdg, fee_raw=500, tick_spacing=10)
    pk_dict_fwd, zfo_fwd = resolve_v4_pool_key(hop_fwd)
    assert int(pk_dict_fwd["currency0"], 16) < int(pk_dict_fwd["currency1"], 16)
    assert zfo_fwd is True

    # Case B: usdg -> weth (int(usdg) > int(weth))
    hop_rev = _make_v4_hop(usdg, weth, fee_raw=500, tick_spacing=10)
    pk_dict_rev, zfo_rev = resolve_v4_pool_key(hop_rev)
    assert int(pk_dict_rev["currency0"], 16) < int(pk_dict_rev["currency1"], 16)
    assert zfo_rev is False


def test_c12_encoded_status_strictly_no_promotion() -> None:
    weth = _make_asset(WETH_ADDRESS_4663)
    usdg = _make_asset(USDG_ADDRESS_4663)
    hop1 = _make_v3_hop(weth, usdg, fee_raw=500)
    hop2 = _make_v3_hop(usdg, weth, fee_raw=500)
    plan = _make_plan([hop1, hop2])

    encoded = encode_execution_plan(plan)
    assert encoded.status == "ENCODED"
    assert encoded.can_atomic_execute is False

    # Assert constructor rejects any attempt to set can_atomic_execute=True
    with pytest.raises(CommandSecurityError, match="Offline encoding cannot promote"):
        EncodedCalldata(
            plan_id="p1",
            route_id="r1",
            chain_id=4663,
            router_address=CANONICAL_UNIVERSAL_ROUTER,
            calldata_hex=encoded.calldata_hex,
            calldata_sha256=encoded.calldata_sha256,
            commands_hex=encoded.commands_hex,
            commands_count=1,
            deadline=plan.deadline,
            amount_in=plan.amount_in.atoms,
            min_amount_out=plan.min_amount_out.atoms,
            can_atomic_execute=True,
        )


def test_c12_unsupported_chain_rejected() -> None:
    weth_bsc = _make_asset(WETH_ADDRESS_4663, chain_id=56)
    usdg_bsc = _make_asset(USDG_ADDRESS_4663, chain_id=56)
    hop1 = _make_v3_hop(weth_bsc, usdg_bsc, fee_raw=500, chain_id=56)
    hop2 = _make_v3_hop(usdg_bsc, weth_bsc, fee_raw=500, chain_id=56)
    plan = _make_plan([hop1, hop2], chain_id=56)

    with pytest.raises(UnsupportedChainError, match="Chain ID 56 is unsupported"):
        encode_execution_plan(plan)


def test_c12_roundtrip_serialization_and_evidence() -> None:
    weth = _make_asset(WETH_ADDRESS_4663)
    usdg = _make_asset(USDG_ADDRESS_4663)
    hop1 = _make_v3_hop(weth, usdg, fee_raw=500)
    hop2 = _make_v3_hop(usdg, weth, fee_raw=500)
    plan = _make_plan([hop1, hop2])

    encoded = encode_execution_plan(plan)
    data = encoded.to_dict()
    assert isinstance(data, dict)
    reconstructed = EncodedCalldata.from_dict(data)
    assert reconstructed.plan_id == encoded.plan_id
    assert reconstructed.calldata_sha256 == encoded.calldata_sha256
    assert reconstructed.dual_verified == encoded.dual_verified

    json_str = encoded.to_json()
    assert isinstance(json_str, str)
    from_json_obj = EncodedCalldata.from_json(json_str)
    assert from_json_obj.calldata_sha256 == encoded.calldata_sha256

    evidence = encoded.to_simulation_evidence(
        block_number=123456,
        block_hash="0x" + "a" * 64,
        from_address=MSG_SENDER,
    )
    assert isinstance(evidence, DraftSimulationEvidence)
    assert evidence.calldata_sha256 == encoded.calldata_sha256
    assert evidence.is_draft is True
    assert evidence.status == "CALL_SUCCEEDED"
