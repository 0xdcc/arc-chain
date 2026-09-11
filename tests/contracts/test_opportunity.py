"""Unit tests verifying opportunity episode lifecycle, temporal monotonicity, and observation integrity (A21-A22)."""

from __future__ import annotations

import unittest

from arbitrage_contracts.identity import Amount, AssetRef, PoolKey, TokenKey
from arbitrage_contracts.opportunity import (
    Observation,
    OpportunityPhase,
    OpportunityRecord,
    compute_observation_id,
    compute_opportunity_id,
)
from arbitrage_contracts.quote import (
    ActorScope,
    DataMode,
    HopDirection,
    HopRef,
    QuoteEvidence,
    QuoteStatus,
    RouteRef,
)


class TestOpportunityRecordMonotonicity(unittest.TestCase):
    """A21: OpportunityRecord temporal monotonicity and deterministic identity."""

    def setUp(self) -> None:
        self.chain_id = 4663
        self.token_a = AssetRef.erc20(
            TokenKey(self.chain_id, "0x1111111111111111111111111111111111111111")
        )
        self.token_b = AssetRef.erc20(
            TokenKey(self.chain_id, "0x2222222222222222222222222222222222222222")
        )
        self.pool_ab = PoolKey(
            self.chain_id,
            "uniswap_v3",
            "factory",
            "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "address",
            "0xab00000000000000000000000000000000000001",
        )
        self.pool_ba = PoolKey(
            self.chain_id,
            "sushiswap",
            "factory",
            "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "address",
            "0xba00000000000000000000000000000000000002",
        )
        self.hop_ab = HopRef(self.pool_ab, self.token_a, self.token_b)
        self.hop_ba = HopRef(self.pool_ba, self.token_b, self.token_a, HopDirection.ONE_FOR_ZERO)
        self.route = RouteRef(self.chain_id, self.token_a, (self.hop_ab, self.hop_ba))
        self.amount_in = Amount.from_atoms_str(self.token_a, "1000000", 6)

    def test_valid_temporal_monotonicity(self) -> None:
        """Verify first_seen <= last_rechecked <= last_seen succeeds."""
        record = OpportunityRecord(
            opportunity_id="opp-001",
            route_id=self.route.route_id,
            amount_in=self.amount_in,
            first_seen_at_ms=1000,
            last_rechecked_at_ms=1500,
            last_seen_at_ms=2000,
        )
        self.assertEqual(record.first_seen_at_ms, 1000)
        self.assertEqual(record.last_rechecked_at_ms, 1500)
        self.assertEqual(record.last_seen_at_ms, 2000)

    def test_temporal_monotonicity_violations_rejected(self) -> None:
        """Verify violations of first <= last_rechecked <= last_seen raise ValueError."""
        # rechecked precedes first
        with self.assertRaises(ValueError):
            OpportunityRecord(
                opportunity_id="opp-err-1",
                route_id=self.route.route_id,
                amount_in=self.amount_in,
                first_seen_at_ms=2000,
                last_rechecked_at_ms=1500,
                last_seen_at_ms=3000,
            )

        # rechecked succeeds last_seen
        with self.assertRaises(ValueError):
            OpportunityRecord(
                opportunity_id="opp-err-2",
                route_id=self.route.route_id,
                amount_in=self.amount_in,
                first_seen_at_ms=1000,
                last_rechecked_at_ms=2500,
                last_seen_at_ms=2000,
            )

        # last_seen precedes first_seen
        with self.assertRaises(ValueError):
            OpportunityRecord(
                opportunity_id="opp-err-3",
                route_id=self.route.route_id,
                amount_in=self.amount_in,
                first_seen_at_ms=3000,
                last_rechecked_at_ms=2000,
                last_seen_at_ms=1000,
            )

    def test_disappeared_timestamp_constraints(self) -> None:
        """Verify disappeared_at_ms must not precede last_seen_at_ms."""
        with self.assertRaises(ValueError):
            OpportunityRecord(
                opportunity_id="opp-disapp-err",
                route_id=self.route.route_id,
                amount_in=self.amount_in,
                first_seen_at_ms=1000,
                last_rechecked_at_ms=1500,
                last_seen_at_ms=2000,
                disappeared_at_ms=1999,
            )

        valid_disapp = OpportunityRecord(
            opportunity_id="opp-disapp-ok",
            route_id=self.route.route_id,
            amount_in=self.amount_in,
            first_seen_at_ms=1000,
            last_rechecked_at_ms=1500,
            last_seen_at_ms=2000,
            disappeared_at_ms=2500,
            phase=OpportunityPhase.DISAPPEARED,
        )
        self.assertEqual(valid_disapp.disappeared_at_ms, 2500)

    def test_deterministic_opportunity_and_observation_ids(self) -> None:
        """Verify deterministic calculation of opportunity_id and observation_id."""
        opp_id_1 = compute_opportunity_id(self.route.route_id, self.amount_in, "event-init-01")
        opp_id_2 = compute_opportunity_id(self.route.route_id, self.amount_in, "event-init-01")
        self.assertEqual(opp_id_1, opp_id_2)

        # Changing route or amount produces different ID
        other_amount = Amount.from_atoms_str(self.token_a, "2000000", 6)
        opp_id_3 = compute_opportunity_id(self.route.route_id, other_amount, "event-init-01")
        self.assertNotEqual(opp_id_1, opp_id_3)

        obs_id_1 = compute_observation_id("run-001", "seq-01", "state-v1", self.amount_in)
        obs_id_2 = compute_observation_id("run-001", "seq-01", "state-v1", self.amount_in)
        self.assertEqual(obs_id_1, obs_id_2)

    def test_deep_immutability_and_defensive_copies(self) -> None:
        """Verify external list mutations do not alter OpportunityRecord."""
        reasons = ["slippage_low"]
        record = OpportunityRecord(
            opportunity_id="opp-immut",
            route_id=self.route.route_id,
            amount_in=self.amount_in,
            first_seen_at_ms=1000,
            last_rechecked_at_ms=1000,
            last_seen_at_ms=1000,
            rejection_reasons=reasons,
        )
        reasons.append("gas_high")
        self.assertEqual(record.rejection_reasons, ("slippage_low",))


class TestObservationAntiFuturePeek(unittest.TestCase):
    """A22: Observation state version correlation and anti-future-peek invariants."""

    def setUp(self) -> None:
        self.chain_id = 4663
        self.token_a = AssetRef.erc20(
            TokenKey(self.chain_id, "0x1111111111111111111111111111111111111111")
        )
        self.token_b = AssetRef.erc20(
            TokenKey(self.chain_id, "0x2222222222222222222222222222222222222222")
        )
        self.pool_ab = PoolKey(
            self.chain_id,
            "uniswap_v3",
            "factory",
            "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "address",
            "0xab00000000000000000000000000000000000001",
        )
        self.pool_ba = PoolKey(
            self.chain_id,
            "sushiswap",
            "factory",
            "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "address",
            "0xba00000000000000000000000000000000000002",
        )
        self.hop_ab = HopRef(self.pool_ab, self.token_a, self.token_b)
        self.hop_ba = HopRef(self.pool_ba, self.token_b, self.token_a, HopDirection.ONE_FOR_ZERO)
        self.route = RouteRef(self.chain_id, self.token_a, (self.hop_ab, self.hop_ba))
        self.amount_in = Amount.from_atoms_str(self.token_a, "1000000", 6)
        self.amount_out = Amount.from_atoms_str(self.token_a, "1020000", 6)

    def test_observation_timestamp_bounded_by_record(self) -> None:
        """Verify observation observed_at_ms must be within [first_seen, last_seen]."""
        obs_past = Observation("obs-past", "run-1", observed_at_ms=900)
        obs_future = Observation("obs-future", "run-1", observed_at_ms=2100)

        with self.assertRaises(ValueError):
            OpportunityRecord(
                opportunity_id="opp-bnd-1",
                route_id=self.route.route_id,
                amount_in=self.amount_in,
                first_seen_at_ms=1000,
                last_rechecked_at_ms=1500,
                last_seen_at_ms=2000,
                observations=(obs_past,),
            )

        with self.assertRaises(ValueError):
            OpportunityRecord(
                opportunity_id="opp-bnd-2",
                route_id=self.route.route_id,
                amount_in=self.amount_in,
                first_seen_at_ms=1000,
                last_rechecked_at_ms=1500,
                last_seen_at_ms=2000,
                observations=(obs_future,),
            )

    def test_future_quote_cannot_evaluate_past_observation(self) -> None:
        """Verify future quote evidence cannot be tied to a past observation time."""
        future_quote = QuoteEvidence(
            quote_id="quote-future",
            route_ref=self.route,
            amount_in=self.amount_in,
            amount_out=self.amount_out,
            started_at_ms=1600,
            status=QuoteStatus.QUOTED,
        )
        # Observation observed at 1500 ms cannot evaluate quote created at 1600 ms
        with self.assertRaises(ValueError):
            Observation(
                observation_id="obs-anti-peek",
                run_id="run-1",
                observed_at_ms=1500,
                quote_evidence=future_quote,
            )

    def test_truncated_search_distinguished_from_no_opportunity(self) -> None:
        """Verify truncated searches due to budget limit cannot falsely report no_opportunity."""
        with self.assertRaises(ValueError):
            Observation(
                observation_id="obs-trunc-err",
                run_id="run-1",
                observed_at_ms=1500,
                is_truncated=True,
                result="no_opportunity",
            )


if __name__ == "__main__":
    unittest.main()
