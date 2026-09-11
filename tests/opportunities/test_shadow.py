"""Shadow orchestration coverage for evidence, budgets, circuit breaking, and lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arbitrage_contracts.identity import Amount
from arbitrage_contracts.quote import QuoteStatus
from opportunities.candidates import Candidate, CandidateSelection, select_candidates
from opportunities.input_gate import load_registry
from opportunities.lifecycle import LifecyclePolicy
from opportunities.quote_adapter import QuoteAdapterRequest
from opportunities.shadow import ShadowConfig, run_shadow
from opportunities.store import AppendOnlyLedger
from tests.opportunities.test_candidates import _candidates, _pool, _registry, _route

CHAIN_ID = 4663
BLOCK = 120
STATE = {
    "chain_id": CHAIN_ID,
    "block_number": BLOCK,
    "block_hash": "0x" + "91" * 32,
    "received_at_ms": 1000,
    "block_timestamp_s": 1,
    "complete_through_block": BLOCK,
    "completeness": "ready",
}
REQUEST = QuoteAdapterRequest(
    "0x33e885eD0Ec9bF04EcfB19341582aADCb4c8A9E7",
    "0x8Dc178eFB8111BB0973Dd9d722ebeFF267c98F94",
    data_mode="synthetic",
    actor_scope="synthetic",
    source_refs=("fixture:offline",),
)


class FixedClock:
    def time_ms(self) -> int:
        return 2_000_000_000


@dataclass
class RpcResponse:
    responses: list[dict[str, Any]]
    raise_error: Exception | None = None

    def call(
        self,
        method: str,
        params: list[Any] | tuple[Any, ...] | None = None,
        block_identifier: str | None = None,
    ) -> dict[str, Any]:
        if self.raise_error is not None:
            raise self.raise_error
        return self.responses.pop(0)


def _output(amount: int, gas: int) -> str:
    from eth_abi import encode as abi_encode

    return "0x" + abi_encode(["uint256", "uint160", "uint32", "uint256"], [amount, 1, 0, gas]).hex()


def _config(max_candidates: int = 1, max_rpc_calls: int = 10) -> ShadowConfig:
    return ShadowConfig(
        lifecycle=LifecyclePolicy(3_000, 250),
        max_candidates=max_candidates,
        max_rpc_calls=max_rpc_calls,
        quote_request=REQUEST,
        registry_semantic_revision="registry-v1",
        conversions=(),
    )


def _ledger(tmp_path: Path) -> AppendOnlyLedger:
    return AppendOnlyLedger(tmp_path / "ledger.jsonl")


def _payloads(ledger: AppendOnlyLedger) -> list[dict[str, Any]]:
    return list(ledger.load().events)


def _selection(max_candidates: int = 2) -> CandidateSelection:
    registry = load_registry(
        _registry(
            [
                _pool("0x0000000000000000000000000000000000000002"),
                _pool("0x0000000000000000000000000000000000000003"),
            ]
        )
    )
    route = _route(
        None,
        [
            "0x0000000000000000000000000000000000000002",
            "0x0000000000000000000000000000000000000003",
        ],
        [
            "0x00000000000000000000000000000000000000bb",
            "0x00000000000000000000000000000000000000aa",
        ],
    )
    return select_candidates(_candidates([route]), registry, max_candidates)


def test_duplicate_rpc_call_is_counted_once_per_attempt(tmp_path: Path) -> None:
    class DuplicateRpc:
        def __init__(self) -> None:
            self.calls = 0

        def call(
            self,
            method: str,
            params: Any = None,
            block_identifier: Any = None,
        ) -> dict[str, Any]:
            self.calls += 1
            return {"result": _output(1000, 273)}

    ledger = _ledger(tmp_path)
    outcome = run_shadow(_selection(), STATE, ledger, _config(), DuplicateRpc(), clock=FixedClock())
    assert outcome.ledger_sequence == 0
    assert outcome.sim_available is False


def test_rpc_error_is_appended_as_unknown_result(tmp_path: Path) -> None:
    class FailingRpc:
        def call(
            self,
            method: str,
            params: Any = None,
            block_identifier: Any = None,
        ) -> dict[str, Any]:
            raise RuntimeError("pool mapping incomplete")

    ledger = _ledger(tmp_path)
    outcome = run_shadow(_selection(), STATE, ledger, _config(), FailingRpc(), clock=FixedClock())
    payloads = _payloads(ledger)
    assert outcome.candidate_count == 1
    assert payloads[0]["schema_id"] == "w2-shadow-event-v1"
    assert payloads[0]["quote_status"] == "rpc_error"
    assert payloads[0]["economic_status"] == "unknown"
    assert all(payload.get("sim_available") is False for payload in payloads)
