"""Tests for T09: Raw Data Recorder, Persistent Cursors, and Bounded Segment Integrity."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from arc_ingest.cursors import CursorStore, DurableCursor
from arc_ingest.recorder import RawDataRecorder
from arc_ingest.segments import SegmentManager
from arc_readiness.errors import ArcValidationError

_HASH_100 = "0x" + "11" * 32
_HASH_101 = "0x" + "22" * 32
_HASH_102 = "0x" + "33" * 32


class TestRawRecorderAndCursors:
    """Test suite for T09 raw ingestion, atomic cursor advancement, and segment sealing."""

    def test_cursor_store_monotonicity_and_atomicity(self, tmp_path: Path) -> None:
        c_file = tmp_path / "cursor.json"
        store = CursorStore(c_file, chain_id=5042)
        assert store.current is None

        # Advance to 100
        cur1 = store.advance(100, _HASH_100, explicit_timestamp=1700000000.0)
        assert cur1.block_number == 100
        assert cur1.block_hash == _HASH_100

        # Reload from disk: verify persistent
        store2 = CursorStore(c_file, chain_id=5042)
        assert store2.current is not None
        assert store2.current.block_number == 100
        assert store2.current.block_hash == _HASH_100

        # Advance to 101
        cur2 = store.advance(101, _HASH_101, explicit_timestamp=1700000001.0)
        assert cur2.block_number == 101

        # Regress to 99: strictly rejected!
        with pytest.raises(ArcValidationError, match="Cannot regress cursor"):
            store.advance(99, _HASH_100)

        # Same block number, different hash: drift rejected!
        with pytest.raises(ArcValidationError, match="Cursor block hash divergence"):
            store.advance(101, _HASH_102)

    def test_raw_recorder_sequential_ingestion_and_empty_blocks(self, tmp_path: Path) -> None:
        rec_dir = tmp_path / "raw_data"
        recorder = RawDataRecorder(rec_dir, chain_id=5042, segment_size_blocks=10)

        # 1. Record block with logs
        b100 = {"number": 100, "hash": _HASH_100, "timestamp": 1700000000}
        logs100 = [{"address": "0x1111111111111111111111111111111111111111", "data": "0xabc"}]
        seg1 = recorder.record_block(b100, logs100)
        assert seg1 == "seg_000000100_000000109"

        assert recorder.latest_cursor is not None
        assert recorder.latest_cursor.block_number == 100

        # 2. Record empty block (boundary condition: empty blocks are legitimate!)
        b101 = {"number": 101, "hash": _HASH_101, "timestamp": 1700000001}
        seg2 = recorder.record_block(b101, logs=[])
        assert seg2 == seg1
        assert recorder.latest_cursor.block_number == 101

        # 3. Replay and verify records
        records = recorder.read_records(seg1)
        assert len(records) == 2
        rec1_payload = json.loads(records[0]["raw_payload"])
        assert rec1_payload["is_empty_block"] is False
        assert rec1_payload["logs_count"] == 1

        rec2_payload = json.loads(records[1]["raw_payload"])
        assert rec2_payload["is_empty_block"] is True
        assert rec2_payload["logs_count"] == 0

    def test_recorder_write_failure_does_not_advance_cursor(self, tmp_path: Path) -> None:
        rec_dir = tmp_path / "raw_data_fail"
        recorder = RawDataRecorder(rec_dir, chain_id=5042, segment_size_blocks=10)

        # First successful write:
        b100 = {"number": 100, "hash": _HASH_100, "timestamp": 1700000000}
        recorder.record_block(b100, logs=[])
        assert recorder.latest_cursor is not None
        assert recorder.latest_cursor.block_number == 100

        # Mock disk write failure during block 101:
        b101 = {"number": 101, "hash": _HASH_101, "timestamp": 1700000001}
        with patch.object(SegmentManager, "append_record", side_effect=OSError("Disk full!")):
            with pytest.raises(IOError, match="Disk full!"):
                recorder.record_block(b101, logs=[])

        # Assert cursor was NOT advanced!
        assert recorder.latest_cursor.block_number == 100

    def test_segment_manager_detects_corrupt_truncated_line(self, tmp_path: Path) -> None:
        seg_dir = tmp_path / "segments"
        mgr = SegmentManager(seg_dir, segment_size_blocks=100)

        mgr.append_record(10, {"valid": 1})
        mgr.append_record(11, {"valid": 2})

        seg_id = mgr.get_segment_id_for_block(10)
        file_path = seg_dir / f"{seg_id}.jsonl"

        # Verify healthy
        valid, count, _ = mgr.verify_segment_integrity(seg_id)
        assert valid is True
        assert count == 2

        # Inject truncated half-line corruption
        with open(file_path, "a", encoding="utf-8") as f:
            f.write('{"truncated_field": "half_wri\n')

        # Integrity verification must catch the corruption!
        with pytest.raises(ArcValidationError, match="Corrupt or truncated record on line 3"):
            mgr.verify_segment_integrity(seg_id)
