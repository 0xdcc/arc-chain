"""Unit tests asserting resource limits, memory bounds, and zero network calls."""

from __future__ import annotations

import json
from pathlib import Path

from tests.state_graph.resource_runner import run_benchmark


def test_resource_limits_and_zero_network() -> None:
    """Verifies that 1000 quick evaluations satisfy memory and network boundaries."""
    metrics = run_benchmark(target_evaluations=1000)

    assert metrics["status"] == "PASS"
    assert metrics["memory"]["rss_delta_within_budget"] is True
    assert metrics["memory"]["rss_delta_mib"] <= 256.0
    assert metrics["memory"]["peak_rss_mib"] <= 512.0
    assert metrics["network"]["external_rpc_calls"] == 0
    assert metrics["network"]["socket_connections"] == 0
    assert metrics["network"]["zero_network_verified"] is True
    assert metrics["latency_ms"]["p95_ms"] < 25.0


def test_resource_metrics_json_persisted() -> None:
    """Verifies that docs/w4/evidence/W4-R/resource_metrics.json exists and is valid."""
    root = Path(__file__).resolve().parents[2]
    metrics_path = root / "docs" / "w4" / "evidence" / "W4-R" / "resource_metrics.json"
    assert metrics_path.is_file()

    data = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert data["target_evaluations"] >= 1000
    assert data["memory"]["rss_delta_within_budget"] is True
