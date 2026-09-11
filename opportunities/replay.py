"""Deterministic offline replay over the append-only W2 ledger."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from opportunities.lifecycle import (
    LifecycleEvent,
    LifecyclePolicy,
    LifecycleResult,
    ObservationMerger,
)
from opportunities.store import AppendOnlyLedger, LedgerError


class ReplayError(Exception):
    """Raised when replay input cannot be trusted."""


def load_events(path: Path) -> list[dict[str, Any]]:
    """Load fixture events in source order without interpreting data as truth."""
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ReplayError(str(error)) from error
    events: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(raw_lines, start=1):
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError as error:
            raise ReplayError(f"invalid fixture JSON on line {line_number}") from error
        if not isinstance(event, dict):
            raise ReplayError(f"fixture line {line_number} must be an object")
        event["_fixture_line"] = line_number
        events.append(event)
    return events


def _filter_available(
    events: list[dict[str, Any]], watermark_ms: int
) -> list[tuple[int, dict[str, Any]]]:
    available: list[tuple[int, dict[str, Any]]] = []
    for sequence, event in enumerate(events):
        barrier = event.get("available_at_ms")
        if type(barrier) is not int:
            raise ReplayError("available_at_ms is required for every event")
        if barrier <= watermark_ms:
            available.append((sequence, event))
    available.sort(key=lambda item: (item[1]["available_at_ms"], item[0]))
    return available


def replay_fixture(
    fixture_path: Path,
    policy: LifecyclePolicy,
    watermark_ms: int,
) -> tuple[LifecycleResult, dict[str, Any]]:
    """Replay a fixture at one explicit availability watermark."""
    events = load_events(fixture_path)
    merger = ObservationMerger(policy)
    for _, event in _filter_available(events, watermark_ms):
        merger.process(LifecycleEvent.from_mapping(event, int(event["_fixture_line"])))
    return merger.finalize(), {"fixture_line_count": len(events), "watermark_ms": watermark_ms}


def replay_ledger(
    ledger_path: Path, policy: LifecyclePolicy
) -> tuple[LifecycleResult, dict[str, Any]]:
    """Replay only hash-chain-verified ledger events."""
    try:
        ledger = AppendOnlyLedger(ledger_path)
        snapshot = ledger.load()
    except (LedgerError, OSError) as error:
        raise ReplayError(str(error)) from error
    merger = ObservationMerger(policy)
    for sequence, payload in enumerate(snapshot.events):
        merger.process(LifecycleEvent.from_mapping(payload, sequence))
    return merger.finalize(), {"ledger_event_count": len(snapshot.events)}


def canonical_hash(result: LifecycleResult) -> str:
    """Hash only business-visible lifecycle output; excludes process timing and paths."""
    episodes: list[dict[str, Any]] = []
    for episode in result.episodes:
        episodes.append(
            {
                "amount_atoms": episode.amount_atoms,
                "continuity_unknown": episode.continuity_unknown,
                "disappeared_at_ms": episode.disappeared_at_ms,
                "first_seen_at_ms": episode.first_seen_at_ms,
                "last_rechecked_at_ms": episode.last_rechecked_at_ms,
                "last_seen_at_ms": episode.last_seen_at_ms,
                "observations": episode.observations,
                "opportunity_id": episode.opportunity_id,
                "parent_opportunity_id": episode.parent_opportunity_id,
                "phase": episode.phase,
                "positive_intervals": episode.positive_intervals,
                "rejection_reasons": episode.rejection_reasons,
                "right_censored": episode.right_censored,
                "route_id": episode.route_id,
                "registry_revision": episode.registry_revision,
            }
        )
    payload = {
        "duplicate_event_count": result.duplicate_event_count,
        "episodes": episodes,
        "late_revision_count": result.late_revision_count,
        "limitations": list(result.limitations),
        "missed_due_count": result.missed_due_count,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()
