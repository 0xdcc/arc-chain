"""Unit tests verifying audit remediation for opportunities/store.py (F04-A, F04-B, F04-C, F04-D)."""

import json
import threading
from pathlib import Path

import pytest

from opportunities.store import (
    AppendOnlyLedger,
    LedgerCorruptionError,
    LedgerError,
    LedgerWriteError,
)


def test_f04_a_empty_file_open_sequence(tmp_path: Path) -> None:
    """F04-A: Opening an empty pre-created ledger file must initialize sequence to -1

    and append the first record at sequence 0, without crashing on reopen.
    """
    path = tmp_path / "empty_ledger.jsonl"
    path.touch()  # Create empty file

    ledger = AppendOnlyLedger(path)
    assert ledger.confirmed_sequence == -1

    seq = ledger.append({"msg": "first"})
    assert seq == 0
    assert ledger.confirmed_sequence == 0

    # Reopen should succeed and load 1 event without sequence mismatch
    reopened = AppendOnlyLedger(path)
    snapshot = reopened.load()
    assert reopened.confirmed_sequence == 0
    assert len(snapshot.events) == 1
    assert snapshot.events[0]["msg"] == "first"


def test_f04_b_auto_recover_lagging_checkpoint(tmp_path: Path) -> None:
    """F04-B: When durable records are intact but checkpoint lagged behind,

    auto_recover=True should advance the checkpoint safely.
    """
    path = tmp_path / "durable_ledger.jsonl"
    ledger = AppendOnlyLedger(path)
    assert ledger.append({"item": 1}) == 0
    assert ledger.append({"item": 2}) == 1

    # Simulate checkpoint lagging behind (reset checkpoint to sequence 0)
    cp_path = Path(str(path) + ".checkpoint")
    with path.open("rb") as f:
        first_line = json.loads(f.readline().decode("utf-8"))
    first_hash = first_line["record_hash"]
    cp_path.write_text(
        json.dumps({"confirmed_sequence": 0, "record_hash": first_hash}),
        encoding="utf-8",
    )

    # Reopen with auto_recover=True should repair checkpoint to sequence 1
    reopened = AppendOnlyLedger(path, auto_recover=True)
    assert reopened.confirmed_sequence == 1
    cp_data = json.loads(cp_path.read_text(encoding="utf-8"))
    assert cp_data["confirmed_sequence"] == 1
    assert cp_data["record_hash"] == reopened.head_hash

    # Without auto_recover, it should reject the mismatch
    cp_path.write_text(
        json.dumps({"confirmed_sequence": 0, "record_hash": first_hash}),
        encoding="utf-8",
    )
    with pytest.raises(LedgerCorruptionError, match="checkpoint does not match durable ledger"):
        AppendOnlyLedger(path, auto_recover=False)


def test_f04_c_checkpoint_head_hash_validation(tmp_path: Path) -> None:
    """F04-C: Checkpoint verification must validate record_hash, not only sequence."""
    path = tmp_path / "hash_check_ledger.jsonl"
    ledger = AppendOnlyLedger(path)
    ledger.append({"event": "payload"})

    cp_path = Path(str(path) + ".checkpoint")
    cp_data = json.loads(cp_path.read_text(encoding="utf-8"))
    # Tamper with record_hash while keeping confirmed_sequence correct
    cp_data["record_hash"] = "0" * 64
    cp_path.write_text(json.dumps(cp_data), encoding="utf-8")

    with pytest.raises(
        LedgerCorruptionError, match="checkpoint hash does not match durable ledger"
    ):
        AppendOnlyLedger(path, auto_recover=False)


def test_f04_d_concurrent_write_collision_rejection(tmp_path: Path) -> None:
    """F04-D: Two ledger instances opened concurrently must not collide on sequence numbers."""
    path = tmp_path / "concurrent_ledger.jsonl"
    l1 = AppendOnlyLedger(path)
    l2 = AppendOnlyLedger(path)

    # l1 writes first record (seq 0)
    assert l1.append({"writer": 1}) == 0

    # l2 attempts to write with stale in-memory state (-1) -> must be rejected
    with pytest.raises(LedgerWriteError, match="concurrent write detected"):
        l2.append({"writer": 2})

    # Ledger file integrity is preserved: reopen sees single valid record
    reopened = AppendOnlyLedger(path)
    assert reopened.confirmed_sequence == 0
    assert len(reopened.load().events) == 1


def test_recovery_rejects_corrupt_lagging_checkpoint_hash(tmp_path):
    p = tmp_path / "ledger.jsonl"
    writer = AppendOnlyLedger(p)
    writer.append({"n": 0})
    writer.append({"n": 1})
    cp = Path(str(p) + ".checkpoint")
    cp.write_text(json.dumps({"confirmed_sequence": 0, "record_hash": "0" * 64}))
    with pytest.raises(LedgerError):
        AppendOnlyLedger(p, auto_recover=True)


def test_append_rejects_partial_tail_before_committing(tmp_path):
    p = tmp_path / "ledger.jsonl"
    writer = AppendOnlyLedger(p)
    writer.append({"n": 0})
    with p.open("ab") as f:
        f.write(b'{"partial":')
    before = p.read_bytes()
    with pytest.raises(LedgerError):
        writer.append({"n": 1})
    assert p.read_bytes() == before


def test_recovery_cannot_overwrite_newer_writer_checkpoint(tmp_path, monkeypatch):
    p = tmp_path / "ledger.jsonl"
    writer = AppendOnlyLedger(p)
    writer.append({"n": 0})
    writer.append({"n": 1})
    cp = Path(str(p) + ".checkpoint")
    first = json.loads(p.read_text().splitlines()[0])
    cp.write_text(json.dumps({"confirmed_sequence": 0, "record_hash": first["record_hash"]}))
    recovery_started = threading.Event()
    release_recovery = threading.Event()
    original = AppendOnlyLedger._write_checkpoint
    errors = []

    def delayed(self):
        if threading.current_thread().name == "audit-recovery":
            recovery_started.set()
            if not release_recovery.wait(5):
                raise RuntimeError("test interleaving timeout")
        return original(self)

    monkeypatch.setattr(AppendOnlyLedger, "_write_checkpoint", delayed)

    def recover():
        try:
            AppendOnlyLedger(p, auto_recover=True)
        except Exception as e:
            errors.append(repr(e))

    thread = threading.Thread(target=recover, name="audit-recovery", daemon=True)
    thread.start()
    assert recovery_started.wait(5)
    writer_started = threading.Event()
    writer_done = threading.Event()
    writer_errors = []

    def append_next():
        writer_started.set()
        try:
            assert writer.append({"n": 2}) == 2
        except Exception as error:
            writer_errors.append(repr(error))
        finally:
            writer_done.set()

    writer_thread = threading.Thread(target=append_next, name="audit-writer", daemon=True)
    writer_thread.start()
    try:
        assert writer_started.wait(5)
        # A correctly locked recovery can block this writer. Release recovery either way.
        # On 6dd8794 the writer finishes first and its checkpoint gets overwritten.
        writer_done.wait(0.5)
    finally:
        release_recovery.set()
        thread.join(5)
        writer_thread.join(5)
    assert not thread.is_alive() and not writer_thread.is_alive()
    assert not errors and not writer_errors, (errors, writer_errors)
    assert json.loads(cp.read_text())["confirmed_sequence"] == 2, (
        "recovery regressed checkpoint 2 -> 1"
    )


@pytest.mark.parametrize("bad_sequence", [True, False, -2, "0"])
def test_checkpoint_sequence_is_strict_integer(tmp_path, bad_sequence):
    path = tmp_path / "strict.jsonl"
    writer = AppendOnlyLedger(path)
    writer.append({"event": 1})
    checkpoint = Path(str(path) + ".checkpoint")
    checkpoint.write_text(
        json.dumps(
            {
                "confirmed_sequence": bad_sequence,
                "record_hash": writer.head_hash,
            }
        )
    )
    with pytest.raises(LedgerCorruptionError):
        AppendOnlyLedger(path)


def test_checkpoint_write_failure_requires_reopen(tmp_path, monkeypatch):
    path = tmp_path / "failure.jsonl"
    writer = AppendOnlyLedger(path)
    writer.append({"event": 0})
    original = AppendOnlyLedger._write_checkpoint

    def fail_checkpoint(self):
        raise LedgerWriteError("injected checkpoint failure")

    monkeypatch.setattr(AppendOnlyLedger, "_write_checkpoint", fail_checkpoint)
    with pytest.raises(LedgerWriteError, match="injected checkpoint failure"):
        writer.append({"event": 1})
    before = path.read_bytes()
    with pytest.raises(LedgerWriteError, match="reopen"):
        writer.append({"event": 1})
    assert path.read_bytes() == before
    monkeypatch.setattr(AppendOnlyLedger, "_write_checkpoint", original)
    recovered = AppendOnlyLedger(path)
    assert len(recovered.load().events) == 2
    assert recovered.append({"event": 2}) == 2
