"""Strict historical replay RPC adapter for dex-sniper-engine.

Provides an offline, deterministic replay client that serves recorded responses
from historical-rpc evidence logs (e.g. tests/fixtures/opportunities/v1/historical-rpc.jsonl)
without network calls.

Enforces strict parity verification on every call:
1. Exact method match
2. Exact params match (including full ABI calldata and casing)
3. Exact block_identifier match
4. Exact sequence preservation
5. Exact call count (raises RuntimeError on excess or unconsumed records)
"""

from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

# Default paths to the immutable historical-rpc evidence log
DEFAULT_FIXTURE_PATH: Path = Path(
    "/root/projects/crypto/arc-chain/.worktrees/repair-integration-20260912"
    "/tests/fixtures/opportunities/v1/historical-rpc.jsonl"
)


def _resolve_default_rpc_file() -> Path:
    """Resolve default historical-rpc.jsonl fixture path."""
    candidates = [
        Path(__file__).resolve().parents[3]
        / "tests"
        / "fixtures"
        / "opportunities"
        / "v1"
        / "historical-rpc.jsonl",
        DEFAULT_FIXTURE_PATH,
        Path(__file__).resolve().parents[2]
        / "tests"
        / "fixtures"
        / "opportunities"
        / "v1"
        / "historical-rpc.jsonl",
        Path("/root/projects/crypto/arc-chain/tests/fixtures/opportunities/v1/historical-rpc.jsonl"),
    ]
    for c in candidates:
        if c.exists():
            return c
    return DEFAULT_FIXTURE_PATH


DEFAULT_RPC_FILE: Path = _resolve_default_rpc_file()


class ReplayAdapter:
    """Strict offline replay client for reproducible historical quotation and testing."""

    def __init__(
        self,
        records_or_path: str | Path | Sequence[dict[str, Any]] | None = None,
    ) -> None:
        """Initialize ReplayAdapter with records or path to jsonl file.

        Args:
            records_or_path: Path to jsonl file, list of record dictionaries,
                or None to use DEFAULT_RPC_FILE.
        """
        if records_or_path is None:
            source_path = _resolve_default_rpc_file()
            self._records = self._load_from_path(source_path)
        elif isinstance(records_or_path, (str, Path)):
            self._records = self._load_from_path(Path(records_or_path))
        elif isinstance(records_or_path, (list, tuple)):
            self._records = [copy.deepcopy(r) for r in records_or_path]
        else:
            raise TypeError(
                f"records_or_path must be Path, str, Sequence, or None, got {type(records_or_path).__name__}"
            )

        self._index: int = 0

    @staticmethod
    def _load_from_path(path: Path) -> list[dict[str, Any]]:
        """Load and parse records from JSONL file."""
        if not path.exists():
            raise FileNotFoundError(f"Replay log file not found: {path}")
        records: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                clean_line = line.strip()
                if not clean_line:
                    continue
                try:
                    records.append(json.loads(clean_line))
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Malformed JSON in replay file at line {line_no}: {exc}"
                    ) from exc
        return records

    @property
    def index(self) -> int:
        """Current consumption pointer index."""
        return self._index

    @property
    def total_count(self) -> int:
        """Total number of recorded RPC calls."""
        return len(self._records)

    @property
    def records(self) -> list[dict[str, Any]]:
        """Read-only reference to loaded records."""
        return list(self._records)

    def consumed_count(self) -> int:
        """Number of RPC calls consumed so far."""
        return self._index

    def remaining_count(self) -> int:
        """Number of RPC calls remaining to be consumed."""
        return len(self._records) - self._index

    def is_finished(self) -> bool:
        """Whether all recorded calls have been consumed."""
        return self._index == len(self._records)

    def reset(self) -> None:
        """Reset the replay consumption pointer to 0."""
        self._index = 0

    def call(
        self,
        method: str,
        params: list[Any] | tuple[Any, ...] | None = None,
        block_identifier: str | None = None,
    ) -> dict[str, Any]:
        """Execute a replay call and verify strict parity with recorded trace.

        Args:
            method: JSON-RPC method name (e.g. 'eth_call', 'eth_blockNumber').
            params: Parameters list sent to the JSON-RPC call.
            block_identifier: Block identifier or tag (e.g. '0x378a957').

        Returns:
            The recorded JSON-RPC response dictionary.

        Raises:
            RuntimeError: If calls exceed total records, or if method, params,
                or block_identifier do not match the expected record.
        """
        if self._index >= len(self._records):
            raise RuntimeError(
                f"Replay exhausted at index {self._index}: unexpected extra call "
                f"method='{method}', params={params}, block_identifier='{block_identifier}'"
            )

        current_idx = self._index
        rec = self._records[current_idx]

        # 1. Strict method comparison
        rec_method = rec.get("method")
        if rec_method != method:
            raise RuntimeError(
                f"Replay method mismatch at index {current_idx}: "
                f"expected '{rec_method}', got '{method}'"
            )

        # 2. Strict params comparison (including full ABI calldata)
        call_params = list(params) if params is not None else []
        rec_params = rec.get("params") or []
        if call_params != rec_params:
            raise RuntimeError(
                f"Replay params mismatch at index {current_idx} for method '{method}':\n"
                f"  expected: {rec_params}\n"
                f"  got:      {call_params}"
            )

        # 3. Strict block_identifier comparison
        rec_block = rec.get("block_identifier")
        if block_identifier != rec_block:
            raise RuntimeError(
                f"Replay block_identifier mismatch at index {current_idx} for method '{method}': "
                f"expected '{rec_block}', got '{block_identifier}'"
            )

        # Parity passed: advance pointer and return response
        self._index += 1
        if "response" in rec and isinstance(rec["response"], dict):
            return rec["response"]
        raise RuntimeError(
            f"Replay record at index {current_idx} missing valid 'response' dictionary payload"
        )

    def verify_complete(self) -> None:
        """Verify that all recorded RPC entries were consumed exactly without deficit.

        Raises:
            RuntimeError: If there are unconsumed records remaining.
        """
        if self._index < len(self._records):
            remaining = len(self._records) - self._index
            raise RuntimeError(
                f"Replay incomplete: {self._index} of {len(self._records)} records consumed "
                f"({remaining} unconsumed records remain in replay)"
            )
