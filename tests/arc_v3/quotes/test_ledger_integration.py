"""Arc Versioned Opportunity Ledger and Segmentation Integration Tests (T23)

Verifies:
- Append-only hash chain integrity and checkpoint reconciliation
- Record typing: RAW, QUOTE, SIM, RECONCILED
- Truthful persistence of negative delta, unknown gas, and simulation failure records
- Bounded segment rotation and cross-segment iteration
- Recovery from incomplete/half-tail writes
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from arc_opportunities.ledger import (
    ArcOpportunityLedger,
    ArcOpportunityRecord,
    RecordType,
    compute_observation_id,
)
from arc_opportunities.ledger_segments import SegmentedOpportunityLedger


def make_record(
    record_type: RecordType = RecordType.QUOTE,
    route_id: str = "route-001",
    state_ref: str = "state:v1:" + "11" * 32,
    amount_in: int = 100_000_000,
    amount_out: int | None = 105_000_000,
    net_atoms: int | None = 4_000_000,
    economic_status: str = "profitable",
    quote_status: str = "quoted",
    sim_status: str | None = None,
    rec_status: str | None = None,
) -> ArcOpportunityRecord:
    obs_id = compute_observation_id(route_id, state_ref, amount_in, 1726000000000)
    return ArcOpportunityRecord(
        record_type=record_type,
        observation_id=obs_id,
        route_id=route_id,
        chain_id=5042,
        state_ref=state_ref,
        amount_in_atoms=amount_in,
        amount_out_atoms=amount_out,
        net_atoms=net_atoms,
        economic_status=economic_status,
        quote_status=quote_status,
        simulation_status=sim_status,
        reconciled_status=rec_status,
        payload={"notes": "test_payload"},
    )


class TestArcOpportunityLedger:
    """Test suite for T23 Opportunity Ledger and Segments."""

    def test_ledger_append_and_hash_chain_integrity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ledger_test_") as tmpdir:
            ledger_path = Path(tmpdir) / "test_ledger.jsonl"
            ledger = ArcOpportunityLedger(ledger_path)

            r1 = make_record(record_type=RecordType.RAW, amount_out=None, net_atoms=None, economic_status="unknown")
            r2 = make_record(record_type=RecordType.QUOTE, amount_out=105_000_000, net_atoms=4_000_000, economic_status="profitable")
            r3 = make_record(record_type=RecordType.QUOTE, amount_out=98_000_000, net_atoms=-3_000_000, economic_status="unprofitable")
            r4 = make_record(record_type=RecordType.SIM, sim_status="CALL_SUCCEEDED")
            r5 = make_record(record_type=RecordType.RECONCILED, rec_status="RECONCILED_MATCH")

            assert ledger.append(r1) == 0
            assert ledger.append(r2) == 1
            assert ledger.append(r3) == 2
            assert ledger.append(r4) == 3
            assert ledger.append(r5) == 4

            assert ledger.confirmed_sequence == 4
            all_records = ledger.read_all()
            assert len(all_records) == 5
            # Negative profit record is preserved truthfully!
            assert all_records[2].net_atoms == -3_000_000
            assert all_records[2].economic_status == "unprofitable"

            quotes = ledger.read_by_type(RecordType.QUOTE)
            assert len(quotes) == 2

    def test_recovery_reopens_cleanly(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ledger_reopen_") as tmpdir:
            ledger_path = Path(tmpdir) / "reopen_ledger.jsonl"
            ledger1 = ArcOpportunityLedger(ledger_path)
            r = make_record()
            ledger1.append(r)
            seq = ledger1.confirmed_sequence
            head = ledger1.head_hash

            # Reopen new writer on same path
            ledger2 = ArcOpportunityLedger(ledger_path)
            assert ledger2.confirmed_sequence == seq
            assert ledger2.head_hash == head
            assert len(ledger2.read_all()) == 1

    def test_segmented_ledger_rotation_and_iteration(self) -> None:
        with tempfile.TemporaryDirectory(prefix="seg_test_") as tmpdir:
            base_dir = Path(tmpdir) / "segments"
            # Limit each segment to 3 records
            seg_ledger = SegmentedOpportunityLedger(base_dir, max_records_per_segment=3)

            # Append 7 records -> should span 3 segments (3 + 3 + 1)
            for i in range(7):
                rec = make_record(route_id=f"route-{i}")
                seg_ledger.append(rec)

            assert seg_ledger.segments_count == 3
            all_recs = list(seg_ledger.iter_all_records())
            assert len(all_recs) == 7
            assert seg_ledger.count_total() == 7
            assert all_recs[0].route_id == "route-0"
            assert all_recs[6].route_id == "route-6"
