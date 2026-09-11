"""Durable, crash-safe, monotonic cursor management for raw Arc block ingestion."""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from arc_readiness.errors import ArcValidationError


@dataclass(frozen=True)
class DurableCursor:
    """Immutable persistent cursor pointer anchoring raw ingestion state."""

    chain_id: int
    block_number: int
    block_hash: str
    cursor_str: str
    updated_at: float

    def __post_init__(self) -> None:
        if self.block_number < 0:
            raise ArcValidationError(f"block_number cannot be negative: {self.block_number}")
        norm_hash = self.block_hash.lower()
        if not norm_hash.startswith("0x") or len(norm_hash) != 66:
            raise ArcValidationError(f"Invalid block_hash in cursor: {self.block_hash}")
        if not self.cursor_str:
            raise ArcValidationError("cursor_str cannot be empty")


class CursorStore:
    """Manages persistent cursor storage with atomic write semantics and monotonicity guards."""

    def __init__(self, cursor_file: str | Path, chain_id: int = 5042) -> None:
        self.cursor_file = Path(cursor_file)
        self.chain_id = chain_id
        self._current_cursor: DurableCursor | None = None
        self.load()

    @property
    def current(self) -> DurableCursor | None:
        return self._current_cursor

    def load(self) -> DurableCursor | None:
        """Load the durable cursor from disk if it exists."""
        if not self.cursor_file.exists():
            self._current_cursor = None
            return None

        try:
            with open(self.cursor_file, encoding="utf-8") as f:
                data = json.load(f)
            cursor = DurableCursor(
                chain_id=int(data["chain_id"]),
                block_number=int(data["block_number"]),
                block_hash=str(data["block_hash"]).lower(),
                cursor_str=str(data["cursor_str"]),
                updated_at=float(data["updated_at"]),
            )
            self._current_cursor = cursor
            return cursor
        except Exception as e:
            raise ArcValidationError(f"Corrupt or unreadable cursor file at {self.cursor_file}: {e}") from e

    def advance(
        self,
        block_number: int,
        block_hash: str,
        explicit_timestamp: float | None = None,
    ) -> DurableCursor:
        """Advance the cursor monotonically, writing atomically to disk via tmp-file replace."""
        if self._current_cursor is not None:
            if block_number < self._current_cursor.block_number:
                raise ArcValidationError(
                    f"Monotonicity violation: Cannot regress cursor from {self._current_cursor.block_number} "
                    f"to {block_number}"
                )
            if block_number == self._current_cursor.block_number and block_hash.lower() != self._current_cursor.block_hash:
                raise ArcValidationError(
                    f"Cursor block hash divergence at block {block_number}: "
                    f"current={self._current_cursor.block_hash}, new={block_hash}"
                )

        now = explicit_timestamp if explicit_timestamp is not None else time.time()
        cursor_str = f"arc:{self.chain_id}:{block_number}:{block_hash[:10]}"

        new_cursor = DurableCursor(
            chain_id=self.chain_id,
            block_number=block_number,
            block_hash=block_hash.lower(),
            cursor_str=cursor_str,
            updated_at=now,
        )

        # Atomic write: write to tmp file in the same directory, fsync, then atomic replace
        self.cursor_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.cursor_file.with_suffix(".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(asdict(new_cursor), f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.cursor_file)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

        self._current_cursor = new_cursor
        return new_cursor
