"""Pure arbitrage strategy calculations."""

from __future__ import annotations

from research.strategies.spread import find_spread_candidates
from research.strategies.triangular import find_triangular_candidates

__all__ = [
    "find_spread_candidates",
    "find_triangular_candidates",
]
