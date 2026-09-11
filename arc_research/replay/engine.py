"""Arc Causal Replay Engine and Opportunity Lifetime Tracking (T35)

Enforces:
- Strict monotonic block progression (prevent future state leakage)
- Opportunity lifetime tracking with left and right boundary censoring
- Single-monetization guard: continuous opportunities spanning multiple blocks count as 1 event
- Retrospective vs live causality classification
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from arc_research.replay.latency import LatencyEvaluator, TimeAnchoredSnapshot


class FutureStateLeakageError(ValueError):
    """Raised when replay attempts to access future block or state."""


@dataclass(frozen=True, slots=True)
class OpportunityObservation:
    """Observation of an actionable route at a discrete block."""

    route_id: str
    net_profit_atoms: int
    snapshot: TimeAnchoredSnapshot


@dataclass(frozen=True, slots=True)
class OpportunityLifetime:
    """Lifespan of an opportunity across continuous blocks."""

    route_id: str
    first_block: int
    last_block: int
    first_seen_utc: float
    last_seen_utc: float
    duration_seconds: float
    blocks_duration: int
    is_censored_left: bool
    is_censored_right: bool
    is_retrospective: bool
    peak_profit_atoms: int
    observations_count: int


class CausalReplayEngine:
    """Replays historical snapshots without future knowledge and tracks lifespans."""

    def __init__(self, evaluator: LatencyEvaluator | None = None) -> None:
        self.evaluator = evaluator if evaluator is not None else LatencyEvaluator()
        self._current_block: int | None = None

    def replay_sequence(
        self,
        sequence: list[tuple[TimeAnchoredSnapshot, list[tuple[str, int]]]],  # snapshot, list[(route_id, profit)]
    ) -> list[OpportunityLifetime]:
        """Replay snapshots in chronological order.

        Invariants:
        1. Sequence must be non-empty and strictly monotonically increasing in block number.
        2. Querying future blocks raises FutureStateLeakageError.
        3. Opportunities active in sequence[0] are flagged as is_censored_left.
        4. Opportunities active in sequence[-1] are flagged as is_censored_right.
        5. Repeated observations of the same route across consecutive blocks are coalesced.
        """
        if not sequence:
            return []

        # Validate monotonic blocks
        for i in range(len(sequence) - 1):
            if sequence[i + 1][0].block_number <= sequence[i][0].block_number:
                raise FutureStateLeakageError(
                    f"Non-monotonic block sequence: block {sequence[i+1][0].block_number} <= {sequence[i][0].block_number}"
                )

        start_block = sequence[0][0].block_number
        end_block = sequence[-1][0].block_number

        # Map active route_id -> list of observations
        active_lifetimes: dict[str, list[OpportunityObservation]] = {}
        completed_lifetimes: list[OpportunityLifetime] = []

        for snapshot, route_obs in sequence:
            self._current_block = snapshot.block_number
            seen_in_block: set[str] = set()

            for route_id, profit in route_obs:
                seen_in_block.add(route_id)
                obs = OpportunityObservation(route_id=route_id, net_profit_atoms=profit, snapshot=snapshot)
                if route_id not in active_lifetimes:
                    active_lifetimes[route_id] = [obs]
                else:
                    active_lifetimes[route_id].append(obs)

            # Check if any previously active opportunity disappeared in this block
            disappeared = [r for r in active_lifetimes if r not in seen_in_block]
            for r in disappeared:
                obs_list = active_lifetimes.pop(r)
                lifetime = self._build_lifetime(obs_list, start_block, end_block)
                completed_lifetimes.append(lifetime)

        # Finalize remaining active lifetimes at end of sequence
        for r, obs_list in active_lifetimes.items():
            lifetime = self._build_lifetime(obs_list, start_block, end_block)
            completed_lifetimes.append(lifetime)

        return completed_lifetimes

    def _build_lifetime(
        self,
        observations: list[OpportunityObservation],
        sequence_start_block: int,
        sequence_end_block: int,
    ) -> OpportunityLifetime:
        first_obs = observations[0]
        last_obs = observations[-1]

        first_time = self.evaluator.compute_earliest_executable_time(first_obs.snapshot)
        last_time = self.evaluator.compute_earliest_executable_time(last_obs.snapshot)

        is_censored_left = first_obs.snapshot.block_number == sequence_start_block
        is_censored_right = last_obs.snapshot.block_number == sequence_end_block
        is_retro = any(o.snapshot.is_retrospective for o in observations)

        peak_profit = max(o.net_profit_atoms for o in observations)
        blocks_dur = (last_obs.snapshot.block_number - first_obs.snapshot.block_number) + 1

        return OpportunityLifetime(
            route_id=first_obs.route_id,
            first_block=first_obs.snapshot.block_number,
            last_block=last_obs.snapshot.block_number,
            first_seen_utc=first_time,
            last_seen_utc=last_time,
            duration_seconds=max(0.0, last_time - first_time),
            blocks_duration=blocks_dur,
            is_censored_left=is_censored_left,
            is_censored_right=is_censored_right,
            is_retrospective=is_retro,
            peak_profit_atoms=peak_profit,
            observations_count=len(observations),
        )
