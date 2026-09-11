"""Disk-focused regression tests for the append-only ledger."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from opportunities.store import (
    AppendOnlyLedger,
    LedgerCorruptionError,
    LedgerPathError,
    LedgerWriteError,
)

MODULE_CODE = """
from pathlib import Path
import sys
from opportunities.store import AppendOnlyLedger
path = Path(sys.argv[1])
ledger = AppendOnlyLedger(path)
ledger.append({"value": int(sys.argv[2])})
print(ledger.confirmed_sequence)
"""

REPO_ROOT = Path(__file__).parents[2]


def _write_module(tmp_path: Path) -> Path:
    module = tmp_path / "ledger_child.py"
    module.write_text(MODULE_CODE, encoding="utf-8")
    return module


def test_append_is_idempotent_and_survives_checkpoint_loss(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    first = AppendOnlyLedger(path)
    assert first.append({"event_id": "one"}) == 0
    second = AppendOnlyLedger(path)
    assert second.append({"event_id": "two"}) == 1
    checkpoint = Path(str(path) + ".checkpoint")
    checkpoint.unlink()
    reopened = AppendOnlyLedger(path)
    snapshot = reopened.load()
    assert reopened.confirmed_sequence == 1
    assert [event["event_id"] for event in snapshot.events] == ["one", "two"]
    assert reopened.append({"event_id": "three"}) == 2
    assert [event["event_id"] for event in reopened.load().events] == ["one", "two", "three"]


def test_real_subprocess_restart_matches_uninterrupted_bytes(tmp_path: Path) -> None:
    module = _write_module(tmp_path)
    interrupted = tmp_path / "interrupted.jsonl"
    uninterrupted = tmp_path / "uninterrupted.jsonl"
    real_run = subprocess.run
    real_run(
        [sys.executable, str(module), str(interrupted), "1"],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        check=True,
        stdout=subprocess.DEVNULL,
    )
    real_run(
        [sys.executable, str(module), str(interrupted), "2"],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        check=True,
        stdout=subprocess.DEVNULL,
    )
    real_run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path;import sys;"
            "from opportunities.store import AppendOnlyLedger;"
            f"l=AppendOnlyLedger(Path('{uninterrupted}'));l.append({{'value':1}});l.append({{'value':2}})",
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        check=True,
        stdout=subprocess.DEVNULL,
    )
    assert [json.loads(line)["payload"] for line in uninterrupted.read_bytes().splitlines()] == [
        json.loads(line)["payload"] for line in interrupted.read_bytes().splitlines()
    ]


def test_truncated_tail_corruption_and_symlink_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger = AppendOnlyLedger(path)
    ledger.append({"event_id": "one"})
    with path.open("ab") as output:
        output.write(b'{"sequence":1,"previous')
    with pytest.raises(LedgerCorruptionError, match="truncated"):
        AppendOnlyLedger(path)
    assert path.read_bytes().endswith(b'{"sequence":1,"previous')
    link = tmp_path / "linked.jsonl"
    link.symlink_to(path)
    with pytest.raises(LedgerPathError, match="symlink"):
        AppendOnlyLedger(link)


def test_middle_corruption_and_traversal_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger = AppendOnlyLedger(path)
    ledger.append({"event_id": "one"})
    ledger.append({"event_id": "two"})
    raw = path.read_bytes().replace(b'"two"', b'"tampered"')
    path.write_bytes(raw)
    with pytest.raises(LedgerCorruptionError, match="record hash mismatch"):
        AppendOnlyLedger(path)
    with pytest.raises(LedgerPathError, match="traverse"):
        AppendOnlyLedger(Path("../outside.jsonl"))


def test_write_failure_is_not_reported_as_success(tmp_path: Path) -> None:
    ledger = AppendOnlyLedger(tmp_path / "ledger.jsonl")
    with pytest.raises(LedgerWriteError):
        ledger.append({"bad": float("nan")})
    with pytest.raises(TypeError):
        ledger.append("not-object")  # type: ignore[arg-type]
    assert not (tmp_path / "ledger.jsonl").exists()
