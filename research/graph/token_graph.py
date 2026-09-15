"""Multi-asset directed graph for token swap liquidity and path evaluation.

Constructs an in-memory directed graph where:
- Nodes represent tokens (normalized address or symbol).
- Edges represent exchange rates and pool fees (DirectedEdge).
- Block numbers are propagated deterministically from quotes to both forward and reverse edges.
- Zero-addresses and malformed identifiers are filtered fail-closed.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from typing import TYPE_CHECKING

from research.graph.models import ZERO_ADDRESS, DirectedEdge

if TYPE_CHECKING:
    from research.market_data.multicall import PriceQuote

logger = logging.getLogger(__name__)

# Known token mappings for Robinhood EVM / Arc research environment
KNOWN_CORE_TOKENS: dict[str, tuple[str, int]] = {
    "WETH": ("0x0bd7d308f8e1639fab988df18a8011f41eacad73", 18),
    "ETH": ("0x0000000000000000000000000000000000000000", 18),
    "USDG": ("0x5fc5360d0400a0fd4f2af552add042d716f1d168", 6),
    "MOO": ("0xd9db30bb0d2b8d2eae3826a1372117e058791e18", 18),
    "PONS": ("0x39dbed3a2bd333467115de45665cc57f813c4571", 18),
    "CASHCAT": ("0x020bfc650a365f8bb26819deaabf3e21291018b4", 18),
    "MEME": ("0x385f4f8ae47651ce5f58f5265395a669f8281e18", 18),
    "TENDIES": ("0x45242320dbb855eea8fd36804c6487e10e97fcf9", 18),
    "SPY": ("0xd8bc240f1eb252d6a5c101c5bdf57ee925232712", 18),
    "NVDA": ("0x43869911fdc5625ff17b9b0fa69634e443422026", 18),
    "GLD": ("0xa05e4fa4418a09bc30678d4924a68ebcc187d7dc", 18),
    "SGOV": ("0x0f4b3602fc5f096230bf9f9640ce1c4c16ca66bf", 18),
    "TSLA": ("0x88fbe6c4f0fd0c497406a4b13a35ff86cb3ca74c", 18),
    "GME": ("0x34d58849eb2f8c5c7db6ef80d9931bdfa3754983", 18),
    "AI": ("0x411aa6d2b389ba24fcba97d8b52c00fa88924b1a", 18),
    "SLV": ("0x6f9ea223395b83965b262a40fb649e1a81283c74", 18),
    "NET": ("0xCA9c78Dd337A67F6e0077F65F5E9218719d30eDf", 18),
    "FATCOIN": ("0x12d5ee7917ca430073c3a638ee1e6f0648a98a01", 18),
    "NASDUCK": ("0x4444444444444444444444444444444444444441", 18),
}


def is_invalid_token(tok: str | None) -> bool:
    """Return True if token is empty, zero address, or has an invalid hex address length."""
    if not tok:
        return True
    clean = str(tok).strip().lower()
    if clean == ZERO_ADDRESS:
        return True
    if clean.replace("0", "").replace("x", "") == "":
        return True
    if clean.startswith("0x"):
        if len(clean) != 42:
            return True
        try:
            return int(clean, 16) == 0
        except ValueError:
            return True
    elif len(clean) == 40:
        try:
            int(clean, 16)
            return True
        except ValueError:
            pass
    return False


def normalize_cycle(cycle: Sequence[str]) -> list[str]:
    """Normalize arbitrage cycle node sequence.

    Convention:
        cycle length = hops + 1 (e.g. 4 nodes for 3 hops), with identical start and end node.
    """
    if not cycle:
        return []

    deduped: list[str] = []
    for node in cycle:
        clean = str(node).strip()
        if not clean:
            continue
        if not deduped or clean.lower() != deduped[-1].lower():
            deduped.append(clean)

    if len(deduped) < 2:
        return deduped

    if deduped[0].lower() != deduped[-1].lower():
        deduped.append(deduped[0])

    return deduped


class TokenGraph:
    """Directed graph of tokens and pools for offline arbitrage route search."""

    def __init__(self, symbol_map: dict[str, str] | None = None) -> None:
        # adj: from_token -> to_token -> list[DirectedEdge]
        self.adj: dict[str, dict[str, list[DirectedEdge]]] = {}
        self.symbol_map: dict[str, str] = dict(symbol_map or {})

        # Populate baseline symbol mappings
        for sym, (addr, _) in KNOWN_CORE_TOKENS.items():
            self.symbol_map[addr.lower()] = sym
            self.symbol_map[sym.upper()] = sym

    def resolve_symbol(self, token_key: str) -> str:
        """Resolve human-readable symbol for a token key."""
        k = token_key.lower()
        if k in self.symbol_map:
            return self.symbol_map[k]
        u = token_key.upper()
        if u in self.symbol_map:
            return self.symbol_map[u]
        return token_key

    def _normalize_token_key(self, token_key: str) -> str:
        """Normalize token address or symbol for internal lookup."""
        k = token_key.strip()
        if k.upper() in KNOWN_CORE_TOKENS:
            return KNOWN_CORE_TOKENS[k.upper()][0].lower()
        return k.lower()

    def add_quote(self, quote: PriceQuote) -> None:
        """Add bidirectional directed edges from a single pool's PriceQuote.

        Edge semantics:
            base -> quote: rate = quote.price, fee = pool.fee_bps
            quote -> base: rate = 1.0 / quote.price, fee = pool.fee_bps
            block_number is propagated deterministically to both directions.
        """
        if quote.price <= 0 or not math.isfinite(quote.price):
            return

        b_raw = quote.base.lower()
        q_raw = quote.quote.lower()

        b = (
            KNOWN_CORE_TOKENS[b_raw.upper()][0].lower()
            if b_raw.upper() in KNOWN_CORE_TOKENS
            else b_raw
        )
        q = (
            KNOWN_CORE_TOKENS[q_raw.upper()][0].lower()
            if q_raw.upper() in KNOWN_CORE_TOKENS
            else q_raw
        )

        # Defense: reject zero-address or malformed token identifiers
        if (
            is_invalid_token(b)
            or is_invalid_token(q)
            or is_invalid_token(b_raw)
            or is_invalid_token(q_raw)
        ):
            return

        # Defense: reject pools whose underlying token fields are zero-address or invalid
        p_t0 = getattr(quote.pool, "token0", "")
        p_t1 = getattr(quote.pool, "token1", "")
        p_c0 = getattr(quote.pool, "currency0", "")
        p_c1 = getattr(quote.pool, "currency1", "")
        for pt in (p_t0, p_t1, p_c0, p_c1):
            if pt and is_invalid_token(str(pt)):
                return

        b_sym = self.resolve_symbol(b)
        q_sym = self.resolve_symbol(q)
        if hasattr(quote, "base_symbol") and quote.base_symbol:
            b_sym = quote.base_symbol
            self.symbol_map[b] = b_sym
        if hasattr(quote, "quote_symbol") and quote.quote_symbol:
            q_sym = quote.quote_symbol
            self.symbol_map[q] = q_sym

        f_bps = quote.pool.fee_bps
        if not math.isfinite(f_bps) or f_bps < 0.0:
            return

        f_ratio = f_bps / 10000.0
        fee_factor = max(0.0, 1.0 - f_ratio)
        quote_block = getattr(quote, "block_number", 0)

        # 1. Forward edge: base -> quote
        rate_fwd = quote.price
        eff_fwd = rate_fwd * fee_factor
        if eff_fwd > 0 and math.isfinite(eff_fwd):
            w_fwd = -math.log(eff_fwd)
            edge_fwd = DirectedEdge(
                from_token=b,
                to_token=q,
                pool=quote.pool,
                rate=rate_fwd,
                fee_bps=f_bps,
                effective_rate=eff_fwd,
                weight=w_fwd,
                from_symbol=b_sym,
                to_symbol=q_sym,
                block_number=quote_block,
            )
            self.adj.setdefault(b, {}).setdefault(q, []).append(edge_fwd)

        # 2. Reverse edge: quote -> base
        rate_rev = 1.0 / quote.price
        eff_rev = rate_rev * fee_factor
        if eff_rev > 0 and math.isfinite(eff_rev):
            w_rev = -math.log(eff_rev)
            edge_rev = DirectedEdge(
                from_token=q,
                to_token=b,
                pool=quote.pool,
                rate=rate_rev,
                fee_bps=f_bps,
                effective_rate=eff_rev,
                weight=w_rev,
                from_symbol=q_sym,
                to_symbol=b_sym,
                block_number=quote_block,
            )
            self.adj.setdefault(q, {}).setdefault(b, []).append(edge_rev)

    def get_best_edge(self, u: str, v: str) -> DirectedEdge | None:
        """Retrieve highest effective exchange rate edge from u to v."""
        u_norm = self._normalize_token_key(u)
        v_norm = self._normalize_token_key(v)

        edges = self.adj.get(u_norm, {}).get(v_norm, [])
        if not edges:
            edges = self.adj.get(u.lower(), {}).get(v.lower(), [])
        if not edges:
            edges = self.adj.get(u, {}).get(v, [])
        if not edges:
            return None

        return max(edges, key=lambda e: e.effective_rate)

    @property
    def nodes(self) -> list[str]:
        """List all distinct token node identifiers present in the graph."""
        all_nodes = set(self.adj.keys())
        for dests in self.adj.values():
            all_nodes.update(dests.keys())
        return sorted(all_nodes)
