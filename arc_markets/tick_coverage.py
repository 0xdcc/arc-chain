"""Uniswap V3 tick coverage tracking, bitmap word states, and missing coverage barriers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from arbitrage_contracts.arc_extensions import TickCoverage
from arc_readiness.errors import ArcMarketIneligibleError, ArcValidationError


class TickWordStatus(StrEnum):
    """Explicit verification state of a 256-bit tick bitmap word."""

    READ_POPULATED = "read_populated"
    READ_EMPTY = "read_empty"
    UNREAD_UNKNOWN = "unread_unknown"


@dataclass(frozen=True)
class TickWord:
    """Individual 256-bit bitmap word observation."""

    word_pos: int
    status: TickWordStatus
    bitmap_value: int = 0

    def __post_init__(self) -> None:
        if self.bitmap_value < 0 or self.bitmap_value >= (1 << 256):
            raise ArcValidationError(f"bitmap_value out of uint256 range: {self.bitmap_value}")
        if self.status == TickWordStatus.READ_EMPTY and self.bitmap_value != 0:
            raise ArcValidationError(f"READ_EMPTY word must have bitmap_value 0, got {self.bitmap_value}")


@dataclass(frozen=True)
class TickData:
    """Initialized tick state containing gross and net liquidity."""

    tick: int
    liquidity_gross: int
    liquidity_net: int
    initialized: bool = True

    def __post_init__(self) -> None:
        if self.liquidity_gross <= 0:
            raise ArcValidationError(f"liquidity_gross of initialized tick must be > 0, got {self.liquidity_gross}")


@dataclass
class TickCoverageSnapshot:
    """Immutable point-in-time tick coverage proof for a specific pool."""

    pool_address: str
    tick_spacing: int
    current_tick: int
    epoch_block: int
    epoch_hash: str
    chain_id: int = 5042
    words: dict[int, TickWord] = field(default_factory=dict)
    ticks: dict[int, TickData] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.tick_spacing <= 0:
            raise ArcValidationError(f"tick_spacing must be positive: {self.tick_spacing}")
        if self.epoch_block < 0:
            raise ArcValidationError(f"epoch_block cannot be negative: {self.epoch_block}")

    def word_position(self, tick: int) -> int:
        """Calculate the 16-bit word index containing the given tick."""
        compressed = tick // self.tick_spacing
        return compressed >> 8

    def is_word_covered(self, word_pos: int) -> bool:
        """Check if a word is confirmed read (populated or empty)."""
        word = self.words.get(word_pos)
        return word is not None and word.status != TickWordStatus.UNREAD_UNKNOWN

    def assert_tick_covered(self, tick: int) -> None:
        """Assert that the tick's containing bitmap word has been explicitly read from chain.

        Fails-closed if unread. NEVER defaults unread words to 0 liquidity!
        """
        w_pos = self.word_position(tick)
        if not self.is_word_covered(w_pos):
            raise ArcMarketIneligibleError(
                f"Tick {tick} falls into unread bitmap word {w_pos} in pool {self.pool_address}. "
                "Coverage is incomplete. Imputing zero liquidity across unread words is strictly prohibited."
            )

    def assert_range_covered(self, min_tick: int, max_tick: int) -> None:
        """Assert all bitmap words spanning [min_tick, max_tick] are verified."""
        if max_tick < min_tick:
            raise ArcValidationError(f"Invalid range: max_tick ({max_tick}) < min_tick ({min_tick})")

        w_start = self.word_position(min_tick)
        w_end = self.word_position(max_tick)

        for w in range(w_start, w_end + 1):
            if not self.is_word_covered(w):
                raise ArcMarketIneligibleError(
                    f"Bitmap word {w} in range [{min_tick}..{max_tick}] is unread. "
                    "Cannot evaluate multi-tick swap over unverified coverage."
                )

    def to_tick_coverage(self, is_complete: bool = False) -> TickCoverage:
        """Export to canonical TickCoverage model for state_graph multi_tick consumption."""
        covered_words = [w for w, tw in self.words.items() if tw.status != TickWordStatus.UNREAD_UNKNOWN]
        if not covered_words:
            min_t = self.current_tick
            max_t = self.current_tick
        else:
            min_w = min(covered_words)
            max_w = max(covered_words)
            min_t = (min_w << 8) * self.tick_spacing
            max_t = (((max_w + 1) << 8) - 1) * self.tick_spacing

        return TickCoverage(
            pool_id=self.pool_address.lower(),
            current_tick=self.current_tick,
            initialized_ticks_count=len(self.ticks),
            min_tick=min_t,
            max_tick=max_t,
            is_complete=is_complete,
            as_of_block=self.epoch_block,
        )
