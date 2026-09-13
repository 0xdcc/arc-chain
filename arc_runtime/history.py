"""Arc Historical Block Replay and Attribution Runtime (T38)

Enforces:
- Separation of historical block timestamp vs real-world observation/arrival timestamp
- Explicit marking of historical state opportunities as PRE_EXECUTION_HYPOTHESIS
- Complete error and failure retention (never masks 100% RPC failures)
- Strict mode isolation: retrospective_state vs causal_replay
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class HistoryReplayConfig:
    """Configuration for arc_history execution."""

    chain_id: int
    from_block: int
    to_block: int
    output_dir: Path
    mode: str = "retrospective_state"  # "retrospective_state" | "causal_replay"
    fixture_mode: bool = False
    rpc_endpoint: str | None = None

    def __post_init__(self) -> None:
        if self.chain_id not in (5042, 5042002):
            raise ValueError(f"Invalid chain_id: {self.chain_id}")
        if self.from_block > self.to_block:
            raise ValueError("from_block cannot exceed to_block")
        if self.mode not in ("retrospective_state", "causal_replay"):
            raise ValueError(f"Invalid mode: {self.mode}")
        if self.to_block - self.from_block + 1 > 500:
            raise ValueError("History scan capped at 500 blocks per execution pass")


def _reject_path_symlinks(path: Path) -> None:
    """Ensure neither the target path nor any ancestor directory is a symlink."""
    abs_norm = (Path.cwd() / path) if not path.is_absolute() else path
    abs_norm = Path(os.path.normpath(str(abs_norm)))
    for part in [abs_norm] + list(abs_norm.parents):
        if part.is_symlink() or os.path.islink(part):
            raise PermissionError(f"Symlink detected in path component: {part}")


@contextmanager
def _locked_output_dir(dir_path: Path):
    """Acquire an exclusive process lock on an output directory."""
    _reject_path_symlinks(dir_path)
    dir_path.mkdir(parents=True, exist_ok=True)
    lock_file = dir_path / ".lock"
    _reject_path_symlinks(lock_file)
    fd = os.open(lock_file, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def run_history_scan(config: HistoryReplayConfig) -> dict[str, Any]:
    """Execute historical scan and emit structured audit reports."""
    start_time = time.monotonic()

    if not config.fixture_mode:
        # A denied live request must be side-effect free. In particular, do
        # not create an output directory that could later be mistaken for a
        # successful historical run.
        raise PermissionError("Live historical scan requires verified RPC endpoint authorization.")

    _reject_path_symlinks(config.output_dir)

    with _locked_output_dir(config.output_dir):
        report_file = config.output_dir / "history_replay_report.json"
        opportunities_file = config.output_dir / "history_hypotheses.jsonl"
        protected_outputs = (report_file, opportunities_file)

        for p in protected_outputs:
            if p.is_symlink() or os.path.islink(p):
                raise FileExistsError(f"Refusing to overwrite existing history evidence: {p}")

        collisions = [str(path) for path in protected_outputs if os.path.lexists(path)]
        if collisions:
            raise FileExistsError(
                "Refusing to overwrite existing history evidence: " + ", ".join(collisions)
            )

        scanned_blocks = 0
        hypotheses: list[dict[str, Any]] = []

        if config.fixture_mode:
            for b in range(config.from_block, config.to_block + 1):
                scanned_blocks += 1
                # Deterministic synthetic opportunity hypothesis
                if b % 2 == 0:
                    item = {
                        "block_number": b,
                        "block_timestamp": 1726000000 + (b * 2),  # historical block clock
                        "observed_at_timestamp": time.time(),  # wall clock
                        "hypothesis_type": "PRE_EXECUTION_HYPOTHESIS",
                        "mode": config.mode,
                        "claimed_profit_guaranteed": False,  # Must never claim guaranteed executable profit
                        "cycle": "USDC -> WETH -> USDC",
                        "spread_bps": 12.5,
                    }
                    hypotheses.append(item)

        summary = {
            "status": "SUCCESS",
            "chain_id": config.chain_id,
            "mode": config.mode,
            "scanned_blocks": scanned_blocks,
            "hypotheses_count": len(hypotheses),
            "report_file": str(report_file),
            "hypotheses_file": str(opportunities_file),
            "disclaimer": "Historical price spreads represent post-block state hypotheses and do not imply guaranteed execution.",
            "elapsed_seconds": round(time.monotonic() - start_time, 4),
            "is_fixture_mode": config.fixture_mode,
        }

        temp_report: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                dir=config.output_dir,
                prefix=".report_",
                suffix=".tmp",
                delete=False,
                encoding="utf-8",
            ) as tf_r:
                temp_report = Path(tf_r.name)
                json.dump(summary, tf_r, indent=2)
                tf_r.flush()
                os.fsync(tf_r.fileno())

            fd = os.open(
                opportunities_file,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                0o644,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for h in hypotheses:
                    f.write(json.dumps(h, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())

            os.replace(temp_report, report_file)
            temp_report = None
        finally:
            if temp_report and temp_report.exists():
                try:
                    temp_report.unlink()
                except OSError:
                    pass

        return summary
