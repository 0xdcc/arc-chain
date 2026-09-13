"""Independent regression test suite for legacy Uniswap V4 plan metadata obligations.

Migrated from legacy test obligation:
`TestExecutorV4CalldataAlignment.test_plan_from_triangular_alert_populates_v4_leg_metadata`
in `tests/test_v4_poolkey.py` (lines 216-295).

Architectural Context & Separation of Concerns:
1. 旧策略入口专属未提供 (Legacy Strategy Entry Point Not Provided in Arc):
   - In Robinhood legacy execution, `TriangularArbAlert` -> `WethArbitrageExecutor.plan_from_triangular_alert`
     was an ad-hoc strategy orchestrator converting alert objects into execution plans.
   - In Arc (read-only research engine for chain 5042), neither `arbitrage.triangular` nor
     `WethArbitrageExecutor` exists or has consumers in the production architecture.
   - Arc strictly models execution through `QuoteEvidence` -> `ArcPlanAssembler` -> `ArcExecutionPlan`.
   - In accordance with task specifications, no fake bridge or `sys.modules` monkey-patching stubs
     are fabricated for the missing legacy alert entry point.

2. 通用metadata跨计划/编码传递 (Universal Metadata Cross-Plan & Calldata Encoding Propagation):
   - The fundamental engineering obligation of the legacy test is verifying that Uniswap V4 leg metadata
     (5-tuple: manager, currency0, currency1, fee, tickSpacing, hooks; plus direction and amount) is:
     a) Faithfully preserved through planning domain models without loss or corruption.
     b) Guarded by Arc production safety policies (e.g. Arc 5042 encoder fails closed on unverified V4 legs).
     c) Correctly assembled into Universal Router calldata (both mixed V3/V4 sequential swaps and pure V4 PathKeys).
     d) Independently verified via low-level EVM ABI decoding (not subject-under-test self-comparison).
     e) Enforced with zero-hook default filling and fail-closed dynamic/unknown fee rejection.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest
from eth_abi import decode as independent_abi_decode
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
    HopQuote,
    HopRef,
    QuoteEvidence,
    QuoteStatus,
    RouteRef,
    TriState,
)
from atomic_execution.arc_encoding import (
    ADDRESS_THIS,
    ArcEncodingError,
    encode_arc_execution_plan,
)
from atomic_execution.arc_planning import (
    ArcExecutionPlan,
    ArcPlanAssembler,
)
from atomic_execution.deployments import (
    ArcExecutionDeploymentBinding,
)
from atomic_execution.encoding import (
    COMMAND_V3_SWAP_EXACT_IN,
    COMMAND_V4_SWAP,
    EXECUTE_SELECTOR_WITH_DEADLINE,
    FLAG_ALLOW_REVERT,
    ROBINHOOD_CHAIN_ID,
    ROUTER_BALANCE,
    ZERO_ADDRESS,
    CommandSecurityError,
    EncodingError,
    encode_execution_plan,
    resolve_v4_pool_key,
)
from atomic_execution.planning import (
    CANONICAL_UNIVERSAL_ROUTER,
    ExecutionPlan,
)
from atomic_execution.policy import ExecutionPolicy

# ---------------------------------------------------------------------------
# Canonical Test Constants (aligned verbatim with tests/test_v4_poolkey.py)
# ---------------------------------------------------------------------------
CHAIN_ARC = 5042
CHAIN_ROBINHOOD = ROBINHOOD_CHAIN_ID

AI_USDG_V4_POOL_ID = "0x7aebd80541bfaaf23dbb6e99ce13d4d31c1a84c91414f971eadbff7db5f85995"
AI_TOKEN_ADDRESS = "0x2E8c31162b855A2ffa90F6F8634643Ad6F111e18"
USDG_TOKEN_ADDRESS = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
CANONICAL_WETH_ADDRESS = "0x4200000000000000000000000000000000000006"

# Synthetic verified infrastructure addresses for isolated tests
ARC_ROUTER_ADDRESS = "0x5555555555555555555555555555555555555042"
ARC_V4_MANAGER_ADDRESS = "0x2222222222222222222222222222222222225042"
V3_POOL_1 = "0xc4a21f9d6485fc5893dd4a491b320a83daf4da1d"
V3_POOL_2 = "0x52e65b17fb6e5ba00ed806f37afcd2daa50271ca"


# ---------------------------------------------------------------------------
# Independent EVM ABI Calldata Decoders (Pure ABI, Zero Self-Comparison)
# ---------------------------------------------------------------------------
def _decode_execute_calldata(calldata_hex: str) -> tuple[bytes, list[bytes], int]:
    """Independently decode Universal Router execute(bytes commands, bytes[] inputs, uint256 deadline)."""
    assert calldata_hex.startswith(EXECUTE_SELECTOR_WITH_DEADLINE), (
        f"Calldata does not start with execute selector {EXECUTE_SELECTOR_WITH_DEADLINE}"
    )
    raw_payload = bytes.fromhex(calldata_hex[10:])
    commands, inputs, deadline = independent_abi_decode(
        ["bytes", "bytes[]", "uint256"], raw_payload
    )
    return commands, inputs, deadline


def _decode_v4_swap_exact_in_single(
    v4_input_bytes: bytes,
) -> tuple[dict[str, Any], bool, int, int]:
    """Independently decode V4 SWAP_EXACT_IN_SINGLE action calldata using standard EVM ABI.

    Decodes actions bytes and parameters, locates SWAP_EXACT_IN_SINGLE (action 0x06),
    and unpacks the canonical PoolKey tuple:
    ((currency0, currency1, fee, tickSpacing, hooks), zeroForOne, amountIn, amountOutMinimum, minHopPriceX36, hookData)
    """
    actions, params = independent_abi_decode(["bytes", "bytes[]"], v4_input_bytes)
    assert 0x06 in actions, f"SWAP_EXACT_IN_SINGLE (0x06) not found in actions: {list(actions)}"
    swap_index = list(actions).index(0x06)

    # exact_in_single_params ABI signature:
    # ((address,address,uint24,int24,address),bool,uint128,uint128,uint256,bytes)
    decoded_struct = independent_abi_decode(
        ["((address,address,uint24,int24,address),bool,uint128,uint128,uint256,bytes)"],
        params[swap_index],
    )[0]

    pk_tuple, zero_for_one, amount_in, amount_out_min, _min_hop, _hook_data = decoded_struct
    pool_key = {
        "currency0": pk_tuple[0],
        "currency1": pk_tuple[1],
        "fee": pk_tuple[2],
        "tickSpacing": pk_tuple[3],
        "hooks": pk_tuple[4],
    }
    return pool_key, zero_for_one, amount_in, amount_out_min


def _decode_v4_swap_exact_in_path_keys(
    v4_input_bytes: bytes,
) -> tuple[str, list[dict[str, Any]], int, int]:
    """Independently decode V4 pure multi-hop SWAP_EXACT_IN action calldata using standard EVM ABI.

    Decodes actions bytes and parameters, locates SWAP_EXACT_IN (action 0x07),
    and unpacks the PathKeys struct array:
    (currencyIn, (intermediateCurrency, fee, tickSpacing, hooks, hookData)[], minHopPriceX36[], amountIn, amountOutMinimum)
    """
    actions, params = independent_abi_decode(["bytes", "bytes[]"], v4_input_bytes)
    assert 0x07 in actions, f"SWAP_EXACT_IN (0x07) not found in actions: {list(actions)}"
    swap_index = list(actions).index(0x07)

    decoded_struct = independent_abi_decode(
        ["(address,(address,uint24,int24,address,bytes)[],uint256[],uint128,uint128)"],
        params[swap_index],
    )[0]

    currency_in, raw_path_keys, _min_hop, amount_in, amount_out_min = decoded_struct
    path_keys = [
        {
            "intermediateCurrency": pk[0],
            "fee": pk[1],
            "tickSpacing": pk[2],
            "hooks": pk[3],
            "hookData": pk[4],
        }
        for pk in raw_path_keys
    ]
    return currency_in, path_keys, amount_in, amount_out_min


# ---------------------------------------------------------------------------
# Synthetic Test Factories (Strictly Aligned with Neighbor Tests)
# ---------------------------------------------------------------------------
def _make_asset(address: str, chain_id: int = CHAIN_ROBINHOOD) -> AssetRef:
    return AssetRef.erc20(TokenKey(chain_id, address))


def _make_v3_hop(
    asset_in: AssetRef,
    asset_out: AssetRef,
    fee_raw: int = 500,
    pool_id: str | None = None,
    chain_id: int = CHAIN_ROBINHOOD,
) -> HopRef:
    resolved_id = pool_id if pool_id is not None else "0x" + "11" * 20
    pk = PoolKey(
        chain_id=chain_id,
        protocol_id="uniswap_v3",
        venue_kind="factory",
        venue_address="0x" + "aa" * 20,
        pool_id_kind="address",
        pool_id=resolved_id,
    )
    assert asset_in.token_key is not None
    assert asset_out.token_key is not None
    in_int = int(asset_in.token_key.address, 16)
    out_int = int(asset_out.token_key.address, 16)
    c0 = asset_in if in_int < out_int else asset_out
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
    fee_raw: int = 2300,
    tick_spacing: int = 23,
    hooks: str | None = ZERO_ADDRESS,
    fee_model: FeeModel | None = None,
    pool_id: str = AI_USDG_V4_POOL_ID,
    chain_id: int = CHAIN_ROBINHOOD,
    manager_address: str = "0x" + "22" * 20,
) -> HopRef:
    pk = PoolKey(
        chain_id=chain_id,
        protocol_id="uniswap_v4",
        venue_kind="manager",
        venue_address=manager_address,
        pool_id_kind="bytes32",
        pool_id=pool_id,
    )
    assert asset_in.token_key is not None
    assert asset_out.token_key is not None
    in_int = int(asset_in.token_key.address, 16)
    out_int = int(asset_out.token_key.address, 16)
    c0 = asset_in if in_int < out_int else asset_out
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


def _make_execution_plan(
    hops: Sequence[HopRef],
    base_asset: AssetRef | None = None,
    amount_in_atoms: int = 10**17,
    expected_out_atoms: int = int(10**17 * 1.02),
    min_amount_out_atoms: int = int(10**17 * 1.01),
    deadline: int = 1700000120,
    chain_id: int = CHAIN_ROBINHOOD,
    plan_id: str = "test_plan_v4_metadata",
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
# Test Suite
# ---------------------------------------------------------------------------
class TestLegacyV4PlanMetadataObligations:
    """Test suite migrating legacy V4 plan leg metadata obligations into Arc architecture."""

    def test_arc_planner_preserves_v4_leg_metadata(self) -> None:
        """ArcPlanAssembler preserves exact V4 5-tuple, manager, tickSpacing, and hooks in ArcExecutionPlan."""
        binding = ArcExecutionDeploymentBinding(
            chain_id=CHAIN_ARC,
            router_address=ARC_ROUTER_ADDRESS,
            v4_pool_manager_address=ARC_V4_MANAGER_ADDRESS,
        )
        assembler = ArcPlanAssembler(deployment=binding)

        # Build 3-hop mixed route on Arc (USDC -> WETH [V3], WETH -> USDG [V4], USDG -> USDC [V3])
        usdc_asset = _make_asset("0x" + "36" * 20, chain_id=CHAIN_ARC)
        weth_asset = _make_asset(CANONICAL_WETH_ADDRESS, chain_id=CHAIN_ARC)
        usdg_asset = _make_asset(USDG_TOKEN_ADDRESS, chain_id=CHAIN_ARC)

        hop1 = _make_v3_hop(usdc_asset, weth_asset, fee_raw=500, pool_id=V3_POOL_1, chain_id=CHAIN_ARC)
        hop2_v4 = _make_v4_hop(
            weth_asset,
            usdg_asset,
            fee_raw=2300,
            tick_spacing=23,
            hooks=ZERO_ADDRESS,
            pool_id=AI_USDG_V4_POOL_ID,
            chain_id=CHAIN_ARC,
            manager_address=ARC_V4_MANAGER_ADDRESS,
        )
        hop3 = _make_v3_hop(usdg_asset, usdc_asset, fee_raw=100, pool_id=V3_POOL_2, chain_id=CHAIN_ARC)

        route = RouteRef(chain_id=CHAIN_ARC, base_asset=usdc_asset, hops=(hop1, hop2_v4, hop3))
        hq1 = HopQuote(0, hop1.pool_key, usdc_asset, weth_asset, Amount(usdc_asset, 100_000_000, 6), Amount(weth_asset, 50_000_000_000_000_000, 18), status=QuoteStatus.QUOTED, fee_model=hop1.pool_descriptor.fee_model if hop1.pool_descriptor else None)
        hq2 = HopQuote(1, hop2_v4.pool_key, weth_asset, usdg_asset, Amount(weth_asset, 50_000_000_000_000_000, 18), Amount(usdg_asset, 101_000_000, 6), status=QuoteStatus.QUOTED, fee_model=hop2_v4.pool_descriptor.fee_model if hop2_v4.pool_descriptor else None)
        hq3 = HopQuote(2, hop3.pool_key, usdg_asset, usdc_asset, Amount(usdg_asset, 101_000_000, 6), Amount(usdc_asset, 103_000_000, 6), status=QuoteStatus.QUOTED, fee_model=hop3.pool_descriptor.fee_model if hop3.pool_descriptor else None)

        quote = QuoteEvidence(
            quote_id="q-arc-v4-metadata-001",
            route_ref=route,
            amount_in=Amount(usdc_asset, 100_000_000, 6),
            amount_out=Amount(usdc_asset, 103_000_000, 6),
            delta_atoms=3_000_000,
            hop_quotes=(hq1, hq2, hq3),
            state_version_ref="state:v1:" + "aa" * 32,
            started_at_ms=1000,
            finished_at_ms=1010,
            status=QuoteStatus.QUOTED,
            fee_included=TriState.YES,
            impact_included=TriState.YES,
        )

        plan = assembler.assemble_plan(quote=quote, base_asset_usd_price=Decimal("1.0"))
        assert isinstance(plan, ArcExecutionPlan)

        # Verify plan invariants and leg metadata preservation
        assert len(plan.route_ref.hops) == 3
        v4_leg = plan.route_ref.hops[1]
        assert v4_leg.pool_key.protocol_id == "uniswap_v4"
        assert v4_leg.pool_key.venue_kind == "manager"
        assert v4_leg.pool_key.canonical_venue_address == ARC_V4_MANAGER_ADDRESS.lower()
        assert v4_leg.pool_key.canonical_pool_id == AI_USDG_V4_POOL_ID.lower()

        assert v4_leg.pool_descriptor is not None
        assert v4_leg.pool_descriptor.fee_model.raw_value == 2300
        assert v4_leg.pool_descriptor.tick_spacing == 23
        assert v4_leg.pool_descriptor.hooks == ZERO_ADDRESS
        assert v4_leg.direction == "zero_for_one"

        # Verify execution guardrails on plan
        assert plan.can_atomic_execute is False
        assert plan.value_atoms == 0
        assert plan.target_router == ARC_ROUTER_ADDRESS

    def test_arc_encoding_fails_closed_for_v4_leg(self) -> None:
        """Arc native encoder strictly rejects V4 legs under current verified V3-only safety policy."""
        binding = ArcExecutionDeploymentBinding(
            chain_id=CHAIN_ARC,
            router_address=ARC_ROUTER_ADDRESS,
            v4_pool_manager_address=ARC_V4_MANAGER_ADDRESS,
        )
        assembler = ArcPlanAssembler(deployment=binding)

        usdc_asset = _make_asset("0x" + "36" * 20, chain_id=CHAIN_ARC)
        weth_asset = _make_asset(CANONICAL_WETH_ADDRESS, chain_id=CHAIN_ARC)
        usdg_asset = _make_asset(USDG_TOKEN_ADDRESS, chain_id=CHAIN_ARC)

        hop1 = _make_v3_hop(usdc_asset, weth_asset, fee_raw=500, pool_id=V3_POOL_1, chain_id=CHAIN_ARC)
        hop2_v4 = _make_v4_hop(
            weth_asset,
            usdg_asset,
            fee_raw=2300,
            tick_spacing=23,
            hooks=ZERO_ADDRESS,
            pool_id=AI_USDG_V4_POOL_ID,
            chain_id=CHAIN_ARC,
            manager_address=ARC_V4_MANAGER_ADDRESS,
        )
        hop3 = _make_v3_hop(usdg_asset, usdc_asset, fee_raw=100, pool_id=V3_POOL_2, chain_id=CHAIN_ARC)

        route = RouteRef(chain_id=CHAIN_ARC, base_asset=usdc_asset, hops=(hop1, hop2_v4, hop3))
        hq1 = HopQuote(0, hop1.pool_key, usdc_asset, weth_asset, Amount(usdc_asset, 100_000_000, 6), Amount(weth_asset, 50_000_000_000_000_000, 18), status=QuoteStatus.QUOTED, fee_model=hop1.pool_descriptor.fee_model if hop1.pool_descriptor else None)
        hq2 = HopQuote(1, hop2_v4.pool_key, weth_asset, usdg_asset, Amount(weth_asset, 50_000_000_000_000_000, 18), Amount(usdg_asset, 101_000_000, 6), status=QuoteStatus.QUOTED, fee_model=hop2_v4.pool_descriptor.fee_model if hop2_v4.pool_descriptor else None)
        hq3 = HopQuote(2, hop3.pool_key, usdg_asset, usdc_asset, Amount(usdg_asset, 101_000_000, 6), Amount(usdc_asset, 103_000_000, 6), status=QuoteStatus.QUOTED, fee_model=hop3.pool_descriptor.fee_model if hop3.pool_descriptor else None)

        quote = QuoteEvidence(
            quote_id="q-arc-v4-gate-002",
            route_ref=route,
            amount_in=Amount(usdc_asset, 100_000_000, 6),
            amount_out=Amount(usdc_asset, 103_000_000, 6),
            delta_atoms=3_000_000,
            hop_quotes=(hq1, hq2, hq3),
            state_version_ref="state:v1:" + "aa" * 32,
            started_at_ms=1000,
            finished_at_ms=1010,
            status=QuoteStatus.QUOTED,
            fee_included=TriState.YES,
            impact_included=TriState.YES,
        )

        plan = assembler.assemble_plan(quote=quote, base_asset_usd_price=Decimal("1.0"))

        with pytest.raises(ArcEncodingError, match="Unsupported protocol/hook/native leg; encoder supports verified V3 only"):
            encode_arc_execution_plan(plan=plan)

    def test_mixed_3hop_v4_calldata_independent_abi_decoding(self) -> None:
        """Universal Router mixed V3/V4/V3 swap encodes exact PoolKey matching AI/USDG 0.23% with independent ABI decode."""
        weth = _make_asset(CANONICAL_WETH_ADDRESS)
        ai = _make_asset(AI_TOKEN_ADDRESS)
        usdg = _make_asset(USDG_TOKEN_ADDRESS)

        # Leg 0: WETH -> AI (V3 1%)
        hop0 = _make_v3_hop(weth, ai, fee_raw=10000, pool_id=V3_POOL_1)
        # Leg 1: AI -> USDG (V4 0.23%, tick_spacing=23, hooks=ZERO_ADDRESS)
        hop1_v4 = _make_v4_hop(
            ai,
            usdg,
            fee_raw=2300,
            tick_spacing=23,
            hooks=ZERO_ADDRESS,
            pool_id=AI_USDG_V4_POOL_ID,
        )
        # Leg 2: USDG -> WETH (V3 0.01%)
        hop2 = _make_v3_hop(usdg, weth, fee_raw=100, pool_id=V3_POOL_2)

        plan = _make_execution_plan([hop0, hop1_v4, hop2], base_asset=weth, amount_in_atoms=10**17)
        encoded = encode_execution_plan(plan)

        # Verify command encoding format
        assert encoded.commands_hex == "0x001000"
        assert encoded.commands_count == 3
        assert encoded.calldata_hex.startswith(EXECUTE_SELECTOR_WITH_DEADLINE)

        # Independent EVM ABI decoding (zero self-comparison against test output)
        commands, inputs, deadline = _decode_execute_calldata(encoded.calldata_hex)
        assert commands == bytes([COMMAND_V3_SWAP_EXACT_IN, COMMAND_V4_SWAP, COMMAND_V3_SWAP_EXACT_IN])
        assert deadline == plan.deadline

        # Verify Bit 7 (allow_revert) is strictly unset across all commands
        for cmd in commands:
            assert (cmd & FLAG_ALLOW_REVERT) == 0, f"Bit 7 (allow_revert) detected in command 0x{cmd:02x}"

        # Decode V4 command (inputs[1])
        actions, params = independent_abi_decode(["bytes", "bytes[]"], inputs[1])
        assert actions == bytes([0x0B, 0x06, 0x0E]), f"Expected SETTLE, SWAP, TAKE actions, got {list(actions)}"

        # Settle intermediate balance check
        settle_currency, settle_amount, payer_is_user = independent_abi_decode(["address", "uint256", "bool"], params[0])
        assert Web3.to_checksum_address(settle_currency) == Web3.to_checksum_address(AI_TOKEN_ADDRESS)
        assert settle_amount == ROUTER_BALANCE
        assert payer_is_user is False

        # Decode SWAP_EXACT_IN_SINGLE
        pk_dict, zero_for_one, amount_in, amount_out_min = _decode_v4_swap_exact_in_single(inputs[1])

        # Assert PoolKey exact 5-tuple matches input metadata
        assert Web3.to_checksum_address(pk_dict["currency0"]) == Web3.to_checksum_address(AI_TOKEN_ADDRESS)
        assert Web3.to_checksum_address(pk_dict["currency1"]) == Web3.to_checksum_address(USDG_TOKEN_ADDRESS)
        assert pk_dict["fee"] == 2300
        assert pk_dict["tickSpacing"] == 23
        assert Web3.to_checksum_address(pk_dict["hooks"]) == Web3.to_checksum_address(ZERO_ADDRESS)

        # Direction and chaining checks
        assert zero_for_one is True, "AI -> USDG must be zero_for_one=True"
        assert amount_in == 0, "Intermediate V4 hop must specify amount_in=0 (using router balance)"
        assert amount_out_min == 1, "Intermediate V4 hop must enforce amount_out_min=1 atom"

        # Take intermediate balance check
        take_currency, take_recipient, take_amount = independent_abi_decode(["address", "address", "uint256"], params[2])
        assert Web3.to_checksum_address(take_currency) == Web3.to_checksum_address(USDG_TOKEN_ADDRESS)
        assert Web3.to_checksum_address(take_recipient) == Web3.to_checksum_address(ADDRESS_THIS)
        assert take_amount == 0

    def test_mixed_3hop_v4_reverse_direction_decoding(self) -> None:
        """Universal Router mixed swap with reverse direction (USDG -> AI) preserves PoolKey and flips zero_for_one."""
        weth = _make_asset(CANONICAL_WETH_ADDRESS)
        ai = _make_asset(AI_TOKEN_ADDRESS)
        usdg = _make_asset(USDG_TOKEN_ADDRESS)

        hop0 = _make_v3_hop(weth, usdg, fee_raw=100, pool_id=V3_POOL_2)
        # Reverse hop: USDG -> AI (one_for_zero)
        hop1_v4_rev = _make_v4_hop(
            usdg,
            ai,
            fee_raw=2300,
            tick_spacing=23,
            hooks=ZERO_ADDRESS,
            pool_id=AI_USDG_V4_POOL_ID,
        )
        hop2 = _make_v3_hop(ai, weth, fee_raw=10000, pool_id=V3_POOL_1)

        plan = _make_execution_plan([hop0, hop1_v4_rev, hop2], base_asset=weth, amount_in_atoms=10**17)
        encoded = encode_execution_plan(plan)

        _commands, inputs, _deadline = _decode_execute_calldata(encoded.calldata_hex)
        pk_dict, zero_for_one, amount_in, amount_out_min = _decode_v4_swap_exact_in_single(inputs[1])

        # currency0 < currency1 lexicographical ordering invariant preserved
        assert Web3.to_checksum_address(pk_dict["currency0"]) == Web3.to_checksum_address(AI_TOKEN_ADDRESS)
        assert Web3.to_checksum_address(pk_dict["currency1"]) == Web3.to_checksum_address(USDG_TOKEN_ADDRESS)
        assert pk_dict["fee"] == 2300
        assert pk_dict["tickSpacing"] == 23
        assert Web3.to_checksum_address(pk_dict["hooks"]) == Web3.to_checksum_address(ZERO_ADDRESS)

        # zero_for_one must be False for one_for_zero swap
        assert zero_for_one is False, "USDG -> AI must decode to zero_for_one=False"
        assert amount_in == 0
        assert amount_out_min == 1

    def test_pure_v4_multihop_pathkeys_independent_abi_decoding(self) -> None:
        """Universal Router pure V4 swap encodes exact PathKeys with fee, tickSpacing, and hooks."""
        weth = _make_asset(CANONICAL_WETH_ADDRESS)
        usdg = _make_asset(USDG_TOKEN_ADDRESS)
        ai = _make_asset(AI_TOKEN_ADDRESS)

        # 3 pure V4 hops
        hop0 = _make_v4_hop(weth, usdg, fee_raw=100, tick_spacing=1, hooks=ZERO_ADDRESS, pool_id="0x" + "01" * 32)
        hop1 = _make_v4_hop(usdg, ai, fee_raw=2300, tick_spacing=23, hooks=ZERO_ADDRESS, pool_id=AI_USDG_V4_POOL_ID)
        hop2 = _make_v4_hop(ai, weth, fee_raw=10000, tick_spacing=200, hooks=ZERO_ADDRESS, pool_id="0x" + "03" * 32)

        plan = _make_execution_plan([hop0, hop1, hop2], base_asset=weth, amount_in_atoms=10**17)
        encoded = encode_execution_plan(plan)

        assert encoded.commands_hex == "0x10"
        assert encoded.commands_count == 1

        commands, inputs, _deadline = _decode_execute_calldata(encoded.calldata_hex)
        assert commands == bytes([COMMAND_V4_SWAP])

        curr_in, path_keys, amt_in, amt_out_min = _decode_v4_swap_exact_in_path_keys(inputs[0])
        assert Web3.to_checksum_address(curr_in) == Web3.to_checksum_address(CANONICAL_WETH_ADDRESS)
        assert amt_in == 10**17
        assert amt_out_min == plan.min_amount_out.atoms
        assert len(path_keys) == 3

        # PathKey 0
        assert Web3.to_checksum_address(path_keys[0]["intermediateCurrency"]) == Web3.to_checksum_address(USDG_TOKEN_ADDRESS)
        assert path_keys[0]["fee"] == 100
        assert path_keys[0]["tickSpacing"] == 1
        assert Web3.to_checksum_address(path_keys[0]["hooks"]) == Web3.to_checksum_address(ZERO_ADDRESS)

        # PathKey 1 (Target V4 0.23% tick_spacing=23)
        assert Web3.to_checksum_address(path_keys[1]["intermediateCurrency"]) == Web3.to_checksum_address(AI_TOKEN_ADDRESS)
        assert path_keys[1]["fee"] == 2300
        assert path_keys[1]["tickSpacing"] == 23
        assert Web3.to_checksum_address(path_keys[1]["hooks"]) == Web3.to_checksum_address(ZERO_ADDRESS)

        # PathKey 2
        assert Web3.to_checksum_address(path_keys[2]["intermediateCurrency"]) == Web3.to_checksum_address(CANONICAL_WETH_ADDRESS)
        assert path_keys[2]["fee"] == 10000
        assert path_keys[2]["tickSpacing"] == 200
        assert Web3.to_checksum_address(path_keys[2]["hooks"]) == Web3.to_checksum_address(ZERO_ADDRESS)

    def test_unknown_hook_rejected(self) -> None:
        """Unknown or missing hook cannot be defaulted to zero: domain constructor allows unknown, but consumer resolution strictly fails-closed."""
        weth = _make_asset(CANONICAL_WETH_ADDRESS)
        ai = _make_asset(AI_TOKEN_ADDRESS)
        usdg = _make_asset(USDG_TOKEN_ADDRESS)
        pk = PoolKey(
            chain_id=CHAIN_ROBINHOOD,
            protocol_id="uniswap_v4",
            venue_kind="manager",
            venue_address="0x" + "22" * 20,
            pool_id_kind="bytes32",
            pool_id=AI_USDG_V4_POOL_ID,
        )
        fm = FeeModel.static(2300)

        # 1. Constructor rejects empty string hook (invalid EVM address length / prefix)
        empty_hook: str = ""
        with pytest.raises(ValueError, match="Invalid EVM address length or prefix: ''"):
            PoolDescriptor(
                key=pk,
                currency0=ai,
                currency1=usdg,
                fee_model=fm,
                tick_spacing=23,
                hooks=empty_hook,
            )

        with pytest.raises(ValueError, match="Invalid EVM address length or prefix: ''"):
            _make_v4_hop(ai, usdg, fee_raw=2300, tick_spacing=23, hooks=empty_hook)

        # 2. Constructor rejects non-string illegal type passed as hook via typed boundary variable
        invalid_type_hook: Any = 0
        with pytest.raises(TypeError, match="Address must be a string, got int"):
            PoolDescriptor(
                key=pk,
                currency0=ai,
                currency1=usdg,
                fee_model=fm,
                tick_spacing=23,
                hooks=invalid_type_hook,
            )

        # Parameterized check: Constructor rejects malformed hex / length boundary hooks
        for bad_hook in ["0x123", "0x" + "11" * 19, "0x" + "zz" * 20]:
            with pytest.raises(ValueError, match="Invalid EVM address"):
                PoolDescriptor(
                    key=pk,
                    currency0=ai,
                    currency1=usdg,
                    fee_model=fm,
                    tick_spacing=23,
                    hooks=bad_hook,
                )

        # 3. Domain model allows hooks=None as valid unknown/unspecified hook state
        desc_unknown = PoolDescriptor(
            key=pk,
            currency0=ai,
            currency1=usdg,
            fee_model=fm,
            tick_spacing=23,
            hooks=None,
        )
        assert desc_unknown.hooks is None

        # 4. Actual consumer entry: resolve_v4_pool_key strictly rejects unknown hook fail-closed
        hop_unknown = _make_v4_hop(ai, usdg, fee_raw=2300, tick_spacing=23, hooks=None)
        with pytest.raises(
            CommandSecurityError,
            match="unknown or missing hook",
        ):
            resolve_v4_pool_key(hop_unknown)

        # 5. Actual consumer entry: Universal Router encoder rejects plan containing unknown hook
        hop0 = _make_v3_hop(weth, ai, fee_raw=10000, pool_id=V3_POOL_1)
        hop2 = _make_v3_hop(usdg, weth, fee_raw=100, pool_id=V3_POOL_2)
        plan_unknown = _make_execution_plan([hop0, hop_unknown, hop2], base_asset=weth, amount_in_atoms=10**17)
        with pytest.raises(
            CommandSecurityError,
            match="unknown or missing hook",
        ):
            encode_execution_plan(plan_unknown)

        # 6. Independent control: Explicit ZERO_ADDRESS is legitimate for zero-hook V4 resolution
        # in the generic router path, but Arc native encoder strictly preserves V3-only fail-closed gate.
        # Universal router compatibility path must not be interpreted as Arc atomic execution capability.
        hop_zero_hook = _make_v4_hop(ai, usdg, fee_raw=2300, tick_spacing=23, hooks=ZERO_ADDRESS)
        pk_dict, zero_for_one = resolve_v4_pool_key(hop_zero_hook)
        assert Web3.to_checksum_address(pk_dict["hooks"]) == Web3.to_checksum_address(ZERO_ADDRESS)
        assert zero_for_one is True

        # Independent control: Arc 5042 native pipeline still strictly rejects V4 leg even with ZERO_ADDRESS
        binding = ArcExecutionDeploymentBinding(
            chain_id=CHAIN_ARC,
            router_address=ARC_ROUTER_ADDRESS,
            v4_pool_manager_address=ARC_V4_MANAGER_ADDRESS,
        )
        assembler = ArcPlanAssembler(deployment=binding)
        usdc_asset = _make_asset("0x" + "36" * 20, chain_id=CHAIN_ARC)
        weth_asset = _make_asset(CANONICAL_WETH_ADDRESS, chain_id=CHAIN_ARC)
        usdg_asset = _make_asset(USDG_TOKEN_ADDRESS, chain_id=CHAIN_ARC)

        hop1 = _make_v3_hop(usdc_asset, weth_asset, fee_raw=500, pool_id=V3_POOL_1, chain_id=CHAIN_ARC)
        hop2_v4 = _make_v4_hop(
            weth_asset,
            usdg_asset,
            fee_raw=2300,
            tick_spacing=23,
            hooks=ZERO_ADDRESS,
            pool_id=AI_USDG_V4_POOL_ID,
            chain_id=CHAIN_ARC,
            manager_address=ARC_V4_MANAGER_ADDRESS,
        )
        hop3 = _make_v3_hop(usdg_asset, usdc_asset, fee_raw=100, pool_id=V3_POOL_2, chain_id=CHAIN_ARC)

        route = RouteRef(chain_id=CHAIN_ARC, base_asset=usdc_asset, hops=(hop1, hop2_v4, hop3))
        hq1 = HopQuote(0, hop1.pool_key, usdc_asset, weth_asset, Amount(usdc_asset, 100_000_000, 6), Amount(weth_asset, 50_000_000_000_000_000, 18), status=QuoteStatus.QUOTED, fee_model=hop1.pool_descriptor.fee_model if hop1.pool_descriptor else None)
        hq2 = HopQuote(1, hop2_v4.pool_key, weth_asset, usdg_asset, Amount(weth_asset, 50_000_000_000_000_000, 18), Amount(usdg_asset, 101_000_000, 6), status=QuoteStatus.QUOTED, fee_model=hop2_v4.pool_descriptor.fee_model if hop2_v4.pool_descriptor else None)
        hq3 = HopQuote(2, hop3.pool_key, usdg_asset, usdc_asset, Amount(usdg_asset, 101_000_000, 6), Amount(usdc_asset, 103_000_000, 6), status=QuoteStatus.QUOTED, fee_model=hop3.pool_descriptor.fee_model if hop3.pool_descriptor else None)

        quote = QuoteEvidence(
            quote_id="q-arc-v4-control-002",
            route_ref=route,
            amount_in=Amount(usdc_asset, 100_000_000, 6),
            amount_out=Amount(usdc_asset, 103_000_000, 6),
            delta_atoms=3_000_000,
            hop_quotes=(hq1, hq2, hq3),
            state_version_ref="state:v1:" + "cc" * 32,
            started_at_ms=1000,
            finished_at_ms=1010,
            status=QuoteStatus.QUOTED,
            fee_included=TriState.YES,
            impact_included=TriState.YES,
        )

        arc_plan = assembler.assemble_plan(quote=quote, base_asset_usd_price=Decimal("1.0"))
        with pytest.raises(
            ArcEncodingError,
            match="Unsupported protocol/hook/native leg; encoder supports verified V3 only",
        ):
            encode_arc_execution_plan(plan=arc_plan)

    unknown_hook_rejected = test_unknown_hook_rejected

    def test_v4_security_policy_rejections(self) -> None:
        """Security policy strictly rejects non-zero hooks, dynamic fees, and invalid tick spacings fail-closed."""
        ai = _make_asset(AI_TOKEN_ADDRESS)
        usdg = _make_asset(USDG_TOKEN_ADDRESS)

        # 1. Non-zero hook rejection
        unauthorized_hook = "0x" + "11" * 20
        hop_bad_hook = _make_v4_hop(ai, usdg, hooks=unauthorized_hook)
        with pytest.raises(CommandSecurityError, match="only zero-hook V4 pools are permitted"):
            resolve_v4_pool_key(hop_bad_hook)

        # 2. Dynamic fee rejection
        dynamic_fee_model = FeeModel.dynamic(hook_ref=ZERO_ADDRESS)
        hop_dynamic_fee = _make_v4_hop(ai, usdg, fee_model=dynamic_fee_model)
        with pytest.raises(CommandSecurityError, match="dynamic fees forbidden per C12"):
            resolve_v4_pool_key(hop_dynamic_fee)

        # 3. Missing or non-positive tick_spacing rejection
        hop_zero_tick = _make_v4_hop(ai, usdg, tick_spacing=0)
        with pytest.raises(EncodingError, match="V4 pool requires positive tick_spacing"):
            resolve_v4_pool_key(hop_zero_tick)

        hop_neg_tick = _make_v4_hop(ai, usdg, tick_spacing=-5)
        with pytest.raises(EncodingError, match="V4 pool requires positive tick_spacing"):
            resolve_v4_pool_key(hop_neg_tick)

    def test_legacy_alert_entry_point_boundary_distinction(self) -> None:
        """Explicitly confirm legacy alert entry point is not provided in Arc, separating architectural concerns."""
        # 1. Verify TriangularArbAlert is not part of Arc planning or atomic execution
        import atomic_execution.arc_planning as arc_plan_mod
        import atomic_execution.planning as exec_plan_mod

        assert not hasattr(arc_plan_mod, "TriangularArbAlert"), (
            "TriangularArbAlert should not exist in Arc domain planning"
        )
        assert not hasattr(arc_plan_mod, "plan_from_triangular_alert"), (
            "plan_from_triangular_alert should not exist in Arc domain planning"
        )
        assert not hasattr(exec_plan_mod, "TriangularArbAlert")
        assert not hasattr(exec_plan_mod, "plan_from_triangular_alert")

        # 2. Confirm Arc uses QuoteEvidence -> ArcPlanAssembler pipeline
        assert hasattr(arc_plan_mod, "ArcPlanAssembler")
        assert hasattr(arc_plan_mod, "ArcExecutionPlan")
