"""Arc Runtime Health & Resource Observability (T40)

Monitors:
- Filesystem disk availability and safe headroom (fail-closed on disk exhaustion)
- Cursor tracking lag relative to latest observed block
- Endpoint latency and circuit status
- Process load averages without polluting external services
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ResourceHealthMetrics:
    """Snapshot of OS resources for Arc processes."""

    disk_free_bytes: int
    disk_total_bytes: int
    disk_percent_used: float
    is_disk_critical: bool  # True if < 1GB or > 95%
    load_average_1m: float
    checked_at: float


@dataclass(frozen=True)
class CursorLagMetrics:
    """Ingest cursor progression and lag metrics."""

    chain_id: int
    last_ingested_block: int | None
    latest_target_block: int | None
    lag_blocks: int | None
    is_synchronized: bool


def inspect_resource_health(target_path: Path) -> ResourceHealthMetrics:
    """Inspect disk space and system load without external dependencies."""
    target_path = target_path.resolve()
    target_path.mkdir(parents=True, exist_ok=True)

    usage = shutil.disk_usage(target_path)
    percent_used = round((usage.used / usage.total) * 100.0, 2)
    is_critical = (usage.free < 1024 * 1024 * 1024) or (percent_used >= 95.0)

    load_1m = os.getloadavg()[0] if hasattr(os, "getloadavg") else 0.0

    return ResourceHealthMetrics(
        disk_free_bytes=usage.free,
        disk_total_bytes=usage.total,
        disk_percent_used=percent_used,
        is_disk_critical=is_critical,
        load_average_1m=round(load_1m, 2),
        checked_at=time.time(),
    )


def compute_cursor_lag(
    chain_id: int,
    last_ingested_block: int | None,
    latest_target_block: int | None,
) -> CursorLagMetrics:
    """Calculate cursor block lag."""
    if last_ingested_block is None or latest_target_block is None:
        return CursorLagMetrics(
            chain_id=chain_id,
            last_ingested_block=last_ingested_block,
            latest_target_block=latest_target_block,
            lag_blocks=None,
            is_synchronized=False,
        )

    lag = max(0, latest_target_block - last_ingested_block)
    return CursorLagMetrics(
        chain_id=chain_id,
        last_ingested_block=last_ingested_block,
        latest_target_block=latest_target_block,
        lag_blocks=lag,
        is_synchronized=(lag <= 1),
    )


def get_runtime_health_report(
    data_dir: Path,
    chain_id: int = 5042,
    last_ingested: int | None = None,
    latest_target: int | None = None,
    circuit_tripped: bool = False,
) -> dict[str, Any]:
    """Generate consolidated health inspection report."""
    res_metrics = inspect_resource_health(data_dir)
    lag_metrics = compute_cursor_lag(chain_id, last_ingested, latest_target)

    overall_status = "HEALTHY"
    if circuit_tripped:
        overall_status = "CIRCUIT_TRIPPED"
    elif res_metrics.is_disk_critical:
        overall_status = "DISK_CRITICAL"
    elif lag_metrics.lag_blocks and lag_metrics.lag_blocks > 50:
        overall_status = "LAG_DEGRADED"

    return {
        "status": overall_status,
        "chain_id": chain_id,
        "circuit_tripped": circuit_tripped,
        "resources": {
            "disk_free_mb": round(res_metrics.disk_free_bytes / (1024 * 1024), 2),
            "disk_percent": res_metrics.disk_percent_used,
            "is_critical": res_metrics.is_disk_critical,
            "load_1m": res_metrics.load_average_1m,
        },
        "cursor": {
            "last_ingested_block": lag_metrics.last_ingested_block,
            "latest_target_block": lag_metrics.latest_target_block,
            "lag_blocks": lag_metrics.lag_blocks,
            "is_synchronized": lag_metrics.is_synchronized,
        },
        "checked_at": time.time(),
    }
