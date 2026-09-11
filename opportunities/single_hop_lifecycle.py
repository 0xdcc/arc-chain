"""Bidirectional single-hop quote curves and pool capability promotion."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from arbitrage_contracts.eligibility import PoolCapability
from arbitrage_contracts.identity import Amount, PoolDescriptor, PoolKey
from arbitrage_contracts.state import StateVersion
from opportunities.store import AppendOnlyLedger, LedgerError


class LifecycleInputError(ValueError):
    """Raised when lifecycle inputs cannot form a safe quote curve."""


class _QuotePort(Protocol):
    """Minimal adapter port consumed by the lifecycle engine."""

    def quote(
        self,
        descriptor: PoolDescriptor,
        direction: str,
        amount_in: Amount,
        state: StateVersion,
    ) -> Any:
        """Issue one independent direction quote."""


@dataclass(frozen=True, slots=True)
class SingleHopCurvePoint:
    """One direction at one amount tier, retaining raw evidence without profit math."""

    pool_id: str
    direction: str
    amount_atoms: int
    status: str
    quote_id: str | None
    amount_out_atoms: int | None
    error: str | None


@dataclass(frozen=True, slots=True)
class SingleHopCurveResult:
    """Output rows and final quote capability for one pool."""

    points: tuple[SingleHopCurvePoint, ...]
    capability: PoolCapability


def _canonical_row(row: dict[str, Any]) -> str:
    return json.dumps(
        row,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    if path.exists():
        raise LifecycleInputError(f"quote curve output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(_canonical_row(row) + "\n" for row in rows).encode("utf-8")
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()


def run_single_hop_lifecycle(
    descriptors: dict[PoolKey, PoolDescriptor],
    tiers: tuple[int, ...],
    state: StateVersion,
    adapter: _QuotePort,
    output_root: Path,
    *,
    ledger: AppendOnlyLedger | None = None,
) -> dict[PoolKey, SingleHopCurveResult]:
    """Quote every eligible pool in both directions and promote only complete curves."""
    if not tiers or any(
        type(value) is not int or isinstance(value, bool) or value <= 0 for value in tiers
    ):
        raise LifecycleInputError("tiers must be positive integer atom amounts")
    if not descriptors:
        raise LifecycleInputError("no eligible pools were supplied")
    if state.completeness != "ready" or state.block_number <= 0:
        raise LifecycleInputError("state must be ready with a positive block number")
    if output_root.is_symlink() or not output_root.is_dir():
        raise LifecycleInputError("output root must be a real directory")

    results: dict[PoolKey, SingleHopCurveResult] = {}
    for pool_key, descriptor in sorted(
        descriptors.items(),
        key=lambda item: (
            item[0].chain_id,
            item[0].protocol_id,
            item[0].canonical_venue_address,
            item[0].canonical_pool_id,
        ),
    ):
        if descriptor.key != pool_key:
            raise LifecycleInputError("descriptor key does not match catalog key")
        points: list[SingleHopCurvePoint] = []
        evidence_ids: list[str] = []
        failures: list[str] = []
        rows: list[dict[str, Any]] = []
        for amount_atoms in tiers:
            for direction in ("zero_for_one", "one_for_zero"):
                asset_in = (
                    descriptor.currency0 if direction == "zero_for_one" else descriptor.currency1
                )
                amount = Amount(asset_in, amount_atoms, 18)
                result = adapter.quote(descriptor, direction, amount, state)
                evidence = result.evidence
                if evidence.status == "quoted":
                    evidence_ids.append(evidence.quote_id)
                    point = SingleHopCurvePoint(
                        descriptor.key.canonical_pool_id,
                        direction,
                        amount_atoms,
                        evidence.status,
                        evidence.quote_id,
                        evidence.amount_out.atoms if evidence.amount_out is not None else None,
                        None,
                    )
                else:
                    point = SingleHopCurvePoint(
                        descriptor.key.canonical_pool_id,
                        direction,
                        amount_atoms,
                        evidence.status,
                        None,
                        None,
                        evidence.error or evidence.status,
                    )
                    failures.append(f"{direction}:{amount_atoms}:{point.error}")
                points.append(point)
                rows.append(
                    {
                        "amount_in_atoms": str(amount_atoms),
                        "amount_out_atoms": (
                            str(point.amount_out_atoms)
                            if point.amount_out_atoms is not None
                            else None
                        ),
                        "data_mode": evidence.data_mode,
                        "direction": direction,
                        "error": point.error,
                        "evidence_level": evidence.evidence_level,
                        "pool_id": point.pool_id,
                        "quote_id": point.quote_id,
                        "state_version_ref": evidence.state_version_ref,
                        "status": evidence.status,
                    }
                )
        can_quote = "supported" if len(evidence_ids) == len(tiers) * 2 else "unsupported"
        capability = PoolCapability(
            pool_key,
            can_quote=can_quote,
            can_simulate="unknown",
            can_atomic_execute="unsupported",
            evidence_refs=tuple(evidence_ids),
            reasons=tuple(dict.fromkeys(failures)),
        )
        pool_name = f"{pool_key.chain_id}:{pool_key.canonical_pool_id}"
        safe_pool_name = pool_name.replace(":", "_")
        _write_jsonl(output_root / safe_pool_name / "quote_curves.jsonl", rows)
        if ledger is not None:
            try:
                for row in rows:
                    if row["status"] != "quoted":
                        continue
                    ledger.append({"kind": "single_hop_quote_event", "payload": row})
            except (LedgerError, TypeError, ValueError) as exc:
                raise LifecycleInputError(f"quote event could not be persisted: {exc}") from exc
        results[pool_key] = SingleHopCurveResult(tuple(points), capability)
    return results
