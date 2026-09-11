"""Unit coverage for fail-closed local shadow report aggregation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from opportunities.report import build_report_from_ledger, render_markdown
from opportunities.store import AppendOnlyLedger, LedgerCorruptionError

REPO_ROOT = Path(__file__).resolve().parents[2]
ALL_NEGATIVE = (
    REPO_ROOT / "tests" / "fixtures" / "opportunities" / "v1" / "all-negative-ledger.jsonl"
)


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_report_reconciles_all_negative_ledger(tmp_path: Path) -> None:
    """All-negative complete input exits at the business layer with no profit aggregate."""
    payload = build_report_from_ledger(ALL_NEGATIVE)
    raw = payload.to_json_dict()
    assert raw["totals"]["all_nonpositive"] is True
    assert raw["totals"]["data_mode"] == "synthetic"
    assert raw["denominators"]["candidate"]["count"] == 0
    assert raw["denominators"]["attempted_quote"]["count"] == 3
    assert raw["denominators"]["quoted"]["count"] == 2
    assert raw["denominators"]["rejected"]["count"] == 1
    assert raw["rejection_reasons"]["exclusive_sum"] is True
    assert raw["consistency"]["event_count_matches_ledger_count"] is True
    assert raw["consistency"]["denominator_sum_matches_total"] is True
    assert [group["amount_atoms"] for group in raw["amount_groups"]] == ["2000", "2001", "2000"]
    assert all(
        group["net_unknown"] == 1 or group["net_negative"] == 1 for group in raw["amount_groups"]
    )
    assert "| 2000 |" not in render_markdown(payload)


def test_report_rejects_unknown_schema(tmp_path: Path) -> None:
    """A schema outside the report contract is integrity corruption."""
    source = ALL_NEGATIVE.read_text(encoding="utf-8")
    target = tmp_path / "unknown.jsonl"
    target.write_text(source.replace("w2-shadow-rejection-v1", "unknown-schema"), encoding="utf-8")
    with pytest.raises(LedgerCorruptionError):
        build_report_from_ledger(target)


def test_report_rejects_empty_ledger(tmp_path: Path) -> None:
    """An empty ledger is insufficient input, not a successful empty report."""
    target = tmp_path / "empty.jsonl"
    target.write_text("", encoding="utf-8")
    with pytest.raises(LedgerCorruptionError):
        build_report_from_ledger(target)


def test_report_rejects_truncated_tail(tmp_path: Path) -> None:
    """Incomplete durable bytes fail closed before report publication."""
    target = tmp_path / "truncated.jsonl"
    target.write_bytes(ALL_NEGATIVE.read_bytes()[:-20])
    with pytest.raises(LedgerCorruptionError):
        build_report_from_ledger(target)


def test_report_rejects_middle_corruption(tmp_path: Path) -> None:
    """Middle corruption is caught by the store hash-chain loader."""
    rows = _rows(ALL_NEGATIVE)
    rows[1]["payload"]["reason"] = "tampered"
    target = tmp_path / "tampered.jsonl"
    target.write_text(
        "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    with pytest.raises(LedgerCorruptionError):
        build_report_from_ledger(target)
