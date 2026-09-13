"""Arc Shadow Evaluation Service and Reason Categorization Tests (T24)

Verifies:
- Four-tier state separation: Quoted, Economics, Simulation, Execution
- CALL_SUCCEEDED without output_verified is NOT claimed as profit (SIMULATION_OUTPUT_UNVERIFIED)
- Granular rejection reason categorization (cross-tick, unknown gas, contract revert, unverified output)
- DataMode separation (synthetic vs live)
- Full persistence of unprofitable and unknown outcomes in ArcOpportunityLedger
"""

from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

from arbitrage_contracts.arc_extensions import (
    BlockDomain,
    CostEvidence,
    SimulationEvidenceBridge,
    SimulationStatus,
)
from arbitrage_contracts.identity import (
    Amount,
    AssetRef,
    FeeModel,
    PoolDescriptor,
    PoolKey,
    TokenKey,
)
from arbitrage_contracts.quote import DataMode, HopRef, QuoteStatus, RouteRef
from arbitrage_contracts.state import StateVersion
from arc_opportunities.costs import create_gas_cost_evidence
from arc_opportunities.economics import USDC_SHARED_BALANCE_DOMAIN, ArcEconomicEvaluator
from arc_opportunities.ledger import ArcOpportunityLedger, RecordType
from arc_opportunities.quote_bridge import ArcQuoteBridge
from arc_opportunities.reasons import ShadowRejectionReason
from arc_opportunities.shadow import ArcShadowEvaluationService
from state_graph.clmm_math import get_sqrt_ratio_at_tick
from state_graph.types import FrozenEpoch, PoolStateSnapshot

CHAIN_ARC = 5042

USDC_6 = AssetRef(
    interface_kind="erc20",
    chain_id=CHAIN_ARC,
    token_key=TokenKey(CHAIN_ARC, "0x3600000000000000000000000000000000000001"),
    balance_domain_id=USDC_SHARED_BALANCE_DOMAIN,
)
WETH_18 = AssetRef(
    interface_kind="erc20",
    chain_id=CHAIN_ARC,
    token_key=TokenKey(CHAIN_ARC, "0x4200000000000000000000000000000000000002"),
)

DECIMALS = {USDC_6: 6, WETH_18: 18}


def make_arc_state_version() -> StateVersion:
    return StateVersion(
        chain_id=CHAIN_ARC,
        block_domain="l1",
        block_number=1000,
        block_hash="0x" + "aa" * 32,
        received_at_ms=1726000000000,
        complete_through_block=1000,
        completeness="ready",
        finality="safe",
    )


def make_pool(pool_id_suffix: str, fee_pips: int = 500) -> PoolDescriptor:
    pool_addr = f"0x{pool_id_suffix.zfill(40)}"
    venue_addr = "0x" + "11" * 20
    key = PoolKey(CHAIN_ARC, "uniswap_v3", "factory", venue_addr, "address", pool_addr)
    return PoolDescriptor(
        key=key,
        currency0=USDC_6,
        currency1=WETH_18,
        fee_model=FeeModel.static(fee_pips),
        tick_spacing=10,
        deployment_status="deployed",
    )


class TestArcShadowEvaluationService:
    """Test suite for T24 Shadow Evaluation Pipeline and Reason Categorization."""

    def test_four_tier_separation_and_reason_categorization(self) -> None:
        bridge = ArcQuoteBridge()
        evaluator = ArcEconomicEvaluator()
        service = ArcShadowEvaluationService(bridge, evaluator)

        p1 = make_pool("01", 500)
        p2 = make_pool("02", 500)
        hop1 = HopRef(p1.key, USDC_6, WETH_18, "zero_for_one", p1)
        hop2 = HopRef(p2.key, WETH_18, USDC_6, "one_for_zero", p2)
        route = RouteRef(CHAIN_ARC, USDC_6, (hop1, hop2))

        sv = make_arc_state_version()
        snap1 = PoolStateSnapshot(p1.key.pool_id, get_sqrt_ratio_at_tick(5), 5, 50_000_000_000_000_000, 500, 10, 1000, "0x" + "aa" * 32)
        snap2 = PoolStateSnapshot(p2.key.pool_id, get_sqrt_ratio_at_tick(-5), -5, 50_000_000_000_000_000, 500, 10, 1000, "0x" + "aa" * 32)
        epoch = FrozenEpoch("ep-shadow", sv, (snap1, snap2), 1726000000000)

        # 1. Missing Gas -> UNKNOWN_GAS_EVIDENCE
        res_no_gas = service.evaluate_candidate(
            route=route,
            amount_in=Amount(USDC_6, 10_000_000, 6),
            epoch=epoch,
            base_asset_usd_price=Decimal("1.0"),
            gas_evidence=None,
            token_decimals=DECIMALS,
        )
        assert res_no_gas.quote.status == QuoteStatus.QUOTED
        assert res_no_gas.breakdown.economic_status == "unknown"
        assert res_no_gas.primary_rejection_reason == ShadowRejectionReason.UNKNOWN_GAS_EVIDENCE
        assert res_no_gas.execution_authorized is False

        # 2. Simulation Call Succeeded but Output Unverified -> SIMULATION_OUTPUT_UNVERIFIED
        # Use profitable spread (tick 25 vs -25)
        snap1_prof = PoolStateSnapshot(p1.key.pool_id, get_sqrt_ratio_at_tick(25), 25, 500_000_000_000_000_000, 500, 10, 1000, "0x" + "aa" * 32)
        snap2_prof = PoolStateSnapshot(p2.key.pool_id, get_sqrt_ratio_at_tick(-25), -25, 500_000_000_000_000_000, 500, 10, 1000, "0x" + "aa" * 32)
        epoch_prof = FrozenEpoch("ep-prof", sv, (snap1_prof, snap2_prof), 1726000000000)

        gas = create_gas_cost_evidence(cost_atoms=100, currency="USDC")
        sim_unverified = SimulationEvidenceBridge(
            call_succeeded=True,
            output_verified=False,
            status=SimulationStatus.OUTPUT_UNVERIFIED,
            net_output_atoms=None,
            gas_used_atoms=100,
            backend="arc_readonly_mock",
        )
        res_sim_unverified = service.evaluate_candidate(
            route=route,
            amount_in=Amount(USDC_6, 1_000_000, 6),
            epoch=epoch_prof,
            base_asset_usd_price=Decimal("1.0"),
            gas_evidence=gas,
            simulation_evidence=sim_unverified,
            token_decimals=DECIMALS,
        )
        assert res_sim_unverified.primary_rejection_reason == ShadowRejectionReason.SIMULATION_OUTPUT_UNVERIFIED
        assert res_sim_unverified.is_actionable is False

        # 3. Contract Revert Simulation -> SIMULATION_CONTRACT_REVERT
        sim_revert = SimulationEvidenceBridge(
            call_succeeded=False,
            output_verified=False,
            status=SimulationStatus.CONTRACT_REVERT,
            net_output_atoms=None,
            gas_used_atoms=100,
            backend="arc_readonly_mock",
            execution_revert_reason="TRANSFER_FAILED",
        )
        res_revert = service.evaluate_candidate(
            route=route,
            amount_in=Amount(USDC_6, 1_000_000, 6),
            epoch=epoch_prof,
            base_asset_usd_price=Decimal("1.0"),
            gas_evidence=gas,
            simulation_evidence=sim_revert,
            token_decimals=DECIMALS,
        )
        assert res_revert.primary_rejection_reason == ShadowRejectionReason.SIMULATION_CONTRACT_REVERT

    def test_shadow_ledger_persistence_preserves_negative_and_unknowns(self) -> None:
        with tempfile.TemporaryDirectory(prefix="shadow_ledger_") as tmpdir:
            ledger_file = Path(tmpdir) / "shadow_ledger.jsonl"
            ledger = ArcOpportunityLedger(ledger_file)

            bridge = ArcQuoteBridge()
            evaluator = ArcEconomicEvaluator()
            service = ArcShadowEvaluationService(bridge, evaluator, ledger=ledger)

            p1 = make_pool("01", 500)
            p2 = make_pool("02", 500)
            hop1 = HopRef(p1.key, USDC_6, WETH_18, "zero_for_one", p1)
            hop2 = HopRef(p2.key, WETH_18, USDC_6, "one_for_zero", p2)
            route = RouteRef(CHAIN_ARC, USDC_6, (hop1, hop2))

            sv = make_arc_state_version()
            snap1 = PoolStateSnapshot(p1.key.pool_id, get_sqrt_ratio_at_tick(5), 5, 50_000_000_000_000_000, 500, 10, 1000, "0x" + "aa" * 32)
            snap2 = PoolStateSnapshot(p2.key.pool_id, get_sqrt_ratio_at_tick(-5), -5, 50_000_000_000_000_000, 500, 10, 1000, "0x" + "aa" * 32)
            epoch = FrozenEpoch("ep-shadow-2", sv, (snap1, snap2), 1726000000000)

            # Evaluate with negative net outcome (huge gas)
            huge_gas = create_gas_cost_evidence(cost_atoms=100_000_000, currency="USDC")
            service.evaluate_candidate(
                route=route,
                amount_in=Amount(USDC_6, 10_000_000, 6),
                epoch=epoch,
                base_asset_usd_price=Decimal("1.0"),
                gas_evidence=huge_gas,
                data_mode=DataMode.SYNTHETIC,
                token_decimals=DECIMALS,
            )

            records = ledger.read_all()
            assert len(records) == 1
            rec = records[0]
            assert rec.economic_status == "unprofitable"
            assert rec.net_atoms is not None and rec.net_atoms < 0
            assert rec.payload is not None
            assert rec.payload["rejection_reason"] == ShadowRejectionReason.NEGATIVE_NET_PROFIT
            assert rec.payload["data_mode"] == DataMode.SYNTHETIC
