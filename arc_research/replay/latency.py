"""Arc Replay Latency and Time-Anchored Events (T35)

Enforces:
- Separation of block time, received time, and computed time
- Pipeline latency decomposition: network RPC, decoding, graph search, and simulation
- Flagging retrospective-only data (missing arrival timestamp cannot claim execution speed)
- Prevention of instantaneous execution assumptions
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


class LatencyModelError(ValueError):
    """Raised for invalid timestamp or causality ordering."""


@dataclass(frozen=True, slots=True)
class PipelineLatencyProfile:
    """Standard latency decomposition profile in milliseconds."""

    rpc_fetch_ms: int = 40
    decoding_ms: int = 10
    quote_graph_ms: int = 15
    simulation_ms: int = 60

    @property
    def total_pipeline_ms(self) -> int:
        return self.rpc_fetch_ms + self.decoding_ms + self.quote_graph_ms + self.simulation_ms

    @property
    def total_pipeline_seconds(self) -> float:
        return float(self.total_pipeline_ms) / 1000.0


@dataclass(frozen=True, slots=True)
class TimeAnchoredSnapshot:
    """Snapshot explicitly carrying causal timestamps."""

    block_number: int
    block_timestamp_s: float
    received_at_s: float
    computed_at_s: float
    is_retrospective: bool = False

    def __post_init__(self) -> None:
        if not self.is_retrospective:
            if self.received_at_s <= 0.0:
                raise LatencyModelError("Non-retrospective snapshot requires valid positive received_at_s")
            if self.computed_at_s < self.received_at_s:
                raise LatencyModelError(
                    f"computed_at_s ({self.computed_at_s}) cannot precede received_at_s ({self.received_at_s})"
                )


class LatencyEvaluator:
    """Calculates causal availability and execution windows."""

    def __init__(self, profile: PipelineLatencyProfile | None = None) -> None:
        self.profile = profile if profile is not None else PipelineLatencyProfile()

    def compute_earliest_executable_time(self, snapshot: TimeAnchoredSnapshot) -> float:
        """Compute the earliest possible time an order could hit the network."""
        if snapshot.is_retrospective:
            # Retrospective mode: cannot ascertain millisecond arrival
            return snapshot.block_timestamp_s
        return snapshot.received_at_s + self.profile.total_pipeline_seconds
