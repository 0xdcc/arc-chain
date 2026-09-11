"""Behavioral tests for private settled-cycle models."""

from __future__ import annotations

import json
import unittest
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

from research.settled_cycles.models import (
    AttributionStatus,
    CycleAction,
    ExecutionCostBreakdown,
    FeeComponentRecord,
    SettledCycleRecord,
    TransactionSubjects,
    canonical_hash,
)


class SettledCycleRecordTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        fixtures = Path(__file__).resolve().parent / "fixtures"
        with (fixtures / "synthetic.jsonl").open(encoding="utf-8") as source:
            cls.records = [SettledCycleRecord.from_dict(json.loads(line)) for line in source]

    def make_record(self) -> SettledCycleRecord:
        """Create an isolated valid record from the independent synthetic fixture."""
        return SettledCycleRecord.from_dict(self.records[0].to_dict())

    def make_modified(self, **overrides: object) -> SettledCycleRecord:
        """Copy the frozen record and return it without validation for mutation."""
        value = self.make_record()
        for name, replaced in overrides.items():
            object.__setattr__(value, name, replaced)
        return value

    def assert_model_rejects(self, overrides: dict[str, object]) -> None:
        """Copy a frozen record, mutate it, and force a new construction to validate."""
        mutation_target = self.make_record()
        for name, replaced in overrides.items():
            object.__setattr__(mutation_target, name, replaced)
        constructor_values = {
            "schema_id": mutation_target.schema_id,
            "schema_version": mutation_target.schema_version,
            "tx_hash": mutation_target.tx_hash,
            "chain_id": mutation_target.chain_id,
            "block_number": mutation_target.block_number,
            "block_hash": mutation_target.block_hash,
            "transaction_index": mutation_target.transaction_index,
            "timestamp_s": mutation_target.timestamp_s,
            "subjects": mutation_target.subjects,
            "actions": mutation_target.actions,
            "subject_deltas": mutation_target.subject_deltas,
            "cost_breakdown": mutation_target.cost_breakdown,
            "attribution_status": mutation_target.attribution_status,
            "economic_status": mutation_target.economic_status,
            "attributed_net_atoms": mutation_target.attributed_net_atoms,
            "attributed_net_usd": mutation_target.attributed_net_usd,
            "rejection_or_unknown_reasons": mutation_target.rejection_or_unknown_reasons,
            "provenance": mutation_target.provenance,
            "data_mode": mutation_target.data_mode,
            "verified": mutation_target.verified,
        }
        with self.assertRaises((ValueError, TypeError)):
            SettledCycleRecord(**constructor_values)

    def test_synthetic_fixture_hashes_match_independent_expected_output(self) -> None:
        fixtures = Path(__file__).resolve().parent / "fixtures"
        with (fixtures / "expected.jsonl").open(encoding="utf-8") as source:
            expected = [json.loads(line) for line in source]
        self.assertEqual(6, len(self.records))
        self.assertEqual(
            [item["canonical_hash"] for item in expected],
            [canonical_hash(value) for value in self.records],
        )

    def test_schema_v1_is_valid_json_with_expected_identity(self) -> None:
        schema_path = Path(__file__).resolve().parents[2] / "research/settled_cycles/schema-v1.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual("http://json-schema.org/draft-07/schema#", schema["$schema"])
        self.assertEqual("w3-settled-research", schema["properties"]["schema_id"]["const"])
        self.assertEqual("1.0.0", schema["properties"]["schema_version"]["const"])

    def test_full_record_round_trip(self) -> None:
        value = self.make_record()
        restored = SettledCycleRecord.from_dict(value.to_dict())
        self.assertEqual(value, restored)
        self.assertEqual(value.to_dict(), restored.to_dict())

    def test_canonical_hash_is_order_and_path_independent(self) -> None:
        value = self.make_record()
        data = value.to_dict()
        reversed_data = {key: data[key] for key in reversed(list(data))}
        self.assertEqual(canonical_hash(value), canonical_hash(value))
        self.assertEqual(
            canonical_hash(value), canonical_hash(SettledCycleRecord.from_dict(reversed_data))
        )
        relocated = deepcopy(data)
        relocated["provenance"]["source_file"] = "another/location.json"
        self.assertEqual(
            canonical_hash(value), canonical_hash(SettledCycleRecord.from_dict(relocated))
        )

    def test_duplicate_component_id_is_rejected(self) -> None:
        component = self.make_record().cost_breakdown.components[0]
        duplicate = FeeComponentRecord(
            component_id=component.component_id,
            asset=component.asset,
            amount_atoms=component.amount_atoms,
            payer=component.payer,
            economic_bearer=component.economic_bearer,
            source=component.source,
            counted_in=component.counted_in,
            dedup_key=component.dedup_key,
        )
        with self.assertRaises(ValueError):
            ExecutionCostBreakdown((component, duplicate))

    def test_unverified_missing_trace_fail_closed(self) -> None:
        self.assert_model_rejects(
            {
                "attribution_status": AttributionStatus.UNVERIFIED_MISSING_TRACE,
                "economic_status": "unknown",
                "attributed_net_atoms": 1,
                "attributed_net_usd": None,
                "rejection_or_unknown_reasons": ("missing_trace",),
            }
        )

    def test_unverified_missing_trace_usd_fail_closed(self) -> None:
        self.assert_model_rejects(
            {
                "attribution_status": AttributionStatus.AMBIGUOUS_COMPLEX_TX,
                "economic_status": "unknown",
                "attributed_net_atoms": None,
                "attributed_net_usd": Decimal("1"),
                "rejection_or_unknown_reasons": ("ambiguous",),
            }
        )

    def test_out_of_order_and_duplicate_step_ids_are_rejected(self) -> None:
        data = self.make_record().to_dict()
        actions = data["actions"]
        self.assert_model_rejects({"actions": (actions[0], actions[0])})
        self.assert_model_rejects(
            {
                "actions": (
                    CycleAction.from_dict({**actions[0], "step_id": 3}),
                    CycleAction.from_dict({**actions[0], "step_id": 1}),
                )
            }
        )

    def test_boolean_and_float_amounts_are_rejected(self) -> None:
        for invalid in (True, False, 1.0):
            self.assert_model_rejects({"attributed_net_atoms": invalid})

    def test_action_amounts_reject_boolean_and_float(self) -> None:
        from research.settled_cycles.models import CycleAction

        for invalid in (True, 1.0):
            action = self.make_record().actions[0]
            with self.assertRaises(TypeError):
                CycleAction(
                    step_id=action.step_id,
                    action_kind=action.action_kind,
                    pool_key=action.pool_key,
                    asset_in=action.asset_in,
                    asset_out=action.asset_out,
                    amount_in_atoms=invalid,
                    amount_out_atoms=action.amount_out_atoms,
                    direction=action.direction,
                    log_index=action.log_index,
                    trace_address=action.trace_address,
                    parent_step_id=action.parent_step_id,
                    execution_status=action.execution_status,
                    evidence_refs=action.evidence_refs,
                )

    def test_invalid_address_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            TransactionSubjects(
                tx_origin="0x123",
                executor_contract=None,
                beneficiary=None,
                gas_payer=None,
                economic_bearer=None,
            )

    def test_unknown_schema_version_is_rejected(self) -> None:
        self.assert_model_rejects({"schema_version": "2.0.0"})

    def test_invalid_enum_values_are_rejected(self) -> None:
        self.assert_model_rejects({"attribution_status": "made_up"})
        self.assert_model_rejects({"economic_status": "made_up"})

    def test_invalid_tx_hash_is_rejected(self) -> None:
        self.assert_model_rejects({"tx_hash": "0x" + "d" * 63})

    def test_non_identity_asset_is_rejected(self) -> None:
        value = self.make_record().subject_deltas[0]
        with self.assertRaises(TypeError):
            type(value)(
                subject_address=value.subject_address,
                asset="not-an-asset",
                delta_atoms=None,
                basis="unknown",
                completeness="unknown",
                reconciliation_diff_atoms=None,
            )


if __name__ == "__main__":
    unittest.main()
