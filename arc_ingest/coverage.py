"""Coverage tracking, gap detection, and continuity verification for raw Arc ingestion."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from arc_readiness.errors import ArcValidationError


@dataclass(frozen=True)
class BlockRange:
    """Inclusive block range [start_block, end_block]."""

    start_block: int
    end_block: int

    def __post_init__(self) -> None:
        if self.start_block < 0:
            raise ArcValidationError(f"start_block must be non-negative: {self.start_block}")
        if self.end_block < self.start_block:
            raise ArcValidationError(
                f"end_block ({self.end_block}) cannot be less than start_block ({self.start_block})"
            )

    @property
    def count(self) -> int:
        return self.end_block - self.start_block + 1

    def contains(self, block_number: int) -> bool:
        return self.start_block <= block_number <= self.end_block


@dataclass
class CoverageManifest:
    """Tracks scanned block ranges, hashes, and gap intervals."""

    chain_id: int = 5042
    ranges: list[BlockRange] = field(default_factory=list)
    block_hashes: dict[int, str] = field(default_factory=dict)

    def add_block(self, block_number: int, block_hash: str) -> None:
        """Record a single block and verify hash immutability."""
        clean_hash = block_hash.strip().lower()
        if not clean_hash.startswith("0x") or len(clean_hash) != 66:
            raise ArcValidationError(f"Invalid block hash format: {block_hash}")

        if block_number in self.block_hashes:
            existing = self.block_hashes[block_number]
            if existing != clean_hash:
                raise ArcValidationError(
                    f"Hash divergence detected at block {block_number}: "
                    f"existing {existing}, new {clean_hash}. Possible reorg or tampering."
                )
            return

        self.block_hashes[block_number] = clean_hash
        self._rebuild_ranges()

    def record_range(self, start_block: int, end_block: int, hashes: dict[int, str] | None = None) -> None:
        """Record a contiguous range of blocks."""
        br = BlockRange(start_block, end_block)
        if hashes:
            for b in range(start_block, end_block + 1):
                if b in hashes:
                    self.add_block(b, hashes[b])
        else:
            # Fallback when hashes not individually supplied
            self.ranges.append(br)
            self._consolidate_ranges()

    def _rebuild_ranges(self) -> None:
        if not self.block_hashes:
            self.ranges = []
            return

        sorted_blocks = sorted(self.block_hashes.keys())
        merged: list[BlockRange] = []
        r_start = sorted_blocks[0]
        r_prev = sorted_blocks[0]

        for b in sorted_blocks[1:]:
            if b == r_prev + 1:
                r_prev = b
            else:
                merged.append(BlockRange(r_start, r_prev))
                r_start = b
                r_prev = b
        merged.append(BlockRange(r_start, r_prev))
        self.ranges = merged

    def _consolidate_ranges(self) -> None:
        if not self.ranges:
            return
        sorted_ranges = sorted(self.ranges, key=lambda r: r.start_block)
        merged: list[BlockRange] = [sorted_ranges[0]]
        for curr in sorted_ranges[1:]:
            prev = merged[-1]
            if curr.start_block <= prev.end_block + 1:
                merged[-1] = BlockRange(prev.start_block, max(prev.end_block, curr.end_block))
            else:
                merged.append(curr)
        self.ranges = merged

    @property
    def total_blocks(self) -> int:
        return sum(r.count for r in self.ranges)

    @property
    def min_block(self) -> int | None:
        return self.ranges[0].start_block if self.ranges else None

    @property
    def max_block(self) -> int | None:
        return self.ranges[-1].end_block if self.ranges else None

    def compute_gaps(self, target_start: int, target_end: int) -> list[BlockRange]:
        """Compute all missing [start, end] intervals within [target_start, target_end]."""
        if target_end < target_start:
            raise ArcValidationError(f"Invalid target range: {target_start}..{target_end}")

        if not self.ranges:
            return [BlockRange(target_start, target_end)]

        gaps: list[BlockRange] = []
        curr = target_start

        for r in self.ranges:
            if r.end_block < curr:
                continue
            if r.start_block > curr:
                gap_end = min(r.start_block - 1, target_end)
                gaps.append(BlockRange(curr, gap_end))
                curr = r.start_block
            curr = max(curr, r.end_block + 1)
            if curr > target_end:
                break

        if curr <= target_end:
            gaps.append(BlockRange(curr, target_end))

        return gaps

    def is_continuous(self, from_block: int, to_block: int) -> bool:
        """Check if [from_block, to_block] is strictly covered with zero missing blocks."""
        gaps = self.compute_gaps(from_block, to_block)
        return len(gaps) == 0

    def assert_continuous_coverage(self, from_block: int, to_block: int) -> None:
        """Raise ArcValidationError if any gaps exist in the requested interval."""
        gaps = self.compute_gaps(from_block, to_block)
        if gaps:
            gap_strs = [f"[{g.start_block}..{g.end_block}]" for g in gaps]
            raise ArcValidationError(
                f"Missing block coverage in interval [{from_block}..{to_block}]. "
                f"Gaps: {', '.join(gap_strs)}. Quoting or trading over gaps is strictly prohibited."
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "ranges": [{"start_block": r.start_block, "end_block": r.end_block} for r in self.ranges],
            "total_blocks": self.total_blocks,
            "min_block": self.min_block,
            "max_block": self.max_block,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CoverageManifest:
        manifest = cls(chain_id=int(data.get("chain_id", 5042)))
        ranges_data = data.get("ranges", [])
        manifest.ranges = [BlockRange(r["start_block"], r["end_block"]) for r in ranges_data]
        manifest._consolidate_ranges()
        return manifest
