"""W3-E read-only coverage classification tests."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from research.settled_cycles.coverage import (
    REGISTRY_SCHEMA,
    CoverageGapKind,
    classify_coverage,
)
from research.settled_cycles.models import SettledCycleRecord


class CoverageClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        data = json.loads(
            (Path(__file__).resolve().parent / "fixtures/synthetic.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()[0]
        )
        data["actions"][0]["pool_key"] = {
            "chain_id": 4663,
            "protocol_id": "test",
            "venue_kind": "factory",
            "venue_address": "0x" + "aa" * 20,
            "pool_id_kind": "address",
            "pool_id": "0x" + "aa" * 20,
        }
        self.record = SettledCycleRecord.from_dict(data)

    def test_missing_registry_and_observations_fail_closed(self) -> None:
        gaps = classify_coverage(self.record)
        self.assertIn(CoverageGapKind.HISTORICAL_REGISTRY_MISSING, [gap.gap_kind for gap in gaps])
        self.assertIn(CoverageGapKind.LATENCY_UNKNOWN, [gap.gap_kind for gap in gaps])

    def test_current_registry_does_not_backfill_historical_eligibility(self) -> None:
        pool = self.record.actions[0].pool_key
        assert pool is not None
        registry = {
            "schema": REGISTRY_SCHEMA,
            "pools": [
                {
                    "pool_key": {
                        "chain_id": pool.chain_id,
                        "protocol_id": pool.protocol_id,
                        "venue_kind": pool.venue_kind,
                        "venue_address": pool.venue_address,
                        "pool_id_kind": pool.pool_id_kind,
                        "pool_id": pool.pool_id,
                    },
                    "current": {"eligibility": "eligible"},
                }
            ],
        }
        gaps = classify_coverage(self.record, registry)
        kinds = [gap.gap_kind for gap in gaps]
        self.assertIn(CoverageGapKind.HISTORICAL_REGISTRY_MISSING, kinds)
        self.assertNotIn(CoverageGapKind.ELIGIBILITY_UNKNOWN, kinds)


if __name__ == "__main__":
    unittest.main()
