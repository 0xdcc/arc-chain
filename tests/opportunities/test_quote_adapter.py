"""Regression tests for fixed-block quotes and strict historical replay."""

from __future__ import annotations

import hashlib
import importlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from eth_abi import encode as abi_encode

from arbitrage_contracts.identity import Amount, AssetRef, PoolDescriptor, PoolKey, TokenKey
from arbitrage_contracts.quote import (
    FeeModel,
    GasEvidenceKind,
    HopRef,
    QuoteStatus,
    RouteRef,
    TriState,
)
from arbitrage_contracts.state import StateVersion
from opportunities.quote_adapter import (
    FixedBlockQuoteAdapter,
    QuoteAdapterInputError,
    QuoteAdapterRequest,
)

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "opportunities"
    / "v1"
    / "historical-rpc.jsonl"
)
EXPECTED_FIXTURE_SHA256 = "c270bb81ae457a211ffe0dc422987678d1c7286ff015f612a1d4672fba9e1239"
CHAIN_ID = 4663
BLOCK = 0x378A957
V3_QUOTER = "0x33e885eD0Ec9bF04EcfB19341582aADCb4c8A9E7"
V4_QUOTER = "0x8Dc178eFB8111BB0973Dd9d722ebeFF267c98F94"


def _token(address: str) -> AssetRef:
    return AssetRef.erc20(TokenKey(CHAIN_ID, address))


def _pool(
    protocol: str,
    venue: str,
    pool_id: str,
    currency0: AssetRef,
    currency1: AssetRef,
    *,
    fee: int,
    tick_spacing: int | None = None,
    hooks: str | None = None,
) -> PoolDescriptor:
    pool_key = PoolKey(
        CHAIN_ID,
        protocol,
        "factory" if protocol == "uniswap_v3" else "manager",
        venue,
        "address" if protocol == "uniswap_v3" else "bytes32",
        pool_id,
    )
    return PoolDescriptor(
        pool_key,
        currency0,
        currency1,
        FeeModel.static(fee),
        tick_spacing=tick_spacing,
        hooks=hooks,
        identity_evidence_refs=("test:pool-mapping",),
    )


def _route() -> tuple[RouteRef, tuple[AssetRef, ...]]:
    base = _token("0x0bd7d308f8e1639fab988df18a8011f41eacad73")
    middle_one = _token("0x020bfc650a365f8bb26819deaabf3e21291018b4")
    middle_two = _token("0x39dbed3a2bd333467115de45665cc57f813c4571")
    v3_pool = _pool(
        "uniswap_v3",
        "0xd42A491087a15E5afd51FEb3606066Cc152d2b09",
        "0xd42A491087a15E5afd51FEb3606066Cc152d2b09",
        base,
        middle_one,
        fee=3000,
    )
    v4_pool = _pool(
        "uniswap_v4",
        "0x0000000000000000000000000000000000000001",
        "0x3ee7a201318a8b5dfeafdb8a6d1e80d8a1909858d9d2ce10c79ea3b315731f32",
        middle_one,
        middle_two,
        fee=1250,
        tick_spacing=13,
        hooks="0x0000000000000000000000000000000000000000",
    )
    final_pool = _pool(
        "uniswap_v3",
        "0xEd50bDeeA8aDC232f159486192a4157281D722ff",
        "0xEd50bDeeA8aDC232f159486192a4157281D722ff",
        base,
        middle_two,
        fee=3000,
    )
    hops = [
        HopRef(v3_pool.key, base, middle_one, direction="zero_for_one", pool_descriptor=v3_pool),
        HopRef(
            v4_pool.key,
            middle_one,
            middle_two,
            direction="zero_for_one",
            pool_descriptor=v4_pool,
        ),
        HopRef(
            final_pool.key,
            middle_two,
            base,
            direction="one_for_zero",
            pool_descriptor=final_pool,
        ),
    ]
    return RouteRef(CHAIN_ID, base, hops), (base, middle_one, middle_two)


def _state(block: int = BLOCK, *, ready: bool = True) -> StateVersion:
    return StateVersion(
        CHAIN_ID,
        block,
        "0x" + "91" * 32,
        1000,
        completeness="ready" if ready else "syncing",
        complete_through_block=block if ready else None,
    )


def _request(**overrides: Any) -> QuoteAdapterRequest:
    return QuoteAdapterRequest(
        quoter_v3=V3_QUOTER,
        quoter_v4=V4_QUOTER,
        data_mode="historical_replay",
        source_refs=("historical-rpc",),
        **overrides,
    )


def _quote_output(hop_index: int) -> str:
    outputs = [10**21, 7 * 10**17, 10**15]
    amount = outputs[hop_index]
    if hop_index % 2 == 0:
        encoded = abi_encode(
            ["uint256", "uint160", "uint32", "uint256"],
            [amount, 1, 0, 70_000],
        )
    else:
        encoded = abi_encode(["uint256", "uint256"], [amount, 50_000])
    return "0x" + encoded.hex()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _records() -> list[dict[str, Any]]:
    return [json.loads(line) for line in FIXTURE_PATH.read_text().splitlines()]


def _record_calldata(index: int) -> str:
    record = _records()[index]
    assert record["method"] == "eth_call"
    return record["params"][0]["data"]


def _historical_call(index: int) -> tuple[str, str]:
    record = _records()[index]
    assert record["method"] == "eth_call"
    return record["params"][0]["to"], record["params"][0]["data"]


def _historical_results_from_subprocess() -> tuple[int, int, list[str]]:
    script = """
import json, sys, types
package = types.ModuleType("arbitrage")
package.__path__ = ["arbitrage"]
sys.modules["arbitrage"] = package
from arbitrage.quoting.replay_adapter import ReplayAdapter
from arbitrage.quoting.quoter import Quoter, run_historical_replay
records = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8")]
adapter = ReplayAdapter(records)
results = run_historical_replay(adapter, Quoter(rpc_client=adapter))
adapter.verify_complete()
print(adapter.consumed_count(), adapter.total_count(), len(results))
print(",".join(result.status.value for result in results))
"""
    process = subprocess.run(
        [sys.executable, "-c", script, str(FIXTURE_PATH)],
        capture_output=True,
        text=True,
        check=False,
    )
    if process.returncode != 0:
        pytest.fail(f"historical replay subprocess failed:\n{process.stdout}\n{process.stderr}")
    counts, categories = process.stdout.splitlines()
    consumed, total, result_count = (int(value) for value in counts.split())
    return consumed, result_count, categories.split(",")


def _tampered_replay_exit(mode: str) -> subprocess.CompletedProcess[str]:
    replay_type, quoter_type, runner = _load_quoting_modules()
    records = _records()
    if mode == "calldata":
        records[14]["params"][0]["data"] = "0xdeadbeef"
    else:
        records[14], records[15] = records[15], records[14]
    adapter = replay_type(records)
    process = subprocess.CompletedProcess(args=[mode], returncode=0, stdout="", stderr="")
    try:
        runner(adapter, quoter_type(rpc_client=adapter))
    except RuntimeError as exc:
        process.returncode = 1
        process.stderr = str(exc)
    return process


def _historical_results_in_process() -> tuple[int, int, list[str]]:
    replay_type, quoter_type, runner = _load_quoting_modules()
    adapter = replay_type(_records())
    results = runner(adapter, quoter_type(rpc_client=adapter))
    adapter.verify_complete()
    return adapter.consumed_count(), len(results), [result.status.value for result in results]


def _historical_error_attribution() -> list[dict[str, Any]]:
    package = type(sys)("arbitrage")
    package.__path__ = ["arbitrage"]
    sys.modules["arbitrage"] = package
    quoting = type(sys)("arbitrage.quoting")
    quoting.__path__ = [str(Path(__file__).resolve().parents[2] / "arbitrage" / "quoting")]
    sys.modules["arbitrage.quoting"] = quoting
    domain = type(sys)("arbitrage.domain")
    domain.__path__ = [str(Path(__file__).resolve().parents[2] / "arbitrage" / "domain")]
    sys.modules["arbitrage.domain"] = domain
    quoter_module = importlib.import_module("arbitrage.quoting.quoter")
    attributed = []
    for record in _records():
        response = record.get("response", {})
        if "error" not in response:
            continue
        diagnostic = quoter_module.classify_rpc_error(response)
        attributed.append(
            {
                "record_index": _records().index(record),
                "category": diagnostic["category"].value,
                "nested_selector": diagnostic["nested_selector"],
                "revert_reason": diagnostic["revert_reason"],
                "pool_id": diagnostic["pool_id"],
                "code": diagnostic["code"],
            }
        )
    return attributed


def _load_quoting_modules() -> tuple[type[Any], Any, Any]:
    package = type(sys)("arbitrage")
    package.__path__ = ["arbitrage"]
    sys.modules["arbitrage"] = package
    quoting = type(sys)("arbitrage.quoting")
    quoting.__path__ = [str(Path(__file__).resolve().parents[2] / "arbitrage" / "quoting")]
    sys.modules["arbitrage.quoting"] = quoting
    domain = type(sys)("arbitrage.domain")
    domain.__path__ = [str(Path(__file__).resolve().parents[2] / "arbitrage" / "domain")]
    sys.modules["arbitrage.domain"] = domain
    replay_module = importlib.import_module("arbitrage.quoting.replay_adapter")
    quoter_module = importlib.import_module("arbitrage.quoting.quoter")
    return replay_module.ReplayAdapter, quoter_module.Quoter, quoter_module.run_historical_replay


class _SpyRpc:
    """Transport spy that serves quote responses without network access."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def call(
        self,
        method: str,
        params: list[Any] | tuple[Any, ...] | None = None,
        block_identifier: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {"method": method, "params": params, "block_identifier": block_identifier}
        )
        return {"result": _quote_output(len(self.calls) - 1)}


class _FailingRpc:
    """Transport stub returning one raw JSON-RPC error response."""

    def __init__(self, response: dict[str, Any], error: Exception | None = None) -> None:
        self.response = response
        self.error = error

    def call(
        self,
        method: str,
        params: list[Any] | tuple[Any, ...] | None = None,
        block_identifier: str | None = None,
    ) -> dict[str, Any]:
        if self.error is not None:
            raise self.error
        return self.response


class TestFixedStateAndPoolMapping:
    """A05 and A06: every call is pinned and pool identity is fail-closed."""

    def test_transport_receives_block_number_on_every_hop(self) -> None:
        route, assets = _route()
        rpc = _SpyRpc()
        result = FixedBlockQuoteAdapter(rpc, _request()).quote_route(
            route, Amount(assets[0], 10**15, 18), _state()
        )
        assert result.evidence.status == QuoteStatus.QUOTED
        assert result.evidence.error is None
        assert len(rpc.calls) == 3
        assert [call["block_identifier"] for call in rpc.calls] == [hex(BLOCK)] * 3
        assert all(call["params"][1] == hex(BLOCK) for call in rpc.calls)

    def test_non_ready_state_is_rejected_before_rpc(self) -> None:
        route, assets = _route()
        rpc = _SpyRpc()
        with pytest.raises(QuoteAdapterInputError, match="ready"):
            FixedBlockQuoteAdapter(rpc, _request()).quote_route(
                route, Amount(assets[0], 10**15, 18), _state(ready=False)
            )
        assert rpc.calls == []

    def test_incomplete_v4_pool_mapping_is_unsupported(self) -> None:
        route, assets = _route()
        bad_descriptor = _pool(
            "uniswap_v4",
            "0x0000000000000000000000000000000000000001",
            "0x3ee7a201318a8b5dfeafdb8a6d1e80d8a1909858d9d2ce10c79ea3b315731f32",
            assets[1],
            assets[2],
            fee=1250,
        )
        bad_hop = HopRef(bad_descriptor.key, assets[1], assets[2], direction="zero_for_one")
        bad_route = RouteRef(CHAIN_ID, assets[0], [route.hops[0], bad_hop, route.hops[2]])
        result = FixedBlockQuoteAdapter(_SpyRpc(), _request()).quote_route(
            bad_route, Amount(assets[0], 10**15, 18), _state()
        )
        assert result.evidence.status == QuoteStatus.UNSUPPORTED
        assert result.evidence.amount_out is None
        assert result.evidence.delta_atoms is None


class TestChainingAndFailures:
    """A07 and A08: integer chaining and precise failure behavior."""

    def test_integer_hops_chain_exact_amounts(self) -> None:
        route, assets = _route()
        result = FixedBlockQuoteAdapter(_SpyRpc(), _request()).quote_route(
            route, Amount(assets[0], 10**15, 18), _state()
        )
        assert [quote.amount_in.atoms for quote in result.evidence.hop_quotes] == [
            10**15,
            10**21,
            7 * 10**17,
        ]
        assert result.evidence.amount_out is not None
        assert result.evidence.amount_out.asset_ref == route.base_asset
        assert result.evidence.delta_atoms == 10**15 - 10**15

    def test_middle_failure_keeps_successful_prefix(self) -> None:
        class _MiddleRevert(_SpyRpc):
            def call(
                self, method: str, params: Any = None, block_identifier: Any = None
            ) -> dict[str, Any]:
                if len(self.calls) == 1:
                    return {
                        "error": {
                            "code": 3,
                            "message": "execution reverted",
                            "data": "0x6190b2b0" + "00" * 96,
                        }
                    }
                return super().call(method, params, block_identifier)

        route, assets = _route()
        result = FixedBlockQuoteAdapter(_MiddleRevert(), _request()).quote_route(
            route, Amount(assets[0], 10**15, 18), _state()
        )
        assert result.evidence.status == QuoteStatus.CONTRACT_REVERT
        assert len(result.evidence.hop_quotes) == 1
        assert result.evidence.amount_out is None
        assert result.evidence.delta_atoms is None

    def test_rpc_errors_are_precisely_classified(self) -> None:
        route, assets = _route()
        adapter = FixedBlockQuoteAdapter(
            _FailingRpc({"error": {"code": -32602, "message": "Archive requests"}}), _request()
        )
        result = adapter.quote_route(route, Amount(assets[0], 10**15, 18), _state())
        assert result.evidence.status == QuoteStatus.NODE_LIMITATION

        adapter = FixedBlockQuoteAdapter(_FailingRpc({}, TimeoutError("timeout")), _request())
        result = adapter.quote_route(route, Amount(assets[0], 10**15, 18), _state())
        assert result.evidence.status == QuoteStatus.RPC_ERROR


class TestHistoricalReplay:
    """A09: original transport records drive exactly thirty strict calls."""

    def test_original_fixture_hash_and_record_count(self) -> None:
        assert _sha256(FIXTURE_PATH) == EXPECTED_FIXTURE_SHA256
        assert len(_records()) == 30

    def test_historical_errors_are_attributed_without_message_matching(self) -> None:
        attributed = _historical_error_attribution()
        assert len(attributed) == 8
        assert sum(item["category"] == "CONTRACT_REVERT" for item in attributed) == 7
        assert sum(item["category"] == "NODE_LIMITATION" for item in attributed) == 1
        assert all(item["revert_reason"] == "NotEnoughLiquidity" for item in attributed[:7])
        assert all(item["nested_selector"] == "0x7a5ed734" for item in attributed[:7])
        assert all(item["pool_id"] is not None for item in attributed[:7])
        assert attributed[-1]["code"] == -32602

    def test_legacy_thirty_record_replay_is_complete_and_attributed(self) -> None:
        consumed, result_count, categories = _historical_results_in_process()
        assert consumed == 30
        assert result_count == 8
        assert categories.count("CONTRACT_REVERT") == 7
        assert categories.count("NODE_LIMITATION") == 1

    def test_tampered_calldata_and_order_fail_closed(self) -> None:
        for mode in ("calldata", "order"):
            process = _tampered_replay_exit(mode)
            assert process.returncode != 0
            assert "Replay" in (process.stdout + process.stderr)

    def test_same_key_hits_cache_and_any_component_misses(self) -> None:
        route, assets = _route()
        rpc = _SpyRpc()
        adapter = FixedBlockQuoteAdapter(rpc, _request())
        first = adapter.quote_route(route, Amount(assets[0], 10**15, 18), _state())
        second = adapter.quote_route(route, Amount(assets[0], 10**15, 18), _state())
        assert first.cache_hit is False
        assert second.cache_hit is True
        assert second.evidence is first.evidence
        assert len(rpc.calls) == 3

        other_amount = adapter.quote_route(route, Amount(assets[0], 2 * 10**15, 18), _state())
        other_state = adapter.quote_route(route, Amount(assets[0], 10**15, 18), _state(BLOCK + 1))
        assert other_amount.cache_hit is False
        assert other_state.cache_hit is False

    def test_evidence_fields_are_rigid(self) -> None:
        route, assets = _route()
        result = FixedBlockQuoteAdapter(_SpyRpc(), _request()).quote_route(
            route, Amount(assets[0], 10**15, 18), _state()
        )
        evidence = result.evidence
        assert evidence.evidence_level == "rpc_quote"
        assert evidence.fee_included == TriState.YES
        assert evidence.impact_included == TriState.YES
        assert evidence.gas_evidence is not None
        assert evidence.gas_evidence.gas_kind == GasEvidenceKind.QUOTER_ESTIMATE
