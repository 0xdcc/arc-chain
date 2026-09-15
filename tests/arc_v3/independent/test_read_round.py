"""Deterministic contract tests for single-round market data reading coordinator."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

from research.market_data.multicall import PoolSpec, PriceQuote
from research.market_data.read_round import (
    ArbitrageRoundCoordinator,
    ReadRoundCoordinator,
    RoundResult,
    RoundStatus,
    execute_read_round,
)


def _make_pool(suffix: str, label: str = "test-pool") -> PoolSpec:
    addr = "0x" + suffix.rjust(40, "0")
    return PoolSpec(
        address=addr,
        label=label,
        fee_bps=30.0,
        token0="0x" + "a" * 40,
        token1="0x" + "b" * 40,
        dec0=18,
        dec1=18,
    )


def test_read_round_coordinator_success_orchestration() -> None:
    """Verify single-round orchestration dispatches quotes, status, and alerts."""
    p1 = _make_pool("01", "pool-1")
    p2 = _make_pool("02", "pool-2")
    q1 = PriceQuote(
        pool=p1,
        base=p1.token0,
        quote=p1.token1,
        price=100.0,
        raw_price_t1_per_t0=100.0,
        block_number=12345,
    )
    q2 = PriceQuote(
        pool=p2,
        base=p2.token0,
        quote=p2.token1,
        price=105.0,
        raw_price_t1_per_t0=105.0,
        block_number=12345,
    )

    mock_reader = MagicMock()
    mock_reader.batch_quote.return_value = [q1, q2]

    alert_calls: list[list[PriceQuote]] = []
    status_calls: list[RoundStatus] = []

    coordinator = ReadRoundCoordinator(
        mode="spread",
        min_tvl=50000.0,
        reader=mock_reader,
        on_spread_alert=lambda qs: alert_calls.append(qs),
        on_status_update=lambda st: status_calls.append(st),
        default_max_workers=10,
    )
    coordinator.pools = [p1, p2]

    threads_before = threading.active_count()
    result: RoundResult = coordinator.scan_round(max_workers=15)
    threads_after = threading.active_count()

    # Verify no persistent threads created
    assert threads_after == threads_before

    # Verify delegation contract
    mock_reader.batch_quote.assert_called_once_with([p1, p2], max_workers=15)

    # Verify result artifact
    assert result.status.success is True
    assert result.status.pool_count == 2
    assert result.status.quotes_count == 2
    assert result.status.latest_block == 12345
    assert len(result.quotes) == 2
    assert len(alert_calls) == 1
    assert alert_calls[0] == [q1, q2]
    assert len(status_calls) == 1
    assert status_calls[0].success is True


def test_read_round_coordinator_missing_reader_error() -> None:
    """Verify missing reader triggers error callback and returns degraded status."""
    error_calls: list[Exception] = []
    status_calls: list[RoundStatus] = []

    coordinator = ReadRoundCoordinator(
        on_error=lambda err: error_calls.append(err),
        on_status_update=lambda st: status_calls.append(st),
    )
    p = _make_pool("01")
    coordinator.pools = [p]

    result = coordinator.scan_round()
    assert result.status.success is False
    assert result.status.quotes_count == 0
    assert "Reader dependency must be configured" in (result.status.error or "")
    assert len(error_calls) == 1
    assert len(status_calls) == 1


def test_read_round_coordinator_batch_quote_exception_handled() -> None:
    """Verify exceptions inside reader.batch_quote are caught and reported without crashing."""
    mock_reader = MagicMock()
    mock_reader.batch_quote.side_effect = RuntimeError("RPC timed out")

    error_calls: list[Exception] = []
    status_calls: list[RoundStatus] = []

    coordinator = ArbitrageRoundCoordinator(
        reader=mock_reader,
        on_error=lambda err: error_calls.append(err),
        on_status_update=lambda st: status_calls.append(st),
    )
    coordinator.pools = [_make_pool("01")]

    result = coordinator.scan_round(max_workers=5)
    assert result.status.success is False
    assert "RPC timed out" in (result.status.error or "")
    assert len(error_calls) == 1
    assert isinstance(error_calls[0], RuntimeError)
    assert len(status_calls) == 1


def test_execute_read_round_functional_helper() -> None:
    """Verify functional execute_read_round helper passes arguments and returns result."""
    p1 = _make_pool("01")
    mock_reader = MagicMock()
    mock_reader.batch_quote.return_value = []

    result = execute_read_round(
        reader=mock_reader,
        pools=[p1],
        max_workers=8,
    )
    assert result.status.success is True
    assert result.status.pool_count == 1
    mock_reader.batch_quote.assert_called_once_with([p1], max_workers=8)
