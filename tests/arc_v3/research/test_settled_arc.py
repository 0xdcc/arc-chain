"""Arc Settled Cycles Reconstruction Tests (T34)

Verifies:
- Reconstructed closed cycles correctly calculate net profit across hops
- Flash loan principal is explicitly separated and NEVER counted as profit
- Multi-actor separation: relayer/initiator vs profit recipient
- Missing internal trace is flagged and separated in summary statistics
- 1-hop cycles fail closed (cannot be an arbitrage loop)
"""

from __future__ import annotations

import pytest

from arc_research.settled.adapter import (
    FlashLoanAttribution,
    SettledAttributionError,
    SettledCycleAdapter,
    SettledSwapHop,
)
from arc_research.settled.report import SettledCycleReporter

USDC_ADDR = "0x" + "11" * 20
WETH_ADDR = "0x" + "22" * 20
INITIATOR = "0x" + "33" * 20
RECIPIENT = "0x" + "44" * 20


class TestArcSettledCycles:
    """Test suite for T34 Settled Cycles Reconstruction."""

    def test_standard_two_hop_cycle_reconstruction(self) -> None:
        """Verify basic 2-hop arbitrage cycle with starting inventory."""
        hops = [
            SettledSwapHop(pool_id="pool-1", token_in=USDC_ADDR, token_out=WETH_ADDR, amount_in_atoms=100_000_000, amount_out_atoms=50_000),
            SettledSwapHop(pool_id="pool-2", token_in=WETH_ADDR, token_out=USDC_ADDR, amount_in_atoms=50_000, amount_out_atoms=105_000_000),
        ]
        cycle = SettledCycleAdapter.reconstruct_cycle(
            tx_hash="0x" + "aa" * 32,
            block_number=1000,
            initiator_address=INITIATOR,
            recipient_address=RECIPIENT,
            base_asset=USDC_ADDR,
            hops=hops,
            gross_payout_atoms=105_000_000,
            starting_inventory_atoms=100_000_000,
            gas_used_atoms=100_000,
            effective_gas_price_atoms=2,  # 200_000 gas fee atoms
        )
        assert cycle.is_profitable is True
        # Net profit = 105M - 100M - 200k = 4,800,000 atoms
        assert cycle.net_profit_atoms == 4_800_000
        assert cycle.has_full_trace is True

    def test_flash_loan_principal_not_counted_as_profit(self) -> None:
        """CRITICAL: Borrowing 50,000,000 atoms via flash loan is a debt liability, not profit."""
        hops = [
            SettledSwapHop(pool_id="pool-1", token_in=USDC_ADDR, token_out=WETH_ADDR, amount_in_atoms=50_000_000, amount_out_atoms=25_000),
            SettledSwapHop(pool_id="pool-2", token_in=WETH_ADDR, token_out=USDC_ADDR, amount_in_atoms=25_000, amount_out_atoms=51_000_000),
        ]
        fl = FlashLoanAttribution(
            lender="0x" + "99" * 20,
            token_address=USDC_ADDR,
            principal_atoms=50_000_000,
            fee_atoms=45_000,  # 9 bps flash loan fee
        )
        cycle = SettledCycleAdapter.reconstruct_cycle(
            tx_hash="0x" + "bb" * 32,
            block_number=1001,
            initiator_address=INITIATOR,
            recipient_address=RECIPIENT,
            base_asset=USDC_ADDR,
            hops=hops,
            gross_payout_atoms=51_000_000,  # Payout from swap 2
            starting_inventory_atoms=0,     # Zero initial inventory (flash loan funded)
            gas_used_atoms=80_000,
            effective_gas_price_atoms=2,   # 160,000 gas fee
            flash_loan=fl,
        )
        # Total cost = 50,000,000 (principal) + 45,000 (fee) + 160,000 (gas) = 50,205,000
        # Net profit = 51,000,000 - 50,205,000 = 795,000 atoms
        assert cycle.net_profit_atoms == 795_000
        assert cycle.is_profitable is True

    def test_single_hop_cycle_rejected(self) -> None:
        """Rule: A single swap cannot constitute an arbitrage cycle."""
        hops = [
            SettledSwapHop(pool_id="pool-1", token_in=USDC_ADDR, token_out=WETH_ADDR, amount_in_atoms=100, amount_out_atoms=50)
        ]
        with pytest.raises(SettledAttributionError, match="requires at least 2 hops"):
            SettledCycleAdapter.reconstruct_cycle(
                tx_hash="0x" + "cc" * 32,
                block_number=1002,
                initiator_address=INITIATOR,
                recipient_address=RECIPIENT,
                base_asset=USDC_ADDR,
                hops=hops,
                gross_payout_atoms=50,
            )

    def test_settled_cycle_reporter_summary(self) -> None:
        """Verify reporter aggregates cycle counts, pools, and trace status."""
        c1 = SettledCycleAdapter.reconstruct_cycle(
            tx_hash="0x" + "11" * 32,
            block_number=100,
            initiator_address=INITIATOR,
            recipient_address=RECIPIENT,
            base_asset=USDC_ADDR,
            hops=[
                SettledSwapHop("p1", USDC_ADDR, WETH_ADDR, 10, 5),
                SettledSwapHop("p2", WETH_ADDR, USDC_ADDR, 5, 12),
            ],
            gross_payout_atoms=12,
            starting_inventory_atoms=10,
            has_full_trace=True,
        )
        c2 = SettledCycleAdapter.reconstruct_cycle(
            tx_hash="0x" + "22" * 32,
            block_number=101,
            initiator_address="0x" + "55" * 20,
            recipient_address="0x" + "55" * 20,
            base_asset=USDC_ADDR,
            hops=[
                SettledSwapHop("p2", USDC_ADDR, WETH_ADDR, 20, 8),
                SettledSwapHop("p3", WETH_ADDR, USDC_ADDR, 8, 18),
            ],
            gross_payout_atoms=18,
            starting_inventory_atoms=20,
            has_full_trace=False,  # Unverified trace
        )
        summary = SettledCycleReporter.generate_summary([c1, c2])
        assert summary.total_cycles_analyzed == 2
        assert summary.profitable_cycles_count == 1
        assert summary.unprofitable_cycles_count == 1
        assert summary.full_trace_confirmed_count == 1
        assert summary.unique_initiators_count == 2
        assert set(summary.unique_pools_touched) == {"p1", "p2", "p3"}
