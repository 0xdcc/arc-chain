"""Boundary and contract tests for arbitrage domain types.

Verifies:
1. 6-decimal (USDG/USDC) vs 18-decimal (WETH) precision and lossless conversions.
2. Address and decimals validation / error rejection.
3. Invariant: Failed QuoteResult MUST have amount_out=None and delta_atoms=None.
4. Slippage safety: ExecutionPlan rejects min_amount_out.atoms <= 0.
5. Cycle integrity: CandidateRoute must be a strictly closed cycle on base_token.
6. JSON / dict roundtrip lossless fidelity (integers stay int, Decimal stays precise).
7. Domain module purity: zero network, thread, file writing or heavyweight side effects.
"""

import ast
import importlib
import json
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from research.market_data.types import (
    CandidateRoute,
    ExecutionPlan,
    MarketSnapshot,
    PoolIdentity,
    PoolStateSnapshot,
    QuoteResult,
    QuoteStatus,
    RouteHop,
    TokenAmount,
    TokenIdentity,
    from_dict,
    to_dict,
)


@pytest.fixture
def token_usdg() -> TokenIdentity:
    return TokenIdentity(
        chain_id=4663,
        address="0x1111111111111111111111111111111111111111",
        decimals=6,
        symbol="USDG",
    )


@pytest.fixture
def token_weth() -> TokenIdentity:
    return TokenIdentity(
        chain_id=4663,
        address="0x2222222222222222222222222222222222222222",
        decimals=18,
        symbol="WETH",
    )


@pytest.fixture
def token_wbtc() -> TokenIdentity:
    return TokenIdentity(
        chain_id=4663,
        address="0x3333333333333333333333333333333333333333",
        decimals=8,
        symbol="WBTC",
    )


@pytest.fixture
def pool_v3_usdg_weth(token_usdg: TokenIdentity, token_weth: TokenIdentity) -> PoolIdentity:
    return PoolIdentity(
        chain_id=4663,
        protocol="uniswap_v3",
        pool_id="0x4444444444444444444444444444444444444444",
        token0=token_usdg.address,
        token1=token_weth.address,
        fee_bps=5.0,
        tick_spacing=10,
        verified_source="on_chain",
    )


@pytest.fixture
def pool_v4_usdg_weth(token_usdg: TokenIdentity, token_weth: TokenIdentity) -> PoolIdentity:
    return PoolIdentity(
        chain_id=4663,
        protocol="uniswap_v4",
        pool_id="0x" + "ab" * 32,
        token0=token_usdg.address,
        token1=token_weth.address,
        fee_bps=5.0,
        tick_spacing=10,
        hooks="0x0000000000000000000000000000000000000000",
        verified_source="on_chain",
    )


class TestTokenAmountPrecision:
    """Test 6-decimal (USDG/USDC) vs 18-decimal (WETH) conversions and boundary precision."""

    def test_6_decimal_precision(self, token_usdg: TokenIdentity):
        # 1 USDG = 1,000,000 atoms
        one_usdg = TokenAmount(token=token_usdg, atoms=1_000_000)
        assert one_usdg.to_decimal() == Decimal("1.0")

        # Fractional value with exact 6 decimals
        val = Decimal("123.456789")
        amt = TokenAmount.from_decimal(token_usdg, val)
        assert amt.atoms == 123456789
        assert amt.to_decimal() == val

        # String input
        amt_str = TokenAmount.from_decimal(token_usdg, "500.000001")
        assert amt_str.atoms == 500000001
        assert amt_str.to_decimal() == Decimal("500.000001")

        # Zero value
        zero_usdg = TokenAmount(token=token_usdg, atoms=0)
        assert zero_usdg.to_decimal() == Decimal("0")

    def test_18_decimal_precision(self, token_weth: TokenIdentity):
        # 1 WETH = 10^18 atoms
        one_weth = TokenAmount(token=token_weth, atoms=10**18)
        assert one_weth.to_decimal() == Decimal("1.0")

        # 1 wei (smallest atom)
        one_wei = TokenAmount(token=token_weth, atoms=1)
        assert one_wei.to_decimal() == Decimal("0.000000000000000001")

        amt_from_wei = TokenAmount.from_decimal(token_weth, Decimal("0.000000000000000001"))
        assert amt_from_wei.atoms == 1

        # Large value with full 18 decimal places without float rounding corruption
        huge_val = Decimal("1000000.123456789012345678")
        huge_amt = TokenAmount.from_decimal(token_weth, huge_val)
        assert huge_amt.atoms == 1000000123456789012345678
        assert huge_amt.to_decimal() == huge_val

    def test_token_amount_invalid_inputs(self, token_usdg: TokenIdentity):
        # Negative atoms must be rejected
        with pytest.raises(ValueError, match="atoms cannot be negative"):
            TokenAmount(token=token_usdg, atoms=-1)

        # Float atoms must be rejected (type check)
        bad_float_atoms: Any = 100.5
        with pytest.raises(TypeError, match="atoms must be an integer"):
            TokenAmount(token=token_usdg, atoms=bad_float_atoms)

        # Boolean atoms must be rejected
        with pytest.raises(TypeError, match="atoms must be an integer"):
            TokenAmount(token=token_usdg, atoms=True)


class TestIdentityValidation:
    """Test validation and error interception for addresses, decimals, and pool configs."""

    @pytest.mark.parametrize(
        "invalid_addr,error_msg",
        [
            ("4663111111111111111111111111111111111111", "must start with '0x'"),
            ("0x1234", "be 42 characters"),
            ("0x" + "11" * 21, "be 42 characters"),
            ("0x" + "g" * 40, "valid hex string"),
            (None, "must be a string"),
            (12345, "must be a string"),
        ],
    )
    def test_invalid_token_addresses(self, invalid_addr, error_msg):
        with pytest.raises((ValueError, TypeError), match=error_msg):
            TokenIdentity(chain_id=4663, address=invalid_addr, decimals=18, symbol="TEST")

    @pytest.mark.parametrize("invalid_decimals", [-1, 19, 100])
    def test_invalid_decimals_range(self, invalid_decimals):
        with pytest.raises(ValueError, match="decimals must be between 0 and 18"):
            TokenIdentity(
                chain_id=4663,
                address="0x" + "11" * 20,
                decimals=invalid_decimals,
                symbol="TEST",
            )

    @pytest.mark.parametrize("invalid_decimals_type", ["18", 18.0, None, True])
    def test_invalid_decimals_type(self, invalid_decimals_type: Any) -> None:
        with pytest.raises(TypeError, match="decimals must be an integer"):
            TokenIdentity(
                chain_id=4663,
                address="0x" + "11" * 20,
                decimals=invalid_decimals_type,
                symbol="TEST",
            )

    def test_invalid_chain_id(self):
        with pytest.raises(ValueError, match="chain_id must be a positive integer"):
            TokenIdentity(chain_id=0, address="0x" + "11" * 20, decimals=18, symbol="TEST")
        with pytest.raises(ValueError, match="chain_id must be a positive integer"):
            TokenIdentity(chain_id=-1, address="0x" + "11" * 20, decimals=18, symbol="TEST")

    def test_pool_identity_normalization(self):
        # Mix uppercase addresses
        p = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v3",
            pool_id="0x" + "AB" * 20,
            token0="0x" + "CC" * 20,
            token1="0x" + "DD" * 20,
            fee_bps=5.0,
            tick_spacing=10,
            hooks="0x" + "EE" * 20,
        )
        assert p.pool_id == ("0x" + "ab" * 20)
        assert p.token0 == ("0x" + "cc" * 20)
        assert p.token1 == ("0x" + "dd" * 20)
        assert p.hooks == ("0x" + "ee" * 20)

    def test_pool_identity_identical_tokens_rejected(self):
        with pytest.raises(ValueError, match="cannot be the same address"):
            PoolIdentity(
                chain_id=4663,
                protocol="uniswap_v3",
                pool_id="0x" + "11" * 20,
                token0="0x" + "22" * 20,
                token1="0x" + "22" * 20,
                fee_bps=5.0,
                tick_spacing=10,
            )

    def test_pool_identity_invalid_source_rejected(self):
        with pytest.raises(ValueError, match="verified_source must be one of"):
            PoolIdentity(
                chain_id=4663,
                protocol="uniswap_v3",
                pool_id="0x" + "11" * 20,
                token0="0x" + "22" * 20,
                token1="0x" + "33" * 20,
                fee_bps=5.0,
                tick_spacing=10,
                verified_source="bogus_source",
            )


class TestQuoteResultInvariance:
    """Verify invariants: Failed QuoteResult MUST have amount_out=None and delta_atoms=None."""

    @pytest.mark.parametrize(
        "failed_status",
        [
            QuoteStatus.CONTRACT_REVERT,
            QuoteStatus.RPC_ERROR,
            QuoteStatus.NODE_LIMITATION,
            QuoteStatus.INVALID_RESPONSE,
        ],
    )
    def test_failed_quote_must_reject_outputs(self, failed_status, token_usdg: TokenIdentity):
        amt_in = TokenAmount(token=token_usdg, atoms=1000_000)
        amt_out = TokenAmount(token=token_usdg, atoms=1010_000)

        # Rejects amount_out != None
        with pytest.raises(ValueError, match="must have amount_out=None and delta_atoms=None"):
            QuoteResult(
                status=failed_status,
                amount_in=amt_in,
                amount_out=amt_out,
                delta_atoms=None,
                error_message="Simulated error",
            )

        # Rejects delta_atoms != None
        with pytest.raises(ValueError, match="must have amount_out=None and delta_atoms=None"):
            QuoteResult(
                status=failed_status,
                amount_in=amt_in,
                amount_out=None,
                delta_atoms=10_000,
                error_message="Simulated error",
            )

        # Legitimate failed quote construction
        valid_fail = QuoteResult(
            status=failed_status,
            amount_in=amt_in,
            amount_out=None,
            delta_atoms=None,
            error_code=500,
            error_message="Execution reverted",
        )
        assert valid_fail.amount_out is None
        assert valid_fail.delta_atoms is None
        assert valid_fail.status == failed_status

    def test_successful_quote_invariants(self, token_usdg: TokenIdentity):
        amt_in = TokenAmount(token=token_usdg, atoms=1000_000)
        amt_out = TokenAmount(token=token_usdg, atoms=1050_000)

        # Rejects amount_out=None on QUOTED
        with pytest.raises(ValueError, match="Successful quote .* must have amount_out"):
            QuoteResult(
                status=QuoteStatus.QUOTED,
                amount_in=amt_in,
                amount_out=None,
                delta_atoms=None,
            )

        # Auto calculates delta_atoms if omitted and same token
        res = QuoteResult(
            status=QuoteStatus.QUOTED,
            amount_in=amt_in,
            amount_out=amt_out,
            delta_atoms=None,
            gas_estimate=150000,
        )
        assert res.amount_out is not None
        assert res.delta_atoms == 50_000


class TestExecutionPlanSlippageSafety:
    """Verify Slippage Safety Red Line: min_amount_out.atoms MUST strictly be > 0."""

    def test_min_amount_out_zero_rejected(
        self,
        token_usdg: TokenIdentity,
        token_weth: TokenIdentity,
        pool_v3_usdg_weth: PoolIdentity,
        pool_v4_usdg_weth: PoolIdentity,
    ):
        hop1 = RouteHop(pool=pool_v3_usdg_weth, token_in=token_usdg, token_out=token_weth)
        hop2 = RouteHop(pool=pool_v4_usdg_weth, token_in=token_weth, token_out=token_usdg)

        amt_in = TokenAmount(token=token_usdg, atoms=1000_000)
        amt_zero = TokenAmount(token=token_usdg, atoms=0)

        with pytest.raises(ValueError, match="min_amount_out.atoms must be > 0"):
            ExecutionPlan(
                plan_id="plan_001",
                candidate_id="cand_001",
                route_type="two_hop_spread",
                base_token=token_usdg,
                amount_in=amt_in,
                min_amount_out=amt_zero,
                hops=(hop1, hop2),
                quoter_block=123456,
                deadline=1700000000,
                estimated_gas_usd=Decimal("0.15"),
                target_router="0x" + "55" * 20,
            )

    def test_amount_in_zero_rejected(
        self,
        token_usdg: TokenIdentity,
        token_weth: TokenIdentity,
        pool_v3_usdg_weth: PoolIdentity,
        pool_v4_usdg_weth: PoolIdentity,
    ):
        hop1 = RouteHop(pool=pool_v3_usdg_weth, token_in=token_usdg, token_out=token_weth)
        hop2 = RouteHop(pool=pool_v4_usdg_weth, token_in=token_weth, token_out=token_usdg)

        amt_in_zero = TokenAmount(token=token_usdg, atoms=0)
        amt_min_out = TokenAmount(token=token_usdg, atoms=990_000)

        with pytest.raises(ValueError, match="amount_in.atoms must be > 0"):
            ExecutionPlan(
                plan_id="plan_001",
                candidate_id="cand_001",
                route_type="two_hop_spread",
                base_token=token_usdg,
                amount_in=amt_in_zero,
                min_amount_out=amt_min_out,
                hops=(hop1, hop2),
                quoter_block=123456,
                deadline=1700000000,
                estimated_gas_usd=Decimal("0.15"),
                target_router="0x" + "55" * 20,
            )

    def test_token_mismatch_with_base_token_rejected(
        self,
        token_usdg: TokenIdentity,
        token_weth: TokenIdentity,
        pool_v3_usdg_weth: PoolIdentity,
        pool_v4_usdg_weth: PoolIdentity,
    ):
        hop1 = RouteHop(pool=pool_v3_usdg_weth, token_in=token_usdg, token_out=token_weth)
        hop2 = RouteHop(pool=pool_v4_usdg_weth, token_in=token_weth, token_out=token_usdg)

        amt_in = TokenAmount(token=token_usdg, atoms=1000_000)
        # min_amount_out uses WETH instead of base_token USDG
        amt_min_out_weth = TokenAmount(token=token_weth, atoms=10**17)

        with pytest.raises(ValueError, match="does not match base_token"):
            ExecutionPlan(
                plan_id="plan_001",
                candidate_id="cand_001",
                route_type="two_hop_spread",
                base_token=token_usdg,
                amount_in=amt_in,
                min_amount_out=amt_min_out_weth,
                hops=(hop1, hop2),
                quoter_block=123456,
                deadline=1700000000,
                estimated_gas_usd=Decimal("0.15"),
                target_router="0x" + "55" * 20,
            )


class TestCandidateRouteClosedCycle:
    """Verify CandidateRoute closed cycle invariants and hop continuity."""

    def test_valid_two_hop_cycle(
        self,
        token_usdg: TokenIdentity,
        token_weth: TokenIdentity,
        pool_v3_usdg_weth: PoolIdentity,
        pool_v4_usdg_weth: PoolIdentity,
    ):
        hop1 = RouteHop(pool=pool_v3_usdg_weth, token_in=token_usdg, token_out=token_weth)
        hop2 = RouteHop(pool=pool_v4_usdg_weth, token_in=token_weth, token_out=token_usdg)

        route = CandidateRoute(
            candidate_id="cand_2hop",
            route_type="two_hop_spread",
            base_token=token_usdg,
            hops=(hop1, hop2),
            observed_gross_bps=12.5,
            snapshot_block=1000,
            created_at=1700000000.0,
        )
        assert route.hops[0].token_in.address.lower() == token_usdg.address.lower()
        assert route.hops[-1].token_out.address.lower() == token_usdg.address.lower()
        assert len(route.hops) == 2

    def test_valid_three_hop_triangular_cycle(
        self,
        token_usdg: TokenIdentity,
        token_weth: TokenIdentity,
        token_wbtc: TokenIdentity,
    ):
        pool_usdg_weth = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v3",
            pool_id="0x" + "41" * 20,
            token0=token_usdg.address,
            token1=token_weth.address,
            fee_bps=5.0,
            tick_spacing=10,
        )
        pool_weth_wbtc = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v3",
            pool_id="0x" + "42" * 20,
            token0=token_wbtc.address,
            token1=token_weth.address,
            fee_bps=5.0,
            tick_spacing=10,
        )
        pool_wbtc_usdg = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v3",
            pool_id="0x" + "43" * 20,
            token0=token_usdg.address,
            token1=token_wbtc.address,
            fee_bps=5.0,
            tick_spacing=10,
        )

        hop1 = RouteHop(pool=pool_usdg_weth, token_in=token_usdg, token_out=token_weth)
        hop2 = RouteHop(pool=pool_weth_wbtc, token_in=token_weth, token_out=token_wbtc)
        hop3 = RouteHop(pool=pool_wbtc_usdg, token_in=token_wbtc, token_out=token_usdg)

        route = CandidateRoute(
            candidate_id="cand_triangular",
            route_type="triangular",
            base_token=token_usdg,
            hops=(hop1, hop2, hop3),
            observed_gross_bps=35.0,
            snapshot_block=1000,
            created_at=1700000000.0,
        )
        assert len(route.hops) == 3
        assert route.hops[0].token_in == token_usdg
        assert route.hops[-1].token_out == token_usdg

    def test_rejected_when_start_not_base_token(
        self,
        token_usdg: TokenIdentity,
        token_weth: TokenIdentity,
        pool_v3_usdg_weth: PoolIdentity,
        pool_v4_usdg_weth: PoolIdentity,
    ):
        hop1 = RouteHop(pool=pool_v3_usdg_weth, token_in=token_weth, token_out=token_usdg)
        hop2 = RouteHop(pool=pool_v4_usdg_weth, token_in=token_usdg, token_out=token_weth)

        with pytest.raises(ValueError, match="Route must start with base_token USDG"):
            CandidateRoute(
                candidate_id="cand_bad_start",
                route_type="two_hop_spread",
                base_token=token_usdg,  # base is USDG, but hop1 starts with WETH
                hops=(hop1, hop2),
                observed_gross_bps=10.0,
                snapshot_block=1000,
                created_at=1700000000.0,
            )

    def test_rejected_when_end_not_base_token(
        self,
        token_usdg: TokenIdentity,
        token_weth: TokenIdentity,
        token_wbtc: TokenIdentity,
        pool_v3_usdg_weth: PoolIdentity,
    ):
        pool_weth_wbtc = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v3",
            pool_id="0x" + "42" * 20,
            token0=token_wbtc.address,
            token1=token_weth.address,
            fee_bps=5.0,
            tick_spacing=10,
        )
        hop1 = RouteHop(pool=pool_v3_usdg_weth, token_in=token_usdg, token_out=token_weth)
        hop2 = RouteHop(pool=pool_weth_wbtc, token_in=token_weth, token_out=token_wbtc)

        with pytest.raises(ValueError, match="Route must end with base_token USDG"):
            CandidateRoute(
                candidate_id="cand_bad_end",
                route_type="two_hop_spread",
                base_token=token_usdg,  # ends with WBTC, not USDG
                hops=(hop1, hop2),
                observed_gross_bps=10.0,
                snapshot_block=1000,
                created_at=1700000000.0,
            )

    def test_rejected_when_hops_discontinuous(
        self,
        token_usdg: TokenIdentity,
        token_weth: TokenIdentity,
        token_wbtc: TokenIdentity,
        pool_v3_usdg_weth: PoolIdentity,
    ):
        pool_wbtc_usdg = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v3",
            pool_id="0x" + "43" * 20,
            token0=token_usdg.address,
            token1=token_wbtc.address,
            fee_bps=5.0,
            tick_spacing=10,
        )
        hop1 = RouteHop(pool=pool_v3_usdg_weth, token_in=token_usdg, token_out=token_weth)
        # Missing WETH->WBTC step; jumps directly from WBTC->USDG
        hop2 = RouteHop(pool=pool_wbtc_usdg, token_in=token_wbtc, token_out=token_usdg)

        with pytest.raises(ValueError, match="Discontinuous hops at step 0->1"):
            CandidateRoute(
                candidate_id="cand_gap",
                route_type="triangular",
                base_token=token_usdg,
                hops=(hop1, hop2),
                observed_gross_bps=10.0,
                snapshot_block=1000,
                created_at=1700000000.0,
            )

    def test_route_hop_token_mismatch(
        self, token_usdg: TokenIdentity, token_wbtc: TokenIdentity, pool_v3_usdg_weth: PoolIdentity
    ):
        # Pool has USDG and WETH, but hop tries to use WBTC
        with pytest.raises(ValueError, match="Hop tokens .* do not match pool tokens"):
            RouteHop(pool=pool_v3_usdg_weth, token_in=token_usdg, token_out=token_wbtc)


class TestJsonDictRoundtripPrecision:
    """Verify JSON / dict roundtrip without any precision loss or type mutation."""

    def test_token_identity_roundtrip(self, token_weth: TokenIdentity):
        d = to_dict(token_weth)
        json_str = json.dumps(d)
        restored = from_dict(TokenIdentity, json.loads(json_str))
        assert restored == token_weth

    def test_large_integer_atoms_roundtrip_fidelity(self, token_weth: TokenIdentity):
        # Big atoms that might lose precision if converted to float (e.g. > 2^53)
        huge_atoms = 123456789012345678901234567890
        amt = TokenAmount(token=token_weth, atoms=huge_atoms)

        d = to_dict(amt)
        assert isinstance(d["atoms"], int)
        json_str = json.dumps(d)

        restored = from_dict(TokenAmount, json.loads(json_str))
        assert restored == amt
        assert isinstance(restored.atoms, int)
        assert restored.atoms == huge_atoms

    def test_market_snapshot_roundtrip(self, pool_v3_usdg_weth: PoolIdentity):
        pool_snap = PoolStateSnapshot(
            pool=pool_v3_usdg_weth,
            block_number=1234567,
            block_timestamp=1700000000,
            sqrt_price_x96=79228162514264337593543950336,
            liquidity=1000000000000000,
            tick=200,
            raw_response={"foo": "bar"},
        )
        market_snap = MarketSnapshot(
            chain_id=4663,
            block_number=1234567,
            captured_at=1700000000.123,
            pools={pool_v3_usdg_weth.pool_id: pool_snap},
        )

        d = to_dict(market_snap)
        json_str = json.dumps(d)
        restored = from_dict(MarketSnapshot, json.loads(json_str))
        assert restored == market_snap
        assert restored.pools[pool_v3_usdg_weth.pool_id].sqrt_price_x96 == pool_snap.sqrt_price_x96

    def test_candidate_route_roundtrip(
        self,
        token_usdg: TokenIdentity,
        token_weth: TokenIdentity,
        pool_v3_usdg_weth: PoolIdentity,
        pool_v4_usdg_weth: PoolIdentity,
    ):
        hop1 = RouteHop(pool=pool_v3_usdg_weth, token_in=token_usdg, token_out=token_weth)
        hop2 = RouteHop(pool=pool_v4_usdg_weth, token_in=token_weth, token_out=token_usdg)
        route = CandidateRoute(
            candidate_id="cand_test_001",
            route_type="two_hop_spread",
            base_token=token_usdg,
            hops=(hop1, hop2),
            observed_gross_bps=15.5,
            snapshot_block=50000,
            created_at=1700001234.56,
        )

        d = to_dict(route)
        json_str = json.dumps(d)
        restored = from_dict(CandidateRoute, json.loads(json_str))
        assert restored == route

    def test_execution_plan_roundtrip(
        self,
        token_usdg: TokenIdentity,
        token_weth: TokenIdentity,
        pool_v3_usdg_weth: PoolIdentity,
        pool_v4_usdg_weth: PoolIdentity,
    ):
        hop1 = RouteHop(pool=pool_v3_usdg_weth, token_in=token_usdg, token_out=token_weth)
        hop2 = RouteHop(pool=pool_v4_usdg_weth, token_in=token_weth, token_out=token_usdg)

        plan = ExecutionPlan(
            plan_id="plan_abc_123",
            candidate_id="cand_test_001",
            route_type="two_hop_spread",
            base_token=token_usdg,
            amount_in=TokenAmount(token=token_usdg, atoms=500_000_000),
            min_amount_out=TokenAmount(token=token_usdg, atoms=501_200_000),
            hops=(hop1, hop2),
            quoter_block=50001,
            deadline=1700001800,
            estimated_gas_usd=Decimal("0.087654321"),
            target_router="0x" + "88" * 20,
        )

        d = to_dict(plan)
        json_str = json.dumps(d)
        restored = from_dict(ExecutionPlan, json.loads(json_str))
        assert restored == plan
        assert restored.estimated_gas_usd == Decimal("0.087654321")
        assert isinstance(restored.estimated_gas_usd, Decimal)


class TestDomainModulePurity:
    """Verify zero external dependency, circular import immunity, and zero side effects."""

    @staticmethod
    def _is_module_path_allowed(module_path: str) -> bool:
        """Check if an imported module or submodule path is strictly permitted."""
        allowed_exact = {"collections.abc"}
        allowed_top_level = {"dataclasses", "decimal", "enum", "typing", "types"}
        if module_path in allowed_exact:
            return True
        top = module_path.split(".")[0]
        if top in allowed_top_level:
            return True
        return False

    @classmethod
    def _extract_imported_modules(cls, tree: ast.AST) -> set[str]:
        imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported_modules.add(node.module)
        return imported_modules

    def test_pure_standard_library_ast_audit(self) -> None:
        """Perform strict AST audit to verify arbitrage/domain only imports standard library."""
        domain_types_path = (
            Path(__file__).resolve().parent.parent / "research" / "market_data" / "types.py"
        )
        assert domain_types_path.exists(), f"File {domain_types_path} does not exist"

        tree = ast.parse(domain_types_path.read_text(encoding="utf-8"))

        allowed_modules = {
            "dataclasses",
            "decimal",
            "enum",
            "typing",
            "types",
            "collections.abc",
        }
        imported_modules = self._extract_imported_modules(tree)

        forbidden = {m for m in imported_modules if not self._is_module_path_allowed(m)}
        assert not forbidden, (
            f"research/market_data/types.py imports forbidden non-stdlib modules: {forbidden}"
        )

    def test_pure_standard_library_ast_audit_negative_controls(self) -> None:
        """Negative control: verify checking predicate rejects requests, socket, subprocess, and unpermitted collections paths."""
        rejected_snippets = [
            "import requests",
            "import socket",
            "import subprocess",
            "import collections",
            "from collections import defaultdict",
            "from collections import deque",
            "from collections import Counter",
            "import collections.defaultdict",
            "from collections.abc import Mapping\nimport requests",
        ]
        for snippet in rejected_snippets:
            tree = ast.parse(snippet)
            imported = self._extract_imported_modules(tree)
            forbidden = {m for m in imported if not self._is_module_path_allowed(m)}
            assert forbidden, (
                f"Negative control failed: snippet '{snippet}' must be rejected by purity predicate"
            )

        disallowed_targets = [
            "requests",
            "socket",
            "subprocess",
            "collections",
            "collections.defaultdict",
            "collections.deque",
            "collections.OrderedDict",
        ]
        for mod in disallowed_targets:
            assert not self._is_module_path_allowed(mod), (
                f"Purity predicate must reject forbidden module '{mod}'"
            )

        permitted_targets = [
            "dataclasses",
            "decimal",
            "enum",
            "typing",
            "types",
            "collections.abc",
        ]
        for mod in permitted_targets:
            assert self._is_module_path_allowed(mod), (
                f"Purity predicate must permit allowed module '{mod}'"
            )

    def test_no_circular_imports_on_reload(self):
        """Verify module reloads cleanly without circular import deadlock."""
        from pathlib import Path

        types_path = Path(__file__).resolve().parents[1] / "research" / "market_data" / "types.py"
        code = types_path.read_text(encoding="utf-8")
        namespace: dict[str, object] = {"__name__": "isolated_types"}
        exec(code, namespace)
        assert "TokenIdentity" in namespace
        assert "ExecutionPlan" in namespace

    def test_zero_thread_side_effects(self):
        """Verify importing domain models does not spawn threads or background tasks."""
        initial_threads = threading.active_count()
        import research.market_data

        after_threads = threading.active_count()
        assert initial_threads == after_threads
