"""Unit tests verifying legacy record adaptation, missing evidence defense, and isolation (A24)."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any

from arbitrage_contracts.identity import PoolDescriptor, TokenKey
from arbitrage_contracts.legacy_adapter import (
    AdaptationResult,
    LegacyContext,
    adapt_legacy_record,
)
from arbitrage_contracts.quote import (
    DataMode,
    GasEvidenceKind,
    QuoteEvidence,
    QuoteStatus,
)
from arbitrage_contracts.serialization import ContractRecord
from arbitrage_contracts.state import StateVersion


class TestLegacyAdapter(unittest.TestCase):
    """A24: Legacy record adaptation, missing evidence detection, and fail-closed safety."""

    def setUp(self) -> None:
        self.chain_id = 4663
        self.context = LegacyContext(
            run_id="test-run-legacy-01",
            data_mode="synthetic",
            chain_id=self.chain_id,
        )

    def test_positive_state_version_upgrade(self) -> None:
        """Verify complete legacy state snapshot upgrades to standard StateVersion ContractRecord."""
        raw = {
            "record_type": "state_version",
            "chain_id": self.chain_id,
            "block_domain": "l2",
            "block_number": 500000,
            "block_hash": "0x1111222233334444555566667777888899990000aaaabbbbccccddddeeeeffff",
            "received_at_ms": 1725000001000,
            "block_timestamp_s": 1725000001,
            "completeness": "syncing",
        }
        result = adapt_legacy_record(raw, self.context)
        self.assertTrue(result.success)
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.missing_fields, ())
        self.assertEqual(result.unresolved_reasons, ())
        self.assertIsInstance(result.record, ContractRecord)
        assert result.record is not None
        self.assertEqual(result.record.record_type, "state_version")
        self.assertIsInstance(result.record.payload, StateVersion)
        assert isinstance(result.record.payload, StateVersion)
        self.assertEqual(result.record.payload.block_number, 500000)
        self.assertEqual(len(result.source_hash), 64)

    def test_positive_pool_descriptor_upgrade(self) -> None:
        """Verify complete legacy pool identity upgrades to standard PoolDescriptor ContractRecord."""
        raw = {
            "record_type": "pool_descriptor",
            "chain_id": self.chain_id,
            "protocol": "uniswap_v3",
            "pool_id": "0x1111111111111111111111111111111111111111",
            "factory": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "token0": "0x2222222222222222222222222222222222222222",
            "token1": "0x3333333333333333333333333333333333333333",
            "fee_bps": 5.0,
            "tick_spacing": 10,
        }
        result = adapt_legacy_record(raw, self.context)
        self.assertTrue(result.success)
        self.assertEqual(result.status, "complete")
        self.assertIsInstance(result.record, ContractRecord)
        assert result.record is not None
        self.assertEqual(result.record.record_type, "pool_descriptor")
        assert isinstance(result.record.payload, PoolDescriptor)
        self.assertEqual(result.record.payload.fee_model.raw_value, 500)
        self.assertEqual(result.record.payload.tick_spacing, 10)

    def test_positive_quote_evidence_upgrade(self) -> None:
        """Verify complete legacy quote upgrades to standard QuoteEvidence ContractRecord."""
        raw = {
            "record_type": "quote",
            "quote_id": "quote-pos-001",
            "status": "QUOTED",
            "route": {
                "chain_id": self.chain_id,
                "base_token": "0x2222222222222222222222222222222222222222",
                "hops": [
                    {
                        "pool": {
                            "chain_id": self.chain_id,
                            "protocol": "uniswap_v3",
                            "pool_id": "0x1111111111111111111111111111111111111111",
                            "factory": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                        },
                        "token_in": "0x2222222222222222222222222222222222222222",
                        "token_out": "0x3333333333333333333333333333333333333333",
                    },
                    {
                        "pool": {
                            "chain_id": self.chain_id,
                            "protocol": "sushiswap",
                            "pool_id": "0x4444444444444444444444444444444444444444",
                            "factory": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                        },
                        "token_in": "0x3333333333333333333333333333333333333333",
                        "token_out": "0x2222222222222222222222222222222222222222",
                        "direction": "one_for_zero",
                    },
                ],
            },
            "amount_in": {"atoms": "1000000", "decimals": 6},
            "amount_out": {"atoms": "1040000", "decimals": 6},
            "gas_estimate": 125000,
        }
        result = adapt_legacy_record(raw, self.context)
        self.assertTrue(result.success)
        self.assertEqual(result.status, "complete")
        self.assertIsInstance(result.record, ContractRecord)
        assert result.record is not None
        self.assertEqual(result.record.record_type, "quote_evidence")
        self.assertIsInstance(result.record.payload, QuoteEvidence)
        assert isinstance(result.record.payload, QuoteEvidence)
        assert result.record.payload.amount_in is not None
        assert result.record.payload.amount_out is not None
        self.assertEqual(result.record.payload.amount_in.atoms, 1000000)
        self.assertEqual(result.record.payload.amount_out.atoms, 1040000)
        assert result.record.payload.gas_evidence is not None
        self.assertEqual(result.record.payload.gas_evidence.gas_units, 125000)
        self.assertEqual(result.record.payload.gas_evidence.gas_kind, GasEvidenceKind.RPC_ESTIMATE)

    def test_negative_state_missing_block_hash(self) -> None:
        """Verify legacy state with price/block_number but missing block_hash fails with incomplete status."""
        raw = {
            "record_type": "state_version",
            "chain_id": self.chain_id,
            "block_number": 500000,
            "captured_at": 1725000000.0,
            "sqrt_price_x96": 18446744073709551616,
        }
        result = adapt_legacy_record(raw, self.context)
        self.assertFalse(result.success)
        self.assertEqual(result.status, "incomplete")
        self.assertIsNone(result.record)
        self.assertIn("block_hash", result.missing_fields)
        self.assertTrue(any("block_hash" in reason.lower() for reason in result.unresolved_reasons))

    def test_negative_pool_missing_factory(self) -> None:
        """Verify legacy pool missing factory address fails with incomplete status."""
        raw = {
            "record_type": "pool_descriptor",
            "chain_id": self.chain_id,
            "protocol": "uniswap_v3",
            "pool_id": "0x1111111111111111111111111111111111111111",
            "token0": "0x2222222222222222222222222222222222222222",
            "token1": "0x3333333333333333333333333333333333333333",
            "fee_bps": 5.0,
            "tick_spacing": 10,
        }
        result = adapt_legacy_record(raw, self.context)
        self.assertFalse(result.success)
        self.assertEqual(result.status, "incomplete")
        self.assertIsNone(result.record)
        self.assertIn("factory", result.missing_fields)

    def test_negative_quote_revert_with_amount_out_rejected(self) -> None:
        """Verify non-QUOTED status with non-null amount_out is rejected fail-closed."""
        raw = {
            "record_type": "quote",
            "quote_id": "quote-neg-001",
            "status": "CONTRACT_REVERT",
            "route": {
                "chain_id": self.chain_id,
                "base_token": "0x2222222222222222222222222222222222222222",
                "hops": [
                    {
                        "pool": {
                            "chain_id": self.chain_id,
                            "protocol": "uniswap_v3",
                            "pool_id": "0x1111111111111111111111111111111111111111",
                            "factory": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                        },
                        "token_in": "0x2222222222222222222222222222222222222222",
                        "token_out": "0x3333333333333333333333333333333333333333",
                    },
                    {
                        "pool": {
                            "chain_id": self.chain_id,
                            "protocol": "sushiswap",
                            "pool_id": "0x4444444444444444444444444444444444444444",
                            "factory": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                        },
                        "token_in": "0x3333333333333333333333333333333333333333",
                        "token_out": "0x2222222222222222222222222222222222222222",
                    },
                ],
            },
            "amount_in": {"atoms": "1000000", "decimals": 6},
            "amount_out": {"atoms": "1040000", "decimals": 6},
        }
        result = adapt_legacy_record(raw, self.context)
        self.assertFalse(result.success)
        self.assertIn(result.status, ("incomplete", "rejected"))
        self.assertIsNone(result.record)
        self.assertTrue(any("non-quoted" in r.lower() for r in result.unresolved_reasons))

    def test_missing_evidence_does_not_fabricate_zero_gas(self) -> None:
        """Verify missing gas info leaves gas_evidence=None and never fabricates 0Gas."""
        raw = {
            "record_type": "quote",
            "quote_id": "quote-pos-no-gas",
            "status": "QUOTED",
            "route": {
                "chain_id": self.chain_id,
                "base_token": "0x2222222222222222222222222222222222222222",
                "hops": [
                    {
                        "pool": {
                            "chain_id": self.chain_id,
                            "protocol": "uniswap_v3",
                            "pool_id": "0x1111111111111111111111111111111111111111",
                            "factory": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                        },
                        "token_in": "0x2222222222222222222222222222222222222222",
                        "token_out": "0x3333333333333333333333333333333333333333",
                    },
                    {
                        "pool": {
                            "chain_id": self.chain_id,
                            "protocol": "sushiswap",
                            "pool_id": "0x4444444444444444444444444444444444444444",
                            "factory": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                        },
                        "token_in": "0x3333333333333333333333333333333333333333",
                        "token_out": "0x2222222222222222222222222222222222222222",
                    },
                ],
            },
            "amount_in": {"atoms": "1000000", "decimals": 6},
            "amount_out": {"atoms": "1040000", "decimals": 6},
        }
        result = adapt_legacy_record(raw, self.context)
        self.assertTrue(result.success)
        assert result.record is not None
        assert isinstance(result.record.payload, QuoteEvidence)
        self.assertIsNone(result.record.payload.gas_evidence)

    def test_state_version_completeness_ready_barrier(self) -> None:
        """Verify state version completeness cannot be 'ready' without complete_through_block."""
        raw = {
            "record_type": "state_version",
            "chain_id": self.chain_id,
            "block_domain": "l2",
            "block_number": 500000,
            "block_hash": "0x1111222233334444555566667777888899990000aaaabbbbccccddddeeeeffff",
            "received_at_ms": 1725000001000,
            "block_timestamp_s": 1725000001,
            "completeness": "ready",
        }
        result = adapt_legacy_record(raw, self.context)
        self.assertFalse(result.success)
        self.assertEqual(result.status, "incomplete")
        self.assertIn("complete_through_block", result.missing_fields)

    def test_non_primitive_or_non_mapping_inputs_rejected(self) -> None:
        """Verify passing non-mapping or objects with custom classes raises TypeError fail-closed."""
        bad_str: Any = "not a mapping"
        bad_int: Any = 12345
        with self.assertRaises(TypeError):
            adapt_legacy_record(bad_str)
        with self.assertRaises(TypeError):
            adapt_legacy_record(bad_int)
        with self.assertRaises(TypeError):
            adapt_legacy_record(
                {"token": TokenKey(1, "0x1111111111111111111111111111111111111111")}
            )

    def test_fixture_file_parity(self) -> None:
        """Verify all test cases in tests/fixtures/contracts/v1/legacy.jsonl produce expected outcomes."""
        fixture_path = (
            Path(__file__).resolve().parent.parent
            / "fixtures"
            / "contracts"
            / "v1"
            / "legacy.jsonl"
        )
        self.assertTrue(fixture_path.is_file(), f"Fixture file not found: {fixture_path}")

        with open(fixture_path, encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                if not line.strip():
                    continue
                data = json.loads(line)
                case_id = data["case_id"]
                expected_status = data["expected_status"]
                res = adapt_legacy_record(data, self.context)
                if expected_status == "complete":
                    self.assertTrue(
                        res.success,
                        f"Case {case_id} on line {line_no} failed: {res.unresolved_reasons}",
                    )
                    self.assertEqual(res.status, "complete")
                    self.assertIsNotNone(res.record)
                else:
                    self.assertFalse(
                        res.success,
                        f"Case {case_id} on line {line_no} was expected to fail but succeeded",
                    )
                    self.assertIsNone(res.record)
                    self.assertIn(res.status, ("incomplete", "rejected"))

    def test_subprocess_isolation_no_legacy_imports(self) -> None:
        """A27: Verify importing legacy_adapter in clean process loads zero legacy modules."""
        script_code = """
import sys
from pathlib import Path

project_root = Path('.').resolve()
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import arbitrage_contracts.legacy_adapter as la

forbidden_prefixes = (
    "apps",
    "backtest",
    "chains",
    "core",
    "execution",
    "monitors",
)
forbidden_exact = {"arbitrage"}

leaks = []
for name in sorted(sys.modules.keys()):
    if name in forbidden_exact:
        leaks.append(name)
    elif name.startswith("arbitrage."):
        leaks.append(name)
    elif any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden_prefixes):
        leaks.append(name)

if leaks:
    print(f"FORBIDDEN_LEAKS:{leaks}")
    sys.exit(1)

print("ADAPTER_ISOLATION_OK")
sys.exit(0)
"""
        proc = subprocess.run(
            [sys.executable, "-I", "-c", script_code],
            cwd=Path(__file__).resolve().parent.parent.parent,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"Legacy adapter isolation failed: {proc.stderr} {proc.stdout}",
        )
        self.assertIn("ADAPTER_ISOLATION_OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
