"""Arc Causal Replay and Opportunity Lifetime Tests (T35)

Verifies:
- Monotonic block progression prevents future state leakage
- Continuous opportunities spanning multiple blocks count as 1 lifecycle (single monetization guard)
- Left and right boundary censoring correctly detected
- Retrospective mode without arrival timestamps correctly flagged
- Pipeline latency decomposition offsets executable time from receipt time
"""

from __future__ import annotations

import pytest

from arc_research.replay.engine import (
    CausalReplayEngine,
    FutureStateLeakageError,
)
from arc_research.replay.latency import (
    LatencyEvaluator,
    LatencyModelError,
    PipelineLatencyProfile,
    TimeAnchoredSnapshot,
)


class TestArcReplayCausality:
    """Test suite for T35 Causal Replay and Lifetime Analysis."""

    def test_latency_pipeline_offset(self) -> None:
        """Verify pipeline latency ms offsets execution time from receipt time."""
        profile = PipelineLatencyProfile(
            rpc_fetch_ms=50,
            decoding_ms=10,
            quote_graph_ms=20,
            simulation_ms=70,
        )
        # Total = 150 ms = 0.15 s
        evaluator = LatencyEvaluator(profile)
        snap = TimeAnchoredSnapshot(
            block_number=100,
            block_timestamp_s=1726000000.0,
            received_at_s=1726000000.20,
            computed_at_s=1726000000.25,
            is_retrospective=False,
        )
        earliest_exec = evaluator.compute_earliest_executable_time(snap)
        assert pytest.approx(earliest_exec, 0.0001) == 1726000000.35

    def test_single_monetization_continuous_lifetime(self) -> None:
        """CRITICAL: Same opportunity across continuous blocks 100, 101, 102 is ONE lifetime."""
        evaluator = LatencyEvaluator()
        engine = CausalReplayEngine(evaluator)

        s1 = TimeAnchoredSnapshot(100, 1000.0, 1000.1, 1000.2)
        s2 = TimeAnchoredSnapshot(101, 1002.0, 1002.1, 1002.2)
        s3 = TimeAnchoredSnapshot(102, 1004.0, 1004.1, 1004.2)

        sequence = [
            (s1, [("route-abc", 5_000_000)]),
            (s2, [("route-abc", 6_000_000)]),
            (s3, [("route-abc", 4_000_000)]),
        ]

        lifetimes = engine.replay_sequence(sequence)
        assert len(lifetimes) == 1
        lt = lifetimes[0]
        assert lt.route_id == "route-abc"
        assert lt.first_block == 100
        assert lt.last_block == 102
        assert lt.blocks_duration == 3
        assert lt.observations_count == 3
        assert lt.peak_profit_atoms == 6_000_000
        # Present in first and last blocks -> both censored!
        assert lt.is_censored_left is True
        assert lt.is_censored_right is True

    def test_uncensored_lifetime_disappearance(self) -> None:
        """Opportunity starts in block 101 and disappears in block 102 (within [100..103]) -> uncensored."""
        engine = CausalReplayEngine()
        s0 = TimeAnchoredSnapshot(100, 1000.0, 1000.1, 1000.2)
        s1 = TimeAnchoredSnapshot(101, 1002.0, 1002.1, 1002.2)
        s2 = TimeAnchoredSnapshot(102, 1004.0, 1004.1, 1004.2)
        s3 = TimeAnchoredSnapshot(103, 1006.0, 1006.1, 1006.2)

        sequence = [
            (s0, []),  # Not present in 100
            (s1, [("route-temp", 1_000_000)]),
            (s2, []),  # Disappeared in 102
            (s3, []),  # Not present in 103
        ]

        lifetimes = engine.replay_sequence(sequence)
        assert len(lifetimes) == 1
        lt = lifetimes[0]
        assert lt.route_id == "route-temp"
        assert lt.first_block == 101
        assert lt.last_block == 101
        assert lt.blocks_duration == 1
        assert lt.is_censored_left is False
        assert lt.is_censored_right is False

    def test_non_monotonic_blocks_rejected(self) -> None:
        """Rule: Sequence containing non-monotonic block ordering raises FutureStateLeakageError."""
        engine = CausalReplayEngine()
        s1 = TimeAnchoredSnapshot(100, 1000.0, 1000.1, 1000.2)
        s2 = TimeAnchoredSnapshot(99, 998.0, 998.1, 998.2)  # Regressed block!

        with pytest.raises(FutureStateLeakageError, match="Non-monotonic block sequence"):
            engine.replay_sequence([(s1, []), (s2, [])])

    def test_retrospective_snapshot_handling(self) -> None:
        """Rule: Missing arrival time must be tagged is_retrospective=True."""
        s_retro = TimeAnchoredSnapshot(
            block_number=200,
            block_timestamp_s=2000.0,
            received_at_s=0.0,
            computed_at_s=0.0,
            is_retrospective=True,
        )
        engine = CausalReplayEngine()
        lifetimes = engine.replay_sequence([(s_retro, [("route-retro", 500)])])
        assert len(lifetimes) == 1
        assert lifetimes[0].is_retrospective is True
