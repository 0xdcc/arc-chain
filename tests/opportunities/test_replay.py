"""Deterministic replay and no-future mutation tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from opportunities.lifecycle import LifecyclePolicy
from opportunities.replay import canonical_hash, replay_fixture

FIXTURE = Path(__file__).parents[1] / "fixtures" / "opportunities" / "v1" / "synthetic-stream.jsonl"
POLICY = LifecyclePolicy(gap_limit_ms=3_000, target_delay_ms=250)


def test_independent_replay_paths_have_identical_business_hash() -> None:
    first = replay_fixture(FIXTURE, POLICY, 10_000)
    second = replay_fixture(FIXTURE, POLICY, 10_000)
    assert canonical_hash(first[0]) == canonical_hash(second[0])
    assert first[1] == second[1]


def test_independent_processes_have_identical_business_hash(tmp_path: Path) -> None:
    child = tmp_path / "replay_child.py"
    child.write_text(
        (Path(__file__).parent / "test_replay_child.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    outputs = []
    for replay_speed in ("slow", "fast"):
        process = subprocess.run(
            [sys.executable, str(child), str(FIXTURE), "10000"],
            cwd=Path(__file__).parents[2],
            env={
                "PATH": "/usr/bin:/bin",
                "PYTHONPATH": str(Path(__file__).parents[2]),
                "REPLAY_SPEED": replay_speed,
            },
            capture_output=True,
            text=True,
            check=True,
        )
        outputs.append(json.loads(process.stdout))
    assert outputs[0]["hash"] == outputs[1]["hash"]


def test_watermark_excludes_future_and_delayed_records() -> None:
    early, _ = replay_fixture(FIXTURE, POLICY, 1_200)
    full, _ = replay_fixture(FIXTURE, POLICY, 10_000)
    assert early.decision_watermark_ms == 1_100
    assert len(early.episodes) < len(full.episodes)


def test_removing_availability_barrier_changes_result() -> None:
    without_barrier = canonical_hash(replay_fixture(FIXTURE, POLICY, -1)[0])
    with_barrier = canonical_hash(replay_fixture(FIXTURE, POLICY, 1_200)[0])
    assert without_barrier != with_barrier
