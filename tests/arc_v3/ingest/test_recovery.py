"""Tests for T11: Checkpoint Gap Recovery, Coverage Continuity, and Hash Divergence Guards."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from arc_ingest.coverage import BlockRange, CoverageManifest
from arc_ingest.history_scan import HistoricalBlockScanner
from arc_ingest.recovery import GapRecoveryEngine
from arc_ingest.recorder import RawDataRecorder
from arc_readiness.errors import ArcValidationError


@pytest.fixture
def temp_recorder(tmp_path):
    return RawDataRecorder(base_dir=tmp_path / "ingest_recovery", chain_id=5042)


class TestGapRecoveryAndCoverage:
    """Test suite for gap detection, checkpoint reconstruction, and backfill verification."""

    def test_coverage_manifest_gap_detection_and_assertion(self) -> None:
        cov = CoverageManifest(chain_id=5042)
        for b in range(10, 16):
            cov.add_block(b, f"0x{'%064x' % b}")
        for b in range(20, 26):
            cov.add_block(b, f"0x{'%064x' % b}")

        assert cov.total_blocks == 12
        assert cov.is_continuous(10, 15) is True
        assert cov.is_continuous(20, 25) is True
        assert cov.is_continuous(10, 25) is False

        gaps = cov.compute_gaps(10, 25)
        assert len(gaps) == 1
        assert gaps[0] == BlockRange(16, 19)

        with pytest.raises(ArcValidationError, match="Missing block coverage in interval.*Gaps: \\[16\\.\\.19\\]"):
            cov.assert_continuous_coverage(10, 25)

    def test_block_hash_divergence_fails_closed(self) -> None:
        cov = CoverageManifest(chain_id=5042)
        hash_a = "0x" + "aa" * 32
        hash_b = "0x" + "bb" * 32

        cov.add_block(100, hash_a)
        # Re-adding identical hash is idempotent:
        cov.add_block(100, hash_a)

        # Divergent hash must fail closed:
        with pytest.raises(ArcValidationError, match="Hash divergence detected at block 100"):
            cov.add_block(100, hash_b)

    def test_rebuild_coverage_from_disk_and_plan_recovery(self, temp_recorder) -> None:
        # Pre-record blocks 1..3 and 7..9 directly into recorder
        for b in (1, 2, 3, 7, 8, 9):
            header = {
                "number": hex(b),
                "hash": f"0x{'%064x' % b}",
                "timestamp": hex(1700000000 + b),
            }
            temp_recorder.record_block(header, logs=[])

        engine = GapRecoveryEngine(recorder=temp_recorder)
        rebuilt = engine.rebuild_coverage_from_disk()

        assert rebuilt.total_blocks == 6
        assert rebuilt.min_block == 1
        assert rebuilt.max_block == 9

        planned_gaps = engine.plan_recovery(1, 9, current_coverage=rebuilt)
        assert len(planned_gaps) == 1
        assert planned_gaps[0] == BlockRange(4, 6)

    def test_execute_recovery_backfills_gaps_successfully(self, temp_recorder) -> None:
        # Initial state: blocks 1..2 and 5..6 exist
        for b in (1, 2, 5, 6):
            header = {
                "number": hex(b),
                "hash": f"0x{'%064x' % b}",
                "timestamp": hex(1700000000 + b),
            }
            temp_recorder.record_block(header, logs=[])

        mock_transport = MagicMock()

        def fake_request(method, params):
            if method == "eth_getBlockByNumber":
                b_num = int(params[0], 16)
                return {
                    "number": hex(b_num),
                    "hash": f"0x{'%064x' % b_num}",
                    "timestamp": hex(1700000000 + b_num),
                }
            if method == "eth_getLogs":
                return []
            return None

        mock_transport.request.side_effect = fake_request

        scanner = HistoricalBlockScanner(
            transport=mock_transport,
            recorder=temp_recorder,
            max_batch_size=10,
        )

        engine = GapRecoveryEngine(recorder=temp_recorder, scanner=scanner)
        recovered_count = engine.execute_recovery(target_start=1, target_end=6)

        assert recovered_count == 2  # blocks 3 and 4
        final_cov = engine.rebuild_coverage_from_disk()
        assert final_cov.total_blocks == 6
        assert final_cov.is_continuous(1, 6) is True
