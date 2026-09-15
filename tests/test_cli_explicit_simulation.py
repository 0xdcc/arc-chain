"""Tests for explicit-input CLI simulation entrypoint (apps/cli.py).

Verifies:
1. Missing --input flag returns exit code 1 (INPUT_INSUFFICIENT) without fabricating default plans.
2. Offline simulation strictly rejects RPC endpoints or private key arguments.
3. Missing or malformed input files return exit code 2 with descriptive errors.
4. Live trade execution without --dry-run is intercepted before ledger, key loading, or RPC.
5. Explicit synthetic stream runs through real offline pipeline and exports verified summary/JSONL.
6. Summary reflects actual execution breakdown (not all success/realized).
7. Negative control: legacy _build_default_execution_plan is never called.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from apps.cli import AuthorizationBlockedError, main

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = str(REPO_ROOT / "tests" / "fixtures" / "atomic_execution" / "v1" / "e2e-stream.jsonl")


class TestExplicitInputValidation:
    """Verifies input requirements and security checks for simulation commands."""

    def test_simulate_without_input_flag_fails_with_code_1(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """cmd_simulate without --input fails with exit code 1 and INPUT_INSUFFICIENT message."""
        exit_code = main(["simulate"])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "[INPUT_INSUFFICIENT]" in captured.err
        assert "Explicit --input JSONL is required" in captured.err

    def test_trade_dry_run_without_input_flag_fails_with_code_1(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """cmd_trade --dry-run without --input fails with exit code 1."""
        exit_code = main(["trade", "--dry-run"])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "[INPUT_INSUFFICIENT]" in captured.err

    def test_simulate_rejects_rpc_endpoint(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Offline simulation rejects explicit --rpc parameter."""
        exit_code = main(
            ["simulate", "--input", FIXTURE_PATH, "--rpc", "http://127.0.0.1:8545"]
        )
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "[SECURITY INTERCEPT]" in captured.err
        assert "Offline simulation does not accept RPC endpoints" in captured.err

    def test_trade_dry_run_rejects_private_key(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Offline trade dry-run rejects explicit private key."""
        exit_code = main(
            [
                "trade",
                "--dry-run",
                "--input",
                FIXTURE_PATH,
                "--private-key",
                "0x" + "aa" * 32,
            ]
        )
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "[SECURITY INTERCEPT]" in captured.err
        assert "Offline simulation does not accept RPC endpoints or private keys" in captured.err


class TestLiveTradeSafetyGuards:
    """Verifies trade live guardrails prevent execution without dry-run."""

    def test_trade_live_blocked_dry_run_required(
        self, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        """Trade without --dry-run raises AuthorizationBlockedError and exits 1."""
        ledger_file = tmp_path / "never_created.sqlite"
        exit_code = main(["trade", "--input", FIXTURE_PATH, "--ledger-db", str(ledger_file)])
        assert exit_code == 1
        assert not ledger_file.exists()
        captured = capsys.readouterr()
        assert "Live trade execution blocked: --dry-run is mandatory" in captured.err

    def test_trade_live_require_key_blocked_when_absent(
        self, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        """Trade with --require-key but missing key raises AuthorizationBlockedError."""
        ledger_file = tmp_path / "never_created_2.sqlite"
        exit_code = main(
            [
                "trade",
                "--dry-run",
                "--require-key",
                "--input",
                FIXTURE_PATH,
                "--ledger-db",
                str(ledger_file),
            ]
        )
        assert exit_code == 1
        assert not ledger_file.exists()
        captured = capsys.readouterr()
        assert "no authorized private key credentials provided" in captured.err


class TestInputFileErrors:
    """Verifies error handling on missing and malformed input files."""

    def test_missing_input_file_returns_code_2(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Non-existent file path results in exit code 2 from pipeline input gate."""
        exit_code = main(["simulate", "--input", "/tmp/non_existent_fixture_998877.jsonl"])
        assert exit_code == 2
        captured = capsys.readouterr()
        assert "INPUT ERROR: Input file not found" in captured.err

    def test_malformed_input_file_returns_code_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Malformed JSON lines in input file trigger exit code 2."""
        bad_file = tmp_path / "malformed.jsonl"
        bad_file.write_text("invalid json content\n{not valid}\n", encoding="utf-8")
        exit_code = main(["simulate", "--input", str(bad_file)])
        assert exit_code == 2
        captured = capsys.readouterr()
        assert "INPUT ERROR" in captured.err


class TestRealOfflinePipelineExecution:
    """Verifies end-to-end execution against real offline pipeline with synthetic fixture."""

    def test_simulate_real_fixture_produces_accurate_summary(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Simulate consumes e2e-stream.jsonl through real pipeline, exporting accurate results."""
        out_jsonl = tmp_path / "sim_out.jsonl"
        sum_json = tmp_path / "sim_sum.json"

        exit_code = main(
            [
                "simulate",
                "--input",
                FIXTURE_PATH,
                "--output",
                str(out_jsonl),
                "--summary-output",
                str(sum_json),
            ]
        )
        assert exit_code == 0
        assert out_jsonl.exists()
        assert sum_json.exists()

        summary: dict[str, Any] = json.loads(sum_json.read_text(encoding="utf-8"))
        assert summary["total_processed"] == 12
        assert summary["passed_count"] == 3
        assert summary["rejected_count"] == 9
        assert summary["simulated_success_count"] == 3
        assert summary["profitable_count"] == 0
        assert summary["is_conserved"] is True

        # Confirm non-trivial breakdown: not every row is success/realized
        breakdown: dict[str, int] = summary["stage_breakdown"]
        assert breakdown["SIMULATION_SUCCEEDED"] == 3
        assert breakdown["SIMULATION_FAILED"] == 3
        assert breakdown["PLANNING_REJECTED"] == 2
        assert breakdown["INPUT_GATE_REJECTED"] == 4

        # Verify exported records line count
        lines = [line.strip() for line in out_jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(lines) == 12

    def test_trade_dry_run_real_fixture_matches_pipeline(
        self, tmp_path: Path
    ) -> None:
        """Trade dry-run delegates to real pipeline identically."""
        out_jsonl = tmp_path / "trade_out.jsonl"
        sum_json = tmp_path / "trade_sum.json"

        exit_code = main(
            [
                "trade",
                "--dry-run",
                "--input",
                FIXTURE_PATH,
                "--output",
                str(out_jsonl),
                "--summary-output",
                str(sum_json),
                "--caller",
                "0x39dBED3a2bd333467115dE45665cC57F813C4571",
            ]
        )
        assert exit_code == 0
        summary: dict[str, Any] = json.loads(sum_json.read_text(encoding="utf-8"))
        assert summary["total_processed"] == 12
        assert summary["passed_count"] == 3
        assert summary["simulated_success_count"] == 3


class TestNegativeControlLegacyPlanBuilder:
    """Negative control: ensures legacy _build_default_execution_plan is never called."""

    def test_legacy_builder_is_never_called(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Legacy _build_default_execution_plan must never be invoked by simulate or trade."""
        mock_called = False

        def _poisoned_builder(*args: Any, **kwargs: Any) -> Any:
            nonlocal mock_called
            mock_called = True
            raise RuntimeError("NEGATIVE CONTROL TRIPPED: legacy default plan builder called!")

        monkeypatch.setattr("apps.cli._build_default_execution_plan", _poisoned_builder)

        # 1. Missing input simulation rejection
        ret_no_input = main(["simulate"])
        assert ret_no_input == 1
        assert not mock_called

        # 2. Live trade rejection
        ret_live_trade = main(["trade"])
        assert ret_live_trade == 1
        assert not mock_called

        # 3. Real input simulation
        out_jsonl = tmp_path / "test_poison.jsonl"
        ret_sim = main(["simulate", "--input", FIXTURE_PATH, "--output", str(out_jsonl)])
        assert ret_sim == 0
        assert not mock_called

        # 4. Real input trade dry-run
        ret_trade = main(["trade", "--dry-run", "--input", FIXTURE_PATH, "--output", str(out_jsonl)])
        assert ret_trade == 0
        assert not mock_called
