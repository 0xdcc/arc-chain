"""End-to-end integration tests for state_graph_shadow CLI and replay pipeline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.state_graph_shadow import main, run_shadow_replay


def test_shadow_replay_e2e_pipeline(tmp_path: Path) -> None:
    fixture_path = (
        Path(__file__).resolve().parent.parent / "fixtures" / "state_graph" / "v2" / "e2e.json"
    )
    assert fixture_path.is_file()

    out_dir = tmp_path / "shadow_out"

    summary = run_shadow_replay(
        manifest_path=str(fixture_path),
        registry_path=str(fixture_path),
        output_dir_str=str(out_dir),
        amounts=[1000, 10000],
    )

    assert summary["status"] == "success"
    assert summary["total_routes_discovered"] == 2
    assert summary["total_quotes_evaluated"] == 4
    assert summary["successful_quotes"] == 4

    quotes_file = out_dir / "quotes.jsonl"
    assert quotes_file.is_file()
    lines = [line.strip() for line in quotes_file.read_text().splitlines() if line.strip()]
    assert len(lines) == 4

    for line in lines:
        record = json.loads(line)
        assert record["schema_id"] == "arbitrage-evidence"
        assert record["record_type"] == "quote_evidence"
        assert record["payload"]["status"] == "quoted"
        assert record["payload"]["delta_atoms"] is not None

    summary_file = out_dir / "summary.json"
    assert summary_file.is_file()
    summary_data = json.loads(summary_file.read_text())
    assert summary_data["total_quotes_evaluated"] == 4


def test_shadow_cli_empty_or_missing_file_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. Missing file
    monkeypatch.setattr(
        "sys.argv",
        [
            "state_graph_shadow",
            "--input-manifest",
            str(tmp_path / "non_existent.json"),
            "--registry",
            str(tmp_path / "non_existent.json"),
            "--output-dir",
            str(tmp_path / "out"),
        ],
    )
    exit_code = main()
    assert exit_code == 2

    # 2. Empty file (0 bytes)
    empty_file = tmp_path / "empty.json"
    empty_file.write_text("")
    monkeypatch.setattr(
        "sys.argv",
        [
            "state_graph_shadow",
            "--input-manifest",
            str(empty_file),
            "--registry",
            str(empty_file),
            "--output-dir",
            str(tmp_path / "out"),
        ],
    )
    exit_code = main()
    assert exit_code == 2


def test_shadow_cli_clean_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture_path = (
        Path(__file__).resolve().parent.parent / "fixtures" / "state_graph" / "v2" / "e2e.json"
    )
    out_dir = tmp_path / "cli_out"

    monkeypatch.setattr(
        "sys.argv",
        [
            "state_graph_shadow",
            "--input-manifest",
            str(fixture_path),
            "--registry",
            str(fixture_path),
            "--output-dir",
            str(out_dir),
            "--amounts",
            "500,2500",
        ],
    )
    exit_code = main()
    assert exit_code == 0
    assert (out_dir / "quotes.jsonl").is_file()
    assert (out_dir / "summary.json").is_file()
