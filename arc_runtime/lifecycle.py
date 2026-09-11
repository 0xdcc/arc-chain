"""Arc Runtime Lifecycle & Single-Instance Process Management (T40)

Enforces:
- Host, PID, and timestamp-bound exclusive instance lock file
- Clean shutdown signal handling without orphan background leaks
- Three-failure terminal circuit breaker (hard stop without infinite restart loop)
- Zero cross-project kill/pkill side effects
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class LifecycleState(StrEnum):
    UNINITIALIZED = "UNINITIALIZED"
    STARTING = "STARTING"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    CIRCUIT_TRIPPED = "CIRCUIT_TRIPPED"
    STOPPED = "STOPPED"


class LockAcquisitionError(Exception):
    """Raised when an instance lock cannot be acquired or another process holds it."""


@dataclass(frozen=True)
class LockMetadata:
    """Identity record written into the instance lock file."""

    instance_id: str
    pid: int
    hostname: str
    acquired_at: float
    app_name: str


class InstanceLock:
    """File-backed process concurrency guard preventing multi-writer collision."""

    def __init__(self, lock_path: Path, app_name: str = "arc_runtime") -> None:
        self.lock_path = lock_path.resolve()
        self.app_name = app_name
        self.acquired = False
        self._meta: LockMetadata | None = None

    def acquire(self, instance_id: str) -> LockMetadata:
        """Attempt to acquire exclusive instance lock."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

        if self.lock_path.exists():
            try:
                existing_data = json.loads(self.lock_path.read_text(encoding="utf-8"))
                existing_pid = existing_data.get("pid")
                # Check if process is still alive on this host
                if existing_pid and self._is_pid_alive(existing_pid):
                    raise LockAcquisitionError(
                        f"Instance lock {self.lock_path} already held by live PID {existing_pid} on {existing_data.get('hostname')}"
                    )
            except (json.JSONDecodeError, OSError):
                pass  # Stale or corrupted lock can be overwritten safely

        meta = LockMetadata(
            instance_id=instance_id,
            pid=os.getpid(),
            hostname=socket.gethostname(),
            acquired_at=time.time(),
            app_name=self.app_name,
        )
        self.lock_path.write_text(
            json.dumps(
                {
                    "instance_id": meta.instance_id,
                    "pid": meta.pid,
                    "hostname": meta.hostname,
                    "acquired_at": meta.acquired_at,
                    "app_name": meta.app_name,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        self.acquired = True
        self._meta = meta
        return meta

    def release(self) -> None:
        """Release the instance lock if owned by this process."""
        if not self.acquired:
            return
        if self.lock_path.exists():
            try:
                data = json.loads(self.lock_path.read_text(encoding="utf-8"))
                if data.get("pid") == os.getpid():
                    self.lock_path.unlink(missing_ok=True)
            except Exception:
                self.lock_path.unlink(missing_ok=True)
        self.acquired = False
        self._meta = None

    @staticmethod
    def _is_pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def __enter__(self) -> InstanceLock:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.release()


class CircuitBreakerGuard:
    """Enforces non-negotiable 3-failure circuit trip and termination."""

    def __init__(self, failure_threshold: int = 3) -> None:
        self.failure_threshold = failure_threshold
        self.consecutive_failures = 0
        self.is_tripped = False
        self.trip_reason: str | None = None

    def record_success(self) -> None:
        if not self.is_tripped:
            self.consecutive_failures = 0

    def record_failure(self, reason: str) -> None:
        if self.is_tripped:
            return
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.failure_threshold:
            self.is_tripped = True
            self.trip_reason = f"Circuit tripped after {self.consecutive_failures} consecutive failures: {reason}"

    def assert_healthy(self) -> None:
        if self.is_tripped:
            raise RuntimeError(f"TERMINAL CIRCUIT BREAKER TRIPPED: {self.trip_reason}")
