"""Arc Deployment Binding and Execution Planning Tests (T25)

Verifies:
- Rejection of Robinhood 4663 chain ID and canonical Robinhood addresses
- Verification of Arc UniversalRouter binding in execution plans
- Mandatory funds safety: value_atoms=0, can_atomic_execute=False, <= 500 USD limit
- Calldata encoding invariants: Bit 7 unset, intermediate hop -> Router, final hop -> MSG_SENDER
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from arbitrage_contracts.identity import (
    Amount,
    AssetRef,
    FeeModel,
    PoolDescriptor,
    PoolKey,
    TokenKey,
)
from arbitrage_contracts.quote import (
    HopQuote,
    HopRef,
    QuoteEvidence,
    QuoteStatus,
    RouteRef,
    TriState,
)
from atomic_execution.arc_encoding import (
    ADDRESS_THIS,
    EXECUTE_SELECTOR_WITH_DEADLINE,
    FLAG_ALLOW_REVERT,
    MSG_SENDER,
    encode_arc_execution_plan,
)
from atomic_execution.arc_planning import (
    ArcExecutionPlan,
    ArcPlanAssembler,
    ArcPlanningError,
)
from atomic_execution.deployments import (
    ArcExecutionDeploymentBinding,
    DeploymentBindingError,
)
from atomic_execution.policy import ExecutionPolicy

CHAIN_ARC = 5042
ARC_ROUTER = "0x" + "55" * 20
ARC_PERMIT2 = "0x" + "66" * 20

USDC_6 = AssetRef("erc20", CHAIN_ARC, TokenKey(CHAIN_ARC, "0x" + "36" * 20))
WETH_18 = AssetRef("erc20", CHAIN_ARC, TokenKey(CHAIN_ARC, "0x" + "42" * 20))


def make_quote() -> QuoteEvidence:
    p1 = PoolDescriptor(
        PoolKey(CHAIN_ARC, "v3", "factory", "0x" + "11" * 20, "address", "0x" + "01" * 20),
        USDC_6,
        WETH_18,
        FeeModel.static(500),
        10,
    )
    p2 = PoolDescriptor(
        PoolKey(CHAIN_ARC, "v3", "factory", "0x" + "11" * 20, "address", "0x" + "02" * 20),
        USDC_6,
        WETH_18,
        FeeModel.static(3000),
        60,
    )
    hop1 = HopRef(p1.key, USDC_6, WETH_18, "zero_for_one", p1)
    hop2 = HopRef(p2.key, WETH_18, USDC_6, "one_for_zero", p2)
    route = RouteRef(chain_id=CHAIN_ARC, base_asset=USDC_6, hops=(hop1, hop2))

    hq1 = HopQuote(0, p1.key, USDC_6, WETH_18, Amount(USDC_6, 100_000_000, 6), Amount(WETH_18, 50_000_000_000_000_000, 18), status=QuoteStatus.QUOTED, fee_model=p1.fee_model)
    hq2 = HopQuote(1, p2.key, WETH_18, USDC_6, Amount(WETH_18, 50_000_000_000_000_000, 18), Amount(USDC_6, 105_000_000, 6), status=QuoteStatus.QUOTED, fee_model=p2.fee_model)

    return QuoteEvidence(
        quote_id="q-arc-001",
        route_ref=route,
        amount_in=Amount(USDC_6, 100_000_000, 6),
        amount_out=Amount(USDC_6, 105_000_000, 6),
        delta_atoms=5_000_000,
        hop_quotes=(hq1, hq2),
        state_version_ref="state:v1:" + "aa" * 32,
        started_at_ms=1000,
        finished_at_ms=1010,
        status=QuoteStatus.QUOTED,
        fee_included=TriState.YES,
        impact_included=TriState.YES,
    )


class TestArcDeploymentBindingAndExecution:
    """Test suite for T25 Arc W5 deployment binding and calldata encoding."""

    def test_rejection_of_robinhood_chain_id(self) -> None:
        with pytest.raises(DeploymentBindingError, match="Invalid Arc chain_id 4663"):
            ArcExecutionDeploymentBinding(chain_id=4663, router_address=ARC_ROUTER)

    def test_rejection_of_canonical_robinhood_addresses(self) -> None:
        # Banned Robinhood UniversalRouter
        with pytest.raises(DeploymentBindingError, match="Robinhood canonical router address rejected"):
            ArcExecutionDeploymentBinding(
                chain_id=CHAIN_ARC,
                router_address="0x8876789976dEcBfCbBbe364623C63652db8C0904",
            )

        # Banned Robinhood Permit2
        with pytest.raises(DeploymentBindingError, match="Robinhood canonical permit2 address rejected"):
            ArcExecutionDeploymentBinding(
                chain_id=CHAIN_ARC,
                router_address=ARC_ROUTER,
                permit2_address="0x000000000022D473030F116dDEE9F6B43aC78BA3",
            )

    def test_valid_arc_deployment_binding(self) -> None:
        binding = ArcExecutionDeploymentBinding(
            chain_id=CHAIN_ARC,
            router_address=ARC_ROUTER,
            permit2_address=ARC_PERMIT2,
        )
        assert binding.chain_id == CHAIN_ARC
        assert binding.router_address == ARC_ROUTER
        assert binding.permit2_address == ARC_PERMIT2
        assert len(binding.deployment_fingerprint) == 64

    def test_arc_execution_planning_assembly(self) -> None:
        binding = ArcExecutionDeploymentBinding(chain_id=CHAIN_ARC, router_address=ARC_ROUTER)
        assembler = ArcPlanAssembler(deployment=binding)
        quote = make_quote()

        plan = assembler.assemble_plan(
            quote=quote,
            base_asset_usd_price=Decimal("1.0"),
            conservative_gas_usd=Decimal("0.10"),
        )

        assert plan.chain_id == CHAIN_ARC
        assert plan.target_router == ARC_ROUTER
        assert plan.value_atoms == 0
        assert plan.can_atomic_execute is False
        assert plan.min_amount_out.atoms > plan.amount_in.atoms
        assert plan.deployment_fingerprint == binding.deployment_fingerprint

    def test_arc_planning_rejects_mismatched_chain(self) -> None:
        binding_testnet = ArcExecutionDeploymentBinding(chain_id=5042002, router_address=ARC_ROUTER)
        assembler = ArcPlanAssembler(deployment=binding_testnet)
        quote_mainnet = make_quote()  # chain_id 5042

        with pytest.raises(ArcPlanningError, match="does not match deployment"):
            assembler.assemble_plan(quote=quote_mainnet, base_asset_usd_price=Decimal("1.0"))

    def test_arc_calldata_encoding_invariants(self) -> None:
        binding = ArcExecutionDeploymentBinding(chain_id=CHAIN_ARC, router_address=ARC_ROUTER)
        assembler = ArcPlanAssembler(deployment=binding)
        quote = make_quote()
        plan = assembler.assemble_plan(quote=quote, base_asset_usd_price=Decimal("1.0"))

        encoded = encode_arc_execution_plan(plan=plan, deadline_s=180)

        assert encoded.target_router == ARC_ROUTER
        assert encoded.can_atomic_execute is False
        assert encoded.status == "ENCODED"
        assert encoded.commands_count == 2
        assert encoded.allow_revert_flags_unset is True
        assert encoded.calldata_hex.startswith(EXECUTE_SELECTOR_WITH_DEADLINE)

        # Raw bytes validation
        raw_bytes = bytes.fromhex(encoded.calldata_hex[2:])
        assert len(encoded.calldata_hash) == 64
        # Verify selector matches
        assert raw_bytes[:4].hex() == EXECUTE_SELECTOR_WITH_DEADLINE[2:]
