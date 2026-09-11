"""Real subprocess end-to-end coverage for the W2 shadow CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from opportunities.store import AppendOnlyLedger

REPO_ROOT = Path(__file__).parents[2]
MANIFEST = REPO_ROOT / "tests" / "fixtures" / "opportunities" / "v1" / "shadow-manifest.json"


def test_real_subprocess_replay_produces_readable_ledger(tmp_path: Path) -> None:
    """The CLI writes a hash-chained ledger and an explicit simulation boundary."""
    output_root = tmp_path / "shadow-output"
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "apps.arb_shadow",
            "--mode",
            "replay",
            "--input",
            str(MANIFEST),
            "--output-root",
            str(output_root),
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True,
        text=True,
        check=True,
    )
    assert process.returncode == 0
    outcome = json.loads((output_root / "outcome.json").read_text(encoding="utf-8"))
    assert outcome["sim_available"] is False
    assert outcome["candidate_count"] == 1
    ledger = AppendOnlyLedger(output_root / "ledger.jsonl")
    snapshot = ledger.load()
    assert snapshot.confirmed_sequence >= 1
    payloads = snapshot.events
    assert any(payload["schema_id"] == "w2-shadow-event-v1" for payload in payloads)
    assert all(
        payload["sim_available"] is False for payload in payloads if "sim_available" in payload
    )
