"""Bounded, offline page replay. Completion applies only to the explicit source scope."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from arbitrage_contracts.identity import (
    PoolDescriptor,
    PoolKey,
    validate_bytes32,
    validate_evm_address,
    validate_non_negative_integer,
    validate_positive_integer,
)


@dataclass(frozen=True)
class DiscoveryScope:
    """Caller-pinned chain, venue and block interval; no implicit latest/network source."""

    chain_id: int
    venue_address: str
    block_from: int
    block_to: int
    block_hash: str
    source_ref: str

    def __post_init__(self) -> None:
        validate_positive_integer(self.chain_id, "chain_id")
        validate_evm_address(self.venue_address)
        validate_non_negative_integer(self.block_from, "block_from")
        validate_non_negative_integer(self.block_to, "block_to")
        validate_bytes32(self.block_hash)
        if self.block_from > self.block_to or not self.source_ref:
            raise ValueError("Invalid discovery scope")


@dataclass(frozen=True)
class PoolObservation:
    """Pool identity plus display metadata, never executable depth or profit evidence."""

    pool: PoolDescriptor
    tvl_usd: str | None = None
    volume_24h_usd: str | None = None


@dataclass(frozen=True)
class DiscoveryPage:
    """One explicit page; empty items alone do not establish end of pagination."""

    number: int
    items: tuple[PoolObservation, ...]
    has_more: bool

    def __post_init__(self) -> None:
        validate_positive_integer(self.number, "number")
        if type(self.has_more) is not bool or type(self.items) is not tuple:
            raise TypeError("Pages require a boolean has_more and immutable tuple items")
        if not all(isinstance(item, PoolObservation) for item in self.items):
            raise TypeError("Invalid page item")


@dataclass(frozen=True)
class PageTrace:
    """Every attempted request, including errors and replayed/out-of-order pages."""

    requested_page: int
    received_page: int | None
    item_count: int
    has_more: bool | None
    outcome: str
    error: str | None = None


@dataclass(frozen=True)
class DiscoveryResult:
    """Bounded observations with explicit gaps; never a whole-chain completeness claim."""

    scope: DiscoveryScope
    observations: tuple[PoolObservation, ...]
    traces: tuple[PageTrace, ...]
    requests_used: int
    pages_received: tuple[int, ...]
    missing_pages: tuple[int, ...]
    terminal_page: int | None
    complete: bool
    empty_results: bool
    is_truncated: bool
    stop_reason: str


class OfflinePageReader(Protocol):
    """Explicit injected local page source; no built-in or default network transport."""

    def __call__(self, scope: DiscoveryScope, number: int, size: int, /) -> DiscoveryPage:
        """Return one bounded recorded page for this pinned scope."""
        ...


def discover_pools(
    read_page: OfflinePageReader,
    *,
    scope: DiscoveryScope,
    page_size: int,
    max_pages: int,
    budget_requests: int,
) -> DiscoveryResult:
    """Replay an explicitly supplied offline reader; count failed requests without retrying.

    The reader receives (scope, missing_page_number, page_size). It must perform one
    bounded local read and never network I/O. Duplicate/out-of-order pages consume
    requests; success requires a terminal page AND every preceding page.
    """
    validate_positive_integer(page_size, "page_size")
    validate_positive_integer(max_pages, "max_pages")
    validate_non_negative_integer(budget_requests, "budget_requests")
    pages: dict[int, DiscoveryPage] = {}
    pools: dict[PoolKey, PoolObservation] = {}
    traces: list[PageTrace] = []
    terminal: int | None = None
    complete = False
    reason = "budget_exhausted"
    while len(traces) < budget_requests:
        if len(pages) >= max_pages:
            reason = "max_pages_reached"
            break
        requested = next(n for n in range(1, max_pages + 1) if n not in pages)
        page: DiscoveryPage | None = None
        try:
            page = read_page(scope, requested, page_size)
            if not isinstance(page, DiscoveryPage):
                raise TypeError("Reader did not return DiscoveryPage")
            if page.number > max_pages or len(page.items) > page_size:
                raise ValueError("Page exceeds configured bounds")
            if terminal is not None and page.number > terminal:
                raise ValueError("Page after terminal page")
            if not page.has_more and (
                (terminal is not None and terminal != page.number)
                or any(n > page.number for n in pages)
            ):
                raise ValueError("Conflicting terminal page")
            previous = pages.get(page.number)
            if previous is not None and previous != page:
                raise ValueError("Conflicting duplicate page")
            additions: dict[PoolKey, PoolObservation] = {}
            for item in page.items:
                key = item.pool.key
                if key.chain_id != scope.chain_id or key.canonical_venue_address != (
                    scope.venue_address.lower()
                ):
                    raise ValueError("Observation outside discovery scope")
                old = additions.get(key, pools.get(key))
                if old is not None and old != item:
                    raise ValueError("Conflicting pool observation")
                additions[key] = item
            pools.update(additions)
            pages[page.number] = page
            if not page.has_more:
                terminal = page.number
            outcome = (
                "duplicate"
                if previous
                else ("out_of_order" if page.number != requested else "accepted")
            )
            traces.append(
                PageTrace(requested, page.number, len(page.items), page.has_more, outcome)
            )
        except Exception as exc:
            traces.append(
                PageTrace(
                    requested,
                    page.number if isinstance(page, DiscoveryPage) else None,
                    0,
                    None,
                    "error",
                    f"{type(exc).__name__}: {exc}",
                )
            )
            reason = "pagination_error"
            break
        if terminal is not None and all(n in pages for n in range(1, terminal + 1)):
            complete = True
            reason = "empty_results" if not pools else "completed"
            break
    end = terminal if terminal is not None else min(max(pages, default=0) + 1, max_pages)
    return DiscoveryResult(
        scope=scope,
        observations=tuple(pools.values()),
        traces=tuple(traces),
        requests_used=len(traces),
        pages_received=tuple(sorted(pages)),
        missing_pages=tuple(n for n in range(1, end + 1) if n not in pages),
        terminal_page=terminal,
        complete=complete,
        empty_results=not pools,
        is_truncated=not complete,
        stop_reason=reason,
    )
