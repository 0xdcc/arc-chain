"""Discrete DEX quote ingestion, pool identity isolation, and error classification kernel."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from arbitrage_contracts import (
    PoolKey,
)
from arbitrage_contracts.serialization import (
    _deserialize_asset_ref,
    _deserialize_pool_descriptor,
    _deserialize_pool_key,
)

from .models import AmountQuotePoint
from .normalize import ExecutionPricePoint, calculate_execution_price_point


@dataclass(frozen=True, slots=True)
class QuoteValidationResult:
    """Detailed validation output for a discrete amount quote point."""

    is_valid: bool
    status: str
    quote_point: AmountQuotePoint
    effective_price_point: ExecutionPricePoint | None
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DiscreteQuotePair:
    """Paired bidirectional quotes for a single pool identity."""

    pool_key: PoolKey
    zero_for_one_quote: AmountQuotePoint | None
    one_for_zero_quote: AmountQuotePoint | None
    is_bidirectional_valid: bool
    state_version_ref: str | None
    reasons: tuple[str, ...]


def parse_quote_point_from_dict(raw: Mapping[str, Any]) -> AmountQuotePoint:
    """Parse dictionary into validated AmountQuotePoint domain entity."""
    if not isinstance(raw, Mapping):
        raise TypeError(f"raw quote must be a Mapping, got {type(raw).__name__}")

    pk = _deserialize_pool_key(raw["pool_key"])
    pd = None
    if raw.get("pool_descriptor") is not None:
        pd = _deserialize_pool_descriptor(raw["pool_descriptor"])
    ain = _deserialize_asset_ref(raw["asset_in"])
    aout = _deserialize_asset_ref(raw["asset_out"])

    amt_in = raw["amount_in_atoms"]
    in_atoms = int(amt_in) if isinstance(amt_in, (int, str)) else 0

    amt_out = raw.get("amount_out_atoms")
    out_atoms = int(amt_out) if isinstance(amt_out, (int, str)) and amt_out is not None else None

    return AmountQuotePoint(
        pool_key=pk,
        direction=raw["direction"],
        asset_in=ain,
        asset_out=aout,
        amount_in_atoms=in_atoms,
        quote_id=raw["quote_id"],
        pool_descriptor=pd,
        amount_out_atoms=out_atoms,
        state_version_ref=raw.get("state_version_ref"),
        fee_included=raw.get("fee_included", "unknown"),
        impact_included=raw.get("impact_included", "unknown"),
        gas_estimate=raw.get("gas_estimate"),
        gas_evidence_kind=raw.get("gas_evidence_kind"),
        status=raw.get("status", "quoted"),
        error=raw.get("error"),
        data_mode=raw.get("data_mode", "synthetic"),
        evidence_level=raw.get("evidence_level", "local_quote"),
    )


def validate_quote_point(
    quote: AmountQuotePoint,
    expected_state_version_ref: str | None = None,
    quote_currency: str = "USDG",
    *,
    token_decimals: int = 18,
    quote_decimals: int = 6,
) -> QuoteValidationResult:
    """Validate quote evidence status, state anchor, and calculate price point (C17-C19, C22-C23)."""
    if not isinstance(quote, AmountQuotePoint):
        raise TypeError("quote must be an AmountQuotePoint")

    # C19: State anchor consistency check
    if expected_state_version_ref is not None:
        if quote.state_version_ref != expected_state_version_ref:
            return QuoteValidationResult(
                is_valid=False,
                status="state_version_mismatch",
                quote_point=quote,
                effective_price_point=None,
                reasons=(
                    f"State version mismatch: quote has {quote.state_version_ref!r}, "
                    f"expected {expected_state_version_ref!r}",
                ),
            )

    # C22: Error classification - if status is not quoted, amount_out must be None
    if quote.status != "quoted":
        if quote.amount_out_atoms is not None:
            raise ValueError(
                f"Quote with status {quote.status!r} must have amount_out_atoms=None, got {quote.amount_out_atoms}"
            )
        return QuoteValidationResult(
            is_valid=False,
            status=quote.status,
            quote_point=quote,
            effective_price_point=None,
            reasons=(f"Quote execution failed with status {quote.status!r}: {quote.error}",),
        )

    # C22: Quoted status requires positive output
    if quote.amount_out_atoms is None or quote.amount_out_atoms <= 0:
        return QuoteValidationResult(
            is_valid=False,
            status="zero_output",
            quote_point=quote,
            effective_price_point=None,
            reasons=("Quoted quote point returned zero or None output atoms",),
        )

    # C23: Gas evidence kind guard (cannot claim atomic or confirmed execution)
    if quote.gas_evidence_kind in ("atomic_simulation", "confirmed_actual"):
        return QuoteValidationResult(
            is_valid=False,
            status="unauthorized_gas_evidence",
            quote_point=quote,
            effective_price_point=None,
            reasons=(f"Single-hop quote cannot claim {quote.gas_evidence_kind!r}",),
        )

    # C03/C18: Resolve decimals according to direction
    if quote.direction == "zero_for_one":
        d_in = token_decimals
        d_out = quote_decimals
    else:
        d_in = quote_decimals
        d_out = token_decimals

    price_pt = calculate_execution_price_point(
        direction=quote.direction,
        amount_in_atoms=quote.amount_in_atoms,
        amount_out_atoms=quote.amount_out_atoms,
        decimals_in=d_in,
        decimals_out=d_out,
        quote_currency=quote_currency,
        is_usd_pegged=False,
    )

    return QuoteValidationResult(
        is_valid=True,
        status="quoted",
        quote_point=quote,
        effective_price_point=price_pt,
        reasons=(),
    )


def assemble_bidirectional_quotes(
    quotes: Sequence[AmountQuotePoint],
    expected_state_version_ref: str | None = None,
    quote_currency: str = "USDG",
) -> dict[PoolKey, DiscreteQuotePair]:
    """Group quotes strictly by PoolKey and evaluate bidirectional completeness (C16, C17)."""
    # C16: Key by PoolKey (6-tuple), NEVER by raw pool_id string
    grouped: dict[PoolKey, list[AmountQuotePoint]] = {}
    for q in quotes:
        grouped.setdefault(q.pool_key, []).append(q)

    pairs: dict[PoolKey, DiscreteQuotePair] = {}
    for pk, pool_quotes in grouped.items():
        z41: AmountQuotePoint | None = None
        o4z: AmountQuotePoint | None = None
        reasons: list[str] = []

        for q in pool_quotes:
            val_res = validate_quote_point(q, expected_state_version_ref, quote_currency)
            if q.direction == "zero_for_one":
                if z41 is None or val_res.is_valid:
                    z41 = q
            elif q.direction == "one_for_zero":
                if o4z is None or val_res.is_valid:
                    o4z = q
            else:
                reasons.append(f"Unknown direction {q.direction!r}")

        # C17: Bidirectional check - both directions must exist and be valid
        z41_valid = z41 is not None and z41.status == "quoted" and (z41.amount_out_atoms or 0) > 0
        o4z_valid = o4z is not None and o4z.status == "quoted" and (o4z.amount_out_atoms or 0) > 0

        is_bidirectional = z41_valid and o4z_valid
        if not z41_valid:
            reasons.append("Missing or invalid zero_for_one quote (cannot derive via reciprocal)")
        if not o4z_valid:
            reasons.append("Missing or invalid one_for_zero quote (cannot derive via reciprocal)")

        ref = z41.state_version_ref if z41 else (o4z.state_version_ref if o4z else None)

        pairs[pk] = DiscreteQuotePair(
            pool_key=pk,
            zero_for_one_quote=z41,
            one_for_zero_quote=o4z,
            is_bidirectional_valid=is_bidirectional,
            state_version_ref=ref,
            reasons=tuple(reasons),
        )

    return pairs
