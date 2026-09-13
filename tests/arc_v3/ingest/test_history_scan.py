"""Tests for T11: Historical Continuous Block Range Scanner and Circuit Breakers."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from arc_ingest.coverage import CoverageManifest
from arc_ingest.history_scan import HistoricalBlockScanner
from arc_ingest.recorder import RawDataRecorder
from arc_readiness.errors import ArcValidationError
from arc_readiness.http_readonly import ArcCircuitBreakerTrippedError


@pytest.fixture
def temp_recorder(tmp_path):
    return RawDataRecorder(base_dir=tmp_path / "ingest_data", chain_id=5042)


class TestHistoricalBlockScanner:
    """Test suite for historical scanning, range boundaries, and circuit breaker guards."""

    def test_scan_range_success(self, temp_recorder) -> None:
        mock_transport = MagicMock()

        def fake_request(method, params):
            if method == "eth_getBlockByNumber":
                b_num = int(params[0], 16)
                return {
                    "number": hex(b_num),
                    "hash": f"0x{b_num:064x}",
                    "timestamp": hex(1700000000 + b_num),
                }
            if method == "eth_getLogs":
                return [
                    {
                        "address": "0x1111111111111111111111111111111111111111",
                        "topics": ["0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"],
                        "data": "0x01",
                    }
                ]
            return None

        mock_transport.request.side_effect = fake_request

        coverage = CoverageManifest(chain_id=5042)
        scanner = HistoricalBlockScanner(
            transport=mock_transport,
            recorder=temp_recorder,
            coverage=coverage,
            max_batch_size=50,
        )

        scanned = scanner.scan_range(100, 105)
        assert scanned == 6
        assert coverage.total_blocks == 6
        assert coverage.min_block == 100
        assert coverage.max_block == 105
        assert coverage.is_continuous(100, 105) is True
        assert temp_recorder.latest_cursor.block_number == 105

    def test_scan_range_invalid_arguments_rejected(self, temp_recorder) -> None:
        scanner = HistoricalBlockScanner(
            transport=MagicMock(),
            recorder=temp_recorder,
            max_batch_size=20,
        )

        with pytest.raises(ArcValidationError, match="from_block cannot be negative"):
            scanner.scan_range(-1, 10)

        with pytest.raises(ArcValidationError, match="Invalid scan range"):
            scanner.scan_range(50, 40)

        with pytest.raises(ArcValidationError, match="exceeds max batch limit"):
            scanner.scan_range(10, 50)  # 41 blocks > 20 limit

    def test_circuit_breaker_trips_after_three_consecutive_failures(self, temp_recorder) -> None:
        mock_transport = MagicMock()
        mock_transport.request.side_effect = ConnectionError("Node unreachable")

        scanner = HistoricalBlockScanner(
            transport=mock_transport,
            recorder=temp_recorder,
        )

        # 1st failure: propagates ConnectionError
        with pytest.raises(ConnectionError):
            scanner.scan_block(1)

        # 2nd failure: propagates ConnectionError
        with pytest.raises(ConnectionError):
            scanner.scan_block(2)

        # 3rd failure: trips the circuit breaker
        with pytest.raises(ArcCircuitBreakerTrippedError, match="Historical scanner halted.*3 consecutive RPC failures"):
            scanner.scan_block(3)

    def test_empty_block_response_fails_closed(self, temp_recorder) -> None:
        mock_transport = MagicMock()
        mock_transport.request.return_value = None

        scanner = HistoricalBlockScanner(
            transport=mock_transport,
            recorder=temp_recorder,
        )

        with pytest.raises(ArcValidationError, match="Null or empty block returned"):
            scanner.scan_block(500)

    def test_received_at_timestamp_is_current(self, temp_recorder) -> None:
        mock_transport = MagicMock()
        mock_transport.request.side_effect = lambda m, p: (
            {"number": "0x10", "hash": "0x" + "aa" * 32, "timestamp": "0x1000"}
            if m == "eth_getBlockByNumber"
            else []
        )

        t_before = time.time()
        scanner = HistoricalBlockScanner(
            transport=mock_transport,
            recorder=temp_recorder,
        )
        block_res = scanner.scan_block(16)
        t_after = time.time()

        assert t_before <= block_res["_received_at"] <= t_after
