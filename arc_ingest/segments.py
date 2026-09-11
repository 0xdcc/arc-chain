"""Bounded segment file management, integrity hashing, and corruption isolation."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from arc_readiness.errors import ArcValidationError


@dataclass(frozen=True)
class SegmentMetadata:
    """Metadata tracking an immutable raw ingestion segment file."""

    segment_id: str
    file_name: str
    from_block: int
    to_block: int
    records_count: int
    sha256_hash: str
    is_sealed: bool = False


class SegmentManager:
    """Manages appending to active segments and verifying line-level integrity."""

    def __init__(
        self,
        base_dir: str | Path,
        segment_size_blocks: int = 1000,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.segment_size_blocks = segment_size_blocks
        self._manifest_file = self.base_dir / "segments_manifest.json"
        self._segments: dict[str, SegmentMetadata] = {}
        self._load_manifest()

    def _load_manifest(self) -> None:
        if not self._manifest_file.exists():
            return
        try:
            with open(self._manifest_file, encoding="utf-8") as f:
                data = json.load(f)
            for item in data.get("segments", []):
                meta = SegmentMetadata(**item)
                self._segments[meta.segment_id] = meta
        except Exception as e:
            raise ArcValidationError(f"Corrupt segment manifest: {e}") from e

    def _save_manifest(self) -> None:
        tmp_path = self._manifest_file.with_suffix(".tmp")
        payload = {"segments": [asdict(s) for s in self._segments.values()]}
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self._manifest_file)

    def get_segment_id_for_block(self, block_number: int) -> str:
        start_block = (block_number // self.segment_size_blocks) * self.segment_size_blocks
        end_block = start_block + self.segment_size_blocks - 1
        return f"seg_{start_block:09d}_{end_block:09d}"

    def append_record(self, block_number: int, record_dict: dict[str, Any]) -> str:
        """Append a JSON record line to the corresponding segment file with fsync."""
        seg_id = self.get_segment_id_for_block(block_number)
        file_path = self.base_dir / f"{seg_id}.jsonl"

        line = json.dumps(record_dict, separators=(",", ":")) + "\n"
        encoded = line.encode("utf-8")

        with open(file_path, "ab") as f:
            f.write(encoded)
            f.flush()
            os.fsync(f.fileno())

        return seg_id

    def verify_segment_integrity(self, seg_id: str) -> tuple[bool, int, str]:
        """Verify that every line in a segment is valid JSON and calculate its SHA-256."""
        file_path = self.base_dir / f"{seg_id}.jsonl"
        if not file_path.exists():
            raise ArcValidationError(f"Segment file does not exist: {file_path}")

        hasher = hashlib.sha256()
        line_count = 0

        with open(file_path, "rb") as f:
            for line_idx, raw_line in enumerate(f, start=1):
                hasher.update(raw_line)
                decoded = raw_line.decode("utf-8").strip()
                if not decoded:
                    continue
                try:
                    json.loads(decoded)
                except Exception as e:
                    raise ArcValidationError(
                        f"Corrupt or truncated record on line {line_idx} of segment {seg_id}: {e}"
                    ) from e
                line_count += 1

        return True, line_count, hasher.hexdigest()

    def seal_segment(self, seg_id: str) -> SegmentMetadata:
        """Seal a segment file, locking its record count and SHA-256 in the manifest."""
        valid, count, sha256 = self.verify_segment_integrity(seg_id)
        parts = seg_id.split("_")
        from_b = int(parts[1])
        to_b = int(parts[2])

        meta = SegmentMetadata(
            segment_id=seg_id,
            file_name=f"{seg_id}.jsonl",
            from_block=from_b,
            to_block=to_b,
            records_count=count,
            sha256_hash=sha256,
            is_sealed=True,
        )
        self._segments[seg_id] = meta
        self._save_manifest()
        return meta

    def list_segments(self) -> list[SegmentMetadata]:
        return list(self._segments.values())
