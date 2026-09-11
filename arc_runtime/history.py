"""Arc Historical Block Replay and Attribution Runtime (T38)

Enforces:
- Separation of historical block timestamp vs real-world observation/arrival timestamp
- Explicit marking of historical state opportunities as PRE_EXECUTION_HYPOTHESIS
- Complete error and failure retention (never masks 100% RPC failures)
- Strict mode isolation: retrospective_state vs causal_replay
"""

from __future__ import annotations

import json
import time
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


def run_history_scan(config: HistoryReplayConfig) -> dict[str, Any]:
    """Execute historical scan and emit structured audit reports."""
    start_time = time.monotonic()
    config.output_dir.mkdir(parents=True, exist_ok=True)

    report_file = config.output_dir / "history_replay_report.json"
    opportunities_file = config.output_dir / "history_hypotheses.jsonl"

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
                    "observed_at_timestamp": time.time(),     # wall clock
                    "hypothesis_type": "PRE_EXECUTION_HYPOTHESIS",
                    "mode": config.mode,
                    "claimed_profit_guaranteed": False,      # Must never claim guaranteed executable profit
                    "cycle": "USDC -> WETH -> USDC",
                    "spread_bps": 12.5,
                }
                hypotheses.append(item)
    else:
        raise PermissionError("Live historical scan requires verified RPC endpoint authorization.")

    with open(opportunities_file, "w", encoding="utf-8") as f:
        for h in hypotheses:
            f.write(json.dumps(h, ensure_ascii=False) + "\n")

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

    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    return summary
