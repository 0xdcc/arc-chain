"""Unit tests verifying audit remediation for atomic_execution (F03, F05, F06)."""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from atomic_execution.encoding import EncodedCalldata
from atomic_execution.inputs import ROBINHOOD_CHAIN_ID
from atomic_execution.models import DraftSimulationEvidence
from atomic_execution.pipeline import PipelineConfig, process_candidate_item
from atomic_execution.planning import CANONICAL_UNIVERSAL_ROUTER
from atomic_execution.policy import PolicyRejectionReason
from atomic_execution.simulation import (
    InventorySubsidyError,
    SimulationAdapter,
    SimulationStatus,
)
from atomic_execution.transport import (
    DeterministicSimulationTransport,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "atomic_execution" / "v1"
E2E_STREAM_PATH = FIXTURES_DIR / "e2e-stream.jsonl"

TEST_CALLER = "0x39dBED3a2bd333467115dE45665cC57F813C4571"
TEST_BLOCK_NUM = 59255390
TEST_BLOCK_HASH = "0x" + "11" * 32
SAMPLE_SHA256 = "a" * 64


def test_f03_three_layer_outcome_semantics_decoupling() -> None:
    """F03: Decouple call_succeeded, output_verified, and verified_net_profit."""
    # 1. 0x return: call succeeded on-chain, but output is unverified
    ev_0x = DraftSimulationEvidence(
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex="0x1234",
        calldata_sha256=SAMPLE_SHA256,
        block_number=TEST_BLOCK_NUM,
        block_hash=TEST_BLOCK_HASH,
        from_address=TEST_CALLER,
        status=str(SimulationStatus.OUTPUT_UNVERIFIED),
        return_data_hex="0x",
    )
    assert ev_0x.call_succeeded is True
    assert ev_0x.output_verified is False

    # 2. Non-empty return data: call succeeded and output observed
    ev_verified = DraftSimulationEvidence(
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex="0x1234",
        calldata_sha256=SAMPLE_SHA256,
        block_number=TEST_BLOCK_NUM,
        block_hash=TEST_BLOCK_HASH,
        from_address=TEST_CALLER,
        status=str(SimulationStatus.CALL_SUCCEEDED),
        return_data_hex="0x" + "00" * 31 + "01",
    )
    assert ev_verified.call_succeeded is True
    assert ev_verified.output_verified is False  # Non-empty bytes are not output evidence.

    # 3. Contract revert: call failed
    ev_revert = DraftSimulationEvidence(
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex="0x1234",
        calldata_sha256=SAMPLE_SHA256,
        block_number=TEST_BLOCK_NUM,
        block_hash=TEST_BLOCK_HASH,
        from_address=TEST_CALLER,
        status=str(SimulationStatus.CONTRACT_REVERT),
        return_data_hex="0x08c379a0",
    )
    assert ev_revert.call_succeeded is False
    assert ev_revert.output_verified is False


def test_f05_empty_path_tokens_inventory_check_bypass_rejected() -> None:
    """F05: Passing empty path_tokens tuple/list to simulate() must be strictly rejected."""
    transport = DeterministicSimulationTransport()
    adapter = SimulationAdapter(transport=transport)
    encoded = EncodedCalldata(
        plan_id="p1",
        route_id="r1",
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=CANONICAL_UNIVERSAL_ROUTER,
        calldata_hex="0x1234",
        calldata_sha256=SAMPLE_SHA256,
        commands_hex="0x00",
        commands_count=1,
        deadline=1799999999,
        amount_in=1000,
        min_amount_out=900,
    )

    # Empty list must raise InventorySubsidyError
    with pytest.raises(InventorySubsidyError, match="Empty path_tokens is strictly prohibited"):
        adapter.simulate(
            target=encoded,
            caller_wallet=TEST_CALLER,
            block_number=TEST_BLOCK_NUM,
            block_hash=TEST_BLOCK_HASH,
            path_tokens=[],
        )


def test_f06_explicit_zero_price_not_masked_by_defaults() -> None:
    """F06: Explicit 0 price must NOT be masked by default $2500, but rejected by policy."""
    transport = DeterministicSimulationTransport()
    adapter = SimulationAdapter(transport=transport)
    config = PipelineConfig()

    # Load valid item from fixture stream and override base_asset_usd_price to "0"
    with E2E_STREAM_PATH.open("r", encoding="utf-8") as f:
        first_line = f.readline().strip()
    raw_item = json.loads(first_line)
    _anchor_synthetic_item(raw_item)
    raw_item["base_asset_usd_price"] = "0"

    result = process_candidate_item(0, raw_item, adapter, config)
    assert result.passed is False
    assert result.stage == "PLANNING"
    assert result.rejection_reason == PolicyRejectionReason.INVALID_PRICE


def test_f06_malformed_price_does_not_abort_pipeline() -> None:
    """F06: Malformed price string must produce structured rejection instead of raising Decimal exception."""
    transport = DeterministicSimulationTransport()
    adapter = SimulationAdapter(transport=transport)
    config = PipelineConfig()

    raw_item: dict[str, Any] = {
        "case_id": "malformed_price_test",
        "base_asset_usd_price": "invalid_not_a_number",
        "candidate": {},
    }

    result = process_candidate_item(0, raw_item, adapter, config)
    assert result.passed is False
    assert result.stage == "INPUT_GATE"
    assert result.rejection_reason == "MALFORMED_INPUT"
    assert "Invalid base_asset_usd_price" in (result.error_message or "")


def _anchor_synthetic_item(item):
    """Migrate only this explicitly synthetic fixture; never re-anchor production observations."""
    from arbitrage_contracts.serialization import _deserialize_state_version
    from arbitrage_contracts.state import canonical_state_ref
    from atomic_execution.inputs import USDG_ADDRESS_4663, WETH_ADDRESS_4663

    state = _deserialize_state_version(item["candidate"]["state_version"])
    assert item["candidate"]["quote_evidence"]["data_mode"] == "synthetic"
    item["candidate"]["quote_evidence"]["state_version_ref"] = canonical_state_ref(state)
    item["router_balances"] = {WETH_ADDRESS_4663: 0, USDG_ADDRESS_4663: 0}
    return item


def _valid_synthetic_item(return_data="0x"):
    item = json.loads(E2E_STREAM_PATH.read_text().splitlines()[0])
    _anchor_synthetic_item(item)
    item["response"]["return_data_hex"] = return_data
    return item


@pytest.mark.parametrize("return_data", ["0x", "0x01", "0x" + "00" * 32])
def test_f03_success_is_not_verified_profit(return_data):
    from atomic_execution.pipeline import execute_pipeline

    results, summary = execute_pipeline([_valid_synthetic_item(return_data)])
    result = results[0]
    assert result.call_succeeded and result.simulated_success
    assert not result.output_verified and not result.is_profitable
    assert result.verified_net_profit is None
    assert summary.simulated_success_count == 1
    assert summary.profitable_count == 0
    assert summary.stage_breakdown["SIMULATION_FAILED"] == 0


def test_f06_good_bad_good_keeps_all_row_results():
    from atomic_execution.pipeline import execute_pipeline

    results, summary = execute_pipeline(
        [_valid_synthetic_item(), "{bad json", _valid_synthetic_item()]
    )
    assert len(results) == summary.total_processed == 3
    assert [row.index for row in results] == [1, 2, 3]
    assert results[0].call_succeeded and results[2].call_succeeded
    assert results[1].rejection_reason == "MALFORMED_INPUT"
    assert summary.is_conserved


@pytest.mark.parametrize("candidate", ["invalid", [1], 42, None])
def test_f06_bad_candidate_shape_is_a_row_rejection(candidate):
    from atomic_execution.pipeline import execute_pipeline

    results, summary = execute_pipeline([{"candidate": candidate}, _valid_synthetic_item()])
    assert results[0].rejection_reason == "MALFORMED_INPUT"
    assert results[1].call_succeeded
    assert summary.total_processed == 2


@pytest.mark.parametrize(
    "field,reason",
    [("base_asset_usd_price", "INVALID_PRICE"), ("conservative_gas_usd", "INVALID_GAS")],
)
def test_f06_zero_values_do_not_use_defaults(field, reason):
    from atomic_execution.pipeline import execute_pipeline

    item = _valid_synthetic_item()
    item[field] = "0"
    results, _ = execute_pipeline([item])
    assert results[0].rejection_reason == reason


def test_f06_real_mode_cannot_use_synthetic_default_price():
    from atomic_execution.pipeline import execute_pipeline

    item = _valid_synthetic_item()
    item.pop("base_asset_usd_price")
    item["candidate"]["quote_evidence"]["data_mode"] = "live_readonly"
    results, _ = execute_pipeline([item], config=PipelineConfig(allow_synthetic_defaults=True))
    assert results[0].rejection_reason == "MISSING_PRICE"


def test_f05_missing_inventory_is_not_assumed_zero():
    from atomic_execution.pipeline import execute_pipeline

    item = _valid_synthetic_item()
    item.pop("router_balances")
    results, _ = execute_pipeline([item])
    assert not results[0].call_succeeded
    assert results[0].rejection_reason == "InventorySubsidyError"


def test_f05_encoded_entry_requires_plan_and_complete_tokens():
    from atomic_execution.inputs import WETH_ADDRESS_4663
    from atomic_execution.pipeline import execute_pipeline

    result = execute_pipeline([_valid_synthetic_item()])[0][0]
    assert result.encoded_calldata is not None and result.execution_plan is not None
    adapter = SimulationAdapter(DeterministicSimulationTransport())
    with pytest.raises(InventorySubsidyError, match="originating execution_plan"):
        adapter.simulate(result.encoded_calldata, TEST_CALLER, TEST_BLOCK_NUM, TEST_BLOCK_HASH)
    with pytest.raises(InventorySubsidyError, match="does not match"):
        adapter.simulate(
            result.encoded_calldata,
            TEST_CALLER,
            TEST_BLOCK_NUM,
            TEST_BLOCK_HASH,
            execution_plan=result.execution_plan,
            path_tokens=[WETH_ADDRESS_4663],
        )


def test_f05_plan_and_encoded_entry_have_identical_checks():
    from atomic_execution.pipeline import execute_pipeline
    from atomic_execution.simulation import SimulationSecurityError

    transport = DeterministicSimulationTransport()
    result = execute_pipeline([_valid_synthetic_item()], transport=transport)[0][0]
    assert result.execution_plan is not None and result.encoded_calldata is not None
    adapter = SimulationAdapter(transport)
    plan_result = adapter.simulate(
        result.execution_plan, TEST_CALLER, TEST_BLOCK_NUM, TEST_BLOCK_HASH
    )
    encoded_result = adapter.simulate(
        result.encoded_calldata,
        TEST_CALLER,
        TEST_BLOCK_NUM,
        TEST_BLOCK_HASH,
        execution_plan=result.execution_plan,
    )
    assert plan_result.call_succeeded and encoded_result.call_succeeded
    assert not plan_result.output_verified and not encoded_result.output_verified
    altered = result.encoded_calldata.to_dict()
    altered["calldata_sha256"] = "f" * 64
    with pytest.raises(SimulationSecurityError, match="does not match plan"):
        adapter.simulate(
            EncodedCalldata.from_dict(altered),
            TEST_CALLER,
            TEST_BLOCK_NUM,
            TEST_BLOCK_HASH,
            execution_plan=result.execution_plan,
        )


def test_f06_synthetic_defaults_are_explicitly_labelled():
    from atomic_execution.pipeline import execute_pipeline

    item = _valid_synthetic_item()
    item.pop("base_asset_usd_price")
    item.pop("conservative_gas_usd")
    results, _ = execute_pipeline([item], config=PipelineConfig(allow_synthetic_defaults=True))
    assert results[0].call_succeeded
    assert results[0].valuation_source == "synthetic_assumption"
    assert results[0].to_dict()["valuation_source"] == "synthetic_assumption"
    assert results[0].verified_net_profit is None
