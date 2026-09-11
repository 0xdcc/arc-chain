"""Read-only coverage-gap classification for settled-cycle research."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from arbitrage_contracts.identity import PoolKey

from .models import ActionKind, AttributionStatus, SettledCycleRecord


class CoverageGapKind(StrEnum):
    """Explicit, non-speculative reasons that a restored cycle is incomplete."""

    IDENTITY_MISSING = "identity_missing"
    HISTORICAL_REGISTRY_MISSING = "historical_registry_missing"
    DECODER_UNSUPPORTED = "decoder_unsupported"
    STATE_UNAVAILABLE = "state_unavailable"
    QUOTE_MISMATCH = "quote_mismatch"
    SIMULATION_UNSUPPORTED = "simulation_unsupported"
    ELIGIBILITY_UNKNOWN = "eligibility_unknown"
    LATENCY_UNKNOWN = "latency_unknown"


@dataclass(frozen=True, slots=True)
class CoverageGap:
    """One normalized, evidence-linked gap."""

    gap_kind: CoverageGapKind
    pool_key_ref: str | None
    evidence_note: str


REGISTRY_SCHEMA = "w3-pool-registry/1.0.0"
QUOTE_SCHEMA = "w3-quote-observations/1.0.0"


def _pool_ref(pool_key: PoolKey) -> str:
    return ":".join(
        (
            str(pool_key.chain_id),
            pool_key.protocol_id,
            pool_key.venue_address.lower(),
            pool_key.pool_id.lower(),
        )
    )


def _normalized_pools(registry: Mapping[str, Any] | None) -> tuple[Mapping[str, Any], ...]:
    if registry is None:
        return ()
    pools = registry.get("pools")
    if isinstance(pools, list) and all(isinstance(item, Mapping) for item in pools):
        return tuple(pools)
    if isinstance(pools, Mapping) and all(isinstance(item, Mapping) for item in pools.values()):
        return tuple(pools.values())
    raise ValueError("registry.pools must be a list or mapping of mappings")


def _period_covers(period: Mapping[str, Any], block_number: int) -> bool:
    start_block = period.get("start_block")
    end_block = period.get("end_block")
    return (
        type(start_block) is int
        and type(end_block) is int
        and start_block <= block_number <= end_block
    )


def _historical_entries(
    pool_entry: Mapping[str, Any], block_number: int
) -> tuple[Mapping[str, Any], ...]:
    periods = pool_entry.get("historical_periods")
    if not isinstance(periods, list):
        return ()
    return tuple(item for item in periods if isinstance(item, Mapping) and _period_covers(item, block_number))


def _historical_entry_count(
    registry: Mapping[str, Any], record: SettledCycleRecord, pool_ref: str
) -> int:
    for pool_entry in _normalized_pools(registry):
        pool_key = pool_entry.get("pool_key")
        if isinstance(pool_key, Mapping) and _pool_ref_key(pool_key) == pool_ref:
            return len(_historical_entries(pool_entry, record.block_number))
    return 0


def _matching_quote(observations: Mapping[str, Any], record: SettledCycleRecord) -> Mapping[str, Any] | None:
    quotes = observations.get("quotes", observations.get("quote_observations"))
    if isinstance(quotes, list):
        candidates = tuple(item for item in quotes if isinstance(item, Mapping))
    elif isinstance(quotes, Mapping):
        candidates = tuple(quotes.values())
    else:
        return None
    for quote in candidates:
        if quote.get("tx_hash") == record.tx_hash and quote.get("chain_id") == record.chain_id:
            return quote
    return None


def _latency_gap(
    observations: Mapping[str, Any] | None, record: SettledCycleRecord
) -> CoverageGap | None:
    if observations is None:
        return CoverageGap(
            CoverageGapKind.LATENCY_UNKNOWN, None, "w2_quote_observations_not_supplied"
        )
    quote = _matching_quote(observations, record)
    if quote is None:
        return CoverageGap(
            CoverageGapKind.LATENCY_UNKNOWN, None, "w2_quote_not_observed_for_transaction"
        )
    observed_at = quote.get("observed_at_s")
    if type(observed_at) is int and not isinstance(observed_at, bool) and observed_at >= 0:
        return None
    return CoverageGap(
        CoverageGapKind.LATENCY_UNKNOWN,
        None,
        "w2_quote_observation_lacks_explicit_observed_at_s",
    )


def _quote_gap(
    observations: Mapping[str, Any] | None, record: SettledCycleRecord
) -> CoverageGap:
    if observations is None:
        return CoverageGap(CoverageGapKind.QUOTE_MISMATCH, None, "w2_quote_not_observed")
    if _matching_quote(observations, record) is None:
        return CoverageGap(
            CoverageGapKind.QUOTE_MISMATCH, None, "w2_quote_not_observed_for_transaction"
        )
    return CoverageGap(
        CoverageGapKind.QUOTE_MISMATCH,
        None,
        "w2_quote_present_without_explicit_execution_comparison",
    )


def _reason_gaps(record: SettledCycleRecord) -> tuple[CoverageGap, ...]:
    gaps: list[CoverageGap] = []
    reasons = " ".join(record.rejection_or_unknown_reasons).lower()
    if "unsupported" in reasons and ("decoder" in reasons or "log" in reasons):
        gaps.append(CoverageGap(CoverageGapKind.DECODER_UNSUPPORTED, None, "record_unknown_reason"))
    if "state" in reasons and ("unavailable" in reasons or "missing" in reasons):
        gaps.append(CoverageGap(CoverageGapKind.STATE_UNAVAILABLE, None, "record_unknown_reason"))
    if "quote" in reasons and ("mismatch" in reasons or "unavailable" in reasons or "missing" in reasons):
        gaps.append(CoverageGap(CoverageGapKind.QUOTE_MISMATCH, None, "record_unknown_reason"))
    if "eligibility" in reasons:
        gaps.append(CoverageGap(CoverageGapKind.ELIGIBILITY_UNKNOWN, None, "record_unknown_reason"))
    if record.attribution_status is AttributionStatus.UNVERIFIED_MISSING_TRACE:
        gaps.append(
            CoverageGap(CoverageGapKind.SIMULATION_UNSUPPORTED, None, "missing_internal_call_trace")
        )
    return tuple(gaps)


def classify_coverage(
    record: SettledCycleRecord,
    registry: Mapping[str, Any] | None = None,
    observations: Mapping[str, Any] | None = None,
) -> tuple[CoverageGap, ...]:
    """Classify only gaps supported by the explicitly supplied registry and observations.

    Historical qualification is never inherited from the current registry entry, and
    latency is never inferred from block timestamps.
    """
    if not isinstance(record, SettledCycleRecord):
        raise TypeError("record must be a SettledCycleRecord")
    if registry is not None and not isinstance(registry, Mapping):
        raise TypeError("registry must be a mapping or None")
    if observations is not None and not isinstance(observations, Mapping):
        raise TypeError("observations must be a mapping or None")
    if registry is not None and registry.get("schema") != REGISTRY_SCHEMA:
        raise ValueError(f"registry schema must be {REGISTRY_SCHEMA}")
    if observations is not None and observations.get("schema") != QUOTE_SCHEMA:
        raise ValueError(f"observations schema must be {QUOTE_SCHEMA}")

    pools = _normalized_pools(registry)
    swap_pools = tuple(
        action.pool_key
        for action in record.actions
        if action.action_kind is ActionKind.SWAP and action.pool_key is not None
    )
    gaps: list[CoverageGap] = []
    for pool_key in swap_pools:
        ref = _pool_ref(pool_key)
        matches = [
            pool
            for pool in pools
            if isinstance(pool.get("pool_key"), Mapping)
            and _pool_ref_key(pool["pool_key"]) == ref
        ]
        if not matches:
            gaps.append(
                CoverageGap(
                    CoverageGapKind.HISTORICAL_REGISTRY_MISSING,
                    ref,
                    f"no_registry_entry_at_block_{record.block_number}",
                )
            )
            continue
        historical = tuple(
            item
            for match in matches
            for item in _historical_entries(match, record.block_number)
        )
        if not historical:
            gaps.append(
                CoverageGap(
                    CoverageGapKind.HISTORICAL_REGISTRY_MISSING,
                    ref,
                    f"no_historical_period_at_block_{record.block_number}",
                )
            )
        current = matches[0].get("current")
        if not isinstance(current, Mapping) or "eligibility" not in current:
            gaps.append(
                CoverageGap(
                    CoverageGapKind.ELIGIBILITY_UNKNOWN,
                    ref,
                    "current_registry_eligibility_not_stated",
                )
            )
    if not swap_pools:
        gaps.append(
            CoverageGap(
                CoverageGapKind.IDENTITY_MISSING,
                None,
                "successful_swap_pool_identity_unresolved",
            )
        )
    latency_gap = _latency_gap(observations, record)
    if latency_gap is not None:
        gaps.append(latency_gap)
    gaps.append(_quote_gap(observations, record))
    gaps.extend(_reason_gaps(record))

    unique: dict[tuple[CoverageGapKind, str | None, str], CoverageGap] = {}
    for gap in gaps:
        unique.setdefault((gap.gap_kind, gap.pool_key_ref, gap.evidence_note), gap)
    return tuple(unique.values())


def _pool_ref_key(value: Mapping[str, Any]) -> str | None:
    try:
        chain_id = value.get("chain_id")
        protocol_id = value.get("protocol_id")
        venue_address = value.get("venue_address")
        pool_id = value.get("pool_id")
        if (
            type(chain_id) is not int
            or not isinstance(protocol_id, str)
            or not isinstance(venue_address, str)
            or not isinstance(pool_id, str)
        ):
            return None
        return ":".join(
            (str(chain_id), protocol_id, venue_address.lower(), pool_id.lower())
        )
    except (AttributeError, TypeError):
        return None


def gap_counts(gaps: Iterable[CoverageGap]) -> dict[str, int]:
    """Return a sorted gap-count mapping suitable for reports and manifests."""
    result: dict[str, int] = {}
    for gap in gaps:
        result[gap.gap_kind.value] = result.get(gap.gap_kind.value, 0) + 1
    return dict(sorted(result.items()))
