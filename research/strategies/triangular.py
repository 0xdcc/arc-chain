"""Pure computation triangular arbitrage strategy.

Consumes a MarketSnapshot and identifies three-hop closed-cycle arbitrage opportunities
across three distinct pools and three distinct tokens.

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


def _pick_preferred_base(tokens: list[TokenIdentity]) -> TokenIdentity:
    """Pick the highest priority base token among candidate cycle nodes."""
    for sym in PREFERRED_BASES:
        for t in tokens:
            if t.symbol.upper() == sym:
                return t
    return min(tokens, key=lambda t: t.address.lower())


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
    raw_price = ratio * ratio

    if in_addr == t0 and out_addr == t1:
        dec_adj = Decimal(10) ** (token_in.decimals - token_out.decimals)
        return raw_price * dec_adj
    if in_addr == t1 and out_addr == t0:
        if raw_price <= 0:
            raise ValueError("Zero raw price")
        dec_adj = Decimal(10) ** (token_in.decimals - token_out.decimals)
        return (Decimal(1) / raw_price) * dec_adj

    raise ValueError(f"Tokens {in_addr}->{out_addr} do not match pool {t0}/{t1}")


def find_triangular_candidates(
    snapshot: MarketSnapshot,
    min_gross_bps: float = 10.0,
    base_token: TokenIdentity | None = None,
    known_tokens: Mapping[str, TokenIdentity] | None = None,
) -> list[CandidateRoute]:
    """Pure calculation detecting three-hop triangular arbitrage candidates.

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

    # 2. Build directed edges and resolve tokens
    token_map: dict[str, TokenIdentity] = {}
    best_edges: dict[tuple[str, str], tuple[PoolStateSnapshot, Decimal]] = {}
    adj: dict[str, set[str]] = {}

    for p in valid_pools:
        t0 = p.pool.token0.lower()
        t1 = p.pool.token1.lower()

        if t0 not in token_map:
            token_map[t0] = _resolve_token(snapshot.chain_id, t0, base_token, known_tokens)
        if t1 not in token_map:
            token_map[t1] = _resolve_token(snapshot.chain_id, t1, base_token, known_tokens)

        tok0 = token_map[t0]
        tok1 = token_map[t1]

        # Edge t0 -> t1
        rate_01 = _calculate_hop_rate(p, tok0, tok1)
        if (t0, t1) not in best_edges or rate_01 > best_edges[(t0, t1)][1]:
            best_edges[(t0, t1)] = (p, rate_01)
        adj.setdefault(t0, set()).add(t1)

        # Edge t1 -> t0
        rate_10 = _calculate_hop_rate(p, tok1, tok0)
        if (t1, t0) not in best_edges or rate_10 > best_edges[(t1, t0)][1]:
            best_edges[(t1, t0)] = (p, rate_10)
        adj.setdefault(t1, set()).add(t0)

    candidates: list[CandidateRoute] = []
    seen_canonical_cycles: set[tuple[str, str, str]] = set()

    # 3. Find 3-hop cycles: u -> v -> w -> u
    nodes = sorted(adj.keys())
    for u in nodes:
        for v in sorted(adj.get(u, ())):
            if v == u:
                continue
            for w in sorted(adj.get(v, ())):
                if w == u or w == v:
                    continue
                if u not in adj.get(w, ()):
                    continue

                p_uv, r_uv = best_edges[(u, v)]
                p_vw, r_vw = best_edges[(v, w)]
                p_wu, r_wu = best_edges[(w, u)]

                # Check all 3 pools are distinct
                pool_ids = {
                    p_uv.pool.pool_id.lower(),
                    p_vw.pool.pool_id.lower(),
                    p_wu.pool.pool_id.lower(),
                }
                if len(pool_ids) < 3:
                    continue

                gross_mult = r_uv * r_vw * r_wu
                observed_gross_bps = float((gross_mult - Decimal(1)) * Decimal(10000))

                if observed_gross_bps < min_gross_bps:
                    continue

                # Handle base_token and canonical deduplication
                if base_token is not None:
                    target_addr = base_token.address.lower()
                    if target_addr not in (u, v, w):
                        continue
                    if target_addr == u:
                        cycle_nodes = (u, v, w)
                        hop_edges = ((u, v, p_uv), (v, w, p_vw), (w, u, p_wu))
                    elif target_addr == v:
                        cycle_nodes = (v, w, u)
                        hop_edges = ((v, w, p_vw), (w, u, p_wu), (u, v, p_uv))
                    else:
                        cycle_nodes = (w, u, v)
                        hop_edges = ((w, u, p_wu), (u, v, p_uv), (v, w, p_vw))

                    if cycle_nodes in seen_canonical_cycles:
                        continue
                    seen_canonical_cycles.add(cycle_nodes)
                else:
                    canon_key = min([(u, v, w), (v, w, u), (w, u, v)])
                    if canon_key in seen_canonical_cycles:
                        continue
                    seen_canonical_cycles.add(canon_key)

                    chosen_base = _pick_preferred_base(
                        [token_map[u], token_map[v], token_map[w]]
                    )
                    chosen_addr = chosen_base.address.lower()
                    if chosen_addr == u:
                        cycle_nodes = (u, v, w)
                        hop_edges = ((u, v, p_uv), (v, w, p_vw), (w, u, p_wu))
                    elif chosen_addr == v:
                        cycle_nodes = (v, w, u)
                        hop_edges = ((v, w, p_vw), (w, u, p_wu), (u, v, p_uv))
                    else:
                        cycle_nodes = (w, u, v)
                        hop_edges = ((w, u, p_wu), (u, v, p_uv), (v, w, p_vw))

                start_tok = token_map[cycle_nodes[0]]
                hops = (
                    RouteHop(
                        pool=hop_edges[0][2].pool,
                        token_in=token_map[hop_edges[0][0]],
                        token_out=token_map[hop_edges[0][1]],
                    ),
                    RouteHop(
                        pool=hop_edges[1][2].pool,
                        token_in=token_map[hop_edges[1][0]],
                        token_out=token_map[hop_edges[1][1]],
                    ),
                    RouteHop(
                        pool=hop_edges[2][2].pool,
                        token_in=token_map[hop_edges[2][0]],
                        token_out=token_map[hop_edges[2][1]],
                    ),
                )

                p1_id = hop_edges[0][2].pool.pool_id.lower().removeprefix("0x")[:8]
                p2_id = hop_edges[1][2].pool.pool_id.lower().removeprefix("0x")[:8]
                p3_id = hop_edges[2][2].pool.pool_id.lower().removeprefix("0x")[:8]
                cand_id = (
                    f"cand_tri_{snapshot.block_number}_"
                    f"{start_tok.symbol.lower()}_{p1_id}_{p2_id}_{p3_id}"
                )

                candidate = CandidateRoute(
                    candidate_id=cand_id,
                    route_type="triangular",
                    base_token=start_tok,
                    hops=hops,
                    observed_gross_bps=round(observed_gross_bps, 4),
                    snapshot_block=snapshot.block_number,
                    created_at=float(snapshot.captured_at),
                )
                candidates.append(candidate)

    # Sort deterministically
    candidates.sort(key=lambda c: (-c.observed_gross_bps, c.candidate_id))
    return candidates
