"""Execution plan assembly module for dex-sniper-engine research.

Constructs immutable ExecutionPlan objects from CandidateRoutes and QuoteResults
with zero credentials, zero network calls, zero file access, explicit gas_atoms,
and strict risk guardrails (floor = max(slippage_floor, amount_in + gas_atoms + 1)).
"""

from __future__ import annotations

import math
import time
import uuid
from decimal import Decimal, InvalidOperation

from atomic_execution.policy import ExcessiveAmountError
from research.market_data.types import (
    CandidateRoute,
    ExecutionPlan,
    QuoteResult,
    QuoteStatus,
    TokenAmount,
)

CANONICAL_UNIVERSAL_ROUTER: str = "0x8876789976dEcBfCbBbe364623C63652db8C0904"
HARD_CAP_MAX_USD: float = 500.0


def build_execution_plan(
    route: CandidateRoute,
    quote: QuoteResult,
    amount_usd: float,
    slippage_pct: float | Decimal = 0.5,
    deadline_seconds: int = 120,
    target_router: str | None = None,
    *,
    plan_id: str | None = None,
    current_time: float | None = None,
    gas_atoms: int | None = None,
    estimated_gas_usd: Decimal | float | int | str | None = None,
) -> ExecutionPlan:
    """Build an immutable ExecutionPlan from CandidateRoute and QuoteResult.

    Args:
        route: Candidate arbitrage route with verified cycle topology.
        quote: Quoter result containing amount_in, amount_out, and status.
        amount_usd: USD notional trade size. Hard capped at 500.0 USD.
        slippage_pct: Slippage percentage (e.g. 0.5 for 0.5%).
        deadline_seconds: Transaction expiry in seconds from now.
        target_router: Target router contract address. Defaults to canonical router.
        plan_id: Optional plan identifier. Defaults to auto-generated ID.
        current_time: Optional base unix timestamp for deadline calculation.
        gas_atoms: Base token atoms for gas fee. Explicitly required, no default fake gas.
        estimated_gas_usd: Explicit estimated gas cost in USD. Required (fail-closed, no fake default).

    Returns:
        Immutable ExecutionPlan ready for validation and dispatch.

    Raises:
        ExcessiveAmountError: If amount_usd exceeds 500.0 USD hard limit.
        ValueError: If inputs are invalid, gas_atoms is missing/invalid, quote is unquoted,
                    topology mismatches, slippage violates bounds, or output floor cannot be satisfied.
    """
    # 1. Trade amount hard limit validation
    if isinstance(amount_usd, bool) or not math.isfinite(amount_usd) or amount_usd <= 0:
        raise ValueError(f"Trade amount must be a positive finite number, got {amount_usd}")

    if amount_usd > HARD_CAP_MAX_USD:
        raise ExcessiveAmountError(
            f"Trade amount ${amount_usd:.2f} USD exceeds safety limit of ${HARD_CAP_MAX_USD:.2f} USD."
        )

    # 2. Quote status & result pre-checks
    if quote.status != QuoteStatus.QUOTED:
        raise ValueError(
            f"Cannot build execution plan for unquoted result (status={quote.status.value})"
        )

    if quote.amount_out is None:
        raise ValueError("Quote result does not contain amount_out")

    # 3. Route & topology consistency checks
    if not route.hops:
        raise ValueError("Candidate route hops cannot be empty")

    base_addr = route.base_token.address.lower()

    if quote.amount_in.token.address.lower() != base_addr:
        raise ValueError(
            f"Quote amount_in token ({quote.amount_in.token.symbol} {quote.amount_in.token.address}) "
            f"does not match route base_token ({route.base_token.symbol} {route.base_token.address})"
        )

    if quote.amount_out.token.address.lower() != base_addr:
        raise ValueError(
            f"Quote amount_out token ({quote.amount_out.token.symbol} {quote.amount_out.token.address}) "
            f"does not match route base_token ({route.base_token.symbol} {route.base_token.address})"
        )

    # 4. Strict slippage red line validation
    if isinstance(slippage_pct, bool) or not isinstance(slippage_pct, (int, float, str, Decimal)):
        raise ValueError(
            f"Slippage percentage must be a non-negative finite number, got {slippage_pct}"
        )
    try:
        slip_dec = Decimal(str(slippage_pct))
    except (InvalidOperation, TypeError, ValueError) as e:
        raise ValueError(
            f"Slippage percentage must be a non-negative finite number, got {slippage_pct}"
        ) from e

    if not slip_dec.is_finite() or slip_dec < Decimal("0"):
        raise ValueError(
            f"Slippage percentage must be a non-negative finite number, got {slippage_pct}"
        )
    if slip_dec == Decimal("0"):
        raise ValueError("Slippage safety violation: slippage_pct cannot be 0")

    # 5. Deadline calculation
    if deadline_seconds <= 0:
        raise ValueError(f"deadline_seconds must be positive, got {deadline_seconds}")

    now = current_time if current_time is not None else time.time()
    deadline = int(now + deadline_seconds)

    # 6. Gas validation: explicit gas_atoms is strictly required (fail-closed, no fabricated default)
    if gas_atoms is None:
        raise ValueError(
            "Missing explicit gas input: gas_atoms is strictly required to satisfy AGENTS.md output_floor"
        )
    if isinstance(gas_atoms, bool) or not isinstance(gas_atoms, int) or gas_atoms < 0:
        raise ValueError(
            f"gas_atoms must be a non-negative integer (excluding bool), got {gas_atoms}"
        )

    # 7. Floor calculations (exact integer ratio arithmetic to prevent floating-point and Decimal-28 rounding on uint256)
    num, den = slip_dec.as_integer_ratio()
    min_slippage_out_atoms = quote.amount_out.atoms * (100 * den - num) // (100 * den)
    if min_slippage_out_atoms <= 0:
        raise ValueError(
            f"Slippage safety violation: min_amount_out_atoms must be > 0, got {min_slippage_out_atoms} "
            f"(amount_out={quote.amount_out.atoms}, slippage_pct={slippage_pct}%)"
        )

    # Required cost floor per AGENTS.md: output_floor >= amount_in + ceil(gas_atoms) + 1 atom
    required_cost_floor = quote.amount_in.atoms + gas_atoms + 1

    # Effective floor takes the stricter (larger) requirement
    output_floor = max(min_slippage_out_atoms, required_cost_floor)

    # Rejection invariant: fail-closed if required floor exceeds expected quote out
    if output_floor > quote.amount_out.atoms:
        raise ValueError(
            f"Quote cannot satisfy output floor: floor ({output_floor}) > expected_out ({quote.amount_out.atoms})"
        )

    min_amount_out = TokenAmount(
        token=route.base_token,
        atoms=output_floor,
    )

    # 8. Router address resolution
    router_address = target_router if target_router is not None else CANONICAL_UNIVERSAL_ROUTER

    # 9. Quoter block resolution
    quoter_block = quote.block_number if quote.block_number is not None else route.snapshot_block

    # 10. Estimated gas usd validation (strictly required, fail-closed, no fabricated default)
    if estimated_gas_usd is None:
        raise ValueError("Missing explicit gas input: estimated_gas_usd is strictly required")
    if isinstance(estimated_gas_usd, bool):
        raise ValueError(f"estimated_gas_usd cannot be bool, got {estimated_gas_usd}")
    if not isinstance(estimated_gas_usd, (int, float, str, Decimal)):
        raise ValueError(
            f"estimated_gas_usd must be a number, string, or Decimal, got {type(estimated_gas_usd).__name__}"
        )
    try:
        gas_usd = Decimal(str(estimated_gas_usd))
    except (InvalidOperation, TypeError, ValueError) as e:
        raise ValueError(f"Invalid estimated_gas_usd: {e}") from e

    if not gas_usd.is_finite() or gas_usd < Decimal("0"):
        raise ValueError(
            f"estimated_gas_usd must be a non-negative finite Decimal, got {estimated_gas_usd}"
        )

    # 11. Plan ID resolution
    pid = plan_id if plan_id is not None else f"plan_{uuid.uuid4().hex[:12]}"

    return ExecutionPlan(
        plan_id=pid,
        candidate_id=route.candidate_id,
        route_type=route.route_type,
        base_token=route.base_token,
        amount_in=quote.amount_in,
        min_amount_out=min_amount_out,
        hops=route.hops,
        quoter_block=quoter_block,
        deadline=deadline,
        estimated_gas_usd=gas_usd,
        target_router=router_address,
    )
