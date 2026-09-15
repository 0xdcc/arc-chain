"""Pure computation cross-pool spread arbitrage strategy.

Consumes a MarketSnapshot and identifies two-hop cross-pool arbitrage opportunities
across distinct DEX pools for the same token pair.

Architecture constraints:
- Zero RPC, zero network, zero file I/O, zero executor dependencies.
- Strictly deterministic and side-effect free pure calculations.
- Accurate observed_gross_bps (gross spread != net profit).
- Never fabricate liquidity depth.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from research.market_data.catalog import get_verified_token
from research.market_data.types import (
    CandidateRoute,
    MarketSnapshot,
    PoolStateSnapshot,
    RouteHop,
    TokenIdentity,
)

_Q96 = 2**96
_Q96_DEC = Decimal(_Q96)
PREFERRED_BASES = ("USDG", "USDC", "USDT", "WETH")


def _resolve_token(
    chain_id: int,
    address: str,
    base_token: TokenIdentity | None = None,
    known_tokens: Mapping[str, TokenIdentity] | None = None,
) -> TokenIdentity:
    """Resolve TokenIdentity safely with precedence: base_token -> known_tokens -> catalog -> fallback."""
    norm_addr = address.lower()
    if base_token is not None and base_token.address.lower() == norm_addr:
        return base_token
    if known_tokens is not None and norm_addr in known_tokens:
        return known_tokens[norm_addr]
    try:
        token = get_verified_token(norm_addr)
        if token.chain_id == chain_id:
            return token
        return TokenIdentity(
            chain_id=chain_id,
            address=token.address,
            decimals=token.decimals,
            symbol=token.symbol,
        )
    except (KeyError, ValueError):
        return TokenIdentity(
            chain_id=chain_id,
            address=norm_addr,
            decimals=18,
            symbol=norm_addr[:10],
        )


def _pick_preferred_base(
    token_a: TokenIdentity,
    token_b: TokenIdentity,
) -> tuple[TokenIdentity, TokenIdentity]:
    """Select the preferred base token and quote token for a pair."""
    sym_a = token_a.symbol.upper()
    sym_b = token_b.symbol.upper()

    idx_a = PREFERRED_BASES.index(sym_a) if sym_a in PREFERRED_BASES else 999
    idx_b = PREFERRED_BASES.index(sym_b) if sym_b in PREFERRED_BASES else 999

    if idx_a < idx_b:
        return token_a, token_b
    if idx_b < idx_a:
        return token_b, token_a
    # Fallback to deterministic lexicographical order of normalized address
    if token_a.address.lower() < token_b.address.lower():
        return token_a, token_b
    return token_b, token_a


def _calculate_hop_rate(
    pool_state: PoolStateSnapshot,
    token_in: TokenIdentity,
    token_out: TokenIdentity,
) -> Decimal:
    """Calculate the marginal exchange rate (token_out units per 1 token_in unit)."""
    sqrt_price = pool_state.sqrt_price_x96
    if sqrt_price is None or sqrt_price <= 0:
        raise ValueError("Invalid sqrt_price_x96")

    t0 = pool_state.pool.token0.lower()
    t1 = pool_state.pool.token1.lower()
    in_addr = token_in.address.lower()
    out_addr = token_out.address.lower()

    sqrt_dec = Decimal(sqrt_price)
    ratio = sqrt_dec / _Q96_DEC
    raw_price = ratio * ratio  # atoms of token1 per atom of token0

    if in_addr == t0 and out_addr == t1:
        dec_adj = Decimal(10) ** (token_in.decimals - token_out.decimals)
        return raw_price * dec_adj
    if in_addr == t1 and out_addr == t0:
        if raw_price <= 0:
            raise ValueError("Zero raw price")
        dec_adj = Decimal(10) ** (token_in.decimals - token_out.decimals)
        return (Decimal(1) / raw_price) * dec_adj

    raise ValueError(f"Tokens {in_addr}->{out_addr} do not match pool {t0}/{t1}")


def find_spread_candidates(
    snapshot: MarketSnapshot,
    min_gross_bps: float = 10.0,
    base_token: TokenIdentity | None = None,
    known_tokens: Mapping[str, TokenIdentity] | None = None,
) -> list[CandidateRoute]:
    """Pure calculation detecting cross-pool two-hop spread arbitrage candidates.

    Args:
        snapshot: Snapshot of market pool states.
        min_gross_bps: Minimum observed gross spread in basis points (default 10.0 bps).
        base_token: Optional base token to anchor the route cycle.
        known_tokens: Optional token dictionary for resolving custom token identities.

    Returns:
        List of CandidateRoute instances meeting the gross spread threshold,
        sorted by observed_gross_bps descending.
    """
    if not isinstance(snapshot, MarketSnapshot):
        raise TypeError(f"snapshot must be MarketSnapshot, got {type(snapshot).__name__}")

    # 1. Filter valid pools (skip bad pools with missing or non-positive price)
    valid_pools: list[PoolStateSnapshot] = []
    for pool_state in snapshot.pools.values():
        if pool_state.sqrt_price_x96 is None or pool_state.sqrt_price_x96 <= 0:
            continue
        if not pool_state.pool or not pool_state.pool.token0 or not pool_state.pool.token1:
            continue
        valid_pools.append(pool_state)

    # 2. Group pools by normalized token pair
    pair_groups: dict[tuple[str, str], list[PoolStateSnapshot]] = {}
    for p in valid_pools:
        t0 = p.pool.token0.lower()
        t1 = p.pool.token1.lower()
        pair_key = (min(t0, t1), max(t0, t1))
        pair_groups.setdefault(pair_key, []).append(p)

    candidates: list[CandidateRoute] = []

    # 3. For each token pair with 2+ pools, check spreads
    for (t_a, t_b), pools in pair_groups.items():
        if len(pools) < 2:
            continue

        tok_a = _resolve_token(snapshot.chain_id, t_a, base_token, known_tokens)
        tok_b = _resolve_token(snapshot.chain_id, t_b, base_token, known_tokens)

        # Determine base token
        if base_token is not None:
            base_addr = base_token.address.lower()
            if base_addr == t_a:
                start_tok = tok_a
                quote_tok = tok_b
            elif base_addr == t_b:
                start_tok = tok_b
                quote_tok = tok_a
            else:
                # Specified base_token does not participate in this pair
                continue
        else:
            start_tok, quote_tok = _pick_preferred_base(tok_a, tok_b)

        # Compare each pair of pools (pool_i, pool_j)
        for i in range(len(pools)):
            for j in range(i + 1, len(pools)):
                p_i = pools[i]
                p_j = pools[j]
                if p_i.pool.pool_id.lower() == p_j.pool.pool_id.lower():
                    continue

                # Try Direction 1: p_i (start->quote) then p_j (quote->start)
                rate_i_fwd = _calculate_hop_rate(p_i, start_tok, quote_tok)
                rate_j_rev = _calculate_hop_rate(p_j, quote_tok, start_tok)
                mult_1 = rate_i_fwd * rate_j_rev
                gross_bps_1 = float((mult_1 - Decimal(1)) * Decimal(10000))

                # Try Direction 2: p_j (start->quote) then p_i (quote->start)
                rate_j_fwd = _calculate_hop_rate(p_j, start_tok, quote_tok)
                rate_i_rev = _calculate_hop_rate(p_i, quote_tok, start_tok)
                mult_2 = rate_j_fwd * rate_i_rev
                gross_bps_2 = float((mult_2 - Decimal(1)) * Decimal(10000))

                # Pick winning direction if above threshold
                if gross_bps_1 >= min_gross_bps and gross_bps_1 >= gross_bps_2:
                    first_pool, second_pool = p_i, p_j
                    best_bps = gross_bps_1
                elif gross_bps_2 >= min_gross_bps:
                    first_pool, second_pool = p_j, p_i
                    best_bps = gross_bps_2
                else:
                    continue

                hop1 = RouteHop(pool=first_pool.pool, token_in=start_tok, token_out=quote_tok)
                hop2 = RouteHop(pool=second_pool.pool, token_in=quote_tok, token_out=start_tok)

                p1_id = first_pool.pool.pool_id.lower().removeprefix("0x")[:8]
                p2_id = second_pool.pool.pool_id.lower().removeprefix("0x")[:8]
                cand_id = (
                    f"cand_spread_{snapshot.block_number}_"
                    f"{start_tok.symbol.lower()}_{p1_id}_{p2_id}"
                )

                candidate = CandidateRoute(
                    candidate_id=cand_id,
                    route_type="two_hop_spread",
                    base_token=start_tok,
                    hops=(hop1, hop2),
                    observed_gross_bps=round(best_bps, 4),
                    snapshot_block=snapshot.block_number,
                    created_at=float(snapshot.captured_at),
                )
                candidates.append(candidate)

    # Sort deterministically: highest gross profit first, tie-break by candidate_id
    candidates.sort(key=lambda c: (-c.observed_gross_bps, c.candidate_id))
    return candidates
