"""Unit tests verifying route topology, quote evidence failure defenses, and economic invariants (A17-A20)."""

from __future__ import annotations

import unittest

from arbitrage_contracts.eligibility import EvidenceLevel
from arbitrage_contracts.identity import (
    Amount,
    AssetRef,
    FeeModel,
    PoolDescriptor,
    PoolKey,
    TokenKey,
)
from arbitrage_contracts.quote import (
    ActorScope,
    DataMode,
    EconomicAssessment,
    EconomicStatus,
    FeeComponent,
    GasEvidence,
    GasEvidenceKind,
    HopDirection,
    HopLimitExceededError,
    HopQuote,
    HopRef,
    PriceEvidence,
    QuoteEvidence,
    QuoteStatus,
    RouteRef,
    TriState,
    compute_route_id,
)


class TestRouteRefTopology(unittest.TestCase):
    """A17: RouteRef bounded N-hop topology validation."""

    def setUp(self) -> None:
        self.chain_id = 4663
        self.token_a = AssetRef.erc20(
            TokenKey(self.chain_id, "0x1111111111111111111111111111111111111111")
        )
        self.token_b = AssetRef.erc20(
            TokenKey(self.chain_id, "0x2222222222222222222222222222222222222222")
        )
        self.token_c = AssetRef.erc20(
            TokenKey(self.chain_id, "0x3333333333333333333333333333333333333333")
        )
        self.token_d = AssetRef.erc20(
            TokenKey(self.chain_id, "0x4444444444444444444444444444444444444444")
        )
        self.token_foreign = AssetRef.erc20(
            TokenKey(56, "0x1111111111111111111111111111111111111111")
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
        self.pool_bc = PoolKey(
            self.chain_id,
            "uniswap_v3",
            "factory",
            "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "address",
            "0xbc00000000000000000000000000000000000003",
        )
        self.pool_ca = PoolKey(
            self.chain_id,
            "curve",
            "factory",
            "0xcccccccccccccccccccccccccccccccccccccccc",
            "address",
            "0xca00000000000000000000000000000000000004",
        )
        self.pool_cd = PoolKey(
            self.chain_id,
            "uniswap_v3",
            "factory",
            "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "address",
            "0xcd00000000000000000000000000000000000005",
        )
        self.pool_da = PoolKey(
            self.chain_id,
            "uniswap_v3",
            "factory",
            "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "address",
            "0xda00000000000000000000000000000000000006",
        )

        self.hop_ab = HopRef(self.pool_ab, self.token_a, self.token_b, HopDirection.ZERO_FOR_ONE)
        self.hop_ba = HopRef(self.pool_ba, self.token_b, self.token_a, HopDirection.ONE_FOR_ZERO)
        self.hop_bc = HopRef(self.pool_bc, self.token_b, self.token_c, HopDirection.ZERO_FOR_ONE)
        self.hop_ca = HopRef(self.pool_ca, self.token_c, self.token_a, HopDirection.ZERO_FOR_ONE)
        self.hop_cd = HopRef(self.pool_cd, self.token_c, self.token_d, HopDirection.ZERO_FOR_ONE)
        self.hop_da = HopRef(self.pool_da, self.token_d, self.token_a, HopDirection.ZERO_FOR_ONE)

    def test_valid_two_hop_cycle(self) -> None:
        """Verify valid 2-hop closed cycle passes and computes route_id."""
        route = RouteRef(
            chain_id=self.chain_id,
            base_asset=self.token_a,
            hops=(self.hop_ab, self.hop_ba),
        )
        self.assertEqual(len(route.hops), 2)
        self.assertEqual(route.base_asset, self.token_a)
        self.assertTrue(len(route.route_id) == 64)

    def test_valid_three_hop_cycle(self) -> None:
        """Verify valid 3-hop closed cycle passes."""
        route = RouteRef(
            chain_id=self.chain_id,
            base_asset=self.token_a,
            hops=(self.hop_ab, self.hop_bc, self.hop_ca),
        )
        self.assertEqual(len(route.hops), 3)

    def test_four_hop_cycle_and_max_hops_exceeded(self) -> None:
        """Verify 4-hop cycle is valid when max_hops=4, and raises HopLimitExceededError when exceeded."""
        hops_4 = (self.hop_ab, self.hop_bc, self.hop_cd, self.hop_da)
        route_4 = RouteRef(
            chain_id=self.chain_id,
            base_asset=self.token_a,
            hops=hops_4,
            max_hops=4,
        )
        self.assertEqual(len(route_4.hops), 4)

        # Exceeding parameterized max_hops limit must raise HopLimitExceededError
        with self.assertRaises(HopLimitExceededError):
            RouteRef(
                chain_id=self.chain_id,
                base_asset=self.token_a,
                hops=hops_4,
                max_hops=3,
            )

    def test_minimum_hops_required(self) -> None:
        """Verify cycle with fewer than 2 hops is rejected."""
        with self.assertRaises(ValueError):
            RouteRef(
                chain_id=self.chain_id,
                base_asset=self.token_a,
                hops=(self.hop_ab,),
            )

    def test_cross_chain_pseudo_cycle_rejected(self) -> None:
        """Verify cross-chain pseudo-cycle with same address is rejected fail-closed."""
        token_foreign_b = AssetRef.erc20(TokenKey(56, "0x2222222222222222222222222222222222222222"))
        pool_foreign = PoolKey(
            56,
            "pancakeswap",
            "factory",
            "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "address",
            "0xab00000000000000000000000000000000000001",
        )
        hop_foreign = HopRef(
            pool_foreign, token_foreign_b, self.token_foreign, direction=HopDirection.ZERO_FOR_ONE
        )
        with self.assertRaises(ValueError):
            RouteRef(
                chain_id=self.chain_id,
                base_asset=self.token_a,
                hops=(self.hop_ab, hop_foreign),
            )

    def test_direction_and_continuity_errors_rejected(self) -> None:
        """Verify adjacent output-input mismatch and terminal mismatches are rejected."""
        # Adjacent mismatch: hop_ab outputs token_b, but hop_ca expects token_c
        with self.assertRaises(ValueError):
            RouteRef(
                chain_id=self.chain_id,
                base_asset=self.token_a,
                hops=(self.hop_ab, self.hop_ca),
            )

        # Terminal mismatch: cycle does not return to base asset
        with self.assertRaises(ValueError):
            RouteRef(
                chain_id=self.chain_id,
                base_asset=self.token_a,
                hops=(self.hop_ab, self.hop_bc),
            )

        # Initial mismatch: first hop does not start with base asset
        with self.assertRaises(ValueError):
            RouteRef(
                chain_id=self.chain_id,
                base_asset=self.token_c,
                hops=(self.hop_ab, self.hop_ba),
            )

    def test_duplicate_pools_rejected(self) -> None:
        """Verify reusing the same liquidity pool in a route cycle is rejected."""
        # Reusing pool_ab in both directions
        hop_ba_same_pool = HopRef(
            self.pool_ab, self.token_b, self.token_a, HopDirection.ONE_FOR_ZERO
        )
        with self.assertRaises(ValueError):
            RouteRef(
                chain_id=self.chain_id,
                base_asset=self.token_a,
                hops=(self.hop_ab, hop_ba_same_pool),
            )

    def test_duplicate_intermediate_tokens_rejected(self) -> None:
        """Verify intermediate token duplicates or early closure are rejected."""
        pool_b_c = self.pool_bc
        pool_c_b = PoolKey(
            self.chain_id,
            "sushiswap",
            "factory",
            "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "address",
            "0xcb00000000000000000000000000000000000007",
        )
        hop_cb = HopRef(pool_c_b, self.token_c, self.token_b, HopDirection.ONE_FOR_ZERO)

        # Cycle visits token_b twice: A -> B -> C -> B -> A
        with self.assertRaises(ValueError):
            RouteRef(
                chain_id=self.chain_id,
                base_asset=self.token_a,
                hops=(self.hop_ab, self.hop_bc, hop_cb, self.hop_ba),
            )

    def test_deterministic_route_id(self) -> None:
        """Verify route_id is strictly deterministic and sensitive to order/pools."""
        r1 = RouteRef(self.chain_id, self.token_a, (self.hop_ab, self.hop_ba))
        r2 = RouteRef(self.chain_id, self.token_a, (self.hop_ab, self.hop_ba))
        self.assertEqual(r1.route_id, r2.route_id)

        # Different base asset or hop produces different route_id
        r3 = RouteRef(self.chain_id, self.token_a, (self.hop_ab, self.hop_bc, self.hop_ca))
        self.assertNotEqual(r1.route_id, r3.route_id)


class TestQuoteEvidenceFailureDefense(unittest.TestCase):
    """A18: QuoteEvidence failure defense and invariant checks."""

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
        self.amount_out = Amount.from_atoms_str(self.token_a, "1050000", 6)

    def test_quoted_status_computes_delta_correctly(self) -> None:
        """Verify valid QUOTED status calculates positive delta."""
        quote = QuoteEvidence(
            quote_id="quote-001",
            route_ref=self.route,
            amount_in=self.amount_in,
            amount_out=self.amount_out,
            status=QuoteStatus.QUOTED,
        )
        self.assertEqual(quote.delta_atoms, 50000)
        self.assertEqual(quote.amount_out, self.amount_out)

    def test_non_quoted_status_forces_amount_out_and_delta_to_none(self) -> None:
        """Verify non-quoted status raises ValueError if amount_out or delta_atoms is provided."""
        for bad_status in (
            QuoteStatus.CONTRACT_REVERT,
            QuoteStatus.RPC_ERROR,
            QuoteStatus.NODE_LIMITATION,
            QuoteStatus.INVALID_RESPONSE,
            QuoteStatus.INCOMPLETE_STATE,
            QuoteStatus.STALE,
            QuoteStatus.UNSUPPORTED,
        ):
            # Proving failure defense: amount_out is not allowed when status != QUOTED
            with self.assertRaises(ValueError):
                QuoteEvidence(
                    quote_id="quote-fail-1",
                    route_ref=self.route,
                    amount_in=self.amount_in,
                    amount_out=self.amount_out,
                    status=bad_status,
                )

            with self.assertRaises(ValueError):
                QuoteEvidence(
                    quote_id="quote-fail-2",
                    route_ref=self.route,
                    amount_in=self.amount_in,
                    delta_atoms=50000,
                    status=bad_status,
                )

            # Clean creation without amount_out / delta_atoms succeeds
            clean_fail = QuoteEvidence(
                quote_id="quote-clean-fail",
                route_ref=self.route,
                amount_in=self.amount_in,
                status=bad_status,
                error="Upstream RPC timeout",
            )
            self.assertIsNone(clean_fail.amount_out)
            self.assertIsNone(clean_fail.delta_atoms)

    def test_preserve_preceding_successful_hops_on_route_revert(self) -> None:
        """Verify individual hop quote evidence is preserved even if the route overall reverted."""
        intermediate_amount = Amount.from_atoms_str(self.token_b, "2000000", 6)
        hop_quote_0 = HopQuote(
            hop_index=0,
            pool_key=self.pool_ab,
            asset_in=self.token_a,
            asset_out=self.token_b,
            amount_in=self.amount_in,
            amount_out=intermediate_amount,
            status=QuoteStatus.QUOTED,
        )
        hop_quote_1 = HopQuote(
            hop_index=1,
            pool_key=self.pool_ba,
            asset_in=self.token_b,
            asset_out=self.token_a,
            amount_in=intermediate_amount,
            amount_out=None,
            status=QuoteStatus.CONTRACT_REVERT,
            error="Pool slippage exceeded",
        )

        route_quote = QuoteEvidence(
            quote_id="quote-partial-001",
            route_ref=self.route,
            amount_in=self.amount_in,
            hop_quotes=(hop_quote_0, hop_quote_1),
            status=QuoteStatus.CONTRACT_REVERT,
            error="Hop 1 execution reverted",
        )
        self.assertIsNone(route_quote.amount_out)
        self.assertIsNone(route_quote.delta_atoms)
        self.assertEqual(len(route_quote.hop_quotes), 2)
        self.assertEqual(route_quote.hop_quotes[0].status, QuoteStatus.QUOTED)
        self.assertEqual(route_quote.hop_quotes[1].status, QuoteStatus.CONTRACT_REVERT)


class TestEconomicPayloadCoexistence(unittest.TestCase):
    """A19: Economic assessment and field coexistence invariant checks."""

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
        self.amount_out = Amount.from_atoms_str(self.token_a, "1050000", 6)

        self.gas_evidence = GasEvidence(
            gas_kind=GasEvidenceKind.RPC_ESTIMATE,
            gas_units=150000,
            gas_price_atoms=3000000000,
        )

    def test_missing_or_unknown_gas_prohibits_determined_net_atoms(self) -> None:
        """Verify net_atoms must be None when gas evidence is missing or unknown."""
        assessment = EconomicAssessment(
            net_atoms=30000,
            economic_status=EconomicStatus.PROFITABLE,
        )
        # Missing gas evidence
        with self.assertRaises(ValueError):
            QuoteEvidence(
                quote_id="quote-econ-1",
                route_ref=self.route,
                amount_in=self.amount_in,
                amount_out=self.amount_out,
                economic_assessment=assessment,
                fee_included=TriState.YES,
                gas_evidence=None,
            )

        # Unknown gas kind
        unknown_gas = GasEvidence(gas_kind=GasEvidenceKind.UNKNOWN)
        with self.assertRaises(ValueError):
            QuoteEvidence(
                quote_id="quote-econ-2",
                route_ref=self.route,
                amount_in=self.amount_in,
                amount_out=self.amount_out,
                economic_assessment=assessment,
                fee_included=TriState.YES,
                gas_evidence=unknown_gas,
            )

    def test_unknown_fee_included_prohibits_determined_net_atoms(self) -> None:
        """Verify fee_included=unknown prohibits claiming determined net_atoms."""
        assessment = EconomicAssessment(
            net_atoms=30000,
            economic_status=EconomicStatus.PROFITABLE,
        )
        with self.assertRaises(ValueError):
            QuoteEvidence(
                quote_id="quote-econ-3",
                route_ref=self.route,
                amount_in=self.amount_in,
                amount_out=self.amount_out,
                economic_assessment=assessment,
                fee_included=TriState.UNKNOWN,
                gas_evidence=self.gas_evidence,
            )

    def test_duplicate_fee_component_id_rejected(self) -> None:
        """Verify duplicate fee component IDs inside economic assessment are rejected fail-closed."""
        comp1 = FeeComponent("pool_fee_hop0", 500, self.token_a)
        comp2 = FeeComponent("pool_fee_hop0", 800, self.token_a)
        with self.assertRaises(ValueError):
            EconomicAssessment(
                fee_components=(comp1, comp2),
                economic_status=EconomicStatus.UNKNOWN,
            )

    def test_quoter_estimate_claiming_confirmed_actual_rejected(self) -> None:
        """Verify quoter gas estimate claiming confirmed actual assessment is rejected."""
        quoter_gas = GasEvidence(gas_kind=GasEvidenceKind.QUOTER_ESTIMATE, gas_units=120000)
        confirmed_assessment = EconomicAssessment(
            net_atoms=25000,
            economic_status=EconomicStatus.PROFITABLE,
            is_estimated=False,  # Contradiction: claimed confirmed while gas is quoter_estimate
        )
        with self.assertRaises(ValueError):
            QuoteEvidence(
                quote_id="quote-econ-contradiction",
                route_ref=self.route,
                amount_in=self.amount_in,
                amount_out=self.amount_out,
                economic_assessment=confirmed_assessment,
                fee_included=TriState.YES,
                gas_evidence=quoter_gas,
            )


class TestEvidenceLevelAndActorScope(unittest.TestCase):
    """A20: Evidence level and actor scope separation."""

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
        self.amount_out = Amount.from_atoms_str(self.token_a, "1050000", 6)

    def test_synthetic_cannot_claim_confirmed_execution(self) -> None:
        """Verify data_mode=synthetic cannot be promoted to CONFIRMED_EXECUTION."""
        with self.assertRaises(ValueError):
            QuoteEvidence(
                quote_id="quote-synth-001",
                route_ref=self.route,
                amount_in=self.amount_in,
                amount_out=self.amount_out,
                data_mode=DataMode.SYNTHETIC,
                evidence_level=EvidenceLevel.CONFIRMED_EXECUTION,
            )

    def test_hop_by_hop_quoter_cannot_claim_atomic_simulation(self) -> None:
        """Verify quoter hop gas estimates cannot be promoted to ATOMIC_SIMULATION."""
        quoter_gas = GasEvidence(gas_kind=GasEvidenceKind.QUOTER_ESTIMATE, gas_units=100000)
        with self.assertRaises(ValueError):
            QuoteEvidence(
                quote_id="quote-quoter-atomic",
                route_ref=self.route,
                amount_in=self.amount_in,
                amount_out=self.amount_out,
                evidence_level=EvidenceLevel.ATOMIC_SIMULATION,
                gas_evidence=quoter_gas,
            )

    def test_third_party_actor_scope_isolated(self) -> None:
        """Verify third party execution preserves actor_scope=third_party without pretending to be own."""
        quote = QuoteEvidence(
            quote_id="quote-third-party-001",
            route_ref=self.route,
            amount_in=self.amount_in,
            amount_out=self.amount_out,
            data_mode=DataMode.CONFIRMED_CHAIN_HISTORY,
            actor_scope=ActorScope.THIRD_PARTY,
            evidence_level=EvidenceLevel.CONFIRMED_EXECUTION,
        )
        self.assertEqual(quote.actor_scope, ActorScope.THIRD_PARTY)
        self.assertEqual(quote.data_mode, DataMode.CONFIRMED_CHAIN_HISTORY)


if __name__ == "__main__":
    unittest.main()
