"""Unit tests for read-only AppendOnlyLedger mode and fail-closed invariants."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opportunities.store import (
    AppendOnlyLedger,
    LedgerCorruptionError,
    LedgerWriteError,
    read_snapshot,
)


def test_readonly_ledger_does_not_create_lock_or_checkpoint(tmp_path: Path) -> None:
    """Read-only ledger open must never create .lock or .checkpoint files."""
    ledger_path = tmp_path / "test.jsonl"
    writer = AppendOnlyLedger(ledger_path)
    writer.append({"item": 1})
    writer.append({"item": 2})

    # Remove lock and checkpoint
    lock_path = Path(str(ledger_path) + ".lock")
    checkpoint_path = Path(str(ledger_path) + ".checkpoint")
    if lock_path.exists():
        lock_path.unlink()
    if checkpoint_path.exists():
        checkpoint_path.unlink()

    # Open in read-only mode
    ro_ledger = AppendOnlyLedger.open_readonly(ledger_path)
    assert ro_ledger.read_only is True
    snapshot = ro_ledger.load()
    assert snapshot.confirmed_sequence == 1
    assert len(snapshot.events) == 2

    # Verify no .lock or .checkpoint was created
    assert not lock_path.exists()
    assert not checkpoint_path.exists()


def test_readonly_ledger_fails_closed_on_append(tmp_path: Path) -> None:
    """Read-only ledger must fail closed when append() is attempted."""
    ledger_path = tmp_path / "test.jsonl"
    writer = AppendOnlyLedger(ledger_path)
    writer.append({"item": "genesis"})

    ro_ledger = AppendOnlyLedger.open_readonly(ledger_path)
    with pytest.raises(LedgerWriteError, match="read-only"):
        ro_ledger.append({"item": "illegal"})


def test_readonly_ledger_rejects_truncated_tail(tmp_path: Path) -> None:
    """Read-only ledger must reject truncated records at tail without pseudo-completion."""
    ledger_path = tmp_path / "test.jsonl"
    writer = AppendOnlyLedger(ledger_path)
    writer.append({"item": "complete"})

    lock_path = Path(str(ledger_path) + ".lock")
    checkpoint_path = Path(str(ledger_path) + ".checkpoint")
    if lock_path.exists():
        lock_path.unlink()

    # Append partial / incomplete record to file
    with ledger_path.open("ab") as f:
        f.write(b'{"sequence": 1, "partial": ')

    # open_readonly fails closed eagerly on truncated tail during construction
    with pytest.raises(LedgerCorruptionError, match="truncated"):
        AppendOnlyLedger.open_readonly(ledger_path)

    # Verify no file side-effects occurred on failed read-only open
    assert not lock_path.exists(), "Failed read-only open must not create .lock"
    assert not Path(str(ledger_path) + ".tmp").exists(), "Failed read-only open must not leave .tmp"
    assert not Path(str(checkpoint_path) + ".tmp").exists(), (
        "Failed read-only open must not leave checkpoint .tmp"
    )

    # Retain load-stage rejection validation when tampering occurs post-construction
    ledger_path_load = tmp_path / "test_load_truncated.jsonl"
    writer_load = AppendOnlyLedger(ledger_path_load)
    writer_load.append({"item": "complete"})
    ro_ledger = AppendOnlyLedger.open_readonly(ledger_path_load)

    with ledger_path_load.open("ab") as f:
        f.write(b'{"sequence": 1, "partial": ')

    with pytest.raises(LedgerCorruptionError, match="truncated"):
        ro_ledger.load()


def test_readonly_ledger_rejects_corrupted_hash_chain(tmp_path: Path) -> None:
    """Read-only ledger must detect tampering in the hash chain."""
    ledger_path = tmp_path / "test.jsonl"
    writer = AppendOnlyLedger(ledger_path)
    writer.append({"item": "original"})
    writer.append({"item": "second"})

    lock_path = Path(str(ledger_path) + ".lock")
    checkpoint_path = Path(str(ledger_path) + ".checkpoint")
    if lock_path.exists():
        lock_path.unlink()

    tampered_bytes = ledger_path.read_bytes().replace(b'"original"', b'"tampered"')
    ledger_path.write_bytes(tampered_bytes)

    # open_readonly fails closed eagerly on hash chain corruption during construction
    with pytest.raises(LedgerCorruptionError, match="hash mismatch"):
        AppendOnlyLedger.open_readonly(ledger_path)

    # Verify no file side-effects occurred on failed read-only open
    assert not lock_path.exists(), "Failed read-only open must not create .lock"
    assert not Path(str(ledger_path) + ".tmp").exists(), "Failed read-only open must not leave .tmp"
    assert not Path(str(checkpoint_path) + ".tmp").exists(), (
        "Failed read-only open must not leave checkpoint .tmp"
    )

    # Retain load-stage rejection validation when tampering occurs post-construction
    ledger_path_load = tmp_path / "test_load_corrupted_hash.jsonl"
    writer_load = AppendOnlyLedger(ledger_path_load)
    writer_load.append({"item": "original"})
    writer_load.append({"item": "second"})
    ro_ledger = AppendOnlyLedger.open_readonly(ledger_path_load)

    tampered_load_bytes = ledger_path_load.read_bytes().replace(b'"original"', b'"tampered"')
    ledger_path_load.write_bytes(tampered_load_bytes)

    with pytest.raises(LedgerCorruptionError, match="hash mismatch"):
        ro_ledger.load()


def test_readonly_ledger_with_matching_checkpoint(tmp_path: Path) -> None:
    """Read-only ledger succeeds when existing checkpoint matches disk snapshot."""
    ledger_path = tmp_path / "test.jsonl"
    writer = AppendOnlyLedger(ledger_path)
    writer.append({"n": 100})

    ro_ledger = AppendOnlyLedger.open_readonly(ledger_path)
    snapshot = ro_ledger.load()
    assert snapshot.confirmed_sequence == 0
    assert snapshot.events[0]["n"] == 100


def test_readonly_ledger_rejects_mismatched_checkpoint(tmp_path: Path) -> None:
    """Read-only ledger fails closed when existing checkpoint does not match snapshot."""
    ledger_path = tmp_path / "test.jsonl"
    writer = AppendOnlyLedger(ledger_path)
    writer.append({"n": 100})

    lock_path = Path(str(ledger_path) + ".lock")
    checkpoint_path = Path(str(ledger_path) + ".checkpoint")
    if lock_path.exists():
        lock_path.unlink()

    checkpoint_path.write_text(
        json.dumps({"confirmed_sequence": 999, "record_hash": "0" * 64}),
        encoding="utf-8",
    )

    # open_readonly fails closed eagerly on mismatched checkpoint during construction
    with pytest.raises(LedgerCorruptionError, match="checkpoint does not match"):
        AppendOnlyLedger.open_readonly(ledger_path)

    # Verify no file side-effects occurred on failed read-only open
    assert not lock_path.exists(), "Failed read-only open must not create .lock"
    assert not Path(str(ledger_path) + ".tmp").exists(), "Failed read-only open must not leave .tmp"
    assert not Path(str(checkpoint_path) + ".tmp").exists(), (
        "Failed read-only open must not leave checkpoint .tmp"
    )

    # Retain load-stage rejection validation when tampering occurs post-construction
    ledger_path_load = tmp_path / "test_load_mismatched_cp.jsonl"
    writer_load = AppendOnlyLedger(ledger_path_load)
    writer_load.append({"n": 100})
    ro_ledger = AppendOnlyLedger.open_readonly(ledger_path_load)

    checkpoint_path_load = Path(str(ledger_path_load) + ".checkpoint")
    checkpoint_path_load.write_text(
        json.dumps({"confirmed_sequence": 999, "record_hash": "0" * 64}),
        encoding="utf-8",
    )

    with pytest.raises(LedgerCorruptionError, match="checkpoint does not match"):
        ro_ledger.load()


def test_read_snapshot_top_level_function(tmp_path: Path) -> None:
    """Top-level read_snapshot helper returns identical validated snapshot."""
    ledger_path = tmp_path / "test.jsonl"
    writer = AppendOnlyLedger(ledger_path)
    writer.append({"key": "val"})

    snapshot = read_snapshot(ledger_path)
    assert snapshot.confirmed_sequence == 0
    assert snapshot.events[0]["key"] == "val"
