"""Read-only monitor orchestration service.

Coordinates market data snapshotting, candidate generation from pure strategies,
and safe read-only report formatting without touching execution mechanisms.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from arbitrage.domain.types import (
    CandidateRoute,
    PoolIdentity,
    TokenIdentity,
)
from arbitrage.market_data.pool_reader import SnapshotCoordinator
from arbitrage.reporting.formatters import (
    ArbitrageReport,
    ExecutionMode,
    format_report_json,
    format_report_text,
)
from arbitrage.strategies.spread import find_spread_candidates
from arbitrage.strategies.triangular import find_triangular_candidates

logger = logging.getLogger(__name__)


class ReadOnlyMonitorService:
    """Orchestrates pool reading, candidate discovery, and safe report generation.

    Strictly read-only; absolutely isolated from trade execution.
    """

    def __init__(
        self,
        coordinator: SnapshotCoordinator | None = None,
        rpc: Any = None,
        min_gross_bps: float = 10.0,
        base_token: TokenIdentity | None = None,
        known_tokens: Mapping[str, TokenIdentity] | None = None,
        mode: str = ExecutionMode.LIVE.value,
        dummy_executor: Any = None,
    ) -> None:
        self.coordinator = coordinator or SnapshotCoordinator(rpc=rpc)
        self.rpc = rpc
        self.min_gross_bps = min_gross_bps
        self.base_token = base_token
        self.known_tokens = known_tokens
        self.mode = mode
        # Dummy executor stored for negative control tests; never accessed or called
        self.dummy_executor = dummy_executor

    def poll_once(
        self,
        pools: Sequence[PoolIdentity],
        block_number: int | None = None,
    ) -> dict[str, Any]:
        """Perform a single read-only polling cycle across the target pools.

        1. Fetches a consistent multi-pool snapshot via SnapshotCoordinator.
        2. Computes spread and triangular candidate routes via pure strategies.
        3. Formats non-authoritative read-only reports for monitoring and alerts.
        4. Never invokes any order placing or transaction handling components.
        """
        snapshot = self.coordinator.read_market_snapshot(
            pools=list(pools),
            rpc=self.rpc,
            block_number=block_number,
        )

        spread_candidates = find_spread_candidates(
            snapshot=snapshot,
            min_gross_bps=self.min_gross_bps,
            base_token=self.base_token,
            known_tokens=self.known_tokens,
        )

        triangular_candidates = find_triangular_candidates(
            snapshot=snapshot,
            min_gross_bps=self.min_gross_bps,
            base_token=self.base_token,
            known_tokens=self.known_tokens,
        )

        all_candidates: list[CandidateRoute] = list(spread_candidates) + list(triangular_candidates)

        reports: list[ArbitrageReport] = []
        reports_json: list[str] = []
        reports_text: list[str] = []

        for cand in all_candidates:
            report_id = f"rpt-{cand.candidate_id}-{uuid.uuid4().hex[:8]}"
            strategy_name = getattr(cand, "route_type", "unknown")
            gross_bps = getattr(cand, "observed_gross_bps", 0.0)
            report = ArbitrageReport(
                report_id=report_id,
                mode=self.mode,
                route=cand,
                metadata={
                    "strategy": strategy_name,
                    "observed_gross_bps": float(gross_bps),
                    "block_number": snapshot.block_number,
                },
            )
            reports.append(report)
            reports_json.append(format_report_json(report))
            reports_text.append(format_report_text(report))

        return {
            "snapshot": snapshot,
            "block_number": snapshot.block_number,
            "spread_candidates": spread_candidates,
            "triangular_candidates": triangular_candidates,
            "candidates": all_candidates,
            "reports": reports,
            "reports_json": reports_json,
            "reports_text": reports_text,
            "status": "success",
        }
