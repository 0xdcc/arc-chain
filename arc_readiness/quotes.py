"""Single-hop bidirectional quote adapters and capability promotion guards for Arc."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from arbitrage_contracts.identity import Amount, AssetRef, PoolKey
from arc_readiness.errors import ArcValidationError


@dataclass(frozen=True, slots=True)
class SingleHopQuoteRequest:
    """Request envelope for single-hop pricing."""

    quote_id: str
    pool_key: PoolKey
    direction: str  # "zero_for_one" or "one_for_zero"
    asset_in: AssetRef
    asset_out: AssetRef
    amount_in: Amount
    block_number: int
    block_hash: str

    def __post_init__(self) -> None:
        if self.direction not in ("zero_for_one", "one_for_zero"):
            raise ArcValidationError(f"Invalid direction: {self.direction!r}")
        if self.amount_in.atoms <= 0:
            raise ArcValidationError("amount_in must be positive")


@dataclass(frozen=True, slots=True)
class SingleHopQuoteResult:
    """Result of a single-hop quote with zero-output truncation and no profit semantics."""

    quote_id: str
    request: SingleHopQuoteRequest
    amount_out: Amount | None
    gas_estimate_atoms: int | None
    status: str  # "quoted", "reverted", "exhausted"
    can_quote: str  # "supported" or "unsupported"
    can_simulate: str = "unknown"
    can_atomic_execute: str = "unsupported"
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status == "quoted" and self.amount_out is None:
            raise ArcValidationError("Quoted status requires non-None amount_out")
        if self.status != "quoted" and self.amount_out is not None:
            raise ArcValidationError("Non-quoted status must have amount_out=None")


def execute_bidirectional_single_hop_quotes(
    pool_key: PoolKey,
    asset0: AssetRef,
    asset1: AssetRef,
    amount_in0: Amount,
    amount_in1: Amount,
    block_number: int,
    block_hash: str,
    quoter_fn: Callable[[str, Amount], tuple[int, int | None]],
) -> tuple[SingleHopQuoteResult, SingleHopQuoteResult]:
    """Execute independent bidirectional quotes on a pool.

    Guards:
    1. zero_for_one and one_for_zero are called INDEPENDENTLY through the quoter.
       Deriving the reverse quote by inverting the forward quote is strictly forbidden.
    2. Any 0 or negative output is truncated to None with status='exhausted'.
    3. Capability promotion is bounded: can_quote='supported' (if quoted),
       can_simulate is ALWAYS 'unknown', and can_atomic_execute is ALWAYS 'unsupported'.
    """
    req_0_to_1 = SingleHopQuoteRequest(
        quote_id=f"quote_{pool_key.venue_address}_0_to_1_{block_number}",
        pool_key=pool_key,
        direction="zero_for_one",
        asset_in=asset0,
        asset_out=asset1,
        amount_in=amount_in0,
        block_number=block_number,
        block_hash=block_hash,
    )
    req_1_to_0 = SingleHopQuoteRequest(
        quote_id=f"quote_{pool_key.venue_address}_1_to_0_{block_number}",
        pool_key=pool_key,
        direction="one_for_zero",
        asset_in=asset1,
        asset_out=asset0,
        amount_in=amount_in1,
        block_number=block_number,
        block_hash=block_hash,
    )

    # 1. Execute direction 0 -> 1 independently
    try:
        out0, gas0 = quoter_fn("zero_for_one", amount_in0)
        if out0 > 0:
            res_0_to_1 = SingleHopQuoteResult(
                quote_id=req_0_to_1.quote_id,
                request=req_0_to_1,
                amount_out=Amount(asset_ref=asset1, atoms=out0, decimals=amount_in1.decimals),
                gas_estimate_atoms=gas0,
                status="quoted",
                can_quote="supported",
            )
        else:
            res_0_to_1 = SingleHopQuoteResult(
                quote_id=req_0_to_1.quote_id,
                request=req_0_to_1,
                amount_out=None,
                gas_estimate_atoms=gas0,
                status="exhausted",
                can_quote="unsupported",
                reasons=("zero_liquidity_depth",),
            )
    except Exception as exc:
        res_0_to_1 = SingleHopQuoteResult(
            quote_id=req_0_to_1.quote_id,
            request=req_0_to_1,
            amount_out=None,
            gas_estimate_atoms=None,
            status="reverted",
            can_quote="unsupported",
            reasons=(f"quoter_revert:{exc}",),
        )

    # 2. Execute direction 1 -> 0 independently
    try:
        out1, gas1 = quoter_fn("one_for_zero", amount_in1)
        if out1 > 0:
            res_1_to_0 = SingleHopQuoteResult(
                quote_id=req_1_to_0.quote_id,
                request=req_1_to_0,
                amount_out=Amount(asset_ref=asset0, atoms=out1, decimals=amount_in0.decimals),
                gas_estimate_atoms=gas1,
                status="quoted",
                can_quote="supported",
            )
        else:
            res_1_to_0 = SingleHopQuoteResult(
                quote_id=req_1_to_0.quote_id,
                request=req_1_to_0,
                amount_out=None,
                gas_estimate_atoms=gas1,
                status="exhausted",
                can_quote="unsupported",
                reasons=("zero_liquidity_depth",),
            )
    except Exception as exc:
        res_1_to_0 = SingleHopQuoteResult(
            quote_id=req_1_to_0.quote_id,
            request=req_1_to_0,
            amount_out=None,
            gas_estimate_atoms=None,
            status="reverted",
            can_quote="unsupported",
            reasons=(f"quoter_revert:{exc}",),
        )

    return res_0_to_1, res_1_to_0
