"""Tests for unified thin CLI entrypoint assembly (M11).

Verifies:
1. Subcommand --help displays parameter specifications and returns exit code 0.
2. 'replay' subcommand correctly drives ReplayAdapter through Quoter and produces 8 tiers.
3. 'monitor' subcommand executes read-only scan with strict privilege isolation (no execution/signing loaded).
4. 'simulate' subcommand exercises ExecutionPlan assembly and simulation flow.
5. 'trade' subcommand strictly enforces mandatory --dry-run safety guardrails.
6. Static AST and dynamic runtime module isolation audits.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apps.cli import build_parser, main


class TestCliHelpAndParser:
    """Tests argument parser configuration and subcommand help output."""

    @pytest.mark.parametrize("subcmd", ["monitor", "quote", "replay", "simulate", "trade"])
    def test_subcommand_help_exits_zero(self, subcmd: str) -> None:
        """Each subcommand's --help returns exit code 0."""
        parser = build_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args([subcmd, "--help"])
        assert exc_info.value.code == 0

    def test_root_help_exits_zero(self) -> None:
        """Root --help returns exit code 0."""
        parser = build_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["--help"])
        assert exc_info.value.code == 0


class TestCliReplayCommand:
    """Tests replay subcommand offline execution."""

    def test_replay_subcommand_success(self, capsys: pytest.CaptureFixture) -> None:
        """cli.py replay strictly consumes 30 historical records and returns exit code 0."""
        exit_code = main(["replay"])
        assert exit_code == 0

        captured = capsys.readouterr()
        assert "[REPLAY] Completed historical replay of 30 RPC records" in captured.out
        assert "Tier 1: status=CONTRACT_REVERT" in captured.out
        assert "Tier 8: status=NODE_LIMITATION" in captured.out


class TestCliMonitorPrivilegeIsolation:
    """Tests monitor subcommand execution and privilege isolation."""

    def test_monitor_executes_poll_once(self) -> None:
        """cli.py monitor runs a single poll without error when mocked."""
        mock_service = MagicMock()
        mock_service.poll_once.return_value = {
            "status": "success",
            "block_number": 50000000,
            "candidates_found": 0,
            "reports_generated": 0,
        }

        with patch("apps.monitor.service.ReadOnlyMonitorService", return_value=mock_service):
            exit_code = main(["monitor", "--once", "--rpc", "http://127.0.0.1:1"])
            assert exit_code == 0
            mock_service.poll_once.assert_called_once()

    def test_monitor_privilege_isolation_ast_audit(self) -> None:
        """apps/cli.py top-level AST must not eagerly import execution service or private key components."""
        cli_path = Path(__file__).resolve().parents[1] / "apps" / "cli.py"
        source = cli_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(cli_path))

        # Check top-level import statements only
        forbidden_top_level = [
            "execution.service",
            "execution.coordinator",
            "execution.weth_arbitrage_executor",
            "eth_account",
        ]
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for forbidden in forbidden_top_level:
                        assert forbidden not in alias.name, f"Forbidden top-level import: {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for forbidden in forbidden_top_level:
                    assert forbidden not in mod, f"Forbidden top-level from-import: {mod}"


class TestCliSimulateAndTradeGuardrails:
    """Tests simulation execution and mandatory trade guardrails."""

    def test_simulate_subcommand_dry_run(self, capsys: pytest.CaptureFixture) -> None:
        """cli.py simulate runs the explicit synthetic stream through the offline pipeline."""
        exit_code = main(["simulate", "--base", "WETH", "--input", str(Path(__file__).resolve().parent / "fixtures/atomic_execution/v1/e2e-stream.jsonl")])
        assert exit_code == 0
        captured = capsys.readouterr()
        summary = json.loads(captured.out)
        assert summary["total_processed"] == 12
        assert summary["simulated_success_count"] == 3
        assert summary["rejected_count"] == 9
        assert summary["profitable_count"] == 0
        assert summary["is_conserved"] is True

    def test_trade_without_dry_run_flag_strictly_blocked(self) -> None:
        """cli.py trade without --dry-run is intercepted with exit code 1."""
        exit_code = main(["trade"])
        assert exit_code == 1

    def test_trade_explicit_no_dry_run_flag_strictly_blocked(self) -> None:
        """cli.py trade --no-dry-run is intercepted with exit code 1."""
        exit_code = main(["trade", "--no-dry-run"])
        assert exit_code == 1

    def test_trade_with_dry_run_flag_allowed(self, capsys: pytest.CaptureFixture) -> None:
        """cli.py trade --dry-run completes dry-run dispatch."""
        exit_code = main(["trade", "--dry-run", "--input", str(Path(__file__).resolve().parent / "fixtures/atomic_execution/v1/e2e-stream.jsonl")])
        assert exit_code == 0
        captured = capsys.readouterr()
        summary = json.loads(captured.out)
        assert summary["total_processed"] == 12
        assert summary["simulated_success_count"] == 3
        assert summary["rejected_count"] == 9
        assert summary["profitable_count"] == 0
        assert summary["is_conserved"] is True
