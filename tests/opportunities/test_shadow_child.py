"""Child process helper for independent W2 shadow verification."""

from __future__ import annotations

import argparse
import dataclasses
import json
from pathlib import Path
from typing import Any

from arbitrage_contracts.quote import QuoteEvidence
from opportunities.candidates import DeterministicOfflineTransport, select_candidates
from opportunities.input_gate import load_registry
from opportunities.lifecycle import LifecyclePolicy
from opportunities.quote_adapter import QuoteAdapterRequest
from opportunities.shadow import ShadowConfig, _result_for_quote, run_shadow
from opportunities.store import AppendOnlyLedger


def _decode_path(root: Path, value: Any, field_name: str) -> Path:
    if type(value) is not str:
        raise ValueError(f"{field_name} must be a path string")
    path = Path(value)
    if path.is_absolute():
        try:
            path.relative_to(root.resolve())
        except ValueError as error:
            raise ValueError(f"{field_name} escapes source root") from error
    else:
        path = root / path
    return path.resolve()


def _minimum_quote(status: str) -> QuoteEvidence:
    quote = QuoteEvidence.__new__(QuoteEvidence)
    object.__setattr__(quote, "status", status)
    return quote


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args()
    root = Path.cwd()
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    registry = load_registry(
        json.loads(
            _decode_path(root, manifest["registry_path"], "registry").read_text(encoding="utf-8")
        )
    )
    candidates = select_candidates(
        json.loads(
            _decode_path(root, manifest["candidates_path"], "candidates").read_text(
                encoding="utf-8"
            )
        ),
        registry,
        manifest["max_candidates"],
    )
    request = QuoteAdapterRequest(
        quoter_v3=manifest["quote_request"]["quoter_v3"],
        quoter_v4=manifest["quote_request"]["quoter_v4"],
        data_mode=manifest["quote_request"].get("data_mode", "synthetic"),
        actor_scope=manifest["quote_request"].get("actor_scope", "synthetic"),
        source_refs=tuple(manifest["quote_request"].get("source_refs", ())),
    )
    config = ShadowConfig(
        lifecycle=LifecyclePolicy(
            manifest["policy"]["gap_limit_ms"], manifest["policy"]["target_delay_ms"]
        ),
        max_candidates=manifest["max_candidates"],
        max_rpc_calls=manifest["max_rpc_calls"],
        quote_request=request,
        registry_semantic_revision=manifest["registry_semantic_revision"],
    )
    output_root = Path(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    ledger = AppendOnlyLedger(output_root / "ledger.jsonl")
    rpc = DeterministicOfflineTransport(manifest["quote_request"], root)
    outcome = run_shadow(candidates, manifest["state"], ledger, config, rpc)
    print(json.dumps(dataclasses.asdict(outcome), sort_keys=True))


def _smoke_contract_status() -> str:
    return _result_for_quote(_minimum_quote("contract_revert"))


if __name__ == "__main__":
    main()
