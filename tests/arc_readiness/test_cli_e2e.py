"""End-to-end and fail-closed CLI integration tests (C28)."""

from __future__ import annotations

import json
from pathlib import Path

from apps.arc_market_catalog import main as catalog_main
from apps.arc_readiness import main as readiness_main

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "arc_readiness" / "e2e_stream.json"
)


# ==============================================================================
# C28: CLI Execution and Fail-Closed Guard Verification
# ==============================================================================


def test_c28_readiness_cli_success(tmp_path: Path) -> None:
    """C28: arc_readiness CLI executes with exit code 0 and produces valid JSON."""
    out_file = tmp_path / "readiness_out.json"
    exit_code = readiness_main(
        ["--offline", "--input", str(FIXTURE_PATH), "--output", str(out_file)]
    )
    assert exit_code == 0
    assert out_file.exists()

    report = json.loads(out_file.read_text(encoding="utf-8"))
    assert report["status"] == "READINESS_OBSERVED"
    assert report["network_identity"]["chain_id"] == 5042002
    assert report["summary"]["total_balance_observations"] == 1
    assert report["summary"]["consistent_balance_observations"] == 1


def test_c28_catalog_cli_success(tmp_path: Path) -> None:
    """C28: arc_market_catalog CLI executes with exit code 0 and produces catalog JSON."""
    out_file = tmp_path / "catalog_out.json"
    exit_code = catalog_main(["--offline", "--input", str(FIXTURE_PATH), "--output", str(out_file)])
    assert exit_code == 0
    assert out_file.exists()

    report = json.loads(out_file.read_text(encoding="utf-8"))
    assert report["status"] == "MARKETS_AVAILABLE"
    assert report["total_assets"] == 2
    assert report["active_markets_count"] == 1


def test_c28_cli_fail_closed_nonexistent_input(tmp_path: Path) -> None:
    """C28: Missing input file returns exit code 2."""
    fake_input = tmp_path / "does_not_exist.json"
    out_file = tmp_path / "out.json"

    assert readiness_main(["--input", str(fake_input), "--output", str(out_file)]) == 2
    assert catalog_main(["--input", str(fake_input), "--output", str(out_file)]) == 2


def test_c28_cli_fail_closed_empty_input(tmp_path: Path) -> None:
    """C28: Zero-byte empty input file returns exit code 2."""
    empty_input = tmp_path / "empty.json"
    empty_input.write_text("", encoding="utf-8")
    out_file = tmp_path / "out.json"

    assert readiness_main(["--input", str(empty_input), "--output", str(out_file)]) == 2
    assert catalog_main(["--input", str(empty_input), "--output", str(out_file)]) == 2


def test_c28_cli_fail_closed_corrupted_json(tmp_path: Path) -> None:
    """C28: Corrupted syntax in input file returns exit code 2."""
    corrupted_input = tmp_path / "corrupted.json"
    corrupted_input.write_text('{"network_identity": {broken syntax', encoding="utf-8")
    out_file = tmp_path / "out.json"

    assert readiness_main(["--input", str(corrupted_input), "--output", str(out_file)]) == 2
    assert catalog_main(["--input", str(corrupted_input), "--output", str(out_file)]) == 2
