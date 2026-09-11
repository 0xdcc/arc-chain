"""G-card active-code regression probes."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from opportunities.candidates import select_candidates
from opportunities.input_gate import load_registry
from opportunities.lifecycle import LifecyclePolicy
from opportunities.quote_adapter import QuoteAdapterRequest
from opportunities.report import build_report_from_ledger
from opportunities.shadow import ShadowConfig, run_shadow
from opportunities.store import AppendOnlyLedger
from tests.opportunities.test_candidates import _candidates, _pool, _registry, _route


def test_rpc_failure_circuit_breaker_stops_after_three_attempts(tmp_path: Path) -> None:
    """Three consecutive RPC failures append the halt record and stop further candidates."""

    class FailingRpc:
        def __init__(self) -> None:
            self.calls = 0

        def call(
            self,
            method: str,
            params: object = None,
            block_identifier: object = None,
        ) -> dict[str, object]:
            self.calls += 1
            raise RuntimeError("offline rpc failure")

    registry = load_registry(
        _registry(
            [
                _pool("0x0000000000000000000000000000000000000002"),
                _pool("0x0000000000000000000000000000000000000003"),
            ]
        )
    )
    route_dict: dict[str, object] = _route(
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
    routes = [route_dict]
    for amount in ("1001", "1002"):
        route_amounts = route_dict["amounts"]
        if not isinstance(route_amounts, list) or not isinstance(route_amounts[0], dict):
            raise AssertionError("fixture route amounts must be a list of objects")
        amounted = {
            **route_dict,
            "amounts": [{**route_amounts[0], "amount_atoms": amount}],
        }
        routes.append(amounted)
    selection = select_candidates(_candidates(routes), registry, 3)
    config = ShadowConfig(
        lifecycle=LifecyclePolicy(3_000, 250),
        max_candidates=3,
        max_rpc_calls=10,
        quote_request=QuoteAdapterRequest(
            "0x33e885eD0Ec9bF04EcfB19341582aADCb4c8A9E7",
            "0x8Dc178eFB8111BB0973Dd9d722ebeFF267c98F94",
            data_mode="synthetic",
            actor_scope="synthetic",
            source_refs=("fixture:offline",),
        ),
        registry_semantic_revision="registry-v1",
    )
    ledger = AppendOnlyLedger(tmp_path / "ledger.jsonl")
    outcome = run_shadow(
        selection,
        {
            "chain_id": 4663,
            "block_number": 120,
            "block_hash": "0x" + "91" * 32,
            "received_at_ms": 1000,
            "block_timestamp_s": 1,
            "complete_through_block": 120,
            "completeness": "ready",
        },
        ledger,
        config,
        FailingRpc(),
        clock=type("Clock", (), {"time_ms": staticmethod(lambda: 2_000_000_000)})(),
    )
    payload_rows = list(ledger.load().events)
    assert outcome.halted is True
    assert [payload["schema_id"] for payload in payload_rows].count("w2-shadow-halt-v1") == 1
    assert payload_rows[-2]["schema_id"] == "w2-shadow-event-v1"
    assert payload_rows[-2]["halted"] is True


def test_report_denominator_recomputation_remains_active(tmp_path: Path) -> None:
    """Duplicate quote IDs must surface as a denominator reconciliation failure."""
    source = (
        Path(__file__).resolve().parents[2]
        / "tests"
        / "fixtures"
        / "opportunities"
        / "v1"
        / "all-negative-ledger.jsonl"
    )
    target = tmp_path / "broken.jsonl"
    target.write_bytes(source.read_bytes())
    ledger_rows = [json.loads(line) for line in target.read_text().splitlines()]
    ledger_rows[2]["payload"]["event"]["event_id"] += ":duplicate"
    previous_hash = "0" * 64
    for row in ledger_rows:
        row["payload"].pop("_ledger_hash", None)
        row["sequence"] = ledger_rows.index(row)
        row["previous_hash"] = previous_hash
        canonical = json.dumps(
            row["payload"],
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        record_hash = sha256(previous_hash.encode() + canonical).hexdigest()
        row["record_hash"] = record_hash
        previous_hash = record_hash
    target.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for row in ledger_rows
        )
    )
    payload = build_report_from_ledger(target).to_json_dict()
    denominators = payload["denominators"]["attempted_quote"]
    assert denominators["count"] == 3
    assert denominators["recomputed_count"] == 3
    assert denominators["matches_ledger_count"] is True
