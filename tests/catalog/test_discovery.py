"""C11/C16 bounded synthetic replay: completeness is scoped and metadata cannot trade."""

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import pytest

from arbitrage_contracts.identity import AssetRef, FeeModel, PoolDescriptor, PoolKey, TokenKey
from market_catalog.discovery import (
    DiscoveryPage,
    DiscoveryScope,
    PoolObservation,
    discover_pools,
)
from market_catalog.verification import verify_pool

FACTORY = "0x1f7d7550b1b028f7571e69a784071f0205fd2efa"
SCOPE = DiscoveryScope(4663, FACTORY, 100, 110, "0x" + "ab" * 32, "synthetic:w1c")


def observation(index: int = 1) -> PoolObservation:
    return PoolObservation(
        PoolDescriptor(
            PoolKey(4663, "uniswap-v3", "factory", FACTORY, "address", f"0x{index:040x}"),
            AssetRef.erc20(TokenKey(4663, "0x" + "11" * 20)),
            AssetRef.erc20(TokenKey(4663, "0x" + "22" * 20)),
            FeeModel.static(3000),
            tick_spacing=60,
        ),
        tvl_usd="1000000000000",
        volume_24h_usd="42",
    )


def replay(pages: Sequence[DiscoveryPage | Exception], **limits: Any) -> Any:
    iterator = iter(pages)

    def read(scope: DiscoveryScope, number: int, size: int) -> DiscoveryPage:
        assert scope == SCOPE and size == limits.get("page_size", 2)
        page = next(iterator)
        if isinstance(page, Exception):
            raise page
        return page

    return discover_pools(
        read,
        scope=SCOPE,
        page_size=limits.get("page_size", 2),
        max_pages=limits.get("max_pages", 4),
        budget_requests=limits.get("budget_requests", 4),
    )


def test_discovery_pagination_budget_truncated() -> None:
    result = replay([DiscoveryPage(1, (observation(),), True)], budget_requests=1)
    assert result.is_truncated is True
    assert result.complete is False
    assert result.stop_reason == "budget_exhausted"
    assert result.requests_used == 1 and result.terminal_page is None
    assert result.missing_pages == (2,)


def test_normal_completion_at_exact_budget() -> None:
    result = replay(
        [
            DiscoveryPage(1, (observation(),), True),
            DiscoveryPage(2, (observation(2),), False),
        ],
        budget_requests=2,
    )
    assert result.complete and not result.is_truncated
    assert result.stop_reason == "completed" and result.requests_used == 2
    assert len(result.observations) == 2 and not result.missing_pages
    assert result.scope == SCOPE


@pytest.mark.parametrize("more", [False, True])
def test_empty_response_is_not_implicit_completion(more: bool) -> None:
    result = replay([DiscoveryPage(1, (), more)], budget_requests=1)
    assert result.empty_results
    assert result.complete is (not more)
    assert result.is_truncated is more
    assert result.stop_reason == ("budget_exhausted" if more else "empty_results")


def test_empty_intermediate_page_continues() -> None:
    result = replay([DiscoveryPage(1, (), True), DiscoveryPage(2, (observation(),), False)])
    assert result.complete and not result.empty_results and result.requests_used == 2


def test_zero_budget_never_calls_reader() -> None:
    result = replay([], budget_requests=0)
    assert result.requests_used == 0 and result.is_truncated and not result.complete
    assert result.empty_results and result.pages_received == ()


def test_max_pages_is_a_separate_stop() -> None:
    result = replay([DiscoveryPage(1, (observation(),), True)], max_pages=1)
    assert result.stop_reason == "max_pages_reached"
    assert result.is_truncated and not result.complete and result.requests_used == 1


def test_duplicate_and_out_of_order_pages_are_idempotent() -> None:
    last = DiscoveryPage(2, (observation(), observation(2)), False)
    result = replay([last, last, DiscoveryPage(1, (observation(),), True)])
    assert result.complete and len(result.observations) == 2
    assert result.requests_used == 3 and result.pages_received == (1, 2)
    assert [t.outcome for t in result.traces] == ["out_of_order", "duplicate", "accepted"]
    assert [t.requested_page for t in result.traces] == [1, 1, 1]


def test_terminal_page_with_gap_does_not_complete() -> None:
    result = replay([DiscoveryPage(3, (), False)], budget_requests=1)
    assert result.terminal_page == 3 and result.missing_pages == (1, 2)
    assert result.is_truncated and not result.complete


def test_failure_counted_and_no_retry() -> None:
    result = replay([DiscoveryPage(1, (observation(),), True), OSError("recorded page failure")])
    assert result.requests_used == 2 and result.is_truncated and not result.complete
    assert result.stop_reason == "pagination_error"
    assert result.traces[-1].error == "OSError: recorded page failure"
    assert len(result.observations) == 1


@pytest.mark.parametrize(
    "pages",
    [
        [DiscoveryPage(1, (), True), DiscoveryPage(1, (observation(),), True)],
        [DiscoveryPage(2, (), True), DiscoveryPage(1, (), False)],
        [DiscoveryPage(2, (), False), DiscoveryPage(3, (), True)],
        [DiscoveryPage(2, (), False), DiscoveryPage(1, (), False)],
        [DiscoveryPage(5, (), False)],
        [DiscoveryPage(1, (observation(), observation(2), observation(3)), False)],
    ],
)
def test_bad_page_boundaries_fail_closed(pages: list[DiscoveryPage]) -> None:
    result = replay(pages)
    assert result.stop_reason == "pagination_error" and result.is_truncated
    assert not result.complete and result.traces[-1].error


def test_conflicting_observation_page_is_atomic() -> None:
    first = observation()
    result = replay(
        [
            DiscoveryPage(1, (first,), True),
            DiscoveryPage(2, (observation(2), replace(first, tvl_usd="changed")), False),
        ]
    )
    assert result.stop_reason == "pagination_error"
    assert result.observations == (first,) and result.pages_received == (1,)


@pytest.mark.parametrize("field,value", [("chain_id", 56), ("venue_address", "0x" + "ff" * 20)])
def test_wrong_chain_or_venue_rejected(field: str, value: Any) -> None:
    item = observation()
    key = item.pool.key
    args = {
        name: getattr(key, name)
        for name in (
            "chain_id",
            "protocol_id",
            "venue_kind",
            "venue_address",
            "pool_id_kind",
            "pool_id",
        )
    }
    args[field] = value
    new_key = PoolKey(**args)
    pool = replace(
        item.pool,
        key=new_key,
        currency0=AssetRef.erc20(TokenKey(new_key.chain_id, "0x" + "11" * 20)),
        currency1=AssetRef.erc20(TokenKey(new_key.chain_id, "0x" + "22" * 20)),
    )
    result = replay([DiscoveryPage(1, (replace(item, pool=pool),), False)])
    assert not result.observations and result.is_truncated
    assert "outside discovery scope" in result.traces[-1].error


@pytest.mark.parametrize(
    "field,value",
    [
        ("page_size", 0),
        ("page_size", True),
        ("max_pages", -1),
        ("max_pages", 1.5),
        ("budget_requests", -1),
        ("budget_requests", False),
    ],
)
def test_bad_limits_rejected_before_read(field: str, value: Any) -> None:
    with pytest.raises((ValueError, TypeError)):
        replay([], **{field: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("chain_id", True),
        ("block_from", -1),
        ("block_to", 99),
        ("block_hash", "0x1234"),
        ("venue_address", "fake"),
        ("source_ref", ""),
    ],
)
def test_bad_scope(field: str, value: Any) -> None:
    with pytest.raises((ValueError, TypeError)):
        replace(SCOPE, **{field: value})


def test_metadata_retained_without_depth_profit_or_approval() -> None:
    result = replay([DiscoveryPage(1, (observation(),), False)])
    item = result.observations[0]
    assert item.tvl_usd == "1000000000000" and item.volume_24h_usd == "42"
    verified = verify_pool(item.pool)
    assert verified.review_status == "pending_review"
    assert verified.can_quote == "unknown" and verified.can_atomic_execute == "unsupported"
    assert not hasattr(item, "executable_depth") and not hasattr(item, "profit")
