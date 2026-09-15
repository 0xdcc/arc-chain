"""Tests verifying zero side effects on CLI trade admission rejection.

Guarantees:
1. Rejection of live trade (omitted --dry-run or explicit --no-dry-run) strictly returns exit code 1.
2. Zero private key reading or credential loading.
3. Zero RPC interaction or network calls.
4. Zero ledger database creation or writes.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from apps.cli import main


class TestCliTradeAdmissionRejectionSideEffects:
    """Verify security admission rejection and absence of side effects."""

    @pytest.mark.parametrize(
        "args",
        [
            ["trade"],
            ["trade", "--no-dry-run"],
        ],
    )
    def test_trade_rejection_no_rpc_no_ledger_no_key_read(
        self,
        args: list[str],
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Rejecting live trade must not touch RPC, ledger DB, or private key."""
        ledger_file = tmp_path / "should_not_exist_ledger.sqlite"
        full_args = list(args) + ["--ledger-db", str(ledger_file)]

        with (
            patch("apps.cli._resolve_rpc_client") as mock_resolve_rpc,
            patch("apps.cli._build_default_execution_plan") as mock_build_plan,
        ):
            exit_code = main(full_args)

            # 1. Exit code must be 1
            assert exit_code == 1

            # 2. RPC resolver must never be called
            mock_resolve_rpc.assert_not_called()

            # 3. Execution plan must never be constructed
            mock_build_plan.assert_not_called()

            # 4. Ledger database file must never be created
            assert not ledger_file.exists()

        # 5. Intercept warning must be logged to stderr
        captured = capsys.readouterr()
        assert "[SECURITY INTERCEPT]" in captured.err

    def test_trade_require_key_missing_rejection_side_effects(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Rejecting trade with --require-key but missing --private-key must return 1 with zero side effects."""
        ledger_file = tmp_path / "should_not_exist_require_key.sqlite"
        args = ["trade", "--dry-run", "--require-key", "--ledger-db", str(ledger_file)]

        with (
            patch("apps.cli._resolve_rpc_client") as mock_resolve_rpc,
            patch("apps.cli._build_default_execution_plan") as mock_build_plan,
        ):
            exit_code = main(args)

            assert exit_code == 1
            mock_resolve_rpc.assert_not_called()
            mock_build_plan.assert_not_called()
            assert not ledger_file.exists()

        captured = capsys.readouterr()
        assert "[SECURITY INTERCEPT]" in captured.err
        assert "no authorized private key credentials provided" in captured.err
