"""Unit tests for independent reference parity comparison."""

from __future__ import annotations

from itertools import count

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
    EvidenceLevel,
    HopQuote,
    HopRef,
    QuoteEvidence,
    QuoteStatus,
    RouteRef,
    TriState,
)
from arbitrage_contracts.state import StateVersion, canonical_state_ref
from state_graph.reference import ParityStatus, compare_reference

_IDS = count()


def make_route() -> RouteRef:
    weth = AssetRef("erc20", 4663, TokenKey(4663, "0x" + "1" * 40))
    usdg = AssetRef("erc20", 4663, TokenKey(4663, "0x" + "2" * 40))
    pk1 = PoolKey(4663, "uniswap_v3", "factory", "0x" + "a" * 40, "address", "0x" + "b" * 40)
    pk2 = PoolKey(4663, "uniswap_v3", "factory", "0x" + "a" * 40, "address", "0x" + "c" * 40)
    desc1 = PoolDescriptor(pk1, weth, usdg, FeeModel.static(500))
    desc2 = PoolDescriptor(pk2, weth, usdg, FeeModel.static(3000))
    hop1 = HopRef(pk1, weth, usdg, "zero_for_one", desc1)
    hop2 = HopRef(pk2, usdg, weth, "one_for_zero", desc2)
    return RouteRef(4663, weth, (hop1, hop2), max_hops=3)


def make_evidence(
    route: RouteRef,
    amount_in: int,
    amount_out: int | None,
    delta: int | None,
    status: str = QuoteStatus.QUOTED,
    level: str = EvidenceLevel.LOCAL_QUOTE,
    error: str | None = None,
) -> QuoteEvidence:
    a_in = Amount(route.base_asset, amount_in, 18)
    a_out = Amount(route.base_asset, amount_out, 18) if amount_out is not None else None
    hqs = []
    if amount_out is not None:
        mid_amount = Amount(route.hops[0].asset_out, amount_in - 20, 6)
        hqs.append(
            HopQuote(
                0,
                route.hops[0].pool_key,
                route.hops[0].asset_in,
                route.hops[0].asset_out,
                a_in,
                mid_amount,
            )
        )
        hqs.append(
            HopQuote(
                1,
                route.hops[1].pool_key,
                route.hops[1].asset_in,
                route.hops[1].asset_out,
                mid_amount,
                a_out,
            )
        )

    return QuoteEvidence(
        quote_id=f"quote-test-{next(_IDS)}",
        state_version_ref=canonical_state_ref(StateVersion(4663, 100, "0x" + "aa" * 32, 1000)),
        route_ref=route,
        amount_in=a_in,
        amount_out=a_out,
        delta_atoms=delta,
        hop_quotes=tuple(hqs),
        evidence_level=level,
        data_mode=DataMode.SYNTHETIC,
        actor_scope=ActorScope.OWN_AUTHORIZED,
        fee_included=TriState.YES,
        impact_included=TriState.YES,
        status=status,
        error=error,
    )


def test_reference_parity_exact_match() -> None:
    route = make_route()
    local = make_evidence(route, 10000, 9950, -50, level=EvidenceLevel.LOCAL_QUOTE)
    ref = make_evidence(route, 10000, 9950, -50, level=EvidenceLevel.RPC_QUOTE)

    result = compare_reference(local, ref)
    assert result.status == ParityStatus.MATCH
    assert result.is_match is True
    assert result.delta_diff_atoms == 0
    assert result.error is None


def test_reference_parity_mismatch() -> None:
    route = make_route()
    local = make_evidence(route, 10000, 9950, -50)
    ref = make_evidence(route, 10000, 9940, -60)

    result = compare_reference(local, ref)
    assert result.status == ParityStatus.MISMATCH
    assert result.is_match is False
    assert result.delta_diff_atoms == 10
    assert "Delta discrepancy of 10 atoms" in str(result.error)


def test_reference_parity_input_mismatch() -> None:
    route = make_route()
    local = make_evidence(route, 10000, 9950, -50)
    ref = make_evidence(route, 20000, 19900, -100)

    result = compare_reference(local, ref)
    assert result.status == ParityStatus.REF_INPUT_MISMATCH
    assert result.is_match is False
    assert result.delta_diff_atoms is None
    assert "Input mismatch" in str(result.error)


def test_reference_parity_unavailable() -> None:
    route = make_route()
    local = make_evidence(route, 10000, 9950, -50)

    result = compare_reference(local, None)
    assert result.status == ParityStatus.REF_UNAVAILABLE
    assert result.is_match is False
    assert result.delta_diff_atoms is None


def test_reference_parity_reference_error() -> None:
    route = make_route()
    local = make_evidence(route, 10000, 9950, -50)
    ref = make_evidence(
        route,
        10000,
        None,
        None,
        status=QuoteStatus.CONTRACT_REVERT,
        level=EvidenceLevel.RPC_QUOTE,
        error="execution reverted",
    )

    result = compare_reference(local, ref)
    assert result.status == ParityStatus.REF_ERROR
    assert result.is_match is False
    assert result.delta_diff_atoms is None
    assert "execution reverted" in str(result.error)


def test_reference_parity_missing_state_never_counts_as_match() -> None:
    from dataclasses import replace

    route = make_route()
    local = make_evidence(route, 10000, 9950, -50)
    reference = make_evidence(route, 10000, 9950, -50)
    for lhs, rhs in (
        (None, reference.state_version_ref),
        (local.state_version_ref, None),
        (None, None),
    ):
        result = compare_reference(
            replace(local, state_version_ref=lhs), replace(reference, state_version_ref=rhs)
        )
        assert result.status == ParityStatus.REF_INPUT_MISMATCH
        assert not result.is_match
