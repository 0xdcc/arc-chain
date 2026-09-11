"""Fixed-epoch exact-input hop-by-hop amount evaluation for state graph."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping
from typing import Any

from arbitrage_contracts.identity import Amount
from arbitrage_contracts.quote import (
    ActorScope,
    DataMode,
    EvidenceLevel,
    HopQuote,
    QuoteEvidence,
    QuoteStatus,
    RouteRef,
    TriState,
)
from arbitrage_contracts.state import canonical_state_ref
from state_graph.clmm_math import (
    compute_swap_step,
    get_sqrt_ratio_at_tick,
)
from state_graph.store import StateStoreError, validate_snapshots
from state_graph.types import FrozenEpoch, PoolStateSnapshot


def resolve_token_decimals(asset: Any, registry: Mapping[Any, int] | None) -> int:
    """Resolve explicit units, rejecting unknown, invalid or conflicting values."""
    if registry is None:
        raise ValueError("Missing token decimals registry")
    keys = [asset]
    if asset.token_key is not None:
        keys.extend(
            [asset.token_key, (asset.chain_id, asset.token_key.address), asset.token_key.address]
        )
    values = [registry[key] for key in keys if key in registry]
    if not values:
        raise ValueError("Missing token decimals for route asset")
    if any(type(value) is not int or not 0 <= value <= 255 for value in values):
        raise ValueError("Invalid token decimals")
    if len(set(values)) != 1:
        raise ValueError("Conflicting token decimals")
    return values[0]


def single_segment_target(snapshot: PoolStateSnapshot, direction: str) -> int:
    """Return a safe segment limit without skipping a zero-distance crossing."""
    from state_graph.clmm_math import MAX_SQRT_RATIO, MAX_TICK, MIN_SQRT_RATIO, MIN_TICK

    fields = (
        snapshot.tick,
        snapshot.sqrt_price_x96,
        snapshot.tick_spacing,
        snapshot.liquidity,
        snapshot.fee_pips,
    )
    if any(type(value) is not int for value in fields):
        raise ValueError("CLMM state must use integers")
    tick, price, spacing, liquidity, fee = fields
    if not MIN_TICK <= tick < MAX_TICK or not MIN_SQRT_RATIO <= price < MAX_SQRT_RATIO:
        raise ValueError("CLMM state out of range")
    if not 0 < spacing < (1 << 23) or not 0 < liquidity < (1 << 128) or not 0 <= fee < 1_000_000:
        raise ValueError("Invalid CLMM spacing, liquidity or fee")
    if not get_sqrt_ratio_at_tick(tick) <= price <= get_sqrt_ratio_at_tick(tick + 1):
        raise ValueError("Inconsistent tick and sqrt price")
    lower = (tick // spacing) * spacing
    upper = lower + spacing
    if direction == "zero_for_one":
        target = max(MIN_SQRT_RATIO + 1, get_sqrt_ratio_at_tick(max(MIN_TICK, lower)))
        if target >= price:
            raise ValueError("Unknown liquidity at starting tick boundary")
    elif direction == "one_for_zero":
        target = min(MAX_SQRT_RATIO - 1, get_sqrt_ratio_at_tick(min(MAX_TICK, upper)))
        if target <= price:
            raise ValueError("Unknown liquidity at starting tick boundary")
    else:
        raise ValueError("Invalid hop direction")
    return target


def evaluate_route_exact_input(
    route: RouteRef,
    amount_in: Amount,
    epoch: FrozenEpoch,
    quote_id: str | None = None,
    data_mode: str = DataMode.SYNTHETIC,
    actor_scope: str = ActorScope.OWN_AUTHORIZED,
    token_decimals: Mapping[Any, int] | None = None,
) -> QuoteEvidence:
    """Evaluates a closed multi-hop route against a fixed immutable FrozenEpoch using CLMM integer math.

    Enforces:
    - Single-epoch state consistency: all hops are evaluated strictly against the provided epoch.
    - Exact-input propagation: the discrete integer output of hop i becomes the input of hop i+1.
    - Zero floating-point drift: all calculations use exact Q64.96 integer arithmetic.
    - Fail-closed error handling: if any pool snapshot is missing or output is 0, returns amount_out=None.
    - Honest negative delta: unprofitable routes truthfully record negative delta_atoms without pruning.
    """
    if amount_in.asset_ref != route.base_asset:
        raise ValueError(
            f"amount_in asset ({amount_in.asset_ref}) does not match route base asset ({route.base_asset})"
        )
    if amount_in.atoms <= 0:
        raise ValueError(f"amount_in.atoms must be positive, got {amount_in.atoms}")

    started_at_ms = int(time.time() * 1000)
    current_amount = amount_in
    hop_quotes: list[HopQuote] = []
    status = QuoteStatus.QUOTED
    error_msg: str | None = None
    state_ref: str | None = None
    try:
        state_ref = canonical_state_ref(epoch.state_version)
        if route.chain_id != epoch.state_version.chain_id or not epoch.state_version.is_ready():
            raise ValueError("Route requires a ready state on the same chain")
        if epoch.state_version.applied_cursor is not None:
            raise ValueError("Pool snapshots do not prove partial-cursor state")
        validate_snapshots(
            epoch.state_version, epoch.snapshots, [hop.pool_key.pool_id for hop in route.hops]
        )
        if resolve_token_decimals(route.base_asset, token_decimals) != amount_in.decimals:
            raise ValueError("Input decimals mismatch")
    except (ValueError, TypeError, StateStoreError) as exc:
        status = QuoteStatus.UNSUPPORTED
        error_msg = str(exc)

    for hop_index, hop in enumerate(route.hops):
        if status != QuoteStatus.QUOTED:
            break
        snapshot = epoch.get_snapshot(hop.pool_key.pool_id)
        if snapshot is None:
            status = QuoteStatus.UNSUPPORTED
            error_msg = f"Missing pool snapshot for pool_id {hop.pool_key.pool_id}"
            break

        if snapshot.liquidity <= 0:
            status = QuoteStatus.UNSUPPORTED
            error_msg = f"Zero or negative liquidity in pool {hop.pool_key.pool_id}"
            break

        descriptor = hop.pool_descriptor
        if (
            descriptor is None
            or descriptor.tick_spacing != snapshot.tick_spacing
            or descriptor.fee_model.kind != "static"
            or descriptor.fee_model.raw_value != snapshot.fee_pips
            or (descriptor.hooks and int(descriptor.hooks, 16) != 0)
        ):
            status = QuoteStatus.UNSUPPORTED
            error_msg = "Snapshot fee/spacing lacks matching descriptor evidence"
            break

        try:
            target_sqrt = single_segment_target(snapshot, hop.direction)
        except ValueError as exc:
            status = QuoteStatus.UNSUPPORTED
            error_msg = f"Swap crossed single-tick boundary or invalid state: {exc}"
            break

        step = compute_swap_step(
            sqrt_ratio_current_x96=snapshot.sqrt_price_x96,
            sqrt_ratio_target_x96=target_sqrt,
            liquidity=snapshot.liquidity,
            amount_remaining=current_amount.atoms,
            fee_pips=snapshot.fee_pips,
        )

        input_consumed = step.amount_in + step.fee_amount
        if input_consumed != current_amount.atoms:
            status = QuoteStatus.UNSUPPORTED
            error_msg = (
                f"Swap crossed single-tick boundary at hop {hop_index} "
                f"(consumed {input_consumed}/{current_amount.atoms} atoms); "
                f"multi-tick bitmap not available"
            )
            break

        if step.amount_out <= 0:
            status = QuoteStatus.UNSUPPORTED
            error_msg = f"Zero output at hop {hop_index} (input depleted or insufficient depth)"
            break

        try:
            out_decimals = resolve_token_decimals(hop.asset_out, token_decimals)
        except ValueError as exc:
            status = QuoteStatus.UNSUPPORTED
            error_msg = str(exc)
            break
        out_amount = Amount(
            asset_ref=hop.asset_out,
            atoms=step.amount_out,
            decimals=out_decimals,
        )

        hq = HopQuote(
            hop_index=hop_index,
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
        final_amount_out: Amount | None = current_amount
        delta_atoms: int | None = current_amount.atoms - amount_in.atoms
    else:
        final_amount_out = None
        delta_atoms = None

    if quote_id is None:
        raw_id_seed = f"{route.route_id}:{epoch.epoch_id}:{amount_in.atoms}:{started_at_ms}"
        quote_id = f"local-quote:{hashlib.sha256(raw_id_seed.encode()).hexdigest()[:16]}"

    finished_at_ms = int(time.time() * 1000)

    return QuoteEvidence(
        quote_id=quote_id,
        route_ref=route,
        amount_in=amount_in,
        amount_out=final_amount_out,
        delta_atoms=delta_atoms,
        hop_quotes=tuple(hop_quotes),
        state_version_ref=state_ref,
        started_at_ms=started_at_ms,
        finished_at_ms=finished_at_ms,
        status=status,
        evidence_level=EvidenceLevel.LOCAL_QUOTE,
        data_mode=data_mode,
        actor_scope=actor_scope,
        fee_included=TriState.YES if status == QuoteStatus.QUOTED else TriState.UNKNOWN,
        impact_included=TriState.YES if status == QuoteStatus.QUOTED else TriState.UNKNOWN,
        error=error_msg,
    )
