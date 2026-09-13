"""Checkpoint-based gap recovery engine and segment backfill reconciliation."""

from __future__ import annotations

import json

from arc_ingest.coverage import BlockRange, CoverageManifest
from arc_ingest.history_scan import HistoricalBlockScanner
from arc_ingest.recorder import RawDataRecorder
from arc_readiness.errors import ArcValidationError


class GapRecoveryEngine:
    """Detects missing block ranges in raw segment storage and orchestrates gap recovery."""

    def __init__(
        self,
        recorder: RawDataRecorder,
        scanner: HistoricalBlockScanner | None = None,
    ) -> None:
        self.recorder = recorder
        self.scanner = scanner

    def rebuild_coverage_from_disk(self) -> CoverageManifest:
        """Scan all segment files on disk to reconstruct verified coverage and block hashes."""
        manifest = CoverageManifest(chain_id=self.recorder.chain_id)
        segment_dir = self.recorder.segment_manager.base_dir

        if not segment_dir.exists():
            return manifest

        for seg_file in sorted(segment_dir.glob("*.jsonl")):
            try:
                with open(seg_file, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                            b_num = int(record["block_number"])
                            b_hash = str(record["block_hash"]).lower()
                            manifest.add_block(b_num, b_hash)
                        except (KeyError, ValueError, json.JSONDecodeError):
                            continue
            except OSError:
                continue

        return manifest

    def plan_recovery(
        self,
        target_start: int,
        target_end: int,
        current_coverage: CoverageManifest | None = None,
    ) -> list[BlockRange]:
        """Compute missing block ranges that must be backfilled."""
        cov = current_coverage or self.rebuild_coverage_from_disk()
        return cov.compute_gaps(target_start, target_end)

    def execute_recovery(
        self,
        target_start: int,
        target_end: int,
        scanner: HistoricalBlockScanner | None = None,
    ) -> int:
        """Execute gap recovery over missing ranges and verify full continuous coverage upon completion."""
        active_scanner = scanner or self.scanner
        if not active_scanner:
            raise ArcValidationError("Cannot execute recovery: no HistoricalBlockScanner provided")

        cov = self.rebuild_coverage_from_disk()
        gaps = cov.compute_gaps(target_start, target_end)

        total_recovered_blocks = 0
        for gap in gaps:
            # Backfill gap range chunk by chunk
            curr = gap.start_block
            while curr <= gap.end_block:
                chunk_end = min(curr + active_scanner.max_batch_size - 1, gap.end_block)
                scanned = active_scanner.scan_range(curr, chunk_end)
                total_recovered_blocks += scanned
                curr = chunk_end + 1

        # Final verification: coverage must now be 100% continuous
        final_cov = self.rebuild_coverage_from_disk()
        final_cov.assert_continuous_coverage(target_start, target_end)

        return total_recovered_blocks
