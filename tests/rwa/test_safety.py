"""Security, manifest reconciliation, and isolation tests for W7 harness (C29, C30, C32)."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from arbitrage_contracts import TokenKey
from rwa_research import InstrumentBinding, RwaResearchRecord
from tools.w7_offline_check import reconcile_manifest


def test_c30_manifest_reconciliation_exact_and_injection(tmp_path: Path) -> None:
    """C30: Manifest double-entry reconciliation passes for expected files, and fails on rogue files."""
    # Test valid set
    expected_files = {
        "apps/rwa_observer.py",
        "rwa_research/__init__.py",
        "rwa_research/models.py",
        "rwa_research/codec.py",
        "rwa_research/normalize.py",
        "rwa_research/validity.py",
        "rwa_research/quote_inputs.py",
        "rwa_research/classify.py",
        "rwa_research/report.py",
        "tests/rwa/conftest.py",
        "tests/rwa/test_models_codec.py",
        "tests/rwa/test_normalize.py",
        "tests/rwa/test_validity.py",
        "tests/rwa/test_quote_inputs.py",
        "tests/rwa/test_classify.py",
        "tests/rwa/test_cli_e2e.py",
        "tests/rwa/test_safety.py",
        "tests/fixtures/rwa/v1/README.md",
        "tests/fixtures/rwa/v1/manifest.json",
        "tests/fixtures/rwa/v1/models.json",
        "tests/fixtures/rwa/v1/normalize.json",
        "tests/fixtures/rwa/v1/validity.json",
        "tests/fixtures/rwa/v1/quotes.json",
        "tests/fixtures/rwa/v1/e2e.json",
        "tools/w7_offline_check.py",
        "tools/w7_sabotage.py",
    }
    ok, msg = reconcile_manifest(expected_files)
    assert ok is True
    assert "1:1 bidirectional match confirmed" in msg

    # Missing a file in expected set
    incomplete_set = set(expected_files) - {"apps/rwa_observer.py"}
    ok_missing, msg_missing = reconcile_manifest(incomplete_set)
    assert ok_missing is False
    assert "unregistered_on_disk" in msg_missing

    # Ghost file in expected set that does not exist on disk
    ghost_set = set(expected_files) | {"rwa_research/ghost_file.py"}
    ok_ghost, msg_ghost = reconcile_manifest(ghost_set)
    assert ok_ghost is False
    assert "missing_on_disk" in msg_ghost


def test_c32_confirmed_execution_mode_strictly_forbidden() -> None:
    """C32: Attempting to mark draft research records as confirmed_execution must fail-closed."""
    inst = InstrumentBinding(
        token_key=TokenKey(4663, "0x1111111111111111111111111111111111111111"),
        issuer_id="rhj",
        underlier_id="NVDA",
        feed_address="0x2222222222222222222222222222222222222222",
        quote_currency="USD",
        token_decimals=18,
        feed_decimals=8,
    )

    with pytest.raises(ValueError, match="data_mode cannot be 'confirmed_execution'"):
        RwaResearchRecord(
            record_id="rec:tamper:001",
            instrument=inst,
            as_of_ms=1700000000000,
            observed_at_ms=1700000001000,
            data_mode="confirmed_execution",  # Unauthorized escalation
        )
