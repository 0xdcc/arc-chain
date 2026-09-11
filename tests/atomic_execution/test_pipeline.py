"""Comprehensive unit and regression test suite for atomic execution pipeline (C23).

Verifies:
- End-to-end stream parsing, planning, encoding, simulation, and accounting.
- Strict conservation: total_processed == passed_count + rejected_count.
- Handling of mixed streams (profitable, unprofitable, invalid topology, revert, timeout, etc.).
- Robust fail-closed handling for empty files, bad syntax, duplicate keys.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from atomic_execution.pipeline import (
    PipelineInputError,
    PipelineSummary,
    execute_pipeline,
)
from atomic_execution.simulation import SimulationStatus
from atomic_execution.transport import DeterministicSimulationTransport

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "atomic_execution" / "v1"
E2E_STREAM_PATH = FIXTURES_DIR / "e2e-stream.jsonl"


def test_e2e_stream_file_integrity() -> None:
    """Fixture e2e-stream.jsonl must exist, have >= 12 cases, and cover all categories."""
    assert E2E_STREAM_PATH.is_file(), f"Missing fixture file: {E2E_STREAM_PATH}"

    cases: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    category_counts: dict[str, int] = {}

    with E2E_STREAM_PATH.open("r", encoding="utf-8") as stream:
        for line_num, line in enumerate(stream, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            case = json.loads(stripped)
            case_id = str(case["case_id"])
            assert case_id not in seen_ids, f"Duplicate case_id {case_id} on line {line_num}"
            seen_ids.add(case_id)
            cat = str(case["category"])
            category_counts[cat] = category_counts.get(cat, 0) + 1
            cases.append(case)

    assert len(cases) >= 12, f"Expected at least 12 stream cases, found {len(cases)}"
    for required_cat in (
        "PROFITABLE",
        "UNPROFITABLE",
        "INVALID_TOPOLOGY",
        "REVERT",
        "TIMEOUT",
        "NODE_LIMITATION",
        "OUTPUT_UNVERIFIED",
    ):
        assert category_counts.get(required_cat, 0) >= 1, f"Missing category {required_cat}"


def test_pipeline_e2e_full_stream_execution() -> None:
    """Execute full pipeline over e2e-stream.jsonl and assert strict conservation."""
    transport = DeterministicSimulationTransport()
    results, summary = execute_pipeline(E2E_STREAM_PATH, transport=transport)

    assert isinstance(summary, PipelineSummary)
    assert summary.is_conserved
    assert summary.total_processed == len(results)
    assert summary.total_processed == summary.passed_count + summary.rejected_count
    assert summary.simulated_success_count == summary.passed_count

    assert summary.total_processed >= 12
    assert summary.passed_count == 3
    assert summary.profitable_count == 0
    assert summary.rejected_count == summary.total_processed - 3

    breakdown = summary.stage_breakdown
    assert breakdown["SIMULATION_SUCCEEDED"] == summary.passed_count
    total_failures = (
        breakdown["INPUT_GATE_REJECTED"]
        + breakdown["PLANNING_REJECTED"]
        + breakdown["ENCODING_REJECTED"]
        + breakdown["SIMULATION_FAILED"]
    )
    assert total_failures == summary.rejected_count


def test_pipeline_individual_profitable_weth() -> None:
    """Case 1: Profitable 2-hop WETH cycle completes with CALL_SUCCEEDED."""
    transport = DeterministicSimulationTransport()
    results, summary = execute_pipeline(E2E_STREAM_PATH, transport=transport)

    item1 = results[0]
    assert item1.case_id == "e2e_profitable_weth_2hop"
    assert item1.stage == "COMPLETE"
    assert item1.passed is True
    assert item1.simulated_success is True
    assert item1.is_profitable is False
    assert item1.verified_net_profit is None
    assert item1.output_verified is False
    assert item1.status == SimulationStatus.OUTPUT_UNVERIFIED
    assert item1.rejection_reason is None
    assert item1.evidence is not None
    assert item1.execution_plan is not None
    assert item1.encoded_calldata is not None
    assert item1.execution_plan.net_profit_usd > Decimal("0")


def test_pipeline_individual_profitable_usdg() -> None:
    """Case 2: Profitable 3-hop USDG cycle completes with CALL_SUCCEEDED."""
    transport = DeterministicSimulationTransport()
    results, summary = execute_pipeline(E2E_STREAM_PATH, transport=transport)

    item2 = results[1]
    assert item2.case_id == "e2e_profitable_usdg_3hop"
    assert item2.stage == "COMPLETE"
    assert item2.passed is True
    assert item2.simulated_success is True
    assert item2.is_profitable is False
    assert item2.verified_net_profit is None
    assert item2.output_verified is False
    assert item2.status == SimulationStatus.OUTPUT_UNVERIFIED


def test_pipeline_unprofitable_below_floor() -> None:
    """Case 3: Unprofitable quote below gas floor is rejected in PLANNING."""
    transport = DeterministicSimulationTransport()
    results, summary = execute_pipeline(E2E_STREAM_PATH, transport=transport)

    item3 = results[2]
    assert item3.case_id == "e2e_unprofitable_below_floor"
    assert item3.stage == "PLANNING"
    assert item3.passed is False
    assert item3.simulated_success is False
    assert item3.rejection_reason == "UNPROFITABLE"


def test_pipeline_unprofitable_excessive_amount() -> None:
    """Case 4: Trade exceeding $500 hard cap is rejected in PLANNING."""
    transport = DeterministicSimulationTransport()
    results, summary = execute_pipeline(E2E_STREAM_PATH, transport=transport)

    item4 = results[3]
    assert item4.case_id == "e2e_unprofitable_excessive_amount"
    assert item4.stage == "PLANNING"
    assert item4.passed is False
    assert item4.rejection_reason == "EXCESSIVE_AMOUNT"


def test_pipeline_input_gate_v2_rejected() -> None:
    """Case 5: Uniswap V2 hop is rejected in INPUT_GATE."""
    transport = DeterministicSimulationTransport()
    results, summary = execute_pipeline(E2E_STREAM_PATH, transport=transport)

    item5 = results[4]
    assert item5.case_id == "e2e_invalid_unsupported_protocol_v2"
    assert item5.stage == "INPUT_GATE"
    assert item5.passed is False
    assert item5.rejection_reason == "UNSUPPORTED_PROTOCOL"


def test_pipeline_input_gate_single_hop_rejected() -> None:
    """Case 6: Single-hop route is rejected in INPUT_GATE."""
    transport = DeterministicSimulationTransport()
    results, summary = execute_pipeline(E2E_STREAM_PATH, transport=transport)

    item6 = results[5]
    assert item6.case_id == "e2e_invalid_hop_count_single_hop"
    assert item6.stage == "INPUT_GATE"
    assert item6.passed is False
    assert item6.rejection_reason == "INVALID_HOP_COUNT"


def test_pipeline_input_gate_unclosed_cycle_rejected() -> None:
    """Case 7: Unclosed route cycle is rejected in INPUT_GATE."""
    transport = DeterministicSimulationTransport()
    results, summary = execute_pipeline(E2E_STREAM_PATH, transport=transport)

    item7 = results[6]
    assert item7.case_id == "e2e_invalid_cycle_not_closed"
    assert item7.stage == "INPUT_GATE"
    assert item7.passed is False
    assert item7.rejection_reason == "CYCLE_NOT_CLOSED"


def test_pipeline_simulation_contract_revert() -> None:
    """Case 8: Simulation returning revert is classified as CONTRACT_REVERT."""
    transport = DeterministicSimulationTransport()
    results, summary = execute_pipeline(E2E_STREAM_PATH, transport=transport)

    item8 = results[7]
    assert item8.case_id == "e2e_simulation_contract_revert"
    assert item8.stage == "SIMULATION"
    assert item8.passed is False
    assert item8.simulated_success is False
    assert item8.status == SimulationStatus.CONTRACT_REVERT


def test_pipeline_simulation_rpc_timeout() -> None:
    """Case 9: RPC timeout is classified as RPC_ERROR."""
    transport = DeterministicSimulationTransport()
    results, summary = execute_pipeline(E2E_STREAM_PATH, transport=transport)

    item9 = results[8]
    assert item9.case_id == "e2e_simulation_timeout_rpc_error"
    assert item9.stage == "SIMULATION"
    assert item9.passed is False
    assert item9.status == SimulationStatus.RPC_ERROR


def test_pipeline_simulation_node_limitation() -> None:
    """Case 10: Missing archive state is classified as NODE_LIMITATION."""
    transport = DeterministicSimulationTransport()
    results, summary = execute_pipeline(E2E_STREAM_PATH, transport=transport)

    item10 = results[9]
    assert item10.case_id == "e2e_simulation_node_limitation"
    assert item10.stage == "SIMULATION"
    assert item10.passed is False
    assert item10.status == SimulationStatus.NODE_LIMITATION


def test_pipeline_simulation_output_unverified() -> None:
    """Case 11: Empty 0x simulation return is classified as OUTPUT_UNVERIFIED."""
    transport = DeterministicSimulationTransport()
    results, summary = execute_pipeline(E2E_STREAM_PATH, transport=transport)

    item11 = results[10]
    assert item11.case_id == "e2e_simulation_output_unverified_0x"
    assert item11.stage == "COMPLETE"
    assert item11.passed is True
    assert item11.call_succeeded is True
    assert item11.output_verified is False
    assert item11.verified_net_profit is None
    assert item11.status == SimulationStatus.OUTPUT_UNVERIFIED


def test_pipeline_input_gate_duplicate_pool_rejected() -> None:
    """Case 12: Duplicate pool in route is rejected in INPUT_GATE."""
    transport = DeterministicSimulationTransport()
    results, summary = execute_pipeline(E2E_STREAM_PATH, transport=transport)

    item12 = results[11]
    assert item12.case_id == "e2e_invalid_duplicate_pool"
    assert item12.stage == "INPUT_GATE"
    assert item12.passed is False
    assert item12.rejection_reason == "DUPLICATE_POOL"


def test_pipeline_empty_input_file_fails_closed(tmp_path: Path) -> None:
    """Empty input file raises PipelineInputError fail-closed."""
    empty_file = tmp_path / "empty.jsonl"
    empty_file.write_text("", encoding="utf-8")
    with pytest.raises(PipelineInputError, match="empty or blank"):
        execute_pipeline(empty_file)


def test_pipeline_malformed_json_fails_closed(tmp_path: Path) -> None:
    """Malformed JSON becomes a rejected row; no execution or silent omission."""
    bad_file = tmp_path / "bad.jsonl"
    bad_file.write_text("{broken json\n", encoding="utf-8")
    results, summary = execute_pipeline(bad_file)
    assert summary.total_processed == summary.rejected_count == 1
    assert results[0].rejection_reason == "MALFORMED_INPUT"
    assert not results[0].call_succeeded


def test_pipeline_duplicate_json_keys_fails_closed(tmp_path: Path) -> None:
    """Duplicate JSON keys reject the row without losing batch accounting."""
    dup_file = tmp_path / "dup.jsonl"
    dup_file.write_text('{"k": 1, "k": 2}\n', encoding="utf-8")
    results, summary = execute_pipeline(dup_file)
    assert summary.total_processed == summary.rejected_count == 1
    assert "Duplicate JSON key detected" in (results[0].error_message or "")
    assert not results[0].call_succeeded


def test_pipeline_all_rejected_stream_conservation(tmp_path: Path) -> None:
    """A stream with only failing items completes without error and preserves conservation."""
    fail_file = tmp_path / "all_fail.jsonl"
    lines: list[str] = []
    with E2E_STREAM_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            obj = json.loads(line)
            if obj["category"] in ("UNPROFITABLE", "INVALID_TOPOLOGY"):
                lines.append(line.strip())
    assert len(lines) >= 3
    fail_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    results, summary = execute_pipeline(fail_file)
    assert summary.is_conserved
    assert summary.total_processed == len(lines)
    assert summary.passed_count == 0
    assert summary.simulated_success_count == 0
    assert summary.profitable_count == 0
    assert summary.rejected_count == len(lines)


def test_pipeline_in_memory_stream() -> None:
    """Pipeline can process an in-memory iterable of dictionaries."""
    cases: list[dict[str, object]] = []
    with E2E_STREAM_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            cases.append(json.loads(line))

    transport = DeterministicSimulationTransport()
    results, summary = execute_pipeline(cases[:2], transport=transport)
    assert summary.total_processed == 2
    assert summary.passed_count == 2
    assert summary.is_conserved
