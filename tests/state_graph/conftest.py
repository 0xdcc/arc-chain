"""Pytest fixtures for state graph tests."""

from __future__ import annotations

import pytest


@pytest.fixture
def sample_sqrt_prices() -> dict[str, int]:
    """Provides boundary and typical sqrt price ratios in Q64.96."""
    return {
        "min": 4295128739,
        "one": 1 << 96,
        "max": 1461446703485210103287273052203988822378723970342,
    }
