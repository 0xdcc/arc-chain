"""Comprehensive unit and regression test suite for W5-E simulation adapter and transport.

Verifies acceptance criteria C13 ~ C17:
- C13: Caller wallet consistency, value_wei == 0, state overrides prohibited;
- C14: Fixed block number, unpinned tag rejection, block reorg detection;
- C15: Five-state classification (CALL_SUCCEEDED, OUTPUT_UNVERIFIED, CONTRACT_REVERT,
  RPC_ERROR, NODE_LIMITATION), zero-assumed output defense;
- C16: Simulation independence and multi-hop quoter masquerade defense;
- C17: Router inventory subsidy defense (zero balance of path tokens);
- Capability & Model safety: DraftSimulationEvidence.can_atomic_execute is strictly False.
"""

from __future__ import annotations

import ast
import json
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest

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
    EXECUTE_SELECTOR_WITH_DEADLINE,
    EncodedCalldata,
    compute_calldata_sha256,
    encode_execution_plan,
)
from atomic_execution.inputs import (
    ROBINHOOD_CHAIN_ID,
    USDG_ADDRESS_4663,
    WETH_ADDRESS_4663,
    ZERO_ADDRESS,
)
from atomic_execution.models import DraftSimulationEvidence
from atomic_execution.planning import CANONICAL_UNIVERSAL_ROUTER, ExecutionPlan
from atomic_execution.policy import ExecutionPolicy
from atomic_execution.simulation import (
    BlockReorganizedError,
    CallerSecurityError,
    ContractRevertError,
    InvalidBlockError,
    InventorySubsidyError,
    NativeValueProhibitedError,
    NodeLimitationError,
    QuoterSimulationProhibitedError,
    SimulationAdapter,
    SimulationRpcError,
    SimulationStatus,
    StateOverrideProhibitedError,
    extract_path_token_addresses,
    parse_revert_data,
    simulate_execution,
)
from atomic_execution.transport import (
    DeterministicSimulationTransport,
    ExactReplayKey,
    SimulationCallRequest,
    SimulationCallResponse,
    SimulationReplayRecord,
    TransportKeyNotFoundError,
    TransportWildcardForbiddenError,
    make_exact_replay_key,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "atomic_execution" / "v1"

TEST_CALLER = "0x39dBED3a2bd333467115dE45665cC57F813C4571"
TEST_ALT_CALLER = "0x5555555555555555555555555555555555555555"
TEST_UNAUTHORIZED = "0x9999999999999999999999999999999999999999"
TEST_BLOCK_NUMBER = 59255390
TEST_BLOCK_HASH = "0x" + "11" * 32
TEST_MUTATED_BLOCK_HASH = "0x" + "99" * 32
QUOTER_V3_ADDRESS = "0x88f28cc20014792ecfbe53e34b971a2e7c37b4e9"
QUOTER_V4_ADDRESS = "0x8dc178efb8111bb0973dd9d722ebeff267c98f94"


def _make_asset(address: str, chain_id: int = ROBINHOOD_CHAIN_ID) -> AssetRef:
    return AssetRef.erc20(TokenKey(chain_id, address))


_test_pool_counter = 0


def _make_v3_hop(
    asset_in: AssetRef,
    asset_out: AssetRef,
    fee_raw: int = 500,
    chain_id: int = ROBINHOOD_CHAIN_ID,
    pool_id: str | None = None,
) -> HopRef:
    global _test_pool_counter
    _test_pool_counter += 1
    resolved_id = pool_id if pool_id is not None else "0x" + f"{_test_pool_counter:x}".zfill(40)
    assert asset_in.token_key is not None
    assert asset_out.token_key is not None
    c0 = (
        asset_in
        if int(asset_in.token_key.address, 16) < int(asset_out.token_key.address, 16)
        else asset_out
    )
    c1 = asset_out if c0 == asset_in else asset_in
    direction = "zero_for_one" if c0 == asset_in else "one_for_zero"
    pk = PoolKey(
        chain_id=chain_id,
        protocol_id="uniswap_v3",
        pool_id=resolved_id,
        pool_id_kind="address",
        venue_address="0x1f7d7550b1b028f7571e69a784071f0205fd2efa",
        venue_kind="factory",
    )
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


def _make_test_plan(
    intermediate_address: str = USDG_ADDRESS_4663,
    target_router: str = CANONICAL_UNIVERSAL_ROUTER,
) -> ExecutionPlan:
    weth = _make_asset(WETH_ADDRESS_4663)
    mid = _make_asset(intermediate_address)
    hop1 = _make_v3_hop(weth, mid)
    hop2 = _make_v3_hop(mid, weth)
    route = RouteRef(chain_id=ROBINHOOD_CHAIN_ID, hops=(hop1, hop2), base_asset=weth)
    amt_in = Amount(asset_ref=weth, atoms=100_000_000_000_000_000, decimals=18)
    exp_out = Amount(asset_ref=weth, atoms=100_500_000_000_000_000, decimals=18)
    min_out = Amount(asset_ref=weth, atoms=100_100_000_000_000_000, decimals=18)
    floor_amt = Amount(asset_ref=weth, atoms=100_100_000_000_000_000, decimals=18)

    return ExecutionPlan(
        plan_id="plan_w5e_test",
        route_ref=route,
        base_asset=weth,
        amount_in=amt_in,
        expected_out=exp_out,
        min_amount_out=min_out,
        output_floor=floor_amt,
        policy=ExecutionPolicy(),
        quoter_block=TEST_BLOCK_NUMBER,
        deadline=1799999999,
        target_router=target_router,
        estimated_gas_usd=Decimal("0.10"),
        gas_atoms=40000000000000,
        net_atoms=min_out.atoms - amt_in.atoms - 40000000000000,
        net_profit_usd=Decimal("0.25"),
        trade_amount_usd=Decimal("250.0"),
        base_asset_usd_price=Decimal("2500.0"),
    )


def _make_sample_calldata(tag: str = "sample") -> tuple[ExecutionPlan, str, str]:
    """Build real Router calldata so classification tests reach the intended guard."""
    plan = _make_test_plan()
    encoded = encode_execution_plan(plan)
    return plan, encoded.calldata_hex, encoded.calldata_sha256


# ==============================================================================
# 1. Fixture Integrity & Replay Execution Tests
# ==============================================================================


def test_fixtures_simulation_cases_file_integrity() -> None:
    """Fixture JSONL must exist, have >= 30 cases, and cover all categories C13~C17."""
    fixture_path = FIXTURES_DIR / "simulation-cases.jsonl"
    assert fixture_path.is_file(), f"Missing fixture file: {fixture_path}"

    cases: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    category_counts: dict[str, int] = {}

    with fixture_path.open("r", encoding="utf-8") as stream:
        for line_num, line in enumerate(stream, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            case = json.loads(stripped)
            case_id = str(case["case_id"])
            assert case_id not in seen_ids, f"Duplicate case_id {case_id} on line {line_num}"
            seen_ids.add(case_id)
            cat = str(case["category"])
            assert cat in ("C13", "C14", "C15", "C16", "C17")
            category_counts[cat] = category_counts.get(cat, 0) + 1
            cases.append(case)

    assert len(cases) >= 30, f"Expected at least 30 fixture cases, found {len(cases)}"
    for required_cat in ("C13", "C14", "C15", "C16", "C17"):
        assert category_counts.get(required_cat, 0) >= 3, (
            f"Insufficient coverage for {required_cat}"
        )


def test_fixtures_simulation_cases_execution() -> None:
    """Execute all fixture cases (both positive and negative) from simulation-cases.jsonl."""
    fixture_path = FIXTURES_DIR / "simulation-cases.jsonl"
    all_transport = DeterministicSimulationTransport()
    loaded_count = all_transport.load_fixture_file(fixture_path)
    assert loaded_count >= 20

    err_map: dict[str, type[Exception]] = {
        "CallerSecurityError": CallerSecurityError,
        "NativeValueProhibitedError": NativeValueProhibitedError,
        "StateOverrideProhibitedError": StateOverrideProhibitedError,
        "InvalidBlockError": InvalidBlockError,
        "QuoterSimulationProhibitedError": QuoterSimulationProhibitedError,
        "InventorySubsidyError": InventorySubsidyError,
        "BlockReorganizedError": BlockReorganizedError,
    }

    executed_count = 0
    with fixture_path.open("r", encoding="utf-8") as stream:
        for line in stream:
            stripped = line.strip()
            if not stripped:
                continue
            case = json.loads(stripped)
            case_id = str(case["case_id"])
            executed_count += 1

            calldata_hex = case["calldata_hex"]
            calldata_sha = case["calldata_sha256"]
            plan = ExecutionPlan.from_dict(case["execution_plan"])
            payload = encode_execution_plan(plan).to_dict()
            payload.update(
                router_address=case["to_address"],
                calldata_hex=calldata_hex,
                calldata_sha256=calldata_sha,
            )
            encoded = EncodedCalldata.from_dict(payload)

            case_transport = DeterministicSimulationTransport()
            if "response" in case:
                case_transport.register_case(case)
            if case_id == "c14_block_reorg_before_simulation_detected":
                case_transport.set_block_hash(
                    case["chain_id"],
                    case["block_number"],
                    "0x1111111111111111111111111111111111111111111111111111111111111111",
                )

            authorized_callers = (
                [TEST_CALLER]
                if case_id in ("c13_caller_mismatch_rejected", "c13_unauthorized_caller_rejected")
                else None
            )
            adapter = SimulationAdapter(
                transport=case_transport,
                authorized_callers=authorized_callers,
            )
            expected_status = case.get("expected_status")

            path_tokens = case.get("path_tokens")

            if case.get("expected") == "reorg_detected":
                with pytest.raises(BlockReorganizedError):
                    adapter.simulate(
                        target=encoded,
                        caller_wallet=case["from_address"],
                        block_number=case["block_number"],
                        block_hash=case["block_hash"],
                        path_tokens=path_tokens,
                        raise_on_reorg=True,
                        execution_plan=plan,
                    )
            elif case.get("expected") == "rejected":
                err_type_name = str(case.get("expected_error"))
                err_cls = err_map.get(err_type_name, Exception)

                with pytest.raises(err_cls):
                    adapter.simulate(
                        target=encoded,
                        caller_wallet=case["from_address"],
                        block_number=case["block_number"],
                        block_hash=case["block_hash"],
                        value_wei=int(case.get("value_wei", 0)),
                        state_override=case.get("state_override"),
                        path_tokens=path_tokens,
                        execution_plan=plan,
                    )
            else:
                evidence = adapter.simulate(
                    target=encoded,
                    caller_wallet=case["from_address"],
                    block_number=case["block_number"],
                    block_hash=case["block_hash"],
                    path_tokens=path_tokens,
                    execution_plan=plan,
                )
                assert evidence.status == expected_status
                assert getattr(evidence, "can_atomic_execute") is False  # noqa: B009

    assert executed_count == 39


# ==============================================================================
# 2. C13: Caller & Permissions Consistency
# ==============================================================================


def test_c13_caller_matches_authorized_wallet_success() -> None:
    """Simulation succeeds when from_address matches caller_wallet and value_wei == 0."""
    plan, cd, cd_sha = _make_sample_calldata("c13_ok")
    key = make_exact_replay_key(
        chain_id=ROBINHOOD_CHAIN_ID,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        from_address=TEST_CALLER,
        to_address=CANONICAL_UNIVERSAL_ROUTER,
        value_wei=0,
        calldata_sha256=cd_sha,
    )
    rec = SimulationReplayRecord(
        key=key,
        response=SimulationCallResponse(status="CALL_SUCCEEDED", return_data_hex="0x01"),
        router_balances={token: 0 for token in extract_path_token_addresses(plan)},
    )
    transport = DeterministicSimulationTransport(records={key: rec})
    adapter = SimulationAdapter(transport=transport, authorized_callers=[TEST_CALLER])

    encoded = EncodedCalldata(
        plan_id=plan.plan_id,
        route_id=plan.route_ref.route_id,
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex=cd,
        calldata_sha256=cd_sha,
        commands_hex=encode_execution_plan(plan).commands_hex,
        commands_count=encode_execution_plan(plan).commands_count,
        deadline=1799999999,
        amount_in=plan.amount_in.atoms,
        min_amount_out=plan.min_amount_out.atoms,
    )

    evidence = adapter.simulate(
        target=encoded,
        caller_wallet=TEST_CALLER,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        execution_plan=plan,
    )
    assert evidence.status == SimulationStatus.CALL_SUCCEEDED
    assert evidence.from_address == TEST_CALLER
    assert getattr(evidence, "can_atomic_execute") is False  # noqa: B009


def test_c13_zero_address_caller_prohibited() -> None:
    """Zero address caller is strictly prohibited fail-closed."""
    plan, cd, cd_sha = _make_sample_calldata("c13_zero")
    transport = DeterministicSimulationTransport()
    adapter = SimulationAdapter(transport=transport)
    encoded = EncodedCalldata(
        plan_id=plan.plan_id,
        route_id=plan.route_ref.route_id,
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex=cd,
        calldata_sha256=cd_sha,
        commands_hex=encode_execution_plan(plan).commands_hex,
        commands_count=encode_execution_plan(plan).commands_count,
        deadline=1799999999,
        amount_in=plan.amount_in.atoms,
        min_amount_out=plan.min_amount_out.atoms,
    )
    with pytest.raises(CallerSecurityError, match="Zero address.*strictly prohibited"):
        adapter.simulate(
            target=encoded,
            caller_wallet=ZERO_ADDRESS,
            block_number=TEST_BLOCK_NUMBER,
            block_hash=TEST_BLOCK_HASH,
            execution_plan=plan,
        )


def test_c13_caller_mismatch_rejected() -> None:
    """Blank or whitespace caller is rejected under C13."""
    plan, cd, cd_sha = _make_sample_calldata("c13_mismatch")
    transport = DeterministicSimulationTransport()
    adapter = SimulationAdapter(transport=transport)
    encoded = EncodedCalldata(
        plan_id=plan.plan_id,
        route_id=plan.route_ref.route_id,
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex=cd,
        calldata_sha256=cd_sha,
        commands_hex=encode_execution_plan(plan).commands_hex,
        commands_count=encode_execution_plan(plan).commands_count,
        deadline=1799999999,
        amount_in=plan.amount_in.atoms,
        min_amount_out=plan.min_amount_out.atoms,
    )
    with pytest.raises(CallerSecurityError):
        adapter.simulate(
            target=encoded,
            caller_wallet="   ",
            block_number=TEST_BLOCK_NUMBER,
            block_hash=TEST_BLOCK_HASH,
            execution_plan=plan,
        )


def test_c13_unauthorized_caller_rejected_by_whitelist() -> None:
    """Caller not in authorized callers whitelist is rejected under C13."""
    plan, cd, cd_sha = _make_sample_calldata("c13_unauth")
    transport = DeterministicSimulationTransport()
    adapter = SimulationAdapter(transport=transport, authorized_callers=[TEST_CALLER])
    encoded = EncodedCalldata(
        plan_id=plan.plan_id,
        route_id=plan.route_ref.route_id,
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex=cd,
        calldata_sha256=cd_sha,
        commands_hex=encode_execution_plan(plan).commands_hex,
        commands_count=encode_execution_plan(plan).commands_count,
        deadline=1799999999,
        amount_in=plan.amount_in.atoms,
        min_amount_out=plan.min_amount_out.atoms,
    )
    with pytest.raises(CallerSecurityError, match="authorized callers whitelist"):
        adapter.simulate(
            target=encoded,
            caller_wallet=TEST_UNAUTHORIZED,
            block_number=TEST_BLOCK_NUMBER,
            block_hash=TEST_BLOCK_HASH,
            execution_plan=plan,
        )


def test_c13_native_value_nonzero_rejected_at_adapter() -> None:
    """Non-zero value_wei is strictly forbidden at adapter boundary under C13."""
    plan, cd, cd_sha = _make_sample_calldata("c13_val")
    transport = DeterministicSimulationTransport()
    adapter = SimulationAdapter(transport=transport)
    encoded = EncodedCalldata(
        plan_id=plan.plan_id,
        route_id=plan.route_ref.route_id,
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex=cd,
        calldata_sha256=cd_sha,
        commands_hex=encode_execution_plan(plan).commands_hex,
        commands_count=encode_execution_plan(plan).commands_count,
        deadline=1799999999,
        amount_in=plan.amount_in.atoms,
        min_amount_out=plan.min_amount_out.atoms,
    )
    with pytest.raises(NativeValueProhibitedError, match="value_wei must be 0"):
        adapter.simulate(
            target=encoded,
            caller_wallet=TEST_CALLER,
            block_number=TEST_BLOCK_NUMBER,
            block_hash=TEST_BLOCK_HASH,
            value_wei=1,
            execution_plan=plan,
        )


def test_c13_native_value_nonzero_rejected_at_transport() -> None:
    """Non-zero value_wei is strictly forbidden in make_exact_replay_key and transport."""
    with pytest.raises(NativeValueProhibitedError):
        make_exact_replay_key(
            chain_id=ROBINHOOD_CHAIN_ID,
            block_number=TEST_BLOCK_NUMBER,
            block_hash=TEST_BLOCK_HASH,
            from_address=TEST_CALLER,
            to_address=CANONICAL_UNIVERSAL_ROUTER,
            value_wei=500,
            calldata_sha256="aa" * 32,
        )


def test_c13_state_override_balance_rejected() -> None:
    """State override tampering balance is strictly forbidden under C13."""
    plan, cd, cd_sha = _make_sample_calldata("c13_ovr_bal")
    transport = DeterministicSimulationTransport()
    adapter = SimulationAdapter(transport=transport)
    encoded = EncodedCalldata(
        plan_id=plan.plan_id,
        route_id=plan.route_ref.route_id,
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex=cd,
        calldata_sha256=cd_sha,
        commands_hex=encode_execution_plan(plan).commands_hex,
        commands_count=encode_execution_plan(plan).commands_count,
        deadline=1799999999,
        amount_in=plan.amount_in.atoms,
        min_amount_out=plan.min_amount_out.atoms,
    )
    with pytest.raises(
        StateOverrideProhibitedError, match="State overrides are strictly prohibited"
    ):
        adapter.simulate(
            target=encoded,
            caller_wallet=TEST_CALLER,
            block_number=TEST_BLOCK_NUMBER,
            block_hash=TEST_BLOCK_HASH,
            state_override={TEST_CALLER: {"balance": "0xffffffff"}},
            execution_plan=plan,
        )


def test_c13_state_override_allowance_rejected() -> None:
    """State override tampering token allowance is strictly forbidden under C13."""
    plan, cd, cd_sha = _make_sample_calldata("c13_ovr_all")
    transport = DeterministicSimulationTransport()
    adapter = SimulationAdapter(transport=transport)
    encoded = EncodedCalldata(
        plan_id=plan.plan_id,
        route_id=plan.route_ref.route_id,
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex=cd,
        calldata_sha256=cd_sha,
        commands_hex=encode_execution_plan(plan).commands_hex,
        commands_count=encode_execution_plan(plan).commands_count,
        deadline=1799999999,
        amount_in=plan.amount_in.atoms,
        min_amount_out=plan.min_amount_out.atoms,
    )
    with pytest.raises(StateOverrideProhibitedError):
        adapter.simulate(
            target=encoded,
            caller_wallet=TEST_CALLER,
            block_number=TEST_BLOCK_NUMBER,
            block_hash=TEST_BLOCK_HASH,
            state_override={WETH_ADDRESS_4663: {"stateDiff": {"0x01": "0x12"}}},
            execution_plan=plan,
        )


def test_c13_empty_state_override_allowed() -> None:
    """Passing None or empty dict for state_override does not trigger prohibition."""
    plan, cd, cd_sha = _make_sample_calldata("c13_ovr_empty")
    key = make_exact_replay_key(
        chain_id=ROBINHOOD_CHAIN_ID,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        from_address=TEST_CALLER,
        to_address=CANONICAL_UNIVERSAL_ROUTER,
        value_wei=0,
        calldata_sha256=cd_sha,
    )
    rec = SimulationReplayRecord(
        key=key,
        response=SimulationCallResponse(status="CALL_SUCCEEDED", return_data_hex="0x01"),
        router_balances={token: 0 for token in extract_path_token_addresses(plan)},
    )
    transport = DeterministicSimulationTransport(records={key: rec})
    adapter = SimulationAdapter(transport=transport)
    encoded = EncodedCalldata(
        plan_id=plan.plan_id,
        route_id=plan.route_ref.route_id,
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex=cd,
        calldata_sha256=cd_sha,
        commands_hex=encode_execution_plan(plan).commands_hex,
        commands_count=encode_execution_plan(plan).commands_count,
        deadline=1799999999,
        amount_in=plan.amount_in.atoms,
        min_amount_out=plan.min_amount_out.atoms,
    )
    evidence = adapter.simulate(
        target=encoded,
        caller_wallet=TEST_CALLER,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        state_override={},
        execution_plan=plan,
    )
    assert evidence.status == SimulationStatus.CALL_SUCCEEDED


# ==============================================================================
# 3. C14: Fixed Block Number & Reorg Defense
# ==============================================================================


def test_c14_pinned_block_fixed_integer_success() -> None:
    """Fixed block number and hash consistently verified before and after simulation."""
    plan, cd, cd_sha = _make_sample_calldata("c14_ok")
    key = make_exact_replay_key(
        chain_id=ROBINHOOD_CHAIN_ID,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        from_address=TEST_CALLER,
        to_address=CANONICAL_UNIVERSAL_ROUTER,
        value_wei=0,
        calldata_sha256=cd_sha,
    )
    rec = SimulationReplayRecord(
        key=key,
        response=SimulationCallResponse(status="CALL_SUCCEEDED", return_data_hex="0x01"),
        router_balances={token: 0 for token in extract_path_token_addresses(plan)},
    )
    transport = DeterministicSimulationTransport(records={key: rec})
    adapter = SimulationAdapter(transport=transport)
    encoded = EncodedCalldata(
        plan_id=plan.plan_id,
        route_id=plan.route_ref.route_id,
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex=cd,
        calldata_sha256=cd_sha,
        commands_hex=encode_execution_plan(plan).commands_hex,
        commands_count=encode_execution_plan(plan).commands_count,
        deadline=1799999999,
        amount_in=plan.amount_in.atoms,
        min_amount_out=plan.min_amount_out.atoms,
    )

    evidence = adapter.simulate(
        target=encoded,
        caller_wallet=TEST_CALLER,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        execution_plan=plan,
    )
    assert evidence.block_number == TEST_BLOCK_NUMBER
    assert evidence.block_hash == TEST_BLOCK_HASH


def test_c14_unpinned_latest_tag_rejected() -> None:
    """Specifying block tag 'latest' is rejected fail-closed under C14."""
    plan, cd, cd_sha = _make_sample_calldata("c14_latest")
    transport = DeterministicSimulationTransport()
    adapter = SimulationAdapter(transport=transport)
    encoded = EncodedCalldata(
        plan_id=plan.plan_id,
        route_id=plan.route_ref.route_id,
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex=cd,
        calldata_sha256=cd_sha,
        commands_hex=encode_execution_plan(plan).commands_hex,
        commands_count=encode_execution_plan(plan).commands_count,
        deadline=1799999999,
        amount_in=plan.amount_in.atoms,
        min_amount_out=plan.min_amount_out.atoms,
    )
    with pytest.raises(InvalidBlockError, match="strictly forbidden under C14"):
        adapter.simulate(
            target=encoded,
            caller_wallet=TEST_CALLER,
            block_number=TEST_BLOCK_NUMBER,
            block_hash="latest",
            execution_plan=plan,
        )


def test_c14_unpinned_pending_tag_rejected() -> None:
    """Specifying block tag 'pending' is rejected fail-closed under C14."""
    plan, cd, cd_sha = _make_sample_calldata("c14_pending")
    transport = DeterministicSimulationTransport()
    adapter = SimulationAdapter(transport=transport)
    encoded = EncodedCalldata(
        plan_id=plan.plan_id,
        route_id=plan.route_ref.route_id,
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex=cd,
        calldata_sha256=cd_sha,
        commands_hex=encode_execution_plan(plan).commands_hex,
        commands_count=encode_execution_plan(plan).commands_count,
        deadline=1799999999,
        amount_in=plan.amount_in.atoms,
        min_amount_out=plan.min_amount_out.atoms,
    )
    with pytest.raises(InvalidBlockError, match="strictly forbidden under C14"):
        adapter.simulate(
            target=encoded,
            caller_wallet=TEST_CALLER,
            block_number=TEST_BLOCK_NUMBER,
            block_hash="pending",
            execution_plan=plan,
        )


def test_c14_zero_and_negative_block_number_rejected() -> None:
    """Non-positive block numbers are rejected under C14."""
    plan, cd, cd_sha = _make_sample_calldata("c14_zero_neg")
    transport = DeterministicSimulationTransport()
    adapter = SimulationAdapter(transport=transport)
    encoded = EncodedCalldata(
        plan_id=plan.plan_id,
        route_id=plan.route_ref.route_id,
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex=cd,
        calldata_sha256=cd_sha,
        commands_hex=encode_execution_plan(plan).commands_hex,
        commands_count=encode_execution_plan(plan).commands_count,
        deadline=1799999999,
        amount_in=plan.amount_in.atoms,
        min_amount_out=plan.min_amount_out.atoms,
    )
    with pytest.raises(InvalidBlockError):
        adapter.simulate(
            target=encoded,
            caller_wallet=TEST_CALLER,
            block_number=0,
            block_hash=TEST_BLOCK_HASH,
            execution_plan=plan,
        )
    with pytest.raises(InvalidBlockError):
        adapter.simulate(
            target=encoded,
            caller_wallet=TEST_CALLER,
            block_number=-10,
            block_hash=TEST_BLOCK_HASH,
            execution_plan=plan,
        )


def test_c14_non_integer_block_number_rejected() -> None:
    """Boolean or string block_number types are rejected."""
    plan, cd, cd_sha = _make_sample_calldata("c14_type")
    transport = DeterministicSimulationTransport()
    adapter = SimulationAdapter(transport=transport)
    encoded = EncodedCalldata(
        plan_id=plan.plan_id,
        route_id=plan.route_ref.route_id,
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex=cd,
        calldata_sha256=cd_sha,
        commands_hex=encode_execution_plan(plan).commands_hex,
        commands_count=encode_execution_plan(plan).commands_count,
        deadline=1799999999,
        amount_in=plan.amount_in.atoms,
        min_amount_out=plan.min_amount_out.atoms,
    )
    with pytest.raises(InvalidBlockError):
        adapter.simulate(
            target=encoded,
            caller_wallet=TEST_CALLER,
            block_number=cast(Any, True),
            block_hash=TEST_BLOCK_HASH,
            execution_plan=plan,
        )


def test_c14_reorg_before_simulation_raises_block_reorganized_error() -> None:
    """Pre-simulation block hash mismatch triggers BlockReorganizedError."""
    plan, cd, cd_sha = _make_sample_calldata("c14_reorg_pre")
    transport = DeterministicSimulationTransport(
        block_hashes={(ROBINHOOD_CHAIN_ID, TEST_BLOCK_NUMBER): TEST_MUTATED_BLOCK_HASH}
    )
    adapter = SimulationAdapter(transport=transport)
    encoded = EncodedCalldata(
        plan_id=plan.plan_id,
        route_id=plan.route_ref.route_id,
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex=cd,
        calldata_sha256=cd_sha,
        commands_hex=encode_execution_plan(plan).commands_hex,
        commands_count=encode_execution_plan(plan).commands_count,
        deadline=1799999999,
        amount_in=plan.amount_in.atoms,
        min_amount_out=plan.min_amount_out.atoms,
    )

    with pytest.raises(BlockReorganizedError, match="Block hash mismatch before simulation"):
        adapter.simulate(
            target=encoded,
            caller_wallet=TEST_CALLER,
            block_number=TEST_BLOCK_NUMBER,
            block_hash=TEST_BLOCK_HASH,
            raise_on_reorg=True,
            execution_plan=plan,
        )


def test_c14_reorg_during_simulation_raises_block_reorganized_error() -> None:
    """Block hash mutation during simulation triggers BlockReorganizedError."""
    plan, cd, cd_sha = _make_sample_calldata("c14_reorg_post")
    key = make_exact_replay_key(
        chain_id=ROBINHOOD_CHAIN_ID,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        from_address=TEST_CALLER,
        to_address=CANONICAL_UNIVERSAL_ROUTER,
        value_wei=0,
        calldata_sha256=cd_sha,
    )
    rec = SimulationReplayRecord(
        key=key,
        response=SimulationCallResponse(status="CALL_SUCCEEDED", return_data_hex="0x01"),
        post_call_block_hash=TEST_MUTATED_BLOCK_HASH,
        router_balances={token: 0 for token in extract_path_token_addresses(plan)},
    )
    transport = DeterministicSimulationTransport(records={key: rec})
    adapter = SimulationAdapter(transport=transport)
    encoded = EncodedCalldata(
        plan_id=plan.plan_id,
        route_id=plan.route_ref.route_id,
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex=cd,
        calldata_sha256=cd_sha,
        commands_hex=encode_execution_plan(plan).commands_hex,
        commands_count=encode_execution_plan(plan).commands_count,
        deadline=1799999999,
        amount_in=plan.amount_in.atoms,
        min_amount_out=plan.min_amount_out.atoms,
    )

    with pytest.raises(
        BlockReorganizedError, match="Block reorganization detected during simulation"
    ):
        adapter.simulate(
            target=encoded,
            caller_wallet=TEST_CALLER,
            block_number=TEST_BLOCK_NUMBER,
            block_hash=TEST_BLOCK_HASH,
            raise_on_reorg=True,
            execution_plan=plan,
        )


def test_c14_reorg_suppressed_raise_records_status_block_reorganized() -> None:
    """When raise_on_reorg=False, evidence is returned with status BLOCK_REORGANIZED."""
    plan, cd, cd_sha = _make_sample_calldata("c14_reorg_status")
    key = make_exact_replay_key(
        chain_id=ROBINHOOD_CHAIN_ID,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        from_address=TEST_CALLER,
        to_address=CANONICAL_UNIVERSAL_ROUTER,
        value_wei=0,
        calldata_sha256=cd_sha,
    )
    rec = SimulationReplayRecord(
        key=key,
        response=SimulationCallResponse(status="CALL_SUCCEEDED", return_data_hex="0x01"),
        post_call_block_hash=TEST_MUTATED_BLOCK_HASH,
        router_balances={token: 0 for token in extract_path_token_addresses(plan)},
    )
    transport = DeterministicSimulationTransport(records={key: rec})
    adapter = SimulationAdapter(transport=transport)
    encoded = EncodedCalldata(
        plan_id=plan.plan_id,
        route_id=plan.route_ref.route_id,
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex=cd,
        calldata_sha256=cd_sha,
        commands_hex=encode_execution_plan(plan).commands_hex,
        commands_count=encode_execution_plan(plan).commands_count,
        deadline=1799999999,
        amount_in=plan.amount_in.atoms,
        min_amount_out=plan.min_amount_out.atoms,
    )

    evidence = adapter.simulate(
        target=encoded,
        caller_wallet=TEST_CALLER,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        raise_on_reorg=False,
        execution_plan=plan,
    )
    assert evidence.status == SimulationStatus.BLOCK_REORGANIZED
    assert "Block reorganization detected" in str(evidence.error_message)


# ==============================================================================
# 4. C15: Five-State Error Taxonomy & Zero-Assumed Output
# ==============================================================================


def test_c15_call_succeeded_multicall_valid_hex() -> None:
    """CALL_SUCCEEDED produces valid non-empty hex return data."""
    plan, cd, cd_sha = _make_sample_calldata("c15_succ")
    key = make_exact_replay_key(
        chain_id=ROBINHOOD_CHAIN_ID,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        from_address=TEST_CALLER,
        to_address=CANONICAL_UNIVERSAL_ROUTER,
        value_wei=0,
        calldata_sha256=cd_sha,
    )
    expected_hex = "0x0000000000000000000000000000000000000000000000000de0b6b3a7640000"
    rec = SimulationReplayRecord(
        key=key,
        response=SimulationCallResponse(
            status="CALL_SUCCEEDED", return_data_hex=expected_hex, gas_used=135000
        ),
        router_balances={token: 0 for token in extract_path_token_addresses(plan)},
    )
    transport = DeterministicSimulationTransport(records={key: rec})
    adapter = SimulationAdapter(transport=transport)
    encoded = EncodedCalldata(
        plan_id=plan.plan_id,
        route_id=plan.route_ref.route_id,
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex=cd,
        calldata_sha256=cd_sha,
        commands_hex=encode_execution_plan(plan).commands_hex,
        commands_count=encode_execution_plan(plan).commands_count,
        deadline=1799999999,
        amount_in=plan.amount_in.atoms,
        min_amount_out=plan.min_amount_out.atoms,
    )

    evidence = adapter.simulate(
        target=encoded,
        caller_wallet=TEST_CALLER,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        execution_plan=plan,
    )
    assert evidence.status == SimulationStatus.CALL_SUCCEEDED
    assert evidence.return_data_hex == expected_hex
    assert evidence.gas_used == 135000
    assert evidence.error_message is None


def test_c15_output_unverified_on_empty_0x_return() -> None:
    """Empty return data (0x) without revert is classified as OUTPUT_UNVERIFIED."""
    plan, cd, cd_sha = _make_sample_calldata("c15_empty")
    key = make_exact_replay_key(
        chain_id=ROBINHOOD_CHAIN_ID,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        from_address=TEST_CALLER,
        to_address=CANONICAL_UNIVERSAL_ROUTER,
        value_wei=0,
        calldata_sha256=cd_sha,
    )
    rec = SimulationReplayRecord(
        key=key,
        response=SimulationCallResponse(
            status="CALL_SUCCEEDED", return_data_hex="0x", gas_used=30000
        ),
        router_balances={token: 0 for token in extract_path_token_addresses(plan)},
    )
    transport = DeterministicSimulationTransport(records={key: rec})
    adapter = SimulationAdapter(transport=transport)
    encoded = EncodedCalldata(
        plan_id=plan.plan_id,
        route_id=plan.route_ref.route_id,
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex=cd,
        calldata_sha256=cd_sha,
        commands_hex=encode_execution_plan(plan).commands_hex,
        commands_count=encode_execution_plan(plan).commands_count,
        deadline=1799999999,
        amount_in=plan.amount_in.atoms,
        min_amount_out=plan.min_amount_out.atoms,
    )

    evidence = adapter.simulate(
        target=encoded,
        caller_wallet=TEST_CALLER,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        execution_plan=plan,
    )
    assert evidence.status == SimulationStatus.OUTPUT_UNVERIFIED
    assert evidence.return_data_hex == "0x"
    assert "output unverified per C15" in str(evidence.error_message)


def test_c15_output_unverified_zero_assumed_output_quoter_prohibited() -> None:
    """Zero-assumed output defense: Quoter expected_out is never copied into simulation output."""
    plan = _make_test_plan()
    encoded = encode_execution_plan(plan)
    key = make_exact_replay_key(
        chain_id=plan.chain_id,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        from_address=TEST_CALLER,
        to_address=plan.target_router,
        value_wei=0,
        calldata_sha256=encoded.calldata_sha256,
    )
    rec = SimulationReplayRecord(
        key=key,
        response=SimulationCallResponse(status="CALL_SUCCEEDED", return_data_hex="0x"),
        router_balances={token: 0 for token in extract_path_token_addresses(plan)},
    )
    transport = DeterministicSimulationTransport(records={key: rec})
    adapter = SimulationAdapter(transport=transport)

    evidence = adapter.simulate(
        target=plan,
        caller_wallet=TEST_CALLER,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        execution_plan=plan,
    )
    assert evidence.status == SimulationStatus.OUTPUT_UNVERIFIED
    assert evidence.return_data_hex == "0x"
    # Verify expected_out is not masquerading as return_data
    assert str(plan.expected_out.atoms) not in evidence.return_data_hex


def test_c15_contract_revert_standard_error_transfer_failed() -> None:
    """CONTRACT_REVERT with Error('TRANSFER_FAILED') parses the revert message string."""
    plan, cd, cd_sha = _make_sample_calldata("c15_rev_tf")
    revert_hex = "0x08c379a00000000000000000000000000000000000000000000000000000000000000020000000000000000000000000000000000000000000000000000000000000000f5452414e534645525f4641494c45440000000000000000000000000000000000"
    key = make_exact_replay_key(
        chain_id=ROBINHOOD_CHAIN_ID,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        from_address=TEST_CALLER,
        to_address=CANONICAL_UNIVERSAL_ROUTER,
        value_wei=0,
        calldata_sha256=cd_sha,
    )
    rec = SimulationReplayRecord(
        key=key,
        response=SimulationCallResponse(status="CONTRACT_REVERT", revert_data_hex=revert_hex),
        router_balances={token: 0 for token in extract_path_token_addresses(plan)},
    )
    transport = DeterministicSimulationTransport(records={key: rec})
    adapter = SimulationAdapter(transport=transport)
    encoded = EncodedCalldata(
        plan_id=plan.plan_id,
        route_id=plan.route_ref.route_id,
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex=cd,
        calldata_sha256=cd_sha,
        commands_hex=encode_execution_plan(plan).commands_hex,
        commands_count=encode_execution_plan(plan).commands_count,
        deadline=1799999999,
        amount_in=plan.amount_in.atoms,
        min_amount_out=plan.min_amount_out.atoms,
    )

    evidence = adapter.simulate(
        target=encoded,
        caller_wallet=TEST_CALLER,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        execution_plan=plan,
    )
    assert evidence.status == SimulationStatus.CONTRACT_REVERT
    assert evidence.return_data_hex == revert_hex
    assert "TRANSFER_FAILED" in str(evidence.error_message)


def test_c15_contract_revert_standard_error_expired() -> None:
    """CONTRACT_REVERT parses Transaction expired string."""
    parsed = parse_revert_data(
        "0x08c379a0000000000000000000000000000000000000000000000000000000000000002000000000000000000000000000000000000000000000000000000000000000135472616e73616374696f6e206578706972656400000000000000000000000000"
    )
    assert parsed == "Reverted: Transaction expired"


def test_c15_contract_revert_panic_overflow() -> None:
    """CONTRACT_REVERT with Panic(0x11) decodes arithmetic overflow."""
    panic_hex = "0x4e487b710000000000000000000000000000000000000000000000000000000000000011"
    parsed = parse_revert_data(panic_hex)
    assert parsed == "Panic: Arithmetic overflow or underflow (0x11)"


def test_c15_contract_revert_panic_assert_false() -> None:
    """CONTRACT_REVERT with Panic(0x01) decodes assert false."""
    panic_hex = "0x4e487b710000000000000000000000000000000000000000000000000000000000000001"
    parsed = parse_revert_data(panic_hex)
    assert parsed == "Panic: Assert evaluated to false (0x01)"


def test_c15_contract_revert_execution_failed_router_unpacked() -> None:
    """CONTRACT_REVERT with ExecutionFailed(commandIndex, bytes) unrolls recursively."""
    from eth_abi import encode as abi_encode

    inner_err = bytes.fromhex("08c379a0" + abi_encode(["string"], ["SLIPPAGE_TOLERANCE"]).hex())
    outer_hex = "0x2c4029e9" + abi_encode(["uint256", "bytes"], [2, inner_err]).hex()
    parsed = parse_revert_data(outer_hex)
    assert parsed is not None
    assert "ExecutionFailed at command 2" in parsed
    assert "SLIPPAGE_TOLERANCE" in parsed


def test_c15_contract_revert_custom_error_hex() -> None:
    """Unknown custom error hex safely outputs selector description."""
    custom_hex = "0x123456780000000000000000000000000000000000000000000000000000000000000001"
    parsed = parse_revert_data(custom_hex)
    assert parsed == "Custom error (0x12345678)"


def test_c15_contract_revert_raises_when_requested() -> None:
    """When raise_on_revert=True, ContractRevertError is raised."""
    plan, cd, cd_sha = _make_sample_calldata("c15_rev_raise")
    key = make_exact_replay_key(
        chain_id=ROBINHOOD_CHAIN_ID,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        from_address=TEST_CALLER,
        to_address=CANONICAL_UNIVERSAL_ROUTER,
        value_wei=0,
        calldata_sha256=cd_sha,
    )
    rec = SimulationReplayRecord(
        key=key,
        response=SimulationCallResponse(status="CONTRACT_REVERT", revert_data_hex="0x08c379a0"),
        router_balances={token: 0 for token in extract_path_token_addresses(plan)},
    )
    transport = DeterministicSimulationTransport(records={key: rec})
    adapter = SimulationAdapter(transport=transport)
    encoded = EncodedCalldata(
        plan_id=plan.plan_id,
        route_id=plan.route_ref.route_id,
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex=cd,
        calldata_sha256=cd_sha,
        commands_hex=encode_execution_plan(plan).commands_hex,
        commands_count=encode_execution_plan(plan).commands_count,
        deadline=1799999999,
        amount_in=plan.amount_in.atoms,
        min_amount_out=plan.min_amount_out.atoms,
    )

    with pytest.raises(ContractRevertError):
        adapter.simulate(
            target=encoded,
            caller_wallet=TEST_CALLER,
            block_number=TEST_BLOCK_NUMBER,
            block_hash=TEST_BLOCK_HASH,
            raise_on_revert=True,
            execution_plan=plan,
        )


def test_c15_rpc_error_timeout_classified() -> None:
    """Transport timeout is classified as RPC_ERROR with details."""
    plan, cd, cd_sha = _make_sample_calldata("c15_rpc_to")
    key = make_exact_replay_key(
        chain_id=ROBINHOOD_CHAIN_ID,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        from_address=TEST_CALLER,
        to_address=CANONICAL_UNIVERSAL_ROUTER,
        value_wei=0,
        calldata_sha256=cd_sha,
    )
    rec = SimulationReplayRecord(
        key=key,
        response=SimulationCallResponse(
            status="RPC_ERROR",
            rpc_error_type="TIMEOUT",
            error_message="ReadTimeout: 5000ms reached",
        ),
        router_balances={token: 0 for token in extract_path_token_addresses(plan)},
    )
    transport = DeterministicSimulationTransport(records={key: rec})
    adapter = SimulationAdapter(transport=transport)
    encoded = EncodedCalldata(
        plan_id=plan.plan_id,
        route_id=plan.route_ref.route_id,
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex=cd,
        calldata_sha256=cd_sha,
        commands_hex=encode_execution_plan(plan).commands_hex,
        commands_count=encode_execution_plan(plan).commands_count,
        deadline=1799999999,
        amount_in=plan.amount_in.atoms,
        min_amount_out=plan.min_amount_out.atoms,
    )

    evidence = adapter.simulate(
        target=encoded,
        caller_wallet=TEST_CALLER,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        execution_plan=plan,
    )
    assert evidence.status == SimulationStatus.RPC_ERROR
    assert "ReadTimeout" in str(evidence.error_message)


def test_c15_rpc_error_rate_limit_429_classified() -> None:
    """HTTP 429 rate limit is classified into RPC_ERROR."""
    plan, cd, cd_sha = _make_sample_calldata("c15_rpc_429")
    key = make_exact_replay_key(
        chain_id=ROBINHOOD_CHAIN_ID,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        from_address=TEST_CALLER,
        to_address=CANONICAL_UNIVERSAL_ROUTER,
        value_wei=0,
        calldata_sha256=cd_sha,
    )
    rec = SimulationReplayRecord(
        key=key,
        response=SimulationCallResponse(
            status="RPC_ERROR",
            rpc_error_type="RATE_LIMIT_429",
            error_message="429: Too Many Requests",
        ),
        router_balances={token: 0 for token in extract_path_token_addresses(plan)},
    )
    transport = DeterministicSimulationTransport(records={key: rec})
    adapter = SimulationAdapter(transport=transport)
    encoded = EncodedCalldata(
        plan_id=plan.plan_id,
        route_id=plan.route_ref.route_id,
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex=cd,
        calldata_sha256=cd_sha,
        commands_hex=encode_execution_plan(plan).commands_hex,
        commands_count=encode_execution_plan(plan).commands_count,
        deadline=1799999999,
        amount_in=plan.amount_in.atoms,
        min_amount_out=plan.min_amount_out.atoms,
    )

    evidence = adapter.simulate(
        target=encoded,
        caller_wallet=TEST_CALLER,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        execution_plan=plan,
    )
    assert evidence.status == SimulationStatus.RPC_ERROR
    assert "429" in str(evidence.error_message)


def test_c15_rpc_error_connection_refused_classified() -> None:
    """Connection drop is classified into RPC_ERROR."""
    plan, cd, cd_sha = _make_sample_calldata("c15_rpc_conn")
    key = make_exact_replay_key(
        chain_id=ROBINHOOD_CHAIN_ID,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        from_address=TEST_CALLER,
        to_address=CANONICAL_UNIVERSAL_ROUTER,
        value_wei=0,
        calldata_sha256=cd_sha,
    )
    rec = SimulationReplayRecord(
        key=key,
        response=SimulationCallResponse(
            status="RPC_ERROR",
            rpc_error_type="CONNECTION_REFUSED",
            error_message="ConnectionRefusedError: unreachable",
        ),
        router_balances={token: 0 for token in extract_path_token_addresses(plan)},
    )
    transport = DeterministicSimulationTransport(records={key: rec})
    adapter = SimulationAdapter(transport=transport)
    encoded = EncodedCalldata(
        plan_id=plan.plan_id,
        route_id=plan.route_ref.route_id,
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex=cd,
        calldata_sha256=cd_sha,
        commands_hex=encode_execution_plan(plan).commands_hex,
        commands_count=encode_execution_plan(plan).commands_count,
        deadline=1799999999,
        amount_in=plan.amount_in.atoms,
        min_amount_out=plan.min_amount_out.atoms,
    )

    evidence = adapter.simulate(
        target=encoded,
        caller_wallet=TEST_CALLER,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        execution_plan=plan,
    )
    assert evidence.status == SimulationStatus.RPC_ERROR


def test_c15_rpc_error_raises_when_requested() -> None:
    """When raise_on_error=True, RPC error raises SimulationRpcError."""
    plan, cd, cd_sha = _make_sample_calldata("c15_rpc_raise")
    key = make_exact_replay_key(
        chain_id=ROBINHOOD_CHAIN_ID,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        from_address=TEST_CALLER,
        to_address=CANONICAL_UNIVERSAL_ROUTER,
        value_wei=0,
        calldata_sha256=cd_sha,
    )
    rec = SimulationReplayRecord(
        key=key,
        response=SimulationCallResponse(status="RPC_ERROR", error_message="Network down"),
        router_balances={token: 0 for token in extract_path_token_addresses(plan)},
    )
    transport = DeterministicSimulationTransport(records={key: rec})
    adapter = SimulationAdapter(transport=transport)
    encoded = EncodedCalldata(
        plan_id=plan.plan_id,
        route_id=plan.route_ref.route_id,
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex=cd,
        calldata_sha256=cd_sha,
        commands_hex=encode_execution_plan(plan).commands_hex,
        commands_count=encode_execution_plan(plan).commands_count,
        deadline=1799999999,
        amount_in=plan.amount_in.atoms,
        min_amount_out=plan.min_amount_out.atoms,
    )

    with pytest.raises(SimulationRpcError):
        adapter.simulate(
            target=encoded,
            caller_wallet=TEST_CALLER,
            block_number=TEST_BLOCK_NUMBER,
            block_hash=TEST_BLOCK_HASH,
            raise_on_error=True,
            execution_plan=plan,
        )


def test_c15_node_limitation_archive_missing_classified() -> None:
    """Archive state missing is classified into NODE_LIMITATION."""
    plan, cd, cd_sha = _make_sample_calldata("c15_node_arc")
    key = make_exact_replay_key(
        chain_id=ROBINHOOD_CHAIN_ID,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        from_address=TEST_CALLER,
        to_address=CANONICAL_UNIVERSAL_ROUTER,
        value_wei=0,
        calldata_sha256=cd_sha,
    )
    rec = SimulationReplayRecord(
        key=key,
        response=SimulationCallResponse(
            status="NODE_LIMITATION",
            node_limitation_type="ARCHIVE_STATE_UNAVAILABLE",
            error_message="missing trie node: state pruned",
        ),
        router_balances={token: 0 for token in extract_path_token_addresses(plan)},
    )
    transport = DeterministicSimulationTransport(records={key: rec})
    adapter = SimulationAdapter(transport=transport)
    encoded = EncodedCalldata(
        plan_id=plan.plan_id,
        route_id=plan.route_ref.route_id,
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex=cd,
        calldata_sha256=cd_sha,
        commands_hex=encode_execution_plan(plan).commands_hex,
        commands_count=encode_execution_plan(plan).commands_count,
        deadline=1799999999,
        amount_in=plan.amount_in.atoms,
        min_amount_out=plan.min_amount_out.atoms,
    )

    evidence = adapter.simulate(
        target=encoded,
        caller_wallet=TEST_CALLER,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        execution_plan=plan,
    )
    assert evidence.status == SimulationStatus.NODE_LIMITATION
    assert "missing trie node" in str(evidence.error_message)


def test_c15_node_limitation_method_unsupported_classified() -> None:
    """Method unsupported error is classified into NODE_LIMITATION."""
    plan, cd, cd_sha = _make_sample_calldata("c15_node_meth")
    key = make_exact_replay_key(
        chain_id=ROBINHOOD_CHAIN_ID,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        from_address=TEST_CALLER,
        to_address=CANONICAL_UNIVERSAL_ROUTER,
        value_wei=0,
        calldata_sha256=cd_sha,
    )
    rec = SimulationReplayRecord(
        key=key,
        response=SimulationCallResponse(
            status="NODE_LIMITATION",
            node_limitation_type="METHOD_UNSUPPORTED",
            error_message="Method unsupported by node",
        ),
        router_balances={token: 0 for token in extract_path_token_addresses(plan)},
    )
    transport = DeterministicSimulationTransport(records={key: rec})
    adapter = SimulationAdapter(transport=transport)
    encoded = EncodedCalldata(
        plan_id=plan.plan_id,
        route_id=plan.route_ref.route_id,
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex=cd,
        calldata_sha256=cd_sha,
        commands_hex=encode_execution_plan(plan).commands_hex,
        commands_count=encode_execution_plan(plan).commands_count,
        deadline=1799999999,
        amount_in=plan.amount_in.atoms,
        min_amount_out=plan.min_amount_out.atoms,
    )

    evidence = adapter.simulate(
        target=encoded,
        caller_wallet=TEST_CALLER,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        execution_plan=plan,
    )
    assert evidence.status == SimulationStatus.NODE_LIMITATION


def test_c15_node_limitation_fixed_block_unsupported_classified() -> None:
    """Fixed block query unsupported is classified into NODE_LIMITATION."""
    plan, cd, cd_sha = _make_sample_calldata("c15_node_blk")
    key = make_exact_replay_key(
        chain_id=ROBINHOOD_CHAIN_ID,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        from_address=TEST_CALLER,
        to_address=CANONICAL_UNIVERSAL_ROUTER,
        value_wei=0,
        calldata_sha256=cd_sha,
    )
    rec = SimulationReplayRecord(
        key=key,
        response=SimulationCallResponse(
            status="NODE_LIMITATION",
            node_limitation_type="FIXED_BLOCK_UNSUPPORTED",
            error_message="Block too old for node tier",
        ),
        router_balances={token: 0 for token in extract_path_token_addresses(plan)},
    )
    transport = DeterministicSimulationTransport(records={key: rec})
    adapter = SimulationAdapter(transport=transport)
    encoded = EncodedCalldata(
        plan_id=plan.plan_id,
        route_id=plan.route_ref.route_id,
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex=cd,
        calldata_sha256=cd_sha,
        commands_hex=encode_execution_plan(plan).commands_hex,
        commands_count=encode_execution_plan(plan).commands_count,
        deadline=1799999999,
        amount_in=plan.amount_in.atoms,
        min_amount_out=plan.min_amount_out.atoms,
    )

    evidence = adapter.simulate(
        target=encoded,
        caller_wallet=TEST_CALLER,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        execution_plan=plan,
    )
    assert evidence.status == SimulationStatus.NODE_LIMITATION


def test_c15_node_limitation_raises_when_requested() -> None:
    """When raise_on_error=True, node limitation raises NodeLimitationError."""
    plan, cd, cd_sha = _make_sample_calldata("c15_node_raise")
    key = make_exact_replay_key(
        chain_id=ROBINHOOD_CHAIN_ID,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        from_address=TEST_CALLER,
        to_address=CANONICAL_UNIVERSAL_ROUTER,
        value_wei=0,
        calldata_sha256=cd_sha,
    )
    rec = SimulationReplayRecord(
        key=key,
        response=SimulationCallResponse(status="NODE_LIMITATION", error_message="No archive"),
        router_balances={token: 0 for token in extract_path_token_addresses(plan)},
    )
    transport = DeterministicSimulationTransport(records={key: rec})
    adapter = SimulationAdapter(transport=transport)
    encoded = EncodedCalldata(
        plan_id=plan.plan_id,
        route_id=plan.route_ref.route_id,
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex=cd,
        calldata_sha256=cd_sha,
        commands_hex=encode_execution_plan(plan).commands_hex,
        commands_count=encode_execution_plan(plan).commands_count,
        deadline=1799999999,
        amount_in=plan.amount_in.atoms,
        min_amount_out=plan.min_amount_out.atoms,
    )

    with pytest.raises(NodeLimitationError):
        adapter.simulate(
            target=encoded,
            caller_wallet=TEST_CALLER,
            block_number=TEST_BLOCK_NUMBER,
            block_hash=TEST_BLOCK_HASH,
            raise_on_error=True,
            execution_plan=plan,
        )


# ==============================================================================
# 5. C16: Simulation Independence & Multi-Hop Quoter Masquerade Defense
# ==============================================================================


def test_c16_router_calldata_single_atomic_simulation_success() -> None:
    """Full atomic swap calldata targeting Universal Router executes successfully."""
    plan = _make_test_plan()
    encoded = encode_execution_plan(plan)
    key = make_exact_replay_key(
        chain_id=plan.chain_id,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        from_address=TEST_CALLER,
        to_address=plan.target_router,
        value_wei=0,
        calldata_sha256=encoded.calldata_sha256,
    )
    rec = SimulationReplayRecord(
        key=key,
        response=SimulationCallResponse(
            status="CALL_SUCCEEDED", return_data_hex="0x0001", gas_used=125000
        ),
        router_balances={token: 0 for token in extract_path_token_addresses(plan)},
    )
    transport = DeterministicSimulationTransport(records={key: rec})
    adapter = SimulationAdapter(transport=transport)

    evidence = adapter.simulate(
        target=plan,
        caller_wallet=TEST_CALLER,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        execution_plan=plan,
    )
    assert evidence.status == SimulationStatus.CALL_SUCCEEDED
    assert evidence.router_address == CANONICAL_UNIVERSAL_ROUTER


def test_c16_quoter_v3_target_address_rejected() -> None:
    """Quoter V3 address as simulation target is rejected fail-closed under C16."""
    plan, cd, cd_sha = _make_sample_calldata("c16_q3_addr")
    transport = DeterministicSimulationTransport()
    adapter = SimulationAdapter(transport=transport)
    encoded = EncodedCalldata(
        plan_id=plan.plan_id,
        route_id=plan.route_ref.route_id,
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=QUOTER_V3_ADDRESS,
        calldata_hex=cd,
        calldata_sha256=cd_sha,
        commands_hex=encode_execution_plan(plan).commands_hex,
        commands_count=encode_execution_plan(plan).commands_count,
        deadline=1799999999,
        amount_in=plan.amount_in.atoms,
        min_amount_out=plan.min_amount_out.atoms,
    )
    with pytest.raises(QuoterSimulationProhibitedError, match="known Quoter contract"):
        adapter.simulate(
            target=encoded,
            caller_wallet=TEST_CALLER,
            block_number=TEST_BLOCK_NUMBER,
            block_hash=TEST_BLOCK_HASH,
            execution_plan=plan,
        )


def test_c16_quoter_v4_target_address_rejected() -> None:
    """Quoter V4 address as simulation target is rejected fail-closed under C16."""
    plan, cd, cd_sha = _make_sample_calldata("c16_q4_addr")
    transport = DeterministicSimulationTransport()
    adapter = SimulationAdapter(transport=transport)
    encoded = EncodedCalldata(
        plan_id=plan.plan_id,
        route_id=plan.route_ref.route_id,
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=QUOTER_V4_ADDRESS,
        calldata_hex=cd,
        calldata_sha256=cd_sha,
        commands_hex=encode_execution_plan(plan).commands_hex,
        commands_count=encode_execution_plan(plan).commands_count,
        deadline=1799999999,
        amount_in=plan.amount_in.atoms,
        min_amount_out=plan.min_amount_out.atoms,
    )
    with pytest.raises(QuoterSimulationProhibitedError, match="known Quoter contract"):
        adapter.simulate(
            target=encoded,
            caller_wallet=TEST_CALLER,
            block_number=TEST_BLOCK_NUMBER,
            block_hash=TEST_BLOCK_HASH,
            execution_plan=plan,
        )


def test_c16_quoter_v3_calldata_selector_rejected() -> None:
    """Calldata starting with quoteExactInputSingle selector (0x0424d622) is rejected under C16."""
    cd = "0x0424d622" + "33" * 64
    cd_sha = compute_calldata_sha256(cd)
    transport = DeterministicSimulationTransport()
    adapter = SimulationAdapter(transport=transport)
    encoded = EncodedCalldata(
        plan_id="p1",
        route_id="r1",
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex=cd,
        calldata_sha256=cd_sha,
        commands_hex="0x00",
        commands_count=1,
        deadline=1799999999,
        amount_in=1000,
        min_amount_out=900,
    )
    with pytest.raises(QuoterSimulationProhibitedError, match="matches Quoter method"):
        adapter.simulate(
            target=encoded,
            caller_wallet=TEST_CALLER,
            block_number=TEST_BLOCK_NUMBER,
            block_hash=TEST_BLOCK_HASH,
        )


def test_c16_quoter_v4_calldata_selector_rejected() -> None:
    """Calldata starting with V4 Quoter selector (0xf7729d43) is rejected under C16."""
    cd = "0xf7729d43" + "44" * 64
    cd_sha = compute_calldata_sha256(cd)
    transport = DeterministicSimulationTransport()
    adapter = SimulationAdapter(transport=transport)
    encoded = EncodedCalldata(
        plan_id="p1",
        route_id="r1",
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex=cd,
        calldata_sha256=cd_sha,
        commands_hex="0x00",
        commands_count=1,
        deadline=1799999999,
        amount_in=1000,
        min_amount_out=900,
    )
    with pytest.raises(QuoterSimulationProhibitedError, match="matches Quoter method"):
        adapter.simulate(
            target=encoded,
            caller_wallet=TEST_CALLER,
            block_number=TEST_BLOCK_NUMBER,
            block_hash=TEST_BLOCK_HASH,
        )


def test_c16_plan_target_router_masquerading_quoter_rejected() -> None:
    """An ExecutionPlan crafted to target a Quoter contract is rejected fail-closed."""
    plan = _make_test_plan(target_router=QUOTER_V3_ADDRESS)
    transport = DeterministicSimulationTransport()
    adapter = SimulationAdapter(transport=transport)
    with pytest.raises(QuoterSimulationProhibitedError):
        adapter.simulate(
            target=plan,
            caller_wallet=TEST_CALLER,
            block_number=TEST_BLOCK_NUMBER,
            block_hash=TEST_BLOCK_HASH,
            execution_plan=plan,
        )


# ==============================================================================
# 6. C17: Inventory Subsidy Defense
# ==============================================================================


def test_c17_router_zero_path_token_balances_success() -> None:
    """When router balance of all route path tokens is 0, simulation succeeds."""
    plan = _make_test_plan()
    encoded = encode_execution_plan(plan)
    key = make_exact_replay_key(
        chain_id=plan.chain_id,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        from_address=TEST_CALLER,
        to_address=plan.target_router,
        value_wei=0,
        calldata_sha256=encoded.calldata_sha256,
    )
    rec = SimulationReplayRecord(
        key=key,
        response=SimulationCallResponse(status="CALL_SUCCEEDED", return_data_hex="0x0001"),
        router_balances={WETH_ADDRESS_4663: 0, USDG_ADDRESS_4663: 0},
    )
    transport = DeterministicSimulationTransport(records={key: rec})
    adapter = SimulationAdapter(transport=transport)

    evidence = adapter.simulate(
        target=plan,
        caller_wallet=TEST_CALLER,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        execution_plan=plan,
    )
    assert evidence.status == SimulationStatus.CALL_SUCCEEDED


def test_c17_router_nonzero_base_asset_balance_rejected() -> None:
    """Positive router balance of base asset is rejected under inventory subsidy defense."""
    plan = _make_test_plan()
    encoded = encode_execution_plan(plan)
    key = make_exact_replay_key(
        chain_id=plan.chain_id,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        from_address=TEST_CALLER,
        to_address=plan.target_router,
        value_wei=0,
        calldata_sha256=encoded.calldata_sha256,
    )
    rec = SimulationReplayRecord(
        key=key,
        response=SimulationCallResponse(status="CALL_SUCCEEDED", return_data_hex="0x0001"),
        router_balances={WETH_ADDRESS_4663: 500_000_000_000_000_000, USDG_ADDRESS_4663: 0},
    )
    transport = DeterministicSimulationTransport(records={key: rec})
    adapter = SimulationAdapter(transport=transport)

    with pytest.raises(InventorySubsidyError, match="Router holds non-zero balance"):
        adapter.simulate(
            target=plan,
            caller_wallet=TEST_CALLER,
            block_number=TEST_BLOCK_NUMBER,
            block_hash=TEST_BLOCK_HASH,
            execution_plan=plan,
        )


def test_c17_router_nonzero_intermediate_token_balance_rejected() -> None:
    """Positive router balance of intermediate token is rejected under inventory subsidy defense."""
    plan = _make_test_plan()
    encoded = encode_execution_plan(plan)
    key = make_exact_replay_key(
        chain_id=plan.chain_id,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        from_address=TEST_CALLER,
        to_address=plan.target_router,
        value_wei=0,
        calldata_sha256=encoded.calldata_sha256,
    )
    rec = SimulationReplayRecord(
        key=key,
        response=SimulationCallResponse(status="CALL_SUCCEEDED", return_data_hex="0x0001"),
        router_balances={WETH_ADDRESS_4663: 0, USDG_ADDRESS_4663: 1_000_000_000_000_000_000},
    )
    transport = DeterministicSimulationTransport(records={key: rec})
    adapter = SimulationAdapter(transport=transport)

    with pytest.raises(InventorySubsidyError, match="Router holds non-zero balance"):
        adapter.simulate(
            target=plan,
            caller_wallet=TEST_CALLER,
            block_number=TEST_BLOCK_NUMBER,
            block_hash=TEST_BLOCK_HASH,
            execution_plan=plan,
        )


def test_c17_router_nonzero_unrelated_token_balance_passes() -> None:
    """Router balance of an unrelated non-path token does not trigger inventory subsidy failure."""
    plan = _make_test_plan()
    encoded = encode_execution_plan(plan)
    key = make_exact_replay_key(
        chain_id=plan.chain_id,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        from_address=TEST_CALLER,
        to_address=plan.target_router,
        value_wei=0,
        calldata_sha256=encoded.calldata_sha256,
    )
    unrelated_token = "0x4444444444444444444444444444444444444444"
    rec = SimulationReplayRecord(
        key=key,
        response=SimulationCallResponse(status="CALL_SUCCEEDED", return_data_hex="0x0001"),
        router_balances={
            WETH_ADDRESS_4663: 0,
            USDG_ADDRESS_4663: 0,
            unrelated_token: 999_999_999,
        },
    )
    transport = DeterministicSimulationTransport(records={key: rec})
    adapter = SimulationAdapter(transport=transport)

    evidence = adapter.simulate(
        target=plan,
        caller_wallet=TEST_CALLER,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        execution_plan=plan,
    )
    assert evidence.status == SimulationStatus.CALL_SUCCEEDED


def test_c17_execution_plan_path_token_extraction_comprehensive() -> None:
    """extract_path_token_addresses correctly extracts and dedupes all route tokens."""
    plan = _make_test_plan()
    extracted = extract_path_token_addresses(plan)
    assert len(extracted) == 2
    assert WETH_ADDRESS_4663 in extracted
    assert USDG_ADDRESS_4663 in extracted


# ==============================================================================
# 7. Capability & Replay Key Safety Tests
# ==============================================================================


def test_deterministic_transport_exact_matching() -> None:
    """DeterministicSimulationTransport matches exact 7-tuple replay keys."""
    plan, cd, cd_sha = _make_sample_calldata("dt_match")
    key = make_exact_replay_key(
        chain_id=ROBINHOOD_CHAIN_ID,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        from_address=TEST_CALLER,
        to_address=CANONICAL_UNIVERSAL_ROUTER,
        value_wei=0,
        calldata_sha256=cd_sha,
    )
    rec = SimulationReplayRecord(
        key=key,
        response=SimulationCallResponse(status="CALL_SUCCEEDED", return_data_hex="0xabcdef"),
        router_balances={token: 0 for token in extract_path_token_addresses(plan)},
    )
    transport = DeterministicSimulationTransport(records={key: rec})

    req = SimulationCallRequest(
        chain_id=ROBINHOOD_CHAIN_ID,
        to_address=CANONICAL_UNIVERSAL_ROUTER,
        from_address=TEST_CALLER,
        calldata_hex=cd,
        calldata_sha256=cd_sha,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        value_wei=0,
    )
    res = transport.simulate_call(req)
    assert res.return_data_hex == "0xabcdef"
    assert len(transport.call_history) == 1


def test_deterministic_transport_wildcard_rejection() -> None:
    """Wildcards and fuzzy patterns in replay keys are strictly rejected."""
    with pytest.raises(TransportWildcardForbiddenError):
        make_exact_replay_key(
            chain_id=ROBINHOOD_CHAIN_ID,
            block_number=TEST_BLOCK_NUMBER,
            block_hash="*",
            from_address=TEST_CALLER,
            to_address=CANONICAL_UNIVERSAL_ROUTER,
            value_wei=0,
            calldata_sha256="aa" * 32,
        )

    with pytest.raises(TransportWildcardForbiddenError):
        make_exact_replay_key(
            chain_id=ROBINHOOD_CHAIN_ID,
            block_number=TEST_BLOCK_NUMBER,
            block_hash=TEST_BLOCK_HASH,
            from_address="ANY",
            to_address=CANONICAL_UNIVERSAL_ROUTER,
            value_wei=0,
            calldata_sha256="aa" * 32,
        )


def test_deterministic_transport_missing_key_raises_not_found() -> None:
    """Missing key in DeterministicSimulationTransport raises TransportKeyNotFoundError."""
    plan, cd, cd_sha = _make_sample_calldata("dt_missing")
    transport = DeterministicSimulationTransport()
    req = SimulationCallRequest(
        chain_id=ROBINHOOD_CHAIN_ID,
        to_address=CANONICAL_UNIVERSAL_ROUTER,
        from_address=TEST_CALLER,
        calldata_hex=cd,
        calldata_sha256=cd_sha,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        value_wei=0,
    )
    with pytest.raises(TransportKeyNotFoundError, match="Exact replay record not found"):
        transport.simulate_call(req)


def test_deterministic_transport_read_only_blocks_send_sign() -> None:
    """DeterministicSimulationTransport strictly blocks write and signing methods."""
    transport = DeterministicSimulationTransport()
    with pytest.raises(AttributeError, match="strictly read-only"):
        getattr(transport, "send_transaction")()  # noqa: B009
    with pytest.raises(AttributeError, match="strictly read-only"):
        getattr(transport, "sign_transaction")()  # noqa: B009


def test_draft_simulation_evidence_can_atomic_execute_strictly_false() -> None:
    """DraftSimulationEvidence.can_atomic_execute is strictly False."""
    evidence = DraftSimulationEvidence(
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex="0x1234",
        calldata_sha256="aa" * 32,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        from_address=TEST_CALLER,
        value_wei=0,
        status="CALL_SUCCEEDED",
    )
    assert getattr(evidence, "can_atomic_execute") is False  # noqa: B009


def test_draft_simulation_evidence_immutable_attribute_assignment_fails() -> None:
    """Assigning to can_atomic_execute or other fields on evidence raises an error."""
    evidence = DraftSimulationEvidence(
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex="0x1234",
        calldata_sha256="aa" * 32,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        from_address=TEST_CALLER,
        value_wei=0,
        status="CALL_SUCCEEDED",
    )
    with pytest.raises(AttributeError, match="can_atomic_execute is immutable"):
        setattr(evidence, "can_atomic_execute", True)  # noqa: B010

    with pytest.raises((FrozenInstanceError, AttributeError)):
        setattr(evidence, "status", "EXECUTED")  # noqa: B010


def test_draft_simulation_evidence_serialization_roundtrip() -> None:
    """DraftSimulationEvidence roundtrips through dict and JSON without loss."""
    original = DraftSimulationEvidence(
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex="0x1234abcd",
        calldata_sha256="55" * 32,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        from_address=TEST_CALLER,
        value_wei=0,
        status="CALL_SUCCEEDED",
        gas_used=120000,
        return_data_hex="0x0001",
        error_message=None,
        is_draft=True,
    )
    serialized_json = original.to_json()
    restored = DraftSimulationEvidence.from_json(serialized_json)
    assert restored == original
    assert getattr(restored, "can_atomic_execute") is False  # noqa: B009


def test_simulation_adapter_with_execution_plan_direct_input() -> None:
    """SimulationAdapter directly takes ExecutionPlan, auto-encodes, and simulates."""
    plan = _make_test_plan()
    encoded = encode_execution_plan(plan)
    key = make_exact_replay_key(
        chain_id=plan.chain_id,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        from_address=TEST_CALLER,
        to_address=plan.target_router,
        value_wei=0,
        calldata_sha256=encoded.calldata_sha256,
    )
    rec = SimulationReplayRecord(
        key=key,
        response=SimulationCallResponse(status="CALL_SUCCEEDED", return_data_hex="0x0001"),
        router_balances={token: 0 for token in extract_path_token_addresses(plan)},
    )
    transport = DeterministicSimulationTransport(records={key: rec})
    adapter = SimulationAdapter(transport=transport)

    evidence = adapter.simulate(
        target=plan,
        caller_wallet=TEST_CALLER,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        execution_plan=plan,
    )
    assert evidence.status == SimulationStatus.CALL_SUCCEEDED
    assert evidence.calldata_sha256 == encoded.calldata_sha256


def test_simulate_execution_functional_wrapper() -> None:
    """Functional wrapper simulate_execution operates equivalently to adapter."""
    plan, cd, cd_sha = _make_sample_calldata("func_wrap")
    key = make_exact_replay_key(
        chain_id=ROBINHOOD_CHAIN_ID,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        from_address=TEST_CALLER,
        to_address=CANONICAL_UNIVERSAL_ROUTER,
        value_wei=0,
        calldata_sha256=cd_sha,
    )
    rec = SimulationReplayRecord(
        key=key,
        response=SimulationCallResponse(status="CALL_SUCCEEDED", return_data_hex="0x9999"),
        router_balances={token: 0 for token in extract_path_token_addresses(plan)},
    )
    transport = DeterministicSimulationTransport(records={key: rec})
    encoded = EncodedCalldata(
        plan_id=plan.plan_id,
        route_id=plan.route_ref.route_id,
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex=cd,
        calldata_sha256=cd_sha,
        commands_hex=encode_execution_plan(plan).commands_hex,
        commands_count=encode_execution_plan(plan).commands_count,
        deadline=1799999999,
        amount_in=plan.amount_in.atoms,
        min_amount_out=plan.min_amount_out.atoms,
    )

    evidence = simulate_execution(
        target=encoded,
        transport=transport,
        caller_wallet=TEST_CALLER,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        execution_plan=plan,
    )
    assert evidence.status == SimulationStatus.CALL_SUCCEEDED
    assert evidence.return_data_hex == "0x9999"


# ==============================================================================
# 8. Architectural Boundary Static AST Checks
# ==============================================================================


def test_architecture_simulation_zero_forbidden_imports() -> None:
    """Static AST check: simulation.py must not import from forbidden legacy packages."""
    sim_path = Path(__file__).resolve().parent.parent.parent / "atomic_execution" / "simulation.py"
    assert sim_path.is_file()
    tree = ast.parse(sim_path.read_text(encoding="utf-8"), filename=str(sim_path))
    forbidden_roots = {"arbitrage", "execution", "core", "chains", "backtest", "monitors"}

    for node in ast.walk(tree):
        imported: list[str] = []
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.append(node.module)

        for mod in imported:
            root_pkg = mod.split(".")[0]
            assert root_pkg not in forbidden_roots, f"Forbidden import '{mod}' in simulation.py"


def test_architecture_transport_zero_forbidden_imports() -> None:
    """Static AST check: transport.py must not import from forbidden legacy packages."""
    trans_path = Path(__file__).resolve().parent.parent.parent / "atomic_execution" / "transport.py"
    assert trans_path.is_file()
    tree = ast.parse(trans_path.read_text(encoding="utf-8"), filename=str(trans_path))
    forbidden_roots = {"arbitrage", "execution", "core", "chains", "backtest", "monitors"}

    for node in ast.walk(tree):
        imported: list[str] = []
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.append(node.module)

        for mod in imported:
            root_pkg = mod.split(".")[0]
            assert root_pkg not in forbidden_roots, f"Forbidden import '{mod}' in transport.py"
