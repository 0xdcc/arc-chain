"""Independent End-to-End Test Suite for Arc Collect CLI (T44 / G1).

Verifies the full ingest pipeline from CLI invocation to durable on-disk artifacts:
- apps/arc_collect.py entrypoint execution and stdout/stderr contract
- raw_envelopes.jsonl format, schema version, and contract fidelity
- coverage_manifest.json completeness, block counts, and coverage ratio
- cursor.json monotonic advancement and crash safety
- Parameter validation, fail-closed error handling, and security bounds
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import apps.arc_collect
from arbitrage_contracts.arc_extensions import BlockDomain
from arc_runtime.collect import CollectorConfig, execute_collection


class TestArcCollectCliE2E:
    """End-to-end tests for arc_collect CLI interface and runtime contracts."""

    def test_cli_entrypoint_fixture_mode_success(self, tmp_path: Path) -> None:
        """Verify apps/arc_collect.py main() runs with CLI args and outputs valid JSON."""
        output_dir = tmp_path / "ingest_cli_e2e"
        test_args = [
            "arc_collect.py",
            "--chain-id",
            "5042",
            "--from-block",
            "1000",
            "--to-block",
            "1005",
            "--output-dir",
            str(output_dir),
            "--fixture-mode",
        ]

        stdout_buf = io.StringIO()
        with patch.object(sys, "argv", test_args), patch("sys.stdout", stdout_buf):
            with pytest.raises(SystemExit) as exc_info:
                apps.arc_collect.main()
            assert exc_info.value.code == 0

        stdout_text = stdout_buf.getvalue()
        output = json.loads(stdout_text)
        assert output["status"] == "SUCCESS"
        assert output["chain_id"] == 5042
        assert output["range"] == [1000, 1005]
        assert output["envelopes_written"] == 6
        assert output["is_fixture_mode"] is True

        # Check files exist on disk
        envelopes_file = output_dir / "raw_envelopes.jsonl"
        manifest_file = output_dir / "coverage_manifest.json"
        cursor_file = output_dir / "cursor.json"

        assert envelopes_file.exists()
        assert manifest_file.exists()
        assert cursor_file.exists()

        # Check raw envelopes content
        lines = envelopes_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 6
        for idx, line in enumerate(lines):
            record = json.loads(line)
            assert record["chain_id"] == 5042
            assert record["block_domain"] == "l1"
            assert record["block_number"] == 1000 + idx
            assert record["block_hash"] == f"0x{1000 + idx:064x}"
            assert record["payload_type"] == "block_summary"
            assert record["schema_version"] == "1.0"
            raw_payload = json.loads(record["raw_payload"])
            assert raw_payload["number"] == 1000 + idx
            assert raw_payload["data_mode"] == "SYNTHETIC_FIXTURE"

        # Check manifest
        with open(manifest_file, encoding="utf-8") as f:
            manifest_data = json.load(f)
        assert manifest_data["chain_id"] == 5042
        assert manifest_data["expected_blocks"] == 6
        assert manifest_data["covered_blocks"] == 6
        assert manifest_data["missing_blocks"] == []
        assert manifest_data["coverage_ratio"] == 1.0

        # Check cursor
        with open(cursor_file, encoding="utf-8") as f:
            cursor_data = json.load(f)
        assert cursor_data["chain_id"] == 5042
        assert cursor_data["last_block"] == 1005
        assert cursor_data["last_cursor"] == "cur_5042_1005"

    def test_cursor_monotonic_continuation(self, tmp_path: Path) -> None:
        """Verify sequential ingest runs monotonically advance the cursor without gaps."""
        output_dir = tmp_path / "ingest_continuation"

        # Run 1: blocks 200..204
        config1 = CollectorConfig(
            chain_id=5042,
            block_domain=BlockDomain.L1,
            from_block=200,
            to_block=204,
            output_dir=output_dir,
            fixture_mode=True,
        )
        summary1 = execute_collection(config1)
        assert summary1.envelopes_written == 5

        cursor_file = output_dir / "cursor.json"
        with open(cursor_file, encoding="utf-8") as f:
            c1 = json.load(f)
        assert c1["last_block"] == 204
        assert c1["last_cursor"] == "cur_5042_204"

        # Run 2: blocks 205..209
        config2 = CollectorConfig(
            chain_id=5042,
            block_domain=BlockDomain.L1,
            from_block=205,
            to_block=209,
            output_dir=output_dir,
            fixture_mode=True,
        )
        summary2 = execute_collection(config2)
        assert summary2.envelopes_written == 5

        with open(cursor_file, encoding="utf-8") as f:
            c2 = json.load(f)
        assert c2["last_block"] == 209
        assert c2["last_cursor"] == "cur_5042_209"
        assert c2["last_block"] > c1["last_block"]

    def test_cli_invalid_arguments_fail_closed(self, tmp_path: Path) -> None:
        """Verify invalid CLI parameters produce exit code 2 and structured error on stderr."""
        output_dir = tmp_path / "ingest_errors"

        # 1. Invalid chain_id (e.g. Robinhood 4663)
        args_invalid_chain = [
            "arc_collect.py",
            "--chain-id",
            "4663",
            "--from-block",
            "100",
            "--to-block",
            "105",
            "--output-dir",
            str(output_dir),
            "--fixture-mode",
        ]
        stderr_buf = io.StringIO()
        with patch.object(sys, "argv", args_invalid_chain), patch("sys.stderr", stderr_buf):
            with pytest.raises(SystemExit) as exc_info:
                apps.arc_collect.main()
            assert exc_info.value.code == 2
        err_output = json.loads(stderr_buf.getvalue())
        assert err_output["status"] == "ERROR"
        assert "Invalid chain_id: 4663" in err_output["message"]

        # 2. Inverted range (from_block > to_block)
        args_inverted = [
            "arc_collect.py",
            "--chain-id",
            "5042",
            "--from-block",
            "500",
            "--to-block",
            "400",
            "--output-dir",
            str(output_dir),
            "--fixture-mode",
        ]
        stderr_buf = io.StringIO()
        with patch.object(sys, "argv", args_inverted), patch("sys.stderr", stderr_buf):
            with pytest.raises(SystemExit) as exc_info:
                apps.arc_collect.main()
            assert exc_info.value.code == 2
        err_output = json.loads(stderr_buf.getvalue())
        assert err_output["status"] == "ERROR"
        assert "cannot exceed" in err_output["message"]

        # 3. Excessive batch size (> 1000 blocks)
        args_oversize = [
            "arc_collect.py",
            "--chain-id",
            "5042",
            "--from-block",
            "1",
            "--to-block",
            "2000",
            "--output-dir",
            str(output_dir),
            "--fixture-mode",
        ]
        stderr_buf = io.StringIO()
        with patch.object(sys, "argv", args_oversize), patch("sys.stderr", stderr_buf):
            with pytest.raises(SystemExit) as exc_info:
                apps.arc_collect.main()
            assert exc_info.value.code == 2
        err_output = json.loads(stderr_buf.getvalue())
        assert err_output["status"] == "ERROR"
        assert "Max block batch range is 1000" in err_output["message"]

    def test_live_mode_without_authorization_fails_closed(self, tmp_path: Path) -> None:
        """Verify live collection mode fails closed when RPC endpoint is unauthorized or missing."""
        output_dir = tmp_path / "ingest_unauth"

        # Missing rpc_endpoint in live mode
        with pytest.raises(ValueError, match="Real collection requires an explicit rpc_endpoint"):
            CollectorConfig(
                chain_id=5042,
                block_domain=BlockDomain.L1,
                from_block=100,
                to_block=105,
                output_dir=output_dir,
                fixture_mode=False,
                rpc_endpoint=None,
            )

        # Real RPC live collection requires G1_LIVE explicit authorization
        config = CollectorConfig(
            chain_id=5042,
            block_domain=BlockDomain.L1,
            from_block=100,
            to_block=105,
            output_dir=output_dir,
            fixture_mode=False,
            rpc_endpoint="https://rpc.arc.io",
        )
        with pytest.raises(PermissionError, match="G1_LIVE"):
            execute_collection(config)
