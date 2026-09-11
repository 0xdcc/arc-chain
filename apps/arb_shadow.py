"""Thin offline replay CLI for the W2 shadow pipeline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from opportunities.candidates import DeterministicOfflineTransport, select_candidates
from opportunities.input_gate import load_registry
from opportunities.lifecycle import LifecyclePolicy
from opportunities.quote_adapter import QuoteAdapterRequest
from opportunities.shadow import ShadowConfig, ShadowInputError, run_shadow
from opportunities.store import AppendOnlyLedger, LedgerError


def _required(raw: dict[str, object], field_name: str) -> object:
    value = raw.get(field_name)
    if value is None:
        raise ShadowInputError(f"manifest.{field_name} is required")
    return value


def _integer(raw: dict[str, object], field_name: str, minimum: int = 0) -> int:
    value = _required(raw, field_name)
    if type(value) is not int or isinstance(value, bool) or value < minimum:
        raise ShadowInputError(f"manifest.{field_name} must be an integer >= {minimum}")
    return int(value)


def _run_pipeline(raw: dict[str, object], output_root: Path) -> None:
    if raw.get("schema_id") != "w2-shadow-manifest-v1":
        raise ShadowInputError("input manifest schema_id must be w2-shadow-manifest-v1")
    if raw.get("mode") != "replay":
        raise ShadowInputError("input manifest mode must be replay")

    registry_path = Path(str(_required(raw, "registry_path")))
    candidates_path = Path(str(_required(raw, "candidates_path")))
    state = _required(raw, "state")
    policy_raw = _required(raw, "policy")
    quote_raw = _required(raw, "quote_request")
    if not isinstance(state, dict) or not isinstance(policy_raw, dict):
        raise ShadowInputError("manifest.state and manifest.policy must be objects")
    if not isinstance(quote_raw, dict):
        raise ShadowInputError("manifest.quote_request must be an object")
    source_refs = quote_raw.get("source_refs", ())
    if not isinstance(source_refs, list) or not all(type(ref) is str for ref in source_refs):
        raise ShadowInputError("manifest.quote_request.source_refs must be a string array")
    if not registry_path.is_file() or not candidates_path.is_file():
        raise ShadowInputError("manifest registry and candidates must be readable files")

    registry = load_registry(json.loads(registry_path.read_text(encoding="utf-8")))
    candidates = select_candidates(
        json.loads(candidates_path.read_text(encoding="utf-8")),
        registry,
        _integer(raw, "max_candidates"),
    )
    policy = LifecyclePolicy(
        gap_limit_ms=_integer(policy_raw, "gap_limit_ms", 1),
        target_delay_ms=_integer(policy_raw, "target_delay_ms"),
    )
    config = ShadowConfig(
        lifecycle=policy,
        max_candidates=_integer(raw, "max_candidates"),
        max_rpc_calls=_integer(raw, "max_rpc_calls"),
        quote_request=QuoteAdapterRequest(
            quoter_v3=str(_required(quote_raw, "quoter_v3")),
            quoter_v4=str(_required(quote_raw, "quoter_v4")),
            data_mode=str(quote_raw.get("data_mode", "synthetic")),
            actor_scope=str(quote_raw.get("actor_scope", "synthetic")),
            source_refs=tuple(source_refs),
            run_baseline_ref=(
                str(quote_raw["run_baseline_ref"]) if "run_baseline_ref" in quote_raw else None
            ),
        ),
        registry_semantic_revision=str(_required(raw, "registry_semantic_revision")),
        rpc_gas_price_atoms=_integer(quote_raw, "rpc_gas_price_atoms"),
        rpc_gas_l1_fee_atoms=_integer(quote_raw, "rpc_gas_l1_fee_atoms"),
    )
    output_root.mkdir(parents=True, exist_ok=False)
    rpc = DeterministicOfflineTransport(quote_raw, Path.cwd())
    ledger = AppendOnlyLedger(output_root / "ledger.jsonl")
    outcome = run_shadow(candidates, state, ledger, config, rpc)
    outcome_payload = {
        "candidate_count": outcome.candidate_count,
        "quoted_count": outcome.quoted_count,
        "ledger_sequence": outcome.ledger_sequence,
        "truncated": outcome.truncated,
        "halted": outcome.halted,
        "sim_available": outcome.sim_available,
        "decision_watermark_ms": outcome.decision_watermark_ms,
    }
    (output_root / "outcome.json").write_text(
        json.dumps(outcome_payload, sort_keys=True) + "\n", encoding="utf-8"
    )


def main() -> int:
    """Run the restricted file-only shadow replay pipeline."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("replay",), required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        raw = json.loads(args.input.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema_id") != "w2-shadow-manifest-v1":
            raise ValueError("input manifest schema_id must be w2-shadow-manifest-v1")
        _run_pipeline(raw, args.output_root)
        return 0
    except (LedgerError, OSError, ShadowInputError, ValueError, json.JSONDecodeError) as error:
        print(str(error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
