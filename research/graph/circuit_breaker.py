"""Circuit breaker, topological cycle verification, and triangular path evaluation.

Contains:
- `calculate_triangular_path`: 3-hop multiplier, block-skew circuit breaker, fee deduction, and finite input validation.
- `find_triangular_opportunities`: Exhaustive 3-hop cycle search with canonical rotation deduplication.
- `bellman_ford_arbitrage`: Log-weighted negative cycle detection for arbitrage paths.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING

from research.graph.models import DirectedEdge, SwapLeg, TriangularArbAlert
from research.graph.token_graph import (
    KNOWN_CORE_TOKENS,
    TokenGraph,
    is_invalid_token,
    normalize_cycle,
)

if TYPE_CHECKING:
    from research.market_data.multicall import PriceQuote

logger = logging.getLogger(__name__)


def _normalize_token_addr(tok: str) -> str:
    """Normalize token address or symbol to lowercase EVM address if known."""
    clean = str(tok).strip()
    if clean.upper() in KNOWN_CORE_TOKENS:
        return KNOWN_CORE_TOKENS[clean.upper()][0].lower()
    return clean.lower()


def calculate_triangular_path(
    leg1: DirectedEdge,
    leg2: DirectedEdge,
    leg3: DirectedEdge,
    slippage_buffer_pct: float = 0.6,
) -> TriangularArbAlert | None:
    """Calculate 3-hop triangular path multiplier with block-skew, topology, and finite boundary checks.

    Formulas:
        gross_multiplier = P1 * P2 * P3
        fee_multiplier = (1 - f1) * (1 - f2) * (1 - f3)
        expected_multiplier = gross_multiplier * fee_multiplier
        gross_profit_pct = (gross_multiplier - 1.0) * 100.0
        total_fee_pct = (1.0 - fee_multiplier) * 100.0
        net_profit_pct = (expected_multiplier - 1.0) * 100.0 - slippage_buffer_pct

    Block Skew Hard Circuit Breaker:
        skew = max(b1, b2, b3) - min(b1, b2, b3)
        If skew > 1, quotes are from divergent block heights (stale / phantom spread).
        Immediately returns None.

    Topology Verification:
        Enforces leg1.to == leg2.from, leg2.to == leg3.from, leg3.to == leg1.from,
        and all 3 intermediate nodes must be distinct.

    Finite Input Boundary Defense:
        Enforces that all rates, effective_rates, fee_bps, and slippage_buffer_pct
        are finite numbers with rates > 0, effective_rates > 0, fee_bps in [0, 10000),
        and slippage_buffer_pct >= 0.

    Financial Boundary:
        `net_profit_pct` is the fee-adjusted and slippage-buffer-adjusted geometric rate spread
        percentage before gas. Transaction gas is unknown at graph screening stage and NOT deducted.
        This rate indication does not constitute realized on-chain net profit or an execution guarantee.
        Execution dispatch enforces discrete uint256 atom arithmetic and physical output_floor bounds
        (output_floor >= amount_in + ceil(gas_atoms) + 1 atom) in downstream planning.
    """
    # 1. Finite boundary validation on slippage_buffer_pct
    if (
        not isinstance(slippage_buffer_pct, (int, float))
        or not math.isfinite(slippage_buffer_pct)
        or slippage_buffer_pct < 0.0
    ):
        logger.warning(
            "[INVALID_PATH_RATES] slippage_buffer_pct 非有限或负值 (val=%s)，丢弃 alert",
            slippage_buffer_pct,
        )
        return None

    # 2. Finite boundary validation on edge rates, effective rates, and fee_bps
    for leg_idx, leg_obj in enumerate((leg1, leg2, leg3), start=1):
        r = getattr(leg_obj, "rate", None)
        eff_r = getattr(leg_obj, "effective_rate", None)
        f_bps = getattr(leg_obj, "fee_bps", None)
        if (
            not isinstance(r, (int, float))
            or not math.isfinite(r)
            or r <= 0.0
            or not isinstance(eff_r, (int, float))
            or not math.isfinite(eff_r)
            or eff_r <= 0.0
            or not isinstance(f_bps, (int, float))
            or not math.isfinite(f_bps)
            or f_bps < 0.0
            or f_bps >= 10000.0
        ):
            logger.warning(
                "[INVALID_PATH_RATES] Leg %d 汇率/费率边界非法 "
                "(rate=%s, effective_rate=%s, fee_bps=%s)，丢弃 alert",
                leg_idx,
                r,
                eff_r,
                f_bps,
            )
            return None

    t1_from = _normalize_token_addr(leg1.from_token)
    t1_to = _normalize_token_addr(leg1.to_token)
    t2_from = _normalize_token_addr(leg2.from_token)
    t2_to = _normalize_token_addr(leg2.to_token)
    t3_from = _normalize_token_addr(leg3.from_token)
    t3_to = _normalize_token_addr(leg3.to_token)

    # Topological cycle continuity verification
    if t1_to != t2_from or t2_to != t3_from or t3_to != t1_from:
        return None

    # Distinct 3-token requirement
    if t1_from == t2_from or t2_from == t3_from or t3_from == t1_from:
        return None

    # Block skew hard circuit breaker
    b1 = getattr(leg1, "block_number", 0)
    b2 = getattr(leg2, "block_number", 0)
    b3 = getattr(leg3, "block_number", 0)
    skew = max(b1, b2, b3) - min(b1, b2, b3)
    if skew > 1:
        return None

    # Zero address and invalid address protection
    legs_to_check = [
        (1, t1_from, t1_to, leg1),
        (2, t2_from, t2_to, leg2),
        (3, t3_from, t3_to, leg3),
    ]
    for leg_num, f_tok, t_tok, leg_obj in legs_to_check:
        if is_invalid_token(f_tok) or is_invalid_token(t_tok):
            logger.warning(
                "[INVALID_PATH_TOKEN] Leg %d token 地址非法 (from=%s, to=%s)，丢弃 alert",
                leg_num,
                f_tok,
                t_tok,
            )
            return None
        if is_invalid_token(leg_obj.from_token) or is_invalid_token(leg_obj.to_token):
            logger.warning(
                "[INVALID_PATH_TOKEN] Leg %d 原始 token 地址非法 (from=%s, to=%s)，丢弃 alert",
                leg_num,
                leg_obj.from_token,
                leg_obj.to_token,
            )
            return None
        for attr in ("token0", "token1", "currency0", "currency1"):
            pt = getattr(leg_obj.pool, attr, "")
            if pt and is_invalid_token(str(pt)):
                logger.warning(
                    "[INVALID_PATH_TOKEN] Leg %d 池包含非法代币地址 (%s=%s)，丢弃 alert",
                    leg_num,
                    attr,
                    pt,
                )
                return None

    latest_block = max(b1, b2, b3)

    p1, f1 = leg1.rate, leg1.fee_bps / 10000.0
    p2, f2 = leg2.rate, leg2.fee_bps / 10000.0
    p3, f3 = leg3.rate, leg3.fee_bps / 10000.0

    gross_mult = p1 * p2 * p3
    fee_mult = (1.0 - f1) * (1.0 - f2) * (1.0 - f3)
    expected_mult = gross_mult * fee_mult

    gross_profit_pct = (gross_mult - 1.0) * 100.0
    total_fee_pct = (1.0 - fee_mult) * 100.0
    net_profit_pct = (expected_mult - 1.0) * 100.0 - slippage_buffer_pct
    net_mult = 1.0 + net_profit_pct / 100.0

    if not math.isfinite(expected_mult) or not math.isfinite(net_profit_pct):
        return None

    s1 = leg1.from_symbol or leg1.from_token
    s2 = leg2.from_symbol or leg2.from_token
    s3 = leg3.from_symbol or leg3.from_token

    if s2.lower() == s1.lower():
        s2 = leg2.from_token[:10]
    if s3.lower() == s1.lower() or s3.lower() == s2.lower():
        s3 = leg3.from_token[:10]

    for node in (s1, s2, s3):
        if is_invalid_token(node):
            logger.warning("[INVALID_PATH_TOKEN] 环路节点包含非法代币 %s，丢弃 alert", node)
            return None

    cycle_list = normalize_cycle([s1, s2, s3, s1])
    if len(cycle_list) != 4 or cycle_list[0].lower() != cycle_list[-1].lower():
        logger.warning(
            "[CYCLE_ANOMALY] 生成 cycle 拓扑异常 (len=%d, expected=4, cycle=%s), 丢弃",
            len(cycle_list),
            cycle_list,
        )
        return None
    cycle = tuple(cycle_list)

    legs = [
        SwapLeg(
            from_token=leg1.from_token,
            to_token=leg1.to_token,
            pool=leg1.pool,
            rate=leg1.rate,
            fee_bps=leg1.fee_bps,
            effective_rate=leg1.effective_rate,
            from_symbol=leg1.from_symbol,
            to_symbol=leg1.to_symbol,
        ),
        SwapLeg(
            from_token=leg2.from_token,
            to_token=leg2.to_token,
            pool=leg2.pool,
            rate=leg2.rate,
            fee_bps=leg2.fee_bps,
            effective_rate=leg2.effective_rate,
            from_symbol=leg2.from_symbol,
            to_symbol=leg2.to_symbol,
        ),
        SwapLeg(
            from_token=leg3.from_token,
            to_token=leg3.to_token,
            pool=leg3.pool,
            rate=leg3.rate,
            fee_bps=leg3.fee_bps,
            effective_rate=leg3.effective_rate,
            from_symbol=leg3.from_symbol,
            to_symbol=leg3.to_symbol,
        ),
    ]

    # Continuous Cournot TVL sizing approximation for research screening
    tvl1 = getattr(leg1.pool, "tvl_usd", 0.0)
    tvl2 = getattr(leg2.pool, "tvl_usd", 0.0)
    tvl3 = getattr(leg3.pool, "tvl_usd", 0.0)
    known_tvl = all(
        isinstance(v, (int, float)) and math.isfinite(v) and v > 0 for v in (tvl1, tvl2, tvl3)
    )
    bottleneck_tvl = min(tvl1, tvl2, tvl3) if known_tvl else 0.0

    net_edge = max(0.0, expected_mult - 1.0)
    k = ((2.0 / tvl1) + (2.0 / tvl2) + (2.0 / tvl3)) if known_tvl else 0.0

    max_cap = net_edge / k if k > 0 else 0.0
    # Strict 500U physical hard cap bound
    opt_size = min(500.0, max_cap / 2.0) if max_cap > 0 else 0.0
    max_profit = opt_size * max(0.0, net_edge - 0.5 * k * opt_size) if opt_size > 0 else 0.0

    size_500 = min(500.0, max_cap)
    profit_500 = size_500 * max(0.0, net_edge - 0.5 * k * size_500) if size_500 > 0 else 0.0

    # PROFIT_ANOMALY physical guard: impossible profit bounds above 1000 USD on 500U cap
    if max_profit > 1000.0 or profit_500 > 1000.0:
        logger.error(
            "[PROFIT_ANOMALY] 三角套利利润计算异常: max_profit_usd=%.2f > 1000.0 "
            "(profit_at_500u=%.2f, cycle=%s), 丢弃该 alert",
            max_profit,
            profit_500,
            cycle,
        )
        return None

    return TriangularArbAlert(
        start_token=leg1.from_token,
        cycle=cycle,
        legs=legs,
        gross_multiplier=gross_mult,
        fee_multiplier=fee_mult,
        expected_multiplier=expected_mult,
        slippage_buffer_pct=slippage_buffer_pct,
        net_multiplier=net_mult,
        gross_profit_pct=gross_profit_pct,
        net_profit_pct=net_profit_pct,
        total_fee_pct=total_fee_pct,
        bottleneck_tvl=bottleneck_tvl,
        max_capacity_usd=max_cap,
        optimal_size_usd=opt_size,
        max_profit_usd=max_profit,
        profit_at_500u=profit_500,
        block_skew=skew,
        block_number=latest_block,
        ts=time.time(),
    )


def _canonical_triangle(u: str, v: str, w: str) -> tuple[str, ...]:
    """Standardize triangle nodes to canonical minimum rotation for deduplication."""
    triplets = [(u, v, w), (v, w, u), (w, u, v)]
    return min(triplets)


def find_triangular_opportunities(
    quotes: Sequence[PriceQuote],
    base_token: str | None = None,
    slippage_buffer_pct: float = 0.6,
    min_profit_pct: float = 0.0,
    graph: TokenGraph | None = None,
) -> list[TriangularArbAlert]:
    """Exhaustively search all 3-hop triangular arbitrage opportunities from quotes.

    Args:
        quotes: Collection of spot PriceQuote items across pools.
        base_token: If specified (e.g. 'USDG' or address), restricts search to paths starting at base_token.
        slippage_buffer_pct: Protection buffer percentage subtracted from rate spread (default 0.6%).
        min_profit_pct: Threshold for net rate spread percentage to emit alert (default 0.0%).
        graph: Optional pre-constructed TokenGraph instance.

    Returns:
        List of TriangularArbAlert opportunities, sorted by net_profit_pct descending.
    """
    g = graph if graph is not None else TokenGraph()
    if graph is None:
        for q in quotes:
            g.add_quote(q)

    target_base: str | None = None
    if base_token:
        bt_clean = base_token.strip()
        for node in g.nodes:
            if (
                node.lower() == bt_clean.lower()
                or g.resolve_symbol(node).upper() == bt_clean.upper()
            ):
                target_base = node
                break
        if target_base is None:
            target_base = bt_clean.lower()

    alerts: list[TriangularArbAlert] = []
    seen_cycles: set[tuple[str, ...]] = set()

    search_roots = [target_base] if target_base and target_base in g.adj else g.nodes

    for u in search_roots:
        neighbors_u = g.adj.get(u, {})
        for v in neighbors_u.keys():
            if v == u:
                continue
            neighbors_v = g.adj.get(v, {})
            for w in neighbors_v.keys():
                if w == u or w == v:
                    continue
                neighbors_w = g.adj.get(w, {})
                if u not in neighbors_w:
                    continue

                # Triangular cycle u -> v -> w -> u found
                canon = _canonical_triangle(u, v, w)
                if target_base is None:
                    if canon in seen_cycles:
                        continue
                    seen_cycles.add(canon)

                e1 = g.get_best_edge(u, v)
                e2 = g.get_best_edge(v, w)
                e3 = g.get_best_edge(w, u)
                if not e1 or not e2 or not e3:
                    continue

                alert = calculate_triangular_path(
                    e1, e2, e3, slippage_buffer_pct=slippage_buffer_pct
                )
                if (
                    alert is not None
                    and alert.expected_multiplier > 1.0
                    and alert.net_profit_pct > min_profit_pct
                ):
                    alerts.append(alert)

    return sorted(alerts, key=lambda a: a.net_profit_pct, reverse=True)


def bellman_ford_arbitrage(
    graph: TokenGraph,
    slippage_buffer_pct: float = 0.6,
) -> list[TriangularArbAlert]:
    """Detect negative cycles in TokenGraph using Bellman-Ford on -ln(effective_rate) weights.

    Identifies 3-hop arbitrage cycles, filters them through calculate_triangular_path,
    and returns deduplicated TriangularArbAlert instances.
    """
    nodes = graph.nodes
    if len(nodes) < 3:
        return []

    # Gather best edge for each directed pair
    edges: list[DirectedEdge] = []
    for u in nodes:
        for v in graph.adj.get(u, {}):
            best_e = graph.get_best_edge(u, v)
            if best_e is not None and best_e.effective_rate > 0:
                edges.append(best_e)

    dist: dict[str, float] = {node: 0.0 for node in nodes}
    pred: dict[str, DirectedEdge | None] = {node: None for node in nodes}

    # Relax |V| - 1 times
    for _ in range(len(nodes) - 1):
        updated = False
        for edge in edges:
            u, v = edge.from_token, edge.to_token
            w = edge.weight
            if dist[u] + w < dist[v] - 1e-12:
                dist[v] = dist[u] + w
                pred[v] = edge
                updated = True
        if not updated:
            break

    # |V|-th relaxation to discover negative cycles
    alerts: list[TriangularArbAlert] = []
    seen_cycles: set[tuple[str, ...]] = set()

    for edge in edges:
        u, v = edge.from_token, edge.to_token
        if dist[u] + edge.weight < dist[v] - 1e-12:
            # Backtrack to locate cycle origin
            curr = v
            for _ in range(len(nodes)):
                pe = pred.get(curr)
                if pe is None:
                    break
                curr = pe.from_token

            start_cycle = curr
            curr_step = start_cycle
            path_edges: list[DirectedEdge] = []
            while True:
                pe = pred.get(curr_step)
                if pe is None:
                    break
                path_edges.append(pe)
                curr_step = pe.from_token
                if curr_step == start_cycle or len(path_edges) > len(nodes):
                    break

            if len(path_edges) == 3 and curr_step == start_cycle:
                path_edges.reverse()
                e1, e2, e3 = path_edges[0], path_edges[1], path_edges[2]
                canon = _canonical_triangle(e1.from_token, e2.from_token, e3.from_token)
                if canon not in seen_cycles:
                    seen_cycles.add(canon)
                    alert = calculate_triangular_path(
                        e1, e2, e3, slippage_buffer_pct=slippage_buffer_pct
                    )
                    if alert is not None and alert.expected_multiplier > 1.0:
                        alerts.append(alert)

    return alerts
