"""Tests for Arc Runtime Health & Lifecycle Process Guard (T40)

Covers:
- Exclusive instance lock acquisition, collision prevention, and clean release
- Non-negotiable 3-failure circuit breaker tripping and success reset
- Resource inspection (disk space, load average)
- Cursor tracking lag calculations
"""

import os
import tempfile
from pathlib import Path

import pytest

from arc_runtime.health import (
    compute_cursor_lag,
    get_runtime_health_report,
    inspect_resource_health,
)
from arc_runtime.lifecycle import (
    CircuitBreakerGuard,
    InstanceLock,
    LockAcquisitionError,
)


class TestRuntimeHealthAndLifecycle:
    """Test suite for T40 process management and health metrics."""

    def test_instance_lock_exclusive_acquisition(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_lock_test_") as tmpdir:
            lock_path = Path(tmpdir) / "arc.lock"
            lock = InstanceLock(lock_path)
            meta = lock.acquire("instance-123")

            assert lock_path.is_file()
            assert meta.pid == os.getpid()
            assert meta.instance_id == "instance-123"

            lock.release()
            assert not lock_path.exists()

    def test_instance_lock_collision_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_lock_test_") as tmpdir:
            lock_path = Path(tmpdir) / "arc.lock"
            lock1 = InstanceLock(lock_path)
            lock1.acquire("instance-first")

            lock2 = InstanceLock(lock_path)
            with pytest.raises(LockAcquisitionError, match="already held by live PID"):
                lock2.acquire("instance-second")

            lock1.release()

    def test_circuit_breaker_tripping(self) -> None:
        guard = CircuitBreakerGuard(failure_threshold=3)
        assert not guard.is_tripped

        guard.record_failure("HTTP 500 error 1")
        assert not guard.is_tripped
        guard.assert_healthy()

        guard.record_failure("HTTP 502 error 2")
        assert not guard.is_tripped
        guard.assert_healthy()

        guard.record_failure("HTTP 503 error 3")
        assert bool(guard.is_tripped)

        with pytest.raises(RuntimeError, match="TERMINAL CIRCUIT BREAKER TRIPPED"):
            guard.assert_healthy()

    def test_circuit_breaker_success_resets(self) -> None:
        guard = CircuitBreakerGuard(failure_threshold=3)
        guard.record_failure("error 1")
        guard.record_failure("error 2")
        assert guard.consecutive_failures == 2

        guard.record_success()
        assert guard.consecutive_failures == 0
        assert not guard.is_tripped
        guard.assert_healthy()

    def test_inspect_resource_health_valid(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_res_test_") as tmpdir:
            metrics = inspect_resource_health(Path(tmpdir))
            assert metrics.disk_total_bytes > 0
            assert metrics.disk_free_bytes > 0
            assert 0.0 <= metrics.disk_percent_used <= 100.0
            assert metrics.load_average_1m >= 0.0

    def test_compute_cursor_lag(self) -> None:
        lag1 = compute_cursor_lag(chain_id=5042, last_ingested_block=1000, latest_target_block=1001)
        assert lag1.lag_blocks == 1
        assert lag1.is_synchronized is True

        lag2 = compute_cursor_lag(chain_id=5042, last_ingested_block=1000, latest_target_block=1060)
        assert lag2.lag_blocks == 60
        assert lag2.is_synchronized is False

        lag_none = compute_cursor_lag(chain_id=5042, last_ingested_block=None, latest_target_block=1000)
        assert lag_none.lag_blocks is None
        assert lag_none.is_synchronized is False

    def test_get_runtime_health_report_degraded_states(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arc_health_rep_") as tmpdir:
            # Healthy
            rep_ok = get_runtime_health_report(
                data_dir=Path(tmpdir),
                last_ingested=1000,
                latest_target=1000,
                circuit_tripped=False,
            )
            assert rep_ok["status"] == "HEALTHY"
            assert rep_ok["circuit_tripped"] is False

            # Tripped
            rep_trip = get_runtime_health_report(
                data_dir=Path(tmpdir),
                last_ingested=1000,
                latest_target=1000,
                circuit_tripped=True,
            )
            assert rep_trip["status"] == "CIRCUIT_TRIPPED"

            # Lag degraded
            rep_lag = get_runtime_health_report(
                data_dir=Path(tmpdir),
                last_ingested=1000,
                latest_target=1080,
                circuit_tripped=False,
            )
            assert rep_lag["status"] == "LAG_DEGRADED"
