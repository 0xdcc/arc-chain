"""Arc Read-Only Simulation Transport and Invariant Tests (T26)

Verifies:
- Inventory query failures NEVER default to 0 (raises InventoryUnknownError)
- State overrides are strictly prohibited
- CALL_SUCCEEDED with empty return data yields status=OUTPUT_UNVERIFIED, output_verified=False
- Contract revert is classified with call_succeeded=False, CONTRACT_REVERT
- Missing gas estimation leaves gas_used_atoms=None, NEVER defaulting to 180,000
- Insufficient caller inventory blocks plan simulation
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
import pytest

from arbitrage_contracts.arc_extensions import SimulationEvidenceBridge, SimulationStatus
from arbitrage_contracts.identity import (
    Amount,
    AssetRef,
    FeeModel,
    PoolDescriptor,
    PoolKey,
    TokenKey,
)
from arbitrage_contracts.quote import HopQuote, HopRef, QuoteEvidence, QuoteStatus, RouteRef, TriState
from atomic_execution.arc_encoding import encode_arc_execution_plan
from atomic_execution.arc_planning import ArcExecutionPlan, ArcPlanAssembler
from atomic_execution.arc_simulation import ArcSimulationService
from atomic_execution.arc_transport import (
    ArcSimulationTransport,
    InventoryUnknownError,
    SimulationCallResult,
    SimulationTransportError,
)
from atomic_execution.deployments import ArcExecutionDeploymentBinding

CHAIN_ARC = 5042
ARC_ROUTER = "0x" + "55" * 20
CALLER_ADDR = "0x" + "77" * 20

USDC_6 = AssetRef("erc20", CHAIN_ARC, TokenKey(CHAIN_ARC, "0x" + "36" * 20))
WETH_18 = AssetRef("erc20", CHAIN_ARC, TokenKey(CHAIN_ARC, "0x" + "42" * 20))


class MockRpcClient:
    """Mock RPC client for testing simulation edge cases."""

    def __init__(
        self,
        balance_response: int | None = 1_000_000_000,
        call_response: dict | None = None,
        estimate_gas_response: int | None = 120_000,
        simulate_network_error: bool = False,
    ) -> None:
        self.balance_response = balance_response
        self.call_response = call_response or {"data": "0x", "is_revert": False}
        self.estimate_gas_response = estimate_gas_response
        self.simulate_network_error = simulate_network_error

    def eth_get_balance_of(self, token: str, account: str, block: int) -> int:
        if self.simulate_network_error:
            raise ConnectionError("RPC connection timeout during balanceOf")
        if self.balance_response is None:
            raise ValueError("Null balance returned")
        return self.balance_response

    def eth_call(self, to: str, data: str, from_addr: str, block: int) -> dict:
        if self.simulate_network_error:
            raise TimeoutError("RPC timeout during eth_call")
        return self.call_response

    def eth_estimate_gas(self, to: str, data: str, from_addr: str, block: int) -> int:
        if self.estimate_gas_response is None:
            raise RuntimeError("estimateGas unsupported by node")
        return self.estimate_gas_response


def make_plan() -> tuple[ArcExecutionPlan, Any]:
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
        FeeModel.static(500),
        10,
    )
    hop1 = HopRef(p1.key, USDC_6, WETH_18, "zero_for_one", p1)
    hop2 = HopRef(p2.key, WETH_18, USDC_6, "one_for_zero", p2)
    route = RouteRef(chain_id=CHAIN_ARC, base_asset=USDC_6, hops=(hop1, hop2))

    hq1 = HopQuote(0, p1.key, USDC_6, WETH_18, Amount(USDC_6, 100_000_000, 6), Amount(WETH_18, 50_000_000_000_000_000, 18), status=QuoteStatus.QUOTED, fee_model=p1.fee_model)
    hq2 = HopQuote(1, p2.key, WETH_18, USDC_6, Amount(WETH_18, 50_000_000_000_000_000, 18), Amount(USDC_6, 105_000_000, 6), status=QuoteStatus.QUOTED, fee_model=p2.fee_model)

    quote = QuoteEvidence(
        quote_id="q-sim-001",
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

    binding = ArcExecutionDeploymentBinding(chain_id=CHAIN_ARC, router_address=ARC_ROUTER)
    assembler = ArcPlanAssembler(deployment=binding)
    plan = assembler.assemble_plan(quote=quote, base_asset_usd_price=Decimal("1.0"))
    encoded = encode_arc_execution_plan(plan=plan)
    return plan, encoded


class TestArcSimulationTransportAndService:
    """Test suite for T26 Simulation Transport and Verification."""

    def test_inventory_network_error_never_defaults_to_zero(self) -> None:
        """CRITICAL: If network fails during balanceOf, must raise InventoryUnknownError, NOT 0."""
        rpc = MockRpcClient(simulate_network_error=True)
        transport = ArcSimulationTransport(rpc_client=rpc)

        with pytest.raises(InventoryUnknownError, match="RPC connection timeout"):
            transport.check_token_balance(
                token_address="0x" + "36" * 20,
                account_address=CALLER_ADDR,
                block_number=1000,
            )

    def test_state_override_is_prohibited(self) -> None:
        """Rule: Simulation transport strictly rejects state overrides."""
        transport = ArcSimulationTransport(rpc_client=MockRpcClient())
        with pytest.raises(SimulationTransportError, match="State overrides are strictly prohibited"):
            transport.execute_simulation_call(
                to_address=ARC_ROUTER,
                calldata_hex="0x1234",
                block_number=1000,
                from_address=CALLER_ADDR,
                state_override={"0x36": {"balance": 100000}},
            )

    def test_empty_return_data_yields_output_unverified(self) -> None:
        """Rule: Universal Router empty return data cannot claim verified profit."""
        plan, encoded = make_plan()
        # Mock eth_call returning empty bytes "0x"
        rpc = MockRpcClient(
            balance_response=500_000_000,
            call_response={"data": "0x", "is_revert": False},
            estimate_gas_response=100_000,
        )
        transport = ArcSimulationTransport(rpc_client=rpc)
        service = ArcSimulationService(transport=transport)

        bridge_evidence = service.simulate_plan(
            plan=plan,
            encoded=encoded,
            caller_address=CALLER_ADDR,
            block_number=1000,
        )

        assert bridge_evidence.call_succeeded is True
        assert bridge_evidence.output_verified is False
        assert bridge_evidence.status == SimulationStatus.OUTPUT_UNVERIFIED
        assert bridge_evidence.net_output_atoms is None
        assert bridge_evidence.gas_used_atoms == 100_000

    def test_contract_revert_classification(self) -> None:
        plan, encoded = make_plan()
        rpc = MockRpcClient(
            balance_response=500_000_000,
            call_response={"data": "0x", "is_revert": True, "revert_reason": "INSUFFICIENT_OUTPUT_AMOUNT"},
        )
        transport = ArcSimulationTransport(rpc_client=rpc)
        service = ArcSimulationService(transport=transport)

        bridge_evidence = service.simulate_plan(
            plan=plan,
            encoded=encoded,
            caller_address=CALLER_ADDR,
            block_number=1000,
        )

        assert bridge_evidence.call_succeeded is False
        assert bridge_evidence.output_verified is False
        assert bridge_evidence.status == SimulationStatus.CONTRACT_REVERT
        assert "INSUFFICIENT_OUTPUT_AMOUNT" in str(bridge_evidence.execution_revert_reason)

    def test_gas_unknown_never_defaults_to_180000(self) -> None:
        """Rule: Unmeasurable gas returns None / UNKNOWN, NEVER 180,000."""
        plan, encoded = make_plan()
        rpc = MockRpcClient(
            balance_response=500_000_000,
            call_response={"data": "0x", "is_revert": False},
            estimate_gas_response=None,  # estimateGas unsupported
        )
        transport = ArcSimulationTransport(rpc_client=rpc)
        service = ArcSimulationService(transport=transport)

        bridge_evidence = service.simulate_plan(
            plan=plan,
            encoded=encoded,
            caller_address=CALLER_ADDR,
            block_number=1000,
        )

        assert bridge_evidence.gas_used_atoms is None
        assert bridge_evidence.gas_used_atoms != 180000

    def test_insufficient_caller_inventory_blocks_simulation(self) -> None:
        plan, encoded = make_plan()
        # plan requires 100_000_000 atoms, but caller only holds 10_000
        rpc = MockRpcClient(balance_response=10_000)
        transport = ArcSimulationTransport(rpc_client=rpc)
        service = ArcSimulationService(transport=transport)

        bridge_evidence = service.simulate_plan(
            plan=plan,
            encoded=encoded,
            caller_address=CALLER_ADDR,
            block_number=1000,
        )

        assert bridge_evidence.call_succeeded is False
        assert "INSUFFICIENT_CALLER_BALANCE" in str(bridge_evidence.execution_revert_reason)
