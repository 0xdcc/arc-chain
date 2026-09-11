"""Arc Quote Capabilities and Routing Dispatcher (T21)

Manages quote engine capability negotiation:
- CAPABILITY_SINGLE_SEGMENT: safe fail-closed single segment quoting (F01)
- CAPABILITY_MULTI_TICK: discrete multi-tick Q64.96 quoting with verified TickCoverage
- Graceful fall-back: routes automatically fall back to single_segment when coverage is missing
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from arbitrage_contracts.arc_extensions import TickCoverage
from arbitrage_contracts.identity import Amount, AssetRef
from arbitrage_contracts.quote import (
    HopQuote,
    QuoteEvidence,
    QuoteStatus,
    RouteRef,
    TriState,
)
from arc_opportunities.quote_bridge import ArcQuoteBridge
from state_graph.multi_tick import PoolTickTable, execute_multi_tick_hop
from state_graph.types import FrozenEpoch

CAPABILITY_SINGLE_SEGMENT = "single_segment"
CAPABILITY_MULTI_TICK = "v3_multi_tick"
SUPPORTED_CAPABILITIES = frozenset({CAPABILITY_SINGLE_SEGMENT, CAPABILITY_MULTI_TICK})


@dataclass(frozen=True, slots=True)
class CapabilityProfile:
    """Declared quoting engine capability and coverage lookup."""

    enabled_capability: str = CAPABILITY_SINGLE_SEGMENT
    pool_tick_tables: dict[str, PoolTickTable] | None = None

    def __post_init__(self) -> None:
        if self.enabled_capability not in SUPPORTED_CAPABILITIES:
            raise ValueError(f"Unknown capability: {self.enabled_capability}")


class ArcCapabilityRouter:
    """Routes quotes according to declared capability and available tick coverage."""

    def __init__(self, bridge: ArcQuoteBridge, profile: CapabilityProfile | None = None) -> None:
        self.bridge = bridge
        self.profile = profile if profile is not None else CapabilityProfile()

    def quote_route(
        self,
        route: RouteRef,
        amount_in: Amount,
        epoch: FrozenEpoch,
        token_decimals: Mapping[Any, int] | None = None,
    ) -> QuoteEvidence:
        """Route quote evaluation based on active capability."""
        if self.profile.enabled_capability == CAPABILITY_SINGLE_SEGMENT:
            # Strictly single-segment
            return self.bridge.quote_exact_input(
                route=route,
                amount_in=amount_in,
                epoch=epoch,
                token_decimals=token_decimals,
            )

        # Multi-tick capability: check if all pools have complete coverage
        tables = self.profile.pool_tick_tables or {}
        has_all_coverage = True
        for hop in route.hops:
            pid = hop.pool_key.pool_id
            if pid not in tables or not tables[pid].coverage.is_complete:
                has_all_coverage = False
                break

        if not has_all_coverage:
            # Fall back to single-segment
            return self.bridge.quote_exact_input(
                route=route,
                amount_in=amount_in,
                epoch=epoch,
                token_decimals=token_decimals,
            )

        # Execute multi-tick hops
        return self._quote_multi_tick_route(route, amount_in, epoch, tables, token_decimals)

    def _quote_multi_tick_route(
        self,
        route: RouteRef,
        amount_in: Amount,
        epoch: FrozenEpoch,
        tables: dict[str, PoolTickTable],
        token_decimals: Mapping[Any, int] | None,
    ) -> QuoteEvidence:
        current_amount = amount_in
        hop_quotes: list[HopQuote] = []
        status = QuoteStatus.QUOTED
        error_msg: str | None = None

        for idx, hop in enumerate(route.hops):
            snap = epoch.get_snapshot(hop.pool_key.pool_id)
            if snap is None:
                status = QuoteStatus.UNSUPPORTED
                error_msg = f"Missing pool snapshot for pool {hop.pool_key.pool_id}"
                break

            table = tables.get(hop.pool_key.pool_id)
            z41 = (hop.direction == "zero_for_one")
            res = execute_multi_tick_hop(
                snapshot=snap,
                amount_in=current_amount.atoms,
                zero_for_one=z41,
                tick_table=table,
            )

            if res.status != QuoteStatus.QUOTED:
                status = QuoteStatus.UNSUPPORTED
                error_msg = res.error or f"Multi-tick failed at hop {idx}"
                break

            out_decimals = 18
            if token_decimals and hop.asset_out in token_decimals:
                out_decimals = token_decimals[hop.asset_out]

            out_amount = Amount(
                asset_ref=hop.asset_out,
                atoms=res.amount_out_produced,
                decimals=out_decimals,
            )

            hq = HopQuote(
                hop_index=idx,
                pool_key=hop.pool_key,
                asset_in=hop.asset_in,
                asset_out=hop.asset_out,
                amount_in=current_amount,
                amount_out=out_amount,
                fee_model=hop.pool_descriptor.fee_model if hop.pool_descriptor else None,
                status=QuoteStatus.QUOTED,
            )
            hop_quotes.append(hq)
            current_amount = out_amount

        if status == QuoteStatus.QUOTED:
            final_out = current_amount
            delta = current_amount.atoms - amount_in.atoms
        else:
            final_out = None
            delta = None

        return QuoteEvidence(
            quote_id=f"multi-tick:{route.route_id[:16]}",
            route_ref=route,
            amount_in=amount_in,
            amount_out=final_out,
            delta_atoms=delta,
            hop_quotes=tuple(hop_quotes),
            state_version_ref=epoch.state_version.block_hash,
            started_at_ms=0,
            finished_at_ms=1,
            status=status,
            fee_included=TriState.YES if status == QuoteStatus.QUOTED else TriState.UNKNOWN,
            impact_included=TriState.YES if status == QuoteStatus.QUOTED else TriState.UNKNOWN,
            error=error_msg,
        )
