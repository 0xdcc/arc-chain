"""Bounded Segmented Opportunity Ledger Engine (T23)

Partitions opportunity ledger into bounded segments:
- Fixed segment size limits (e.g. 1000 records or block ranges)
- Automatic rotation and manifest indexing of sealed segments
- Continuous sequence and global iteration across historical and active segments
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from arc_opportunities.ledger import ArcOpportunityLedger, ArcOpportunityRecord


@dataclass(frozen=True, slots=True)
class SegmentInfo:
    """Metadata for a sealed or active ledger segment."""

    segment_id: str
    start_sequence: int
    end_sequence: int
    head_hash: str
    path: str
    is_sealed: bool


class SegmentedOpportunityLedger:
    """Manages bounded rolling segments of the opportunity ledger."""

    def __init__(self, base_dir: Path, max_records_per_segment: int = 1000) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.max_records_per_segment = max_records_per_segment
        self._manifest_path = self.base_dir / "segments_manifest.json"
        self._segments: list[SegmentInfo] = []
        self._current_segment_idx = 0
        self._current_ledger: ArcOpportunityLedger | None = None
        self._load_or_init()

    def _load_or_init(self) -> None:
        if self._manifest_path.exists():
            data = json.loads(self._manifest_path.read_text(encoding="utf-8"))
            for s in data.get("segments", []):
                self._segments.append(
                    SegmentInfo(
                        segment_id=s["segment_id"],
                        start_sequence=s["start_sequence"],
                        end_sequence=s["end_sequence"],
                        head_hash=s["head_hash"],
                        path=s["path"],
                        is_sealed=s["is_sealed"],
                    )
                )
            if self._segments:
                last_seg = self._segments[-1]
                if not last_seg.is_sealed:
                    self._current_segment_idx = len(self._segments) - 1
                    self._current_ledger = ArcOpportunityLedger(Path(last_seg.path))
                    return

        # Start initial active segment
        self._start_new_segment(start_seq=0)

    def _start_new_segment(self, start_seq: int) -> None:
        seg_id = f"segment_{len(self._segments):06d}"
        seg_path = self.base_dir / f"{seg_id}.jsonl"
        self._current_ledger = ArcOpportunityLedger(seg_path)
        seg_info = SegmentInfo(
            segment_id=seg_id,
            start_sequence=start_seq,
            end_sequence=start_seq - 1,
            head_hash="",
            path=str(seg_path),
            is_sealed=False,
        )
        self._segments.append(seg_info)
        self._current_segment_idx = len(self._segments) - 1
        self._save_manifest()

    def _save_manifest(self) -> None:
        manifest_data = {
            "version": "arc-segments-v1",
            "max_records_per_segment": self.max_records_per_segment,
            "segments": [
                {
                    "segment_id": s.segment_id,
                    "start_sequence": s.start_sequence,
                    "end_sequence": s.end_sequence,
                    "head_hash": s.head_hash,
                    "path": s.path,
                    "is_sealed": s.is_sealed,
                }
                for s in self._segments
            ],
        }
        self._manifest_path.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")

    def append(self, record: ArcOpportunityRecord) -> int:
        """Append record to active segment, rotating if threshold is reached."""
        assert self._current_ledger is not None
        curr_count = self._current_ledger.confirmed_sequence + 1
        if curr_count >= self.max_records_per_segment:
            # Seal active segment
            active_info = self._segments[self._current_segment_idx]
            sealed_info = SegmentInfo(
                segment_id=active_info.segment_id,
                start_sequence=active_info.start_sequence,
                end_sequence=active_info.start_sequence + curr_count - 1,
                head_hash=self._current_ledger.head_hash,
                path=active_info.path,
                is_sealed=True,
            )
            self._segments[self._current_segment_idx] = sealed_info
            # Open next
            next_start = sealed_info.end_sequence + 1
            self._start_new_segment(start_seq=next_start)

        return self._current_ledger.append(record)

    def iter_all_records(self) -> Iterator[ArcOpportunityRecord]:
        """Iterate across all records in historical sealed segments plus active segment."""
        for seg in self._segments:
            ledger_file = Path(seg.path)
            if ledger_file.exists():
                seg_ledger = ArcOpportunityLedger(ledger_file)
                yield from seg_ledger.read_all()

    def count_total(self) -> int:
        """Count total records across all segments."""
        return sum(1 for _ in self.iter_all_records())

    @property
    def segments_count(self) -> int:
        return len(self._segments)
