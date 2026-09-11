"""Lifecycle reduction tests covering A15, A18, A19, A20, and A21."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opportunities.lifecycle import (
    LifecycleError,
    LifecycleEvent,
    LifecyclePolicy,
    ObservationMerger,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "opportunities" / "v1" / "synthetic-stream.jsonl"
POLICY = LifecyclePolicy(gap_limit_ms=3_000, target_delay_ms=250)


def _events() -> list[dict]:
    return [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines()]


def _merge_available(watermark: int = 10_000) -> tuple:
    merger = ObservationMerger(POLICY)
    events = sorted(
        (event for event in _events() if event["available_at_ms"] <= watermark),
        key=lambda event: event["available_at_ms"],
    )
    for event in events:
        merger.process(LifecycleEvent.from_mapping(event, event["observed_at_ms"]))
    return merger.finalize(), events


def test_route_and_amount_groups_do_not_double_count() -> None:
    result, events = _merge_available()
    assert result.duplicate_event_count == 1
    route_a = [episode for episode in result.episodes if episode.route_id == "route-a"]
    route_b = [episode for episode in result.episodes if episode.route_id == "route-b"]
    assert len(route_a) == 3
    assert len(route_b) == 1
    observations = sum(len(episode.observations) for episode in result.episodes)
    assert observations == len(events) - 1


def test_availability_barrier_hides_future_event() -> None:
    result, _ = _merge_available(1_200)
    assert all(episode.last_seen_at_ms <= 1_200 for episode in result.episodes)
    assert result.decision_watermark_ms == 1_100


def test_clock_fields_are_not_mixed() -> None:
    event = LifecycleEvent.from_mapping(
        {
            **_events()[0],
            "event_id": "clock-test",
            "source_record_id": "clock",
            "observed_at_ms": 5_000,
            "available_at_ms": 6_000,
            "block_time_s": 5,
            "monotonic_ns": 123,
        },
        99,
    )
    assert event.observed_at_ms == 5_000
    assert event.available_at_ms == 6_000
    assert event.block_time_s == 5
    assert event.monotonic_ns == 123


def test_failures_and_negative_observations_do_not_close_candidate() -> None:
    merger = ObservationMerger(POLICY)
    base = _events()[0]
    for index, outcome in enumerate(("profitable", "unprofitable", "quote_failed", "profitable")):
        event = dict(base)
        event.update(
            {
                "event_id": f"closed-{index}",
                "source_record_id": f"closed-{index}",
                "observed_at_ms": 1_000 + index * 100,
                "available_at_ms": 1_000 + index * 100,
                "result": outcome,
                "complete_scan": False,
                "candidate": True,
            }
        )
        merger.process(LifecycleEvent.from_mapping(event, index))
    result = merger.finalize()
    assert len(result.episodes) == 1
    assert result.episodes[0].positive_intervals == [[1000, 1100], [1300, 1300]]
    assert result.episodes[0].phase == "observed"
    assert set(result.episodes[0].rejection_reasons) == {"unprofitable", "quote_failed"}


def test_gap_creates_continuity_unknown_episode_with_parent() -> None:
    result, _ = _merge_available()
    assert len(result.episodes) == 4
    assert result.episodes[1].continuity_unknown is True
    assert result.episodes[0].disappeared_at_ms == 4_000
    assert result.episodes[3].first_seen_at_ms == 9_000


def test_truncated_and_empty_streams_are_censored_not_absent() -> None:
    merger = ObservationMerger(POLICY)
    assert merger.finalize().limitations == ("data_insufficient",)
    event = dict(_events()[0])
    event.update({"result": "no_opportunity", "is_truncated": True, "complete_scan": False})
    with pytest.raises(LifecycleError, match="truncated"):
        LifecycleEvent.from_mapping(event, 1)


def test_late_event_is_revision_evidence_not_a_past_rewrite() -> None:
    merger = ObservationMerger(POLICY)
    base = _events()[0]
    first = dict(base)
    second = dict(base)
    second.update({"event_id": "late", "source_record_id": "late", "observed_at_ms": 500})
    merger.process(LifecycleEvent.from_mapping(first, 1))
    merger.process(LifecycleEvent.from_mapping(second, 2))
    result = merger.finalize()
    assert result.late_revision_count == 1
    assert result.episodes[0].first_seen_at_ms == 1_000


def test_recheck_uses_actual_availability_and_missed_due() -> None:
    merger = ObservationMerger(POLICY)
    base = _events()[0]
    first = dict(base)
    first.update({"event_id": "recheck-first", "source_record_id": "recheck-first"})
    due_event = dict(base)
    due_event.update(
        {
            "event_id": "recheck-due",
            "source_record_id": "recheck-due",
            "observed_at_ms": 1_250,
            "available_at_ms": 1_400,
            "result": "profitable",
        }
    )
    merger.process(LifecycleEvent.from_mapping(first, 1))
    merger.process(LifecycleEvent.from_mapping(due_event, 2))
    result = merger.finalize()
    missed = [item for item in result.episodes[0].observations if item.get("missed_due")]
    assert result.missed_due_count == 1
    assert missed[0]["target_delay_ms"] == 250
    assert missed[0]["actual_elapsed_ms"] == 250


def test_true_duplicate_is_noop() -> None:
    merger = ObservationMerger(POLICY)
    events = _events()
    first = LifecycleEvent.from_mapping(events[0], 1)
    merger.process(first)
    merger.process(LifecycleEvent.from_mapping(events[0], 2))
    result = merger.finalize()
    assert result.duplicate_event_count == 1
    assert len(result.episodes[0].observations) == 1
