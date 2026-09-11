"""Unit tests verifying audit remediation for state_graph (F01, F02-A, F02-C, F07)."""

from pathlib import Path

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
    HopRef,
    QuoteEvidence,
    QuoteStatus,
    RouteRef,
)
from arbitrage_contracts.state import StateVersion
from state_graph.evaluate import evaluate_route_exact_input
from state_graph.reference import ParityStatus, compare_reference
from state_graph.store import StateStore, StateStoreError
from state_graph.types import FrozenEpoch, PoolStateSnapshot


def _make_asset(name: str, chain_id: int = 4663) -> AssetRef:
    hex_addr = "0x" + name.encode().hex().rjust(40, "0")[:40]
    return AssetRef(
        interface_kind="erc20",
        chain_id=chain_id,
        token_key=TokenKey(chain_id, hex_addr),
    )


def _make_pool(
    token0: AssetRef,
    token1: AssetRef,
    pool_id_str: str,
    fee_pips: int = 500,
    chain_id: int = 4663,
) -> PoolDescriptor:
    pool_addr = "0x" + pool_id_str.encode().hex().rjust(40, "0")[:40]
    venue_addr = "0x" + b"factory".hex().rjust(40, "0")[:40]
    key = PoolKey(
        chain_id=chain_id,
        protocol_id="uniswap_v3",
        venue_kind="factory",
        venue_address=venue_addr,
        pool_id_kind="address",
        pool_id=pool_addr,
    )
    return PoolDescriptor(
        key=key,
        currency0=token0,
        currency1=token1,
        fee_model=FeeModel.static(fee_pips),
        tick_spacing=10,
        deployment_status="deployed",
    )


def _make_epoch(snapshots: list[PoolStateSnapshot], block_number: int = 100) -> FrozenEpoch:
    state = StateVersion(
        chain_id=4663,
        block_number=block_number,
        block_hash="0x" + "aa" * 32,
        received_at_ms=1000,
        complete_through_block=block_number,
        completeness="ready",
    )
    return FrozenEpoch(
        epoch_id=f"epoch_{block_number}",
        state_version=state,
        snapshots=tuple(snapshots),
        created_at_ms=1000,
    )


def test_f01_single_tick_boundary_exceeded_fail_closed() -> None:
    """F01: When input atoms exceed single tick segment depth, evaluate must fail-closed."""
    chain_id = 4663
    weth = _make_asset("weth")
    usdg = _make_asset("usdg")

    p1 = _make_pool(weth, usdg, "p1_tiny", fee_pips=500)
    p2 = _make_pool(weth, usdg, "p2_large", fee_pips=500)

    hop1 = HopRef(p1.key, weth, usdg, "zero_for_one", p1)
    hop2 = HopRef(p2.key, usdg, weth, "one_for_zero", p2)
    route = RouteRef(chain_id, weth, (hop1, hop2), max_hops=3)

    # Liquidity is very tiny on hop 1: 1000 atoms
    snap1 = PoolStateSnapshot(
        pool_id=p1.key.pool_id,
        sqrt_price_x96=1 << 96,
        tick=0,
        liquidity=1000,
        fee_pips=500,
        tick_spacing=10,
        block_number=100,
        block_hash="0x" + "aa" * 32,
    )
    snap2 = PoolStateSnapshot(
        pool_id=p2.key.pool_id,
        sqrt_price_x96=1 << 96,
        tick=0,
        liquidity=10**22,
        fee_pips=500,
        tick_spacing=10,
        block_number=100,
        block_hash="0x" + "aa" * 32,
    )
    epoch = _make_epoch([snap1, snap2], 100)

    # Huge input: 10^18 atoms (far exceeds 1000 liquidity capacity)
    amt_in = Amount(weth, 10**18, 18)
    evidence = evaluate_route_exact_input(route, amt_in, epoch, token_decimals={weth: 18, usdg: 18})

    # Must be intercepted as UNSUPPORTED, not quoted
    assert evidence.status == QuoteStatus.UNSUPPORTED
    assert "Swap crossed single-tick boundary" in (evidence.error or "")


def test_f02_a_state_store_regression_and_mixed_block_rejected() -> None:
    """F02-A: StateStore must reject lower block numbers and mixed-block snapshots."""
    store = StateStore()
    s100 = StateVersion(
        chain_id=4663,
        block_number=100,
        block_hash="0x" + "11" * 32,
        received_at_ms=1000,
        complete_through_block=100,
        completeness="ready",
    )
    snap100 = PoolStateSnapshot("p1", 1 << 96, 0, 10**18, 500, 10, 100, "0x" + "11" * 32)
    store.publish_epoch(s100, [snap100])

    # 1. Reject state regression (100 -> 99)
    s99 = StateVersion(
        chain_id=4663,
        block_number=99,
        block_hash="0x" + "99" * 32,
        received_at_ms=999,
        complete_through_block=99,
        completeness="ready",
    )
    snap99 = PoolStateSnapshot("p1", 1 << 96, 0, 10**18, 500, 10, 99, "0x" + "99" * 32)
    with pytest.raises(StateStoreError, match="State regression rejected"):
        store.publish_epoch(s99, [snap99])

    # 2. Reject mixed-block snapshots (epoch is 101, but snapshot claims block 100)
    s101 = StateVersion(
        chain_id=4663,
        block_number=101,
        block_hash="0x" + "22" * 32,
        received_at_ms=1001,
        complete_through_block=101,
        completeness="ready",
    )
    with pytest.raises(StateStoreError, match="Mixed-block snapshot rejected"):
        store.publish_epoch(s101, [snap100])  # snap100 has block 100 != 101


def test_f02_c_compare_reference_enforces_state_parity() -> None:
    """F02-C: compare_reference must reject matching deltas calculated from different state versions."""
    weth = _make_asset("weth")
    usdg = _make_asset("usdg")
    p1 = _make_pool(weth, usdg, "p1")
    p2 = _make_pool(weth, usdg, "p2")

    hop1 = HopRef(p1.key, weth, usdg, "zero_for_one", p1)
    hop2 = HopRef(p2.key, usdg, weth, "one_for_zero", p2)
    route = RouteRef(4663, weth, (hop1, hop2))

    a_in = Amount(weth, 1000, 18)
    a_out = Amount(weth, 1050, 18)

    # Local quote on epoch-100
    q_local = QuoteEvidence(
        quote_id="q1",
        route_ref=route,
        amount_in=a_in,
        amount_out=a_out,
        delta_atoms=50,
        hop_quotes=(),
        state_version_ref="epoch:100:0x1111",
        status=QuoteStatus.QUOTED,
    )

    # Reference quote on epoch-99 (different state)
    q_ref = QuoteEvidence(
        quote_id="q2",
        route_ref=route,
        amount_in=a_in,
        amount_out=a_out,
        delta_atoms=50,
        hop_quotes=(),
        state_version_ref="epoch:99:0x9999",
        status=QuoteStatus.QUOTED,
    )

    result = compare_reference(q_local, q_ref)
    assert result.status == ParityStatus.REF_INPUT_MISMATCH
    assert result.is_match is False
    assert "State version mismatch" in (result.error or "")


def test_f07_intermediate_hop_preserves_distinct_token_decimals() -> None:
    """F07: Intermediate hop with distinct asset precision (e.g. USDC 6 dec) must not inherit 18 dec."""
    chain_id = 4663
    weth = _make_asset("weth")  # 18 decimals
    usdc = _make_asset("usdc")  # 6 decimals

    pool1 = _make_pool(weth, usdc, "p_weth_usdc")
    pool2 = _make_pool(weth, usdc, "p_usdc_weth")

    hop1 = HopRef(pool1.key, weth, usdc, direction="zero_for_one", pool_descriptor=pool1)
    hop2 = HopRef(pool2.key, usdc, weth, direction="one_for_zero", pool_descriptor=pool2)
    route = RouteRef(chain_id, weth, (hop1, hop2))

    from state_graph.clmm_math import get_sqrt_ratio_at_tick

    snap1 = PoolStateSnapshot(
        pool1.key.pool_id, get_sqrt_ratio_at_tick(5), 5, 10**24, 500, 10, 100, "0x" + "aa" * 32
    )
    snap2 = PoolStateSnapshot(
        pool2.key.pool_id, get_sqrt_ratio_at_tick(5), 5, 10**24, 500, 10, 100, "0x" + "aa" * 32
    )
    epoch = _make_epoch([snap1, snap2], 100)

    # Pass explicit token_decimals mapping
    assert usdc.token_key is not None
    token_decimals = {
        weth: 18,
        usdc: 6,
        usdc.token_key.address.lower(): 6,
    }

    amt_in = Amount(weth, 10**16, 18)
    evidence = evaluate_route_exact_input(route, amt_in, epoch, token_decimals=token_decimals)

    assert evidence.status == QuoteStatus.QUOTED
    assert len(evidence.hop_quotes) == 2
    # Hop 0 output is USDC -> decimals MUST be 6, NOT inherited 18!
    assert evidence.hop_quotes[0].amount_out is not None
    assert evidence.hop_quotes[0].amount_out.decimals == 6
    assert evidence.hop_quotes[0].amount_out.asset_ref == usdc
    # Hop 1 output is WETH -> decimals is 18 (base asset)
    assert evidence.hop_quotes[1].amount_out is not None
    assert evidence.hop_quotes[1].amount_out.decimals == 18


@pytest.mark.parametrize("tick,direction", [(0, "zero_for_one"), (-1, "one_for_zero")])
def test_f01_starting_boundary_never_skips_unknown_crossing(tick, direction):
    from state_graph.evaluate import single_segment_target

    snapshot = PoolStateSnapshot("p", 1 << 96, tick, 10**24, 500, 10, 100, "0x" + "aa" * 32)
    with pytest.raises(ValueError, match="starting tick boundary"):
        single_segment_target(snapshot, direction)


def test_f01_extreme_tick_clamped_before_lookup():
    from state_graph.clmm_math import MIN_SQRT_RATIO
    from state_graph.evaluate import single_segment_target

    snapshot = PoolStateSnapshot(
        "p", MIN_SQRT_RATIO, -887272, 10**24, 500, 10, 100, "0x" + "aa" * 32
    )
    with pytest.raises(ValueError, match="starting tick boundary"):
        single_segment_target(snapshot, "zero_for_one")


def test_f02_exact_anchor_rejects_height_and_hash_substrings():
    from arbitrage_contracts.state import canonical_state_ref, matches_state_ref

    state = _make_epoch([]).state_version
    assert matches_state_ref(canonical_state_ref(state), state)
    for bad in (None, "100", "block:100", state.block_hash, "prefix:" + state.block_hash):
        assert not matches_state_ref(bad, state)
    other = StateVersion(4663, 100, "0x" + "bb" * 32, 1000)
    assert not matches_state_ref(canonical_state_ref(state), other)


def test_f02_same_height_wrong_snapshot_hash_is_rejected():
    state = _make_epoch([]).state_version
    wrong = PoolStateSnapshot("p", 1 << 96, 0, 10**24, 500, 10, 100, "0x" + "bb" * 32)
    with pytest.raises(StateStoreError, match="hash mismatch"):
        StateStore().publish_epoch(state, [wrong])


def test_f02_parent_duplicate_and_empty_coverage_rejected():
    state = _make_epoch([]).state_version
    snap = PoolStateSnapshot("p", 1 << 96, 0, 10**24, 500, 10, 100, state.block_hash)
    with pytest.raises(StateStoreError, match="Duplicate"):
        StateStore().publish_epoch(state, [snap, snap])
    with pytest.raises(StateStoreError, match="coverage"):
        StateStore().publish_epoch(state, [])
    store = StateStore()
    store.publish_epoch(state, [snap])
    next_state = StateVersion(4663, 101, "0x" + "cc" * 32, 1001, parent_hash="0x" + "dd" * 32)
    next_snap = PoolStateSnapshot("p", 1 << 96, 0, 10**24, 500, 10, 101, next_state.block_hash)
    with pytest.raises(StateStoreError, match="parent"):
        store.publish_epoch(next_state, [next_snap])


@pytest.mark.parametrize("registry", [None, {}, {"unrelated": 18}])
def test_f07_missing_units_fail_closed(registry):
    from state_graph.evaluate import resolve_token_decimals

    with pytest.raises(ValueError, match="Missing"):
        resolve_token_decimals(_make_asset("asset"), registry)


def test_f07_zero_precision_and_conflicting_keys():
    from state_graph.evaluate import resolve_token_decimals

    asset = _make_asset("asset")
    assert resolve_token_decimals(asset, {asset: 0}) == 0
    with pytest.raises(ValueError, match="Conflicting"):
        resolve_token_decimals(asset, {asset: 6, asset.token_key.address: 18})


def test_f07_shadow_cli_uses_registry_for_base_and_intermediate(tmp_path):
    import json

    from apps.state_graph_shadow import run_shadow_replay
    from state_graph.clmm_math import get_sqrt_ratio_at_tick

    a, b = "0x" + "01" * 20, "0x" + "02" * 20
    pool_ids = ["0x" + "ab" * 20, "0x" + "cd" * 20]
    registry = {
        "chain_id": 4663,
        "base_assets": [a],
        "token_decimals": {a: 6, b: 18},
        "pools": [
            {"pool_id": pool, "currency0": a, "currency1": b, "fee_pips": 500, "tick_spacing": 10}
            for pool in pool_ids
        ],
    }
    block_hash = "0x" + "aa" * 32
    manifest = {
        "state_version": {
            "chain_id": 4663,
            "block_number": 100,
            "block_hash": block_hash,
            "completeness": "ready",
            "complete_through_block": 100,
        },
        "pool_snapshots": [
            {
                "pool_id": pool,
                "sqrt_price_x96": get_sqrt_ratio_at_tick(5),
                "tick": 5,
                "liquidity": 10**24,
                "fee_pips": 500,
                "tick_spacing": 10,
                "block_number": 100,
                "block_hash": block_hash,
            }
            for pool in pool_ids
        ],
    }
    rp, mp = tmp_path / "registry.json", tmp_path / "manifest.json"
    rp.write_text(json.dumps(registry))
    mp.write_text(json.dumps(manifest))
    result = run_shadow_replay(str(mp), str(rp), str(tmp_path / "out"), amounts=[1000000])
    assert result["successful_quotes"] > 0
    lines = Path(result["output_quotes_file"]).read_text().splitlines()
    for line in lines:
        payload = json.loads(line)["payload"]
        assert payload["amount_in"]["decimals"] == 6
        assert payload["hop_quotes"][0]["amount_out"]["decimals"] == 18
