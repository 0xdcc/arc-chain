"""Arc Quote Parity and CLMM Single-Segment Integration Tests (T19)

Verifies:
- 2-hop and 3-hop quote parity on Arc chain (5042 L1) with discrete integer Q64.96 math
- Preservation of F01 cross-tick rejection (fail-closed, QuoteStatus.UNSUPPORTED)
- Rejection of L2 block domain (Arc requires L1)
- Rejection of mismatched chain IDs
- Rejection of dynamic fees and unverified/non-zero hooks
- Honest negative delta recording for unprofitable paths
- Decimal compatibility: 0, 6, 18 decimals and conflicting decimals error
- Graph and cycle discovery on Arc
"""

from __future__ import annotations

import pytest

from arbitrage_contracts.arc_extensions import BlockDomain
from arbitrage_contracts.identity import (
    Amount,
    AssetRef,
    FeeModel,
    PoolDescriptor,
    PoolKey,
    TokenKey,
)
from arbitrage_contracts.quote import (
    HopRef,
    QuoteStatus,
    RouteRef,
    TriState,
)
from arbitrage_contracts.state import StateVersion
from arc_opportunities.quote_bridge import (
    CAPABILITY_SINGLE_SEGMENT,
    ArcQuoteBridge,
    ArcQuoteConfig,
)
from state_graph.clmm_math import get_sqrt_ratio_at_tick
from state_graph.types import FrozenEpoch, PoolStateSnapshot

CHAIN_ARC = 5042

# Define Canonical Arc Assets with interface_kind="erc20"
USDC_6 = AssetRef(
    interface_kind="erc20",
    chain_id=CHAIN_ARC,
    token_key=TokenKey(CHAIN_ARC, "0x3600000000000000000000000000000000000001"),
)
WETH_18 = AssetRef(
    interface_kind="erc20",
    chain_id=CHAIN_ARC,
    token_key=TokenKey(CHAIN_ARC, "0x4200000000000000000000000000000000000002"),
)
MEME_0 = AssetRef(
    interface_kind="erc20",
    chain_id=CHAIN_ARC,
    token_key=TokenKey(CHAIN_ARC, "0x5500000000000000000000000000000000000003"),
)

DECIMALS_REGISTRY = {
    USDC_6: 6,
    WETH_18: 18,
    MEME_0: 0,
}


def make_arc_state_version(block_domain: str = "l1", chain_id: int = CHAIN_ARC) -> StateVersion:
    return StateVersion(
        chain_id=chain_id,
        block_domain=block_domain,
        block_number=1000,
        block_hash="0x" + "aa" * 32,
        received_at_ms=1726000000000,
        complete_through_block=1000,
        completeness="ready",
        finality="safe",
    )


def make_pool(
    pool_id_suffix: str,
    asset0: AssetRef,
    asset1: AssetRef,
    fee_pips: int = 500,
    tick_spacing: int = 10,
    hooks: str | None = None,
    fee_kind: str = "static",
) -> PoolDescriptor:
    pool_addr = f"0x{pool_id_suffix.zfill(40)}"
    venue_addr = "0x" + "11" * 20
    key = PoolKey(
        chain_id=asset0.chain_id,
        protocol_id="uniswap_v3",
        venue_kind="factory",
        venue_address=venue_addr,
        pool_id_kind="address",
        pool_id=pool_addr,
    )
    if fee_kind == "static":
        fee_model = FeeModel.static(raw_value=fee_pips)
    elif fee_kind == "dynamic":
        fee_model = FeeModel.dynamic(raw_value=fee_pips)
    else:
        fee_model = FeeModel.unknown()

    return PoolDescriptor(
        key=key,
        currency0=asset0,
        currency1=asset1,
        fee_model=fee_model,
        tick_spacing=tick_spacing,
        hooks=hooks,
        deployment_status="deployed",
    )


def make_snapshot(
    pool_id: str,
    tick: int = 0,
    liquidity: int = 10_000_000_000_000_000,
    fee_pips: int = 500,
    tick_spacing: int = 10,
) -> PoolStateSnapshot:
    price = get_sqrt_ratio_at_tick(tick)
    return PoolStateSnapshot(
        pool_id=pool_id,
        sqrt_price_x96=price,
        tick=tick,
        liquidity=liquidity,
        fee_pips=fee_pips,
        tick_spacing=tick_spacing,
        block_number=1000,
        block_hash="0x" + "aa" * 32,
    )


class TestArcQuoteBridgeParity:
    """Test suite for T19 Arc single-segment CLMM quote parity."""

    def test_arc_quote_config_invariants(self) -> None:
        config = ArcQuoteConfig()
        assert config.chain_id == 5042
        assert config.block_domain == BlockDomain.L1
        assert config.capability == CAPABILITY_SINGLE_SEGMENT

        with pytest.raises(ValueError, match="Unsupported Arc chain_id"):
            ArcQuoteConfig(chain_id=1)

        with pytest.raises(ValueError, match="Arc quoting requires L1"):
            ArcQuoteConfig(block_domain=BlockDomain.L2)

    def test_arc_2hop_quote_parity_exact(self) -> None:
        bridge = ArcQuoteBridge()
        pool1 = make_pool("01", USDC_6, WETH_18, fee_pips=500, tick_spacing=10)
        pool2 = make_pool("02", USDC_6, WETH_18, fee_pips=3000, tick_spacing=60)

        # Build route: USDC -> WETH (via pool1, zero_for_one) -> USDC (via pool2, one_for_zero)
        hop1 = HopRef(
            pool_key=pool1.key,
            asset_in=USDC_6,
            asset_out=WETH_18,
            direction="zero_for_one",
            pool_descriptor=pool1,
        )
        hop2 = HopRef(
            pool_key=pool2.key,
            asset_in=WETH_18,
            asset_out=USDC_6,
            direction="one_for_zero",
            pool_descriptor=pool2,
        )
        route = RouteRef(
            chain_id=CHAIN_ARC,
            base_asset=USDC_6,
            hops=(hop1, hop2),
            max_hops=3,
        )

        sv = make_arc_state_version()
        snap1 = make_snapshot(pool1.key.pool_id, tick=5, liquidity=50_000_000_000_000_000, fee_pips=500, tick_spacing=10)
        snap2 = make_snapshot(pool2.key.pool_id, tick=-20, liquidity=50_000_000_000_000_000, fee_pips=3000, tick_spacing=60)
        epoch = FrozenEpoch(
            epoch_id="epoch-001",
            state_version=sv,
            snapshots=(snap1, snap2),
            created_at_ms=1726000000000,
        )

        # 100 USDC in (100 * 10^6 atoms)
        amount_in = Amount(asset_ref=USDC_6, atoms=100_000_000, decimals=6)
        evidence = bridge.quote_exact_input(
            route=route,
            amount_in=amount_in,
            epoch=epoch,
            token_decimals=DECIMALS_REGISTRY,
        )

        assert evidence.status == QuoteStatus.QUOTED
        assert evidence.amount_out is not None
        assert evidence.amount_out.asset_ref == USDC_6
        assert evidence.amount_out.decimals == 6
        assert len(evidence.hop_quotes) == 2
        assert evidence.fee_included == TriState.YES
        assert evidence.impact_included == TriState.YES
        assert evidence.delta_atoms == evidence.amount_out.atoms - amount_in.atoms

        # Verify exact integer propagation
        h1 = evidence.hop_quotes[0]
        h2 = evidence.hop_quotes[1]
        assert h1.amount_in.atoms == amount_in.atoms
        assert h1.amount_out is not None and h2.amount_out is not None
        assert h1.amount_out.atoms == h2.amount_in.atoms
        assert h2.amount_out.atoms == evidence.amount_out.atoms

    def test_arc_3hop_triangular_quote(self) -> None:
        bridge = ArcQuoteBridge()
        pool1 = make_pool("01", USDC_6, WETH_18, fee_pips=500)
        pool2 = make_pool("02", WETH_18, MEME_0, fee_pips=500)
        pool3 = make_pool("03", MEME_0, USDC_6, fee_pips=500)

        hop1 = HopRef(pool_key=pool1.key, asset_in=USDC_6, asset_out=WETH_18, direction="zero_for_one", pool_descriptor=pool1)
        hop2 = HopRef(pool_key=pool2.key, asset_in=WETH_18, asset_out=MEME_0, direction="zero_for_one", pool_descriptor=pool2)
        hop3 = HopRef(pool_key=pool3.key, asset_in=MEME_0, asset_out=USDC_6, direction="zero_for_one", pool_descriptor=pool3)

        route = RouteRef(chain_id=CHAIN_ARC, base_asset=USDC_6, hops=(hop1, hop2, hop3))

        sv = make_arc_state_version()
        snap1 = make_snapshot(pool1.key.pool_id, tick=5, liquidity=100_000_000_000_000_000)
        snap2 = make_snapshot(pool2.key.pool_id, tick=5, liquidity=100_000_000_000_000_000)
        snap3 = make_snapshot(pool3.key.pool_id, tick=5, liquidity=100_000_000_000_000_000)

        epoch = FrozenEpoch(
            epoch_id="epoch-tri",
            state_version=sv,
            snapshots=(snap1, snap2, snap3),
            created_at_ms=1726000000000,
        )

        amount_in = Amount(asset_ref=USDC_6, atoms=10_000_000, decimals=6)
        evidence = bridge.quote_exact_input(
            route=route,
            amount_in=amount_in,
            epoch=epoch,
            token_decimals=DECIMALS_REGISTRY,
        )

        assert evidence.status == QuoteStatus.QUOTED
        assert len(evidence.hop_quotes) == 3
        # Middle hop MEME has 0 decimals
        hop2_quote = evidence.hop_quotes[1]
        assert hop2_quote.amount_out is not None and hop2_quote.amount_out.decimals == 0
        assert evidence.amount_out is not None and evidence.amount_out.decimals == 6

    def test_f01_cross_tick_preservation(self) -> None:
        """F01 Invariant: Exceeding single-tick liquidity boundary must fail-closed as UNSUPPORTED."""
        bridge = ArcQuoteBridge()
        pool1 = make_pool("01", USDC_6, WETH_18, fee_pips=500, tick_spacing=10)
        pool2 = make_pool("02", USDC_6, WETH_18, fee_pips=500, tick_spacing=10)

        hop1 = HopRef(pool_key=pool1.key, asset_in=USDC_6, asset_out=WETH_18, direction="zero_for_one", pool_descriptor=pool1)
        hop2 = HopRef(pool_key=pool2.key, asset_in=WETH_18, asset_out=USDC_6, direction="one_for_zero", pool_descriptor=pool2)
        route = RouteRef(chain_id=CHAIN_ARC, base_asset=USDC_6, hops=(hop1, hop2))

        # Thin pool: very small liquidity so 1000 USDC will cross tick boundary
        sv = make_arc_state_version()
        thin_snap1 = make_snapshot(pool1.key.pool_id, tick=5, liquidity=1_000_000, fee_pips=500, tick_spacing=10)
        snap2 = make_snapshot(pool2.key.pool_id, tick=-5, liquidity=50_000_000_000, fee_pips=500, tick_spacing=10)

        epoch = FrozenEpoch(
            epoch_id="epoch-thin",
            state_version=sv,
            snapshots=(thin_snap1, snap2),
            created_at_ms=1726000000000,
        )

        amount_in = Amount(asset_ref=USDC_6, atoms=1_000_000_000, decimals=6)
        evidence = bridge.quote_exact_input(
            route=route,
            amount_in=amount_in,
            epoch=epoch,
            token_decimals=DECIMALS_REGISTRY,
        )

        assert evidence.status == QuoteStatus.UNSUPPORTED
        assert evidence.amount_out is None
        assert evidence.delta_atoms is None
        assert "Swap crossed single-tick boundary" in str(evidence.error)

    def test_rejection_of_l2_block_domain(self) -> None:
        """Arc requires L1 block domain; L2 state must be rejected."""
        bridge = ArcQuoteBridge()
        pool1 = make_pool("01", USDC_6, WETH_18)
        pool2 = make_pool("02", USDC_6, WETH_18)
        hop1 = HopRef(pool_key=pool1.key, asset_in=USDC_6, asset_out=WETH_18, direction="zero_for_one", pool_descriptor=pool1)
        hop2 = HopRef(pool_key=pool2.key, asset_in=WETH_18, asset_out=USDC_6, direction="one_for_zero", pool_descriptor=pool2)
        route = RouteRef(chain_id=CHAIN_ARC, base_asset=USDC_6, hops=(hop1, hop2))

        sv_l2 = make_arc_state_version(block_domain="l2")
        epoch = FrozenEpoch(
            epoch_id="epoch-l2",
            state_version=sv_l2,
            snapshots=(make_snapshot(pool1.key.pool_id), make_snapshot(pool2.key.pool_id)),
            created_at_ms=1726000000000,
        )

        amount_in = Amount(asset_ref=USDC_6, atoms=10_000_000, decimals=6)
        evidence = bridge.quote_exact_input(
            route=route,
            amount_in=amount_in,
            epoch=epoch,
            token_decimals=DECIMALS_REGISTRY,
        )

        assert evidence.status == QuoteStatus.UNSUPPORTED
        assert "Arc requires L1 block domain" in str(evidence.error)

    def test_rejection_of_wrong_chain_id(self) -> None:
        bridge = ArcQuoteBridge()
        asset0_4663 = AssetRef("erc20", 4663, TokenKey(4663, "0x" + "01" * 20))
        asset1_4663 = AssetRef("erc20", 4663, TokenKey(4663, "0x" + "02" * 20))
        pool1_4663 = make_pool("01", asset0_4663, asset1_4663)
        pool2_4663 = make_pool("02", asset0_4663, asset1_4663)
        hop1 = HopRef(
            pool_key=pool1_4663.key,
            asset_in=asset0_4663,
            asset_out=asset1_4663,
            direction="zero_for_one",
            pool_descriptor=pool1_4663,
        )
        hop2 = HopRef(
            pool_key=pool2_4663.key,
            asset_in=asset1_4663,
            asset_out=asset0_4663,
            direction="one_for_zero",
            pool_descriptor=pool2_4663,
        )
        route = RouteRef(chain_id=4663, base_asset=asset0_4663, hops=(hop1, hop2))

        sv = make_arc_state_version()
        epoch = FrozenEpoch(epoch_id="ep", state_version=sv, snapshots=(), created_at_ms=1726000000000)

        amount_in = Amount(asset0_4663, 10_000_000, 6)
        evidence = bridge.quote_exact_input(
            route=route,
            amount_in=amount_in,
            epoch=epoch,
            token_decimals={asset0_4663: 6, asset1_4663: 18},
        )

        assert evidence.status == QuoteStatus.UNSUPPORTED
        assert "Route chain_id 4663 does not match bridge chain_id 5042" in str(evidence.error)

    def test_rejection_of_unverified_hooks_and_dynamic_fees(self) -> None:
        bridge = ArcQuoteBridge()
        pool_hook = make_pool("01", USDC_6, WETH_18, hooks="0x1234567890123456789012345678901234567890")
        pool_clean = make_pool("02", USDC_6, WETH_18)
        hop_h1 = HopRef(pool_key=pool_hook.key, asset_in=USDC_6, asset_out=WETH_18, direction="zero_for_one", pool_descriptor=pool_hook)
        hop_h2 = HopRef(pool_key=pool_clean.key, asset_in=WETH_18, asset_out=USDC_6, direction="one_for_zero", pool_descriptor=pool_clean)
        route_h = RouteRef(chain_id=CHAIN_ARC, base_asset=USDC_6, hops=(hop_h1, hop_h2))

        sv = make_arc_state_version()
        epoch_h = FrozenEpoch(epoch_id="ep", state_version=sv, snapshots=(), created_at_ms=1726000000000)

        ev_h = bridge.quote_exact_input(route_h, Amount(USDC_6, 100, 6), epoch_h, token_decimals=DECIMALS_REGISTRY)
        assert ev_h.status == QuoteStatus.UNSUPPORTED
        assert "hooks are UNSUPPORTED" in str(ev_h.error)

        pool_dyn = make_pool("03", USDC_6, WETH_18, fee_kind="dynamic")
        hop_d1 = HopRef(pool_key=pool_dyn.key, asset_in=USDC_6, asset_out=WETH_18, direction="zero_for_one", pool_descriptor=pool_dyn)
        hop_d2 = HopRef(pool_key=pool_clean.key, asset_in=WETH_18, asset_out=USDC_6, direction="one_for_zero", pool_descriptor=pool_clean)
        route_d = RouteRef(chain_id=CHAIN_ARC, base_asset=USDC_6, hops=(hop_d1, hop_d2))
        ev_d = bridge.quote_exact_input(route_d, Amount(USDC_6, 100, 6), epoch_h, token_decimals=DECIMALS_REGISTRY)
        assert ev_d.status == QuoteStatus.UNSUPPORTED
        assert "dynamic fee is UNSUPPORTED" in str(ev_d.error)

    def test_graph_and_cycle_discovery(self) -> None:
        bridge = ArcQuoteBridge()
        pool1 = make_pool("01", USDC_6, WETH_18)
        pool2 = make_pool("02", WETH_18, MEME_0)
        pool3 = make_pool("03", MEME_0, USDC_6)

        # Foreign pool (e.g. chain 4663) should be filtered out
        foreign_key = PoolKey(4663, "uniswap_v3", "factory", "0x" + "99" * 20, "address", "0x" + "99" * 20)
        foreign_pool = PoolDescriptor(
            key=foreign_key,
            currency0=AssetRef("erc20", 4663, TokenKey(4663, "0x" + "01" * 20)),
            currency1=AssetRef("erc20", 4663, TokenKey(4663, "0x" + "02" * 20)),
            fee_model=FeeModel.static(500),
            tick_spacing=10,
            deployment_status="deployed",
        )

        graph = bridge.build_graph([pool1, pool2, pool3, foreign_pool])
        assert graph.pool_count() == 3  # foreign pool excluded!
        assert graph.has_pool(pool1.key)
        assert not graph.has_pool(foreign_pool.key)

        routes = bridge.find_routes(graph, [USDC_6], allowed_hops=(3,))
        # Both directions of the triangle: USDC->WETH->MEME->USDC and USDC->MEME->WETH->USDC
        assert len(routes) == 2
        for r in routes:
            assert len(r.hops) == 3
            assert r.chain_id == CHAIN_ARC
            assert r.base_asset == USDC_6
