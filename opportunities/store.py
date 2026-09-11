"""Append-only JSONL ledger with hash-chain recovery."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GENESIS_HASH = "0" * 64


class LedgerError(Exception):
    """Base class for ledger integrity and path errors."""


class LedgerPathError(LedgerError):
    """Raised when a ledger or checkpoint path is unsafe."""


class LedgerCorruptionError(LedgerError):
    """Raised when persisted bytes cannot be trusted."""


class LedgerWriteError(LedgerError):
    """Raised when an append was not durably committed."""


@dataclass(frozen=True, slots=True)
class LedgerSnapshot:
    """A validated read view of an append-only ledger."""

    events: tuple[dict[str, Any], ...]
    confirmed_sequence: int
    head_hash: str
    truncated_tail: bytes | None = None


def _canonical_json(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _record_hash(previous_hash: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256(previous_hash.encode("ascii") + _canonical_json(payload)).hexdigest()


def _validate_relative_path(path: Path) -> None:
    if path.is_absolute():
        return
    if not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise LedgerPathError("relative ledger path must not traverse directories")


def _reject_symlink(path: Path) -> None:
    try:
        if path.is_symlink():
            raise LedgerPathError(f"symlink ledger path rejected: {path}")
    except OSError as error:
        raise LedgerPathError(f"ledger path could not be inspected: {error}") from error


def _validate_regular_file(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise LedgerPathError(str(error)) from error
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise LedgerPathError("ledger must be a regular file without hard links")


def _parse_line(line: bytes, expected_sequence: int, previous_hash: str) -> dict[str, Any]:
    try:
        envelope = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        detail = f"invalid ledger JSON at sequence {expected_sequence}"
        raise LedgerCorruptionError(detail) from error
    if not isinstance(envelope, dict):
        raise LedgerCorruptionError(f"ledger entry {expected_sequence} is not an object")
    if type(envelope.get("sequence")) is not int or envelope["sequence"] != expected_sequence:
        raise LedgerCorruptionError(f"ledger sequence mismatch at {expected_sequence}")
    if envelope.get("previous_hash") != previous_hash:
        raise LedgerCorruptionError(f"ledger hash chain broken at {expected_sequence}")
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise LedgerCorruptionError(f"ledger entry {expected_sequence} has no payload object")
    try:
        encoded = _canonical_json(payload)
    except (TypeError, ValueError) as error:
        raise LedgerCorruptionError(f"ledger entry {expected_sequence} is not canonical") from error
    actual_hash = hashlib.sha256(previous_hash.encode("ascii") + encoded).hexdigest()
    if envelope.get("record_hash") != actual_hash:
        raise LedgerCorruptionError(f"ledger record hash mismatch at {expected_sequence}")
    payload["_ledger_hash"] = actual_hash
    return payload


def _read_snapshot(path: Path) -> LedgerSnapshot:
    _reject_symlink(path)
    _validate_relative_path(path)
    _validate_regular_file(path)
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise LedgerPathError(str(error)) from error
    events: list[dict[str, Any]] = []
    previous_hash = GENESIS_HASH
    truncated_tail: bytes | None = None
    if raw and not raw.endswith(b"\n"):
        newline_index = raw.rfind(b"\n")
        body = raw[: newline_index + 1] if newline_index >= 0 else b""
        truncated_tail = raw[newline_index + 1 :] if newline_index >= 0 else raw
    else:
        body = raw
    for sequence, raw_line in enumerate(body.splitlines()):
        if not raw_line:
            raise LedgerCorruptionError(f"blank ledger line at sequence {sequence}")
        events.append(_parse_line(raw_line, sequence, previous_hash))
        previous_hash = events[-1].get("_ledger_hash", previous_hash)
    confirmed_seq = len(events) - 1 if events else -1
    return LedgerSnapshot(tuple(events), confirmed_seq, previous_hash, truncated_tail)


class AppendOnlyLedger:
    """Durable ledger; recovery, reads and appends share one process-safe lock.

    A failed append poisons that writer. Reopen to validate the durable tail and
    recover a lagging checkpoint; never retry a partially committed append blindly.
    """

    def __init__(self, path: Path, *, auto_recover: bool = True) -> None:
        self.path = Path(path)
        self._checkpoint_path = Path(str(self.path) + ".checkpoint")
        self._lock_path = Path(str(self.path) + ".lock")
        self._sequence = -1
        self._head_hash = GENESIS_HASH
        self._truncated_tail: bytes | None = None
        self._failed = False
        self._missing_checkpoint = True
        with self._locked():
            snapshot = self._snapshot()
            self._sequence = snapshot.confirmed_sequence
            self._head_hash = snapshot.head_hash
            self._missing_checkpoint = not self._checkpoint_path.exists()
            self._reconcile_checkpoint(snapshot, auto_recover=auto_recover)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        for path in (self.path, self._checkpoint_path, self._lock_path):
            _validate_relative_path(path)
            _reject_symlink(path)
            _validate_regular_file(path)
        fd: int | None = None
        try:
            fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise LedgerPathError("ledger lock must be a regular file without hard links")
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as error:
            if fd is not None:
                os.close(fd)
            raise LedgerWriteError(f"cannot acquire ledger lock: {error}") from error
        except BaseException:
            if fd is not None:
                os.close(fd)
            raise
        try:
            yield
        finally:
            # Closing releases flock even if unlocking explicitly fails.
            os.close(fd)

    def _snapshot(self) -> LedgerSnapshot:
        if not self.path.exists():
            return LedgerSnapshot((), -1, GENESIS_HASH)
        snapshot = _read_snapshot(self.path)
        if snapshot.truncated_tail is not None:
            raise LedgerCorruptionError("ledger ends with a truncated record")
        return snapshot

    def _read_checkpoint(self) -> tuple[int, str] | None:
        _reject_symlink(self._checkpoint_path)
        _validate_regular_file(self._checkpoint_path)
        if not self._checkpoint_path.exists():
            return None
        try:
            checkpoint = json.loads(self._checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LedgerCorruptionError("checkpoint could not be read") from error
        if not isinstance(checkpoint, dict):
            raise LedgerCorruptionError("checkpoint must be an object")
        sequence = checkpoint.get("confirmed_sequence")
        head = checkpoint.get("record_hash")
        if type(sequence) is not int or sequence < -1:
            raise LedgerCorruptionError("checkpoint sequence must be an integer >= -1")
        if (
            type(head) is not str
            or len(head) != 64
            or any(c not in "0123456789abcdef" for c in head)
        ):
            raise LedgerCorruptionError("checkpoint hash must be canonical SHA-256 hex")
        return sequence, head

    def _reconcile_checkpoint(self, snapshot: LedgerSnapshot, *, auto_recover: bool) -> None:
        checkpoint = self._read_checkpoint()
        if checkpoint is None:
            return
        sequence, head = checkpoint
        if sequence > snapshot.confirmed_sequence:
            raise LedgerCorruptionError("checkpoint does not match durable ledger")
        # A lagging checkpoint is trustworthy ONLY if it anchors the corresponding
        # validated historical prefix, not merely because its sequence is smaller.
        prefix_hash = GENESIS_HASH if sequence == -1 else snapshot.events[sequence]["_ledger_hash"]
        if head != prefix_hash:
            raise LedgerCorruptionError("checkpoint hash does not match durable ledger prefix")
        if sequence < snapshot.confirmed_sequence:
            if not auto_recover:
                raise LedgerCorruptionError("checkpoint does not match durable ledger")
            self._write_checkpoint()  # Under the SAME lock as append; errors must propagate.
        self._missing_checkpoint = False

    @property
    def confirmed_sequence(self) -> int:
        """Return the last sequence acknowledged by this writer."""
        return self._sequence

    @property
    def head_hash(self) -> str:
        """Return this writer's hash-chain head."""
        return self._head_hash

    def _verify_checkpoint(
        self, confirmed_sequence: int, expected_head_hash: str | None = None
    ) -> None:
        checkpoint = self._read_checkpoint()
        if checkpoint is None:
            if not self._missing_checkpoint:
                raise LedgerCorruptionError("checkpoint disappeared after recovery")
            return
        sequence, head = checkpoint
        if sequence != confirmed_sequence:
            raise LedgerCorruptionError("checkpoint does not match durable ledger")
        if expected_head_hash is not None and head != expected_head_hash:
            raise LedgerCorruptionError("checkpoint hash does not match durable ledger head")

    def _write_checkpoint(self) -> None:
        temp_path = Path(str(self._checkpoint_path) + ".tmp")
        _reject_symlink(temp_path)
        _validate_regular_file(temp_path)
        try:
            fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as checkpoint:
                metadata = os.fstat(checkpoint.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise LedgerPathError("checkpoint temporary file is unsafe")
                checkpoint.truncate(0)
                checkpoint.write(
                    _canonical_json(
                        {
                            "confirmed_sequence": self._sequence,
                            "record_hash": self._head_hash,
                        }
                    )
                )
                checkpoint.flush()
                os.fsync(checkpoint.fileno())
            os.replace(temp_path, self._checkpoint_path)
            directory_fd = os.open(self._checkpoint_path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as error:
            raise LedgerWriteError(str(error)) from error
        self._missing_checkpoint = False

    def append(self, payload: dict[str, Any]) -> int:
        """Append once, acknowledging only after data and checkpoint durability."""
        if not isinstance(payload, dict):
            raise TypeError("payload must be a dict")
        try:
            # Freeze caller-owned mutable data before hashing and writing it.
            frozen_payload = json.loads(_canonical_json(payload))
        except (TypeError, ValueError) as error:
            raise LedgerWriteError("payload must be canonical JSON") from error
        with self._locked():
            if self._failed:
                raise LedgerWriteError("previous append failed; reopen ledger before writing")
            snapshot = self._snapshot()  # Reject a partial tail BEFORE any write.
            if (
                snapshot.confirmed_sequence != self._sequence
                or snapshot.head_hash != self._head_hash
            ):
                raise LedgerWriteError(
                    f"concurrent write detected: memory sequence {self._sequence} "
                    f"does not match durable disk sequence {snapshot.confirmed_sequence}"
                )
            self._verify_checkpoint(self._sequence, self._head_hash)
            sequence = self._sequence + 1
            previous_hash = self._head_hash
            record_hash = _record_hash(previous_hash, frozen_payload)
            line = (
                _canonical_json(
                    {
                        "sequence": sequence,
                        "previous_hash": previous_hash,
                        "record_hash": record_hash,
                        "payload": frozen_payload,
                    }
                )
                + b"\n"
            )
            try:
                fd = os.open(
                    self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600
                )
                with os.fdopen(fd, "ab") as ledger:
                    metadata = os.fstat(ledger.fileno())
                    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                        raise LedgerPathError("ledger target changed while writing")
                    ledger.write(line)
                    ledger.flush()
                    os.fsync(ledger.fileno())
                self._sequence = sequence
                self._head_hash = record_hash
                self._write_checkpoint()
            except OSError as error:
                self._failed = True
                raise LedgerWriteError(str(error)) from error
            except BaseException:
                self._failed = True
                raise
            return sequence

    def load(self) -> LedgerSnapshot:
        """Read a consistent snapshot; reject changed or partially committed bytes."""
        with self._locked():
            snapshot = self._snapshot()
            if (
                snapshot.confirmed_sequence != self._sequence
                or snapshot.head_hash != self._head_hash
            ):
                raise LedgerCorruptionError("ledger changed since this process recovered")
            self._verify_checkpoint(snapshot.confirmed_sequence, snapshot.head_hash)
            return snapshot
