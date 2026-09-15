"""Independent offline token graph and cross-block triangular path contract.

Provides:
- DirectedEdge, SwapLeg, TriangularArbAlert models;
- TokenGraph in-memory multi-asset graph with deterministic block_number propagation;
- calculate_triangular_path with block-skew circuit breaker, fee deduction, and cycle validation;
- find_triangular_opportunities exhaustive 3-hop search with canonical rotation deduplication;
- bellman_ford_arbitrage negative cycle detection;
- Zero-address and malformed token guardrails.
"""

from __future__ import annotations

from research.graph.circuit_breaker import (
    bellman_ford_arbitrage,
    calculate_triangular_path,
    find_triangular_opportunities,
)
from research.graph.models import (
    ZERO_ADDRESS,
    DirectedEdge,
    SwapLeg,
    TriangularArbAlert,
)
from research.graph.token_graph import (
    TokenGraph,
    is_invalid_token,
    normalize_cycle,
)

__all__ = [
    "ZERO_ADDRESS",
    "DirectedEdge",
    "SwapLeg",
    "TokenGraph",
    "TriangularArbAlert",
    "bellman_ford_arbitrage",
    "calculate_triangular_path",
    "find_triangular_opportunities",
    "is_invalid_token",
    "normalize_cycle",
]
