"""End-to-end integration and failure boundary tests for apps/rwa_observer CLI (C28, C31)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


def test_cli_e2e_normal_flow(tmp_path: Path) -> None:
    """Verify normal CLI execution on valid e2e bundle produces deterministic artifacts."""
    fixtures_dir = Path(__file__).resolve().parent.parent / "fixtures" / "rwa" / "v1"
    e2e_input = fixtures_dir / "e2e.json"
    assert e2e_input.exists()

    out_dir = tmp_path / "run_success"
    cmd = [
        sys.executable,
        "apps/rwa_observer.py",
        "--input",
        str(e2e_input),
        "--as-of-ms",
        "1700000005000",
        "--output",
        str(out_dir),
    ]

    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert proc.returncode == 0
    assert "Successfully processed 1 records" in proc.stdout

    # Verify generated artifacts
    report_file = out_dir / "report.md"
    jsonl_file = out_dir / "records.jsonl"
    manifest_file = out_dir / "manifest.json"

    assert report_file.exists()
    assert jsonl_file.exists()
    assert manifest_file.exists()

    # Verify report contents
    rep_text = report_file.read_text(encoding="utf-8")
    assert "rec:e2e:nvda:001" in rep_text
    assert "NVDA" in rep_text
    assert "candidate_for_w2_validation" in rep_text

    # Verify manifest data
    manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
    assert manifest_data["format"] == "w7-rwa-run-manifest-v1"
    assert manifest_data["records_count"] == 1
    assert len(manifest_data["artifacts"]) == 2


def test_c28_empty_or_whitespace_input_rejected_with_exit_2(tmp_path: Path) -> None:
    """C28: Empty or whitespace-only input file must fail-closed with exit code 2."""
    empty_file = tmp_path / "empty.json"
    empty_file.write_text("   \n\t  ", encoding="utf-8")

    out_dir = tmp_path / "out_empty"
    cmd = [
        sys.executable,
        "apps/rwa_observer.py",
        "--input",
        str(empty_file),
        "--as-of-ms",
        "1700000005000",
        "--output",
        str(out_dir),
    ]

    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert proc.returncode == 2
    assert "empty or contains only whitespace" in proc.stderr
    assert not out_dir.exists() or len(list(out_dir.iterdir())) == 0


def test_c28_corrupt_json_or_invalid_format_rejected(tmp_path: Path) -> None:
    """C28: Corrupt JSON or invalid bundle format exits with code 2."""
    corrupt_file = tmp_path / "corrupt.json"
    corrupt_file.write_text("{invalid_json_here", encoding="utf-8")

    out_dir = tmp_path / "out_corrupt"
    cmd = [
        sys.executable,
        "apps/rwa_observer.py",
        "--input",
        str(corrupt_file),
        "--as-of-ms",
        "1700000005000",
        "--output",
        str(out_dir),
    ]

    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert proc.returncode == 2
    assert "invalid JSON" in proc.stderr


def test_c31_as_of_future_data_rejection(tmp_path: Path) -> None:
    """C31: If any record has as_of_ms in the future relative to CLI as-of, fail-closed exit 2."""
    future_bundle = tmp_path / "future.json"
    payload = {
        "format": "w7-rwa-e2e-bundle-v1",
        "records": [
            {
                "schema_id": "w7-rwa-research",
                "schema_version": "0.1.0-draft",
                "record_id": "rec:future:001",
                "is_draft": True,
                "as_of_ms": 1700000010000,  # in the future
                "observed_at_ms": 1700000010000,
                "instrument": {
                    "token_key": {"chain_id": 4663, "address": "0x1111111111111111111111111111111111111111"},
                    "issuer_id": "rhj",
                    "underlier_id": "NVDA",
                    "feed_address": "0x2222222222222222222222222222222222222222",
                    "quote_currency": "USD",
                    "token_decimals": 18,
                    "feed_decimals": 8,
                },
                "quotes": [],
            }
        ],
    }
    future_bundle.write_text(json.dumps(payload), encoding="utf-8")

    out_dir = tmp_path / "out_future"
    # Run with CLI as-of = 1700000005000 (earlier than record as-of 1700000010000)
    cmd = [
        sys.executable,
        "apps/rwa_observer.py",
        "--input",
        str(future_bundle),
        "--as-of-ms",
        "1700000005000",
        "--output",
        str(out_dir),
    ]

    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert proc.returncode == 2
    assert "in the future relative to CLI as-of" in proc.stderr
