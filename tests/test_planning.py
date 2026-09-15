"""Contract and risk guardrail test suite for research/planning.py."""

from __future__ import annotations

import ast
import time
from decimal import Decimal
from pathlib import Path

import pytest

from atomic_execution.policy import ExcessiveAmountError
from research.market_data.types import (
    CandidateRoute,
    ExecutionPlan,
    PoolIdentity,
    QuoteResult,
    QuoteStatus,
    RouteHop,
    TokenAmount,
    TokenIdentity,
    from_dict,
    to_dict,
)
from research.planning import (
    CANONICAL_UNIVERSAL_ROUTER,
    HARD_CAP_MAX_USD,
    build_execution_plan,
)

# ---------------------------------------------------------------------------
# Test Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def token_weth() -> TokenIdentity:
    return TokenIdentity(
        chain_id=4663,
        address="0x2222222222222222222222222222222222222222",
        decimals=18,
        symbol="WETH",
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
def pool_v3(token_weth: TokenIdentity, token_usdg: TokenIdentity) -> PoolIdentity:
    return PoolIdentity(
        chain_id=4663,
        protocol="uniswap_v3",
        pool_id="0x3333333333333333333333333333333333333333",
        token0=token_usdg.address,
        token1=token_weth.address,
        fee_bps=5.0,
        tick_spacing=10,
        verified_source="on_chain",
    )


@pytest.fixture
def pool_v4(token_weth: TokenIdentity, token_usdg: TokenIdentity) -> PoolIdentity:
    return PoolIdentity(
        chain_id=4663,
        protocol="uniswap_v4",
        pool_id="0x" + "aa" * 32,
        token0=token_usdg.address,
        token1=token_weth.address,
        fee_bps=5.0,
        tick_spacing=10,
        hooks="0x0000000000000000000000000000000000000000",
        verified_source="on_chain",
    )


@pytest.fixture
def weth_candidate_route(
    token_weth: TokenIdentity,
    token_usdg: TokenIdentity,
    pool_v3: PoolIdentity,
    pool_v4: PoolIdentity,
) -> CandidateRoute:
    hop1 = RouteHop(pool=pool_v3, token_in=token_weth, token_out=token_usdg)
    hop2 = RouteHop(pool=pool_v4, token_in=token_usdg, token_out=token_weth)
    return CandidateRoute(
        candidate_id="cand_weth_cycle_01",
        route_type="two_hop_spread",
        base_token=token_weth,
        hops=(hop1, hop2),
        observed_gross_bps=35.0,
        snapshot_block=60001,
        created_at=1700000000.0,
    )


@pytest.fixture
def usdg_candidate_route(
    token_weth: TokenIdentity,
    token_usdg: TokenIdentity,
    pool_v3: PoolIdentity,
    pool_v4: PoolIdentity,
) -> CandidateRoute:
    hop1 = RouteHop(pool=pool_v4, token_in=token_usdg, token_out=token_weth)
    hop2 = RouteHop(pool=pool_v3, token_in=token_weth, token_out=token_usdg)
    return CandidateRoute(
        candidate_id="cand_usdg_cycle_01",
        route_type="two_hop_spread",
        base_token=token_usdg,
        hops=(hop1, hop2),
        observed_gross_bps=42.0,
        snapshot_block=60002,
        created_at=1700000005.0,
    )


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------


class TestPlanningWethCycle:
    """WETH closed-loop arbitrage execution plan assembly tests."""

    def test_weth_cycle_success(
        self,
        token_weth: TokenIdentity,
        weth_candidate_route: CandidateRoute,
    ) -> None:
        amount_in = TokenAmount(token=token_weth, atoms=1_000_000_000_000_000_000)  # 1 WETH
        amount_out = TokenAmount(token=token_weth, atoms=1_020_000_000_000_000_000)  # 1.02 WETH
        quote = QuoteResult(
            status=QuoteStatus.QUOTED,
            amount_in=amount_in,
            amount_out=amount_out,
            delta_atoms=20_000_000_000_000_000,
            gas_estimate=150000,
            block_number=60005,
        )

        plan = build_execution_plan(
            route=weth_candidate_route,
            quote=quote,
            amount_usd=300.0,
            slippage_pct=0.5,
            deadline_seconds=90,
            gas_atoms=3_000_000_000_000_000,
            estimated_gas_usd=Decimal("0.10"),
        )

        assert isinstance(plan, ExecutionPlan)
        assert plan.candidate_id == "cand_weth_cycle_01"
        assert plan.route_type == "two_hop_spread"
        assert plan.base_token == token_weth
        assert plan.amount_in == amount_in
        assert plan.hops == weth_candidate_route.hops
        assert plan.quoter_block == 60005
        assert plan.target_router == CANONICAL_UNIVERSAL_ROUTER

        expected_min_atoms = int(amount_out.atoms * (1.0 - 0.5 / 100.0))
        assert plan.min_amount_out.atoms == expected_min_atoms
        assert plan.min_amount_out.atoms > 0
        assert plan.min_amount_out.token == token_weth

        # Check deadline
        now = time.time()
        assert plan.deadline >= int(now) + 85
        assert plan.deadline <= int(now) + 95


class TestPlanningUsdgCycle:
    """USDG closed-loop arbitrage execution plan assembly tests."""

    def test_usdg_cycle_success_with_custom_router(
        self,
        token_usdg: TokenIdentity,
        usdg_candidate_route: CandidateRoute,
    ) -> None:
        amount_in = TokenAmount(token=token_usdg, atoms=100_000_000)  # 100 USDG
        amount_out = TokenAmount(token=token_usdg, atoms=101_500_000)  # 101.5 USDG
        quote = QuoteResult(
            status=QuoteStatus.QUOTED,
            amount_in=amount_in,
            amount_out=amount_out,
            delta_atoms=1_500_000,
            gas_estimate=120000,
            block_number=60010,
        )

        custom_router = "0x" + "77" * 20
        plan = build_execution_plan(
            route=usdg_candidate_route,
            quote=quote,
            amount_usd=100.0,
            slippage_pct=1.0,
            deadline_seconds=180,
            target_router=custom_router,
            plan_id="plan_custom_usdg_01",
            current_time=1700000000.0,
            gas_atoms=50_000,
            estimated_gas_usd=Decimal("0.05"),
        )

        assert plan.plan_id == "plan_custom_usdg_01"
        assert plan.base_token == token_usdg
        assert plan.target_router == custom_router
        assert plan.deadline == 1700000180
        assert plan.estimated_gas_usd == Decimal("0.05")

        expected_min_atoms = int(101_500_000 * 0.99)
        assert plan.min_amount_out.atoms == expected_min_atoms
        assert plan.min_amount_out.atoms > 0

        # Verify full dict roundtrip fidelity
        d = to_dict(plan)
        restored = from_dict(ExecutionPlan, d)
        assert restored == plan


class TestSlippageGuardrails:
    """Strict slippage red line tests (MEV sandwich protection)."""

    def test_excessive_slippage_100_pct_rejected(
        self,
        token_weth: TokenIdentity,
        weth_candidate_route: CandidateRoute,
    ) -> None:
        quote = QuoteResult(
            status=QuoteStatus.QUOTED,
            amount_in=TokenAmount(token=token_weth, atoms=1_000_000_000_000_000_000),
            amount_out=TokenAmount(token=token_weth, atoms=1_020_000_000_000_000_000),
            delta_atoms=20_000_000_000_000_000,
        )

        with pytest.raises(ValueError, match="min_amount_out_atoms must be > 0"):
            build_execution_plan(
                route=weth_candidate_route,
                quote=quote,
                amount_usd=200.0,
                slippage_pct=100.0,
                gas_atoms=3_000_000_000_000_000,
            )

    def test_excessive_slippage_over_100_pct_rejected(
        self,
        token_weth: TokenIdentity,
        weth_candidate_route: CandidateRoute,
    ) -> None:
        quote = QuoteResult(
            status=QuoteStatus.QUOTED,
            amount_in=TokenAmount(token=token_weth, atoms=1_000_000_000_000_000_000),
            amount_out=TokenAmount(token=token_weth, atoms=1_020_000_000_000_000_000),
            delta_atoms=20_000_000_000_000_000,
        )

        with pytest.raises(ValueError, match="min_amount_out_atoms must be > 0"):
            build_execution_plan(
                route=weth_candidate_route,
                quote=quote,
                amount_usd=200.0,
                slippage_pct=110.0,
                gas_atoms=3_000_000_000_000_000,
            )

    def test_tiny_amount_out_slippage_rounds_to_zero_rejected(
        self,
        token_weth: TokenIdentity,
        weth_candidate_route: CandidateRoute,
    ) -> None:
        quote = QuoteResult(
            status=QuoteStatus.QUOTED,
            amount_in=TokenAmount(token=token_weth, atoms=1),
            amount_out=TokenAmount(token=token_weth, atoms=1),
            delta_atoms=0,
        )

        # 50% slippage on 1 atom -> int(1 * 0.5) == 0 atoms -> must be intercepted!
        with pytest.raises(ValueError, match="min_amount_out_atoms must be > 0"):
            build_execution_plan(
                route=weth_candidate_route,
                quote=quote,
                amount_usd=1.0,
                slippage_pct=50.0,
                gas_atoms=0,
            )

    def test_negative_or_nan_slippage_rejected(
        self,
        token_weth: TokenIdentity,
        weth_candidate_route: CandidateRoute,
    ) -> None:
        quote = QuoteResult(
            status=QuoteStatus.QUOTED,
            amount_in=TokenAmount(token=token_weth, atoms=1_000_000),
            amount_out=TokenAmount(token=token_weth, atoms=1_020_000),
            delta_atoms=20_000,
        )

        with pytest.raises(ValueError, match="non-negative"):
            build_execution_plan(
                route=weth_candidate_route,
                quote=quote,
                amount_usd=50.0,
                slippage_pct=-0.5,
                gas_atoms=3_000_000_000_000_000,
            )

        with pytest.raises(ValueError, match="non-negative"):
            build_execution_plan(
                route=weth_candidate_route,
                quote=quote,
                amount_usd=50.0,
                slippage_pct=float("nan"),
                gas_atoms=3_000_000_000_000_000,
            )

    def test_slippage_exact_integer_precision_atoms_1485_weth(
        self,
        token_weth: TokenIdentity,
        weth_candidate_route: CandidateRoute,
    ) -> None:
        """Verify exact integer ratio floor eliminates floating point deviation (16,384 atoms)."""
        amount_in = TokenAmount(token=token_weth, atoms=140_000_000_000_000_000_000)
        amount_out = TokenAmount(token=token_weth, atoms=148_500_000_000_000_000_000)
        quote = QuoteResult(
            status=QuoteStatus.QUOTED,
            amount_in=amount_in,
            amount_out=amount_out,
            delta_atoms=8_500_000_000_000_000_000,
            gas_estimate=150000,
            block_number=60005,
        )

        plan = build_execution_plan(
            route=weth_candidate_route,
            quote=quote,
            amount_usd=300.0,
            slippage_pct=0.5,
            gas_atoms=1000,
            estimated_gas_usd=Decimal("0.10"),
        )

        # Exact integer floor: 148_500_000_000_000_000_000 * 199 // 200 = 147_757_500_000_000_000_000
        assert plan.min_amount_out.atoms == 147_757_500_000_000_000_000

        # Contrast with IEEE 754 float deviation: 147_757_499_999_999_983_616 (-16,384 atoms)
        float_floor = int(amount_out.atoms * (1.0 - 0.5 / 100.0))
        assert plan.min_amount_out.atoms != float_floor
        assert plan.min_amount_out.atoms - float_floor == 16_384

        # Serialization roundtrip preservation
        d = to_dict(plan)
        restored = from_dict(ExecutionPlan, d)
        assert restored == plan

    def test_slippage_large_uint256_integer_reference(
        self,
        token_weth: TokenIdentity,
        weth_candidate_route: CandidateRoute,
    ) -> None:
        """Verify integer ratio calculation preserves arbitrary uint256 precision without Decimal-28 truncation."""
        u256_atoms = 2**256 - 1
        quote_u256 = QuoteResult(
            status=QuoteStatus.QUOTED,
            amount_in=TokenAmount(token=token_weth, atoms=10**18),
            amount_out=TokenAmount(token=token_weth, atoms=u256_atoms),
            delta_atoms=u256_atoms - 10**18,
            gas_estimate=150000,
            block_number=60005,
        )
        plan = build_execution_plan(
            route=weth_candidate_route,
            quote=quote_u256,
            amount_usd=100.0,
            slippage_pct=0.5,
            gas_atoms=1000,
            estimated_gas_usd=Decimal("0.10"),
        )
        expected_u256 = u256_atoms * 199 // 200
        assert plan.min_amount_out.atoms == expected_u256
        assert plan.min_amount_out.atoms.bit_length() > 255

        # Serialization roundtrip preservation
        d = to_dict(plan)
        restored = from_dict(ExecutionPlan, d)
        assert restored == plan


class TestAmountLimits:
    """Trade amount safety red line tests (500 USD hard cap)."""

    def test_amount_exceeding_500_usd_rejected(
        self,
        token_weth: TokenIdentity,
        weth_candidate_route: CandidateRoute,
    ) -> None:
        quote = QuoteResult(
            status=QuoteStatus.QUOTED,
            amount_in=TokenAmount(token=token_weth, atoms=1_000_000_000_000_000_000),
            amount_out=TokenAmount(token=token_weth, atoms=1_020_000_000_000_000_000),
            delta_atoms=20_000_000_000_000_000,
        )

        with pytest.raises(ExcessiveAmountError, match="exceeds safety limit"):
            build_execution_plan(
                route=weth_candidate_route,
                quote=quote,
                amount_usd=500.01,
                gas_atoms=3_000_000_000_000_000,
            )

        with pytest.raises(ExcessiveAmountError, match="exceeds safety limit"):
            build_execution_plan(
                route=weth_candidate_route,
                quote=quote,
                amount_usd=1000.0,
                gas_atoms=3_000_000_000_000_000,
            )

    def test_amount_at_boundary_500_usd_accepted(
        self,
        token_weth: TokenIdentity,
        weth_candidate_route: CandidateRoute,
    ) -> None:
        quote = QuoteResult(
            status=QuoteStatus.QUOTED,
            amount_in=TokenAmount(token=token_weth, atoms=1_000_000_000_000_000_000),
            amount_out=TokenAmount(token=token_weth, atoms=1_020_000_000_000_000_000),
            delta_atoms=20_000_000_000_000_000,
        )

        plan = build_execution_plan(
            route=weth_candidate_route,
            quote=quote,
            amount_usd=HARD_CAP_MAX_USD,
            gas_atoms=3_000_000_000_000_000,
            estimated_gas_usd=Decimal("0.10"),
        )
        assert plan.amount_in == quote.amount_in

    def test_non_positive_or_invalid_amount_rejected(
        self,
        token_weth: TokenIdentity,
        weth_candidate_route: CandidateRoute,
    ) -> None:
        quote = QuoteResult(
            status=QuoteStatus.QUOTED,
            amount_in=TokenAmount(token=token_weth, atoms=1_000_000_000_000_000_000),
            amount_out=TokenAmount(token=token_weth, atoms=1_020_000_000_000_000_000),
            delta_atoms=20_000_000_000_000_000,
        )

        with pytest.raises(ValueError, match="positive finite number"):
            build_execution_plan(
                route=weth_candidate_route,
                quote=quote,
                amount_usd=0.0,
                gas_atoms=3_000_000_000_000_000,
            )

        with pytest.raises(ValueError, match="positive finite number"):
            build_execution_plan(
                route=weth_candidate_route,
                quote=quote,
                amount_usd=-10.0,
                gas_atoms=3_000_000_000_000_000,
            )


class TestFailedQuotesRejected:
    """Pre-check tests for rejected/unquoted results."""

    @pytest.mark.parametrize(
        "fail_status",
        [
            QuoteStatus.CONTRACT_REVERT,
            QuoteStatus.RPC_ERROR,
            QuoteStatus.NODE_LIMITATION,
            QuoteStatus.INVALID_RESPONSE,
        ],
    )
    def test_unquoted_status_rejected(
        self,
        fail_status: QuoteStatus,
        token_weth: TokenIdentity,
        weth_candidate_route: CandidateRoute,
    ) -> None:
        quote = QuoteResult(
            status=fail_status,
            amount_in=TokenAmount(token=token_weth, atoms=1_000_000_000_000_000_000),
            amount_out=None,
            delta_atoms=None,
            error_message="Simulation reverted",
        )

        with pytest.raises(ValueError, match="Cannot build execution plan for unquoted result"):
            build_execution_plan(
                route=weth_candidate_route,
                quote=quote,
                amount_usd=100.0,
                gas_atoms=3_000_000_000_000_000,
            )


class TestTopologyConsistency:
    """Route and quote topology consistency check tests."""

    def test_quote_amount_in_token_mismatch_rejected(
        self,
        token_weth: TokenIdentity,
        token_usdg: TokenIdentity,
        weth_candidate_route: CandidateRoute,
    ) -> None:
        quote = QuoteResult(
            status=QuoteStatus.QUOTED,
            amount_in=TokenAmount(token=token_usdg, atoms=1_000_000),  # USDG instead of WETH
            amount_out=TokenAmount(token=token_weth, atoms=1_000_000_000_000_000_000),
            delta_atoms=None,
        )

        with pytest.raises(
            ValueError, match="Quote amount_in token.*does not match route base_token"
        ):
            build_execution_plan(
                route=weth_candidate_route,
                quote=quote,
                amount_usd=100.0,
                gas_atoms=3_000_000_000_000_000,
            )

    def test_quote_amount_out_token_mismatch_rejected(
        self,
        token_weth: TokenIdentity,
        token_usdg: TokenIdentity,
        weth_candidate_route: CandidateRoute,
    ) -> None:
        quote = QuoteResult(
            status=QuoteStatus.QUOTED,
            amount_in=TokenAmount(token=token_weth, atoms=1_000_000_000_000_000_000),
            amount_out=TokenAmount(token=token_usdg, atoms=1_000_000),  # USDG instead of WETH
            delta_atoms=None,
        )

        with pytest.raises(
            ValueError, match="Quote amount_out token.*does not match route base_token"
        ):
            build_execution_plan(
                route=weth_candidate_route,
                quote=quote,
                amount_usd=100.0,
                gas_atoms=3_000_000_000_000_000,
            )


class TestStaticASTAudit:
    """Static AST audit verifying planning.py never imports networking, reads keys, or touches env."""

    FORBIDDEN_MODULES = {
        "web3",
        "requests",
        "urllib",
        "aiohttp",
        "httpx",
        "socket",
        "http",
        "websockets",
        "dotenv",
        "subprocess",
        "shutil",
    }

    FORBIDDEN_CALLS = {
        "open",
        "exec",
        "eval",
        "compile",
        "__import__",
    }

    def test_ast_security_audit(self) -> None:
        file_path = Path(__file__).resolve().parent.parent / "research" / "planning.py"
        assert file_path.exists(), f"{file_path} not found"

        source = file_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(file_path))

        for node in ast.walk(tree):
            # Check import x
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mod_root = alias.name.split(".")[0]
                    assert mod_root not in self.FORBIDDEN_MODULES, (
                        f"Forbidden import found in planning.py: {alias.name}"
                    )

            # Check from x import y
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                mod_root = mod.split(".")[0]
                assert mod_root not in self.FORBIDDEN_MODULES, (
                    f"Forbidden import-from found in planning.py: {mod}"
                )

            # Check forbidden function calls
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    assert node.func.id not in self.FORBIDDEN_CALLS, (
                        f"Forbidden call '{node.func.id}()' found in planning.py"
                    )

        # Check raw keywords
        forbidden_keywords = [".env", "PRIVATE_KEY", "keystore", ".jsonl"]
        for kw in forbidden_keywords:
            assert kw not in source, f"Sensitive keyword '{kw}' found in planning.py"


class TestExplicitGasAndCostFloorGuardrails:
    """Explicit gas_atoms and AGENTS.md output_floor >= amount_in + gas_atoms + 1 guardrails."""

    def test_missing_gas_atoms_rejected(
        self,
        token_weth: TokenIdentity,
        weth_candidate_route: CandidateRoute,
    ) -> None:
        quote = QuoteResult(
            status=QuoteStatus.QUOTED,
            amount_in=TokenAmount(token=token_weth, atoms=1_000_000_000_000_000_000),
            amount_out=TokenAmount(token=token_weth, atoms=1_020_000_000_000_000_000),
            delta_atoms=20_000_000_000_000_000,
        )

        with pytest.raises(ValueError, match="Missing explicit gas"):
            build_execution_plan(
                route=weth_candidate_route,
                quote=quote,
                amount_usd=200.0,
                slippage_pct=0.5,
            )

    def test_invalid_gas_atoms_rejected(
        self,
        token_weth: TokenIdentity,
        weth_candidate_route: CandidateRoute,
    ) -> None:
        quote = QuoteResult(
            status=QuoteStatus.QUOTED,
            amount_in=TokenAmount(token=token_weth, atoms=1_000_000_000_000_000_000),
            amount_out=TokenAmount(token=token_weth, atoms=1_020_000_000_000_000_000),
            delta_atoms=20_000_000_000_000_000,
        )

        # bool not allowed
        with pytest.raises(ValueError, match="gas_atoms must be a non-negative integer"):
            build_execution_plan(
                route=weth_candidate_route,
                quote=quote,
                amount_usd=200.0,
                gas_atoms=True,
            )

        # negative int not allowed
        with pytest.raises(ValueError, match="gas_atoms must be a non-negative integer"):
            build_execution_plan(
                route=weth_candidate_route,
                quote=quote,
                amount_usd=200.0,
                gas_atoms=-10,
            )

    def test_cost_floor_dominates_when_higher_than_slippage_floor(
        self,
        token_weth: TokenIdentity,
        weth_candidate_route: CandidateRoute,
    ) -> None:
        amount_in = TokenAmount(token=token_weth, atoms=1_000_000_000_000_000_000)
        amount_out = TokenAmount(token=token_weth, atoms=1_020_000_000_000_000_000)
        quote = QuoteResult(
            status=QuoteStatus.QUOTED,
            amount_in=amount_in,
            amount_out=amount_out,
            delta_atoms=20_000_000_000_000_000,
        )

        # slippage_floor = int(1_020_000_000_000_000_000 * 0.995) = 1_014_900_000_000_000_000
        # required_cost_floor = 1_000_000_000_000_000_000 + 16_000_000_000_000_000 + 1 = 1_016_000_000_000_000_001
        # output_floor = max(1_014_900_000_000_000_000, 1_016_000_000_000_000_001) = 1_016_000_000_000_000_001
        plan = build_execution_plan(
            route=weth_candidate_route,
            quote=quote,
            amount_usd=200.0,
            slippage_pct=0.5,
            gas_atoms=16_000_000_000_000_000,
            estimated_gas_usd=Decimal("0.10"),
        )

        assert plan.min_amount_out.atoms == 1_016_000_000_000_000_001
        assert plan.min_amount_out.atoms > int(amount_out.atoms * (1.0 - 0.5 / 100.0))

    def test_output_floor_exceeding_quote_amount_out_rejected(
        self,
        token_weth: TokenIdentity,
        weth_candidate_route: CandidateRoute,
    ) -> None:
        amount_in = TokenAmount(token=token_weth, atoms=1_000_000_000_000_000_000)
        amount_out = TokenAmount(token=token_weth, atoms=1_020_000_000_000_000_000)
        quote = QuoteResult(
            status=QuoteStatus.QUOTED,
            amount_in=amount_in,
            amount_out=amount_out,
            delta_atoms=20_000_000_000_000_000,
        )

        # gas_atoms = 25_000_000_000_000_000 -> required_cost_floor = 1_025_000_000_000_001 > amount_out (1_020_000_000_000_000_000)
        with pytest.raises(ValueError, match="Quote cannot satisfy output floor"):
            build_execution_plan(
                route=weth_candidate_route,
                quote=quote,
                amount_usd=200.0,
                slippage_pct=0.5,
                gas_atoms=25_000_000_000_000_000,
            )

    def test_missing_estimated_gas_usd_rejected(
        self,
        token_weth: TokenIdentity,
        weth_candidate_route: CandidateRoute,
    ) -> None:
        """Verify missing estimated_gas_usd (None/omitted) is rejected fail-closed."""
        quote = QuoteResult(
            status=QuoteStatus.QUOTED,
            amount_in=TokenAmount(token=token_weth, atoms=1_000_000_000_000_000_000),
            amount_out=TokenAmount(token=token_weth, atoms=1_020_000_000_000_000_000),
            delta_atoms=20_000_000_000_000_000,
        )

        with pytest.raises(
            ValueError, match="Missing explicit gas input: estimated_gas_usd is strictly required"
        ):
            build_execution_plan(
                route=weth_candidate_route,
                quote=quote,
                amount_usd=200.0,
                slippage_pct=0.5,
                gas_atoms=3_000_000_000_000_000,
            )

        with pytest.raises(
            ValueError, match="Missing explicit gas input: estimated_gas_usd is strictly required"
        ):
            build_execution_plan(
                route=weth_candidate_route,
                quote=quote,
                amount_usd=200.0,
                slippage_pct=0.5,
                gas_atoms=3_000_000_000_000_000,
                estimated_gas_usd=None,
            )

    def test_invalid_estimated_gas_usd_negative_rejected(
        self,
        token_weth: TokenIdentity,
        weth_candidate_route: CandidateRoute,
    ) -> None:
        """Verify negative estimated_gas_usd is rejected."""
        quote = QuoteResult(
            status=QuoteStatus.QUOTED,
            amount_in=TokenAmount(token=token_weth, atoms=1_000_000_000_000_000_000),
            amount_out=TokenAmount(token=token_weth, atoms=1_020_000_000_000_000_000),
            delta_atoms=20_000_000_000_000_000,
        )

        neg_cases: list[Decimal | float | int | str] = [Decimal("-0.01"), -1, "-1.5"]
        for neg in neg_cases:
            with pytest.raises(
                ValueError, match="estimated_gas_usd must be a non-negative finite Decimal"
            ):
                build_execution_plan(
                    route=weth_candidate_route,
                    quote=quote,
                    amount_usd=200.0,
                    slippage_pct=0.5,
                    gas_atoms=3_000_000_000_000_000,
                    estimated_gas_usd=neg,
                )

    def test_invalid_estimated_gas_usd_nan_inf_rejected(
        self,
        token_weth: TokenIdentity,
        weth_candidate_route: CandidateRoute,
    ) -> None:
        """Verify NaN and Infinity estimated_gas_usd are rejected."""
        quote = QuoteResult(
            status=QuoteStatus.QUOTED,
            amount_in=TokenAmount(token=token_weth, atoms=1_000_000_000_000_000_000),
            amount_out=TokenAmount(token=token_weth, atoms=1_020_000_000_000_000_000),
            delta_atoms=20_000_000_000_000_000,
        )

        nan_inf_cases: list[Decimal | float | str] = [
            "NaN",
            Decimal("NaN"),
            "Infinity",
            Decimal("Infinity"),
            float("nan"),
            float("inf"),
        ]
        for invalid_num in nan_inf_cases:
            with pytest.raises(
                ValueError, match="estimated_gas_usd must be a non-negative finite Decimal"
            ):
                build_execution_plan(
                    route=weth_candidate_route,
                    quote=quote,
                    amount_usd=200.0,
                    slippage_pct=0.5,
                    gas_atoms=3_000_000_000_000_000,
                    estimated_gas_usd=invalid_num,
                )

    def test_invalid_estimated_gas_usd_bool_and_invalid_str_rejected(
        self,
        token_weth: TokenIdentity,
        weth_candidate_route: CandidateRoute,
    ) -> None:
        """Verify bool types and unparseable strings for estimated_gas_usd are rejected."""
        quote = QuoteResult(
            status=QuoteStatus.QUOTED,
            amount_in=TokenAmount(token=token_weth, atoms=1_000_000_000_000_000_000),
            amount_out=TokenAmount(token=token_weth, atoms=1_020_000_000_000_000_000),
            delta_atoms=20_000_000_000_000_000,
        )

        bool_cases: list[object] = [True, False]
        for b in bool_cases:
            with pytest.raises(ValueError, match="estimated_gas_usd cannot be bool"):
                build_execution_plan(
                    route=weth_candidate_route,
                    quote=quote,
                    amount_usd=200.0,
                    slippage_pct=0.5,
                    gas_atoms=3_000_000_000_000_000,
                    estimated_gas_usd=b,  # type: ignore[arg-type]
                )

        with pytest.raises(ValueError, match="Invalid estimated_gas_usd"):
            build_execution_plan(
                route=weth_candidate_route,
                quote=quote,
                amount_usd=200.0,
                slippage_pct=0.5,
                gas_atoms=3_000_000_000_000_000,
                estimated_gas_usd="invalid_gas",
            )

    def test_explicit_zero_estimated_gas_usd_accepted(
        self,
        token_weth: TokenIdentity,
        weth_candidate_route: CandidateRoute,
    ) -> None:
        """Verify explicit zero estimated_gas_usd is accepted and preserved during serialization."""
        quote = QuoteResult(
            status=QuoteStatus.QUOTED,
            amount_in=TokenAmount(token=token_weth, atoms=1_000_000_000_000_000_000),
            amount_out=TokenAmount(token=token_weth, atoms=1_020_000_000_000_000_000),
            delta_atoms=20_000_000_000_000_000,
        )

        zero_cases: list[Decimal | float | int | str] = [Decimal("0"), 0, "0", 0.0]
        for zero_val in zero_cases:
            plan = build_execution_plan(
                route=weth_candidate_route,
                quote=quote,
                amount_usd=200.0,
                slippage_pct=0.5,
                gas_atoms=3_000_000_000_000_000,
                estimated_gas_usd=zero_val,
            )
            assert plan.estimated_gas_usd == Decimal(str(zero_val))
            d = to_dict(plan)
            restored = from_dict(ExecutionPlan, d)
            assert restored == plan
            assert restored.estimated_gas_usd == Decimal(str(zero_val))
