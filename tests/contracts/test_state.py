"""Unit tests for state versioning, consistency barriers, and block cursor contracts."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from arbitrage_contracts.state import (
    CompletenessBarrier,
    Cursor,
    FinalityStatus,
    StateVersion,
)


class TestCursor(unittest.TestCase):
    """Test suite verifying Cursor format, invariants, and safety."""

    def test_valid_cursor_creation(self) -> None:
        """Verify creation of a valid Cursor with block, transaction, and log offsets."""
        block_hash = "0x" + "11" * 32
        tx_hash = "0x" + "22" * 32
        cursor = Cursor(
            block_hash=block_hash,
            transaction_hash=tx_hash,
            transaction_index=5,
            log_index=12,
        )
        self.assertEqual(cursor.block_hash, block_hash)
        self.assertEqual(cursor.transaction_hash, tx_hash)
        self.assertEqual(cursor.transaction_index, 5)
        self.assertEqual(cursor.log_index, 12)

    def test_rejection_of_invalid_cursor_hashes(self) -> None:
        """Verify truncated or malformed block/tx hashes are rejected."""
        valid_hash = "0x" + "11" * 32
        for invalid_hash in [valid_hash[:-1], valid_hash + "0", "0X" + "11" * 32, "11" * 32]:
            with self.assertRaises(ValueError):
                Cursor(block_hash=invalid_hash)
            with self.assertRaises(ValueError):
                Cursor(block_hash=valid_hash, transaction_hash=invalid_hash)

    def test_rejection_of_negative_or_boolean_indices(self) -> None:
        """Verify negative integers or boolean values are rejected for transaction and log indices."""
        valid_hash = "0x" + "11" * 32
        with self.assertRaises(ValueError):
            Cursor(block_hash=valid_hash, transaction_index=-1)
        with self.assertRaises(ValueError):
            Cursor(block_hash=valid_hash, log_index=-1)
        with self.assertRaises(TypeError):
            Cursor(block_hash=valid_hash, transaction_index=True)
        with self.assertRaises(TypeError):
            Cursor(block_hash=valid_hash, log_index=False)


class TestStateVersion(unittest.TestCase):
    """Test suite verifying StateVersion reorg detection, completeness barriers, and immutability."""

    def setUp(self) -> None:
        self.block_hash_alpha = "0x" + "aa" * 32
        self.block_hash_beta = "0x" + "bb" * 32

    def test_reorg_detection_same_height_different_hash(self) -> None:
        """Verify two versions at identical block number with different hashes trigger reorg invalidation."""
        state_alpha = StateVersion(
            chain_id=1,
            block_number=1000,
            block_hash=self.block_hash_alpha,
            received_at_ms=1700000000000,
        )
        state_beta = StateVersion(
            chain_id=1,
            block_number=1000,
            block_hash=self.block_hash_beta,
            received_at_ms=1700000000500,
        )
        self.assertTrue(state_alpha.is_reorg_of(state_beta))
        self.assertTrue(state_beta.is_reorg_of(state_alpha))

    def test_reorg_not_triggered_for_matching_or_different_heights(self) -> None:
        """Verify reorg is not flagged when hashes match or when block heights/chains differ."""
        state_base = StateVersion(
            chain_id=1,
            block_number=1000,
            block_hash=self.block_hash_alpha,
            received_at_ms=1700000000000,
        )
        state_identical = StateVersion(
            chain_id=1,
            block_number=1000,
            block_hash=self.block_hash_alpha,
            received_at_ms=1700000000100,
        )
        state_next_block = StateVersion(
            chain_id=1,
            block_number=1001,
            block_hash=self.block_hash_beta,
            received_at_ms=1700000001000,
        )
        state_diff_chain = StateVersion(
            chain_id=56,
            block_number=1000,
            block_hash=self.block_hash_beta,
            received_at_ms=1700000000000,
        )
        self.assertFalse(state_base.is_reorg_of(state_identical))
        self.assertFalse(state_base.is_reorg_of(state_next_block))
        self.assertFalse(state_base.is_reorg_of(state_diff_chain))

    def test_completeness_ready_barrier_invariants(self) -> None:
        """Verify completeness='ready' requires complete_through_block >= block_number."""
        with self.assertRaises(ValueError, msg="Ready without complete_through_block must fail"):
            StateVersion(
                chain_id=1,
                block_number=1000,
                block_hash=self.block_hash_alpha,
                received_at_ms=1700000000000,
                completeness=CompletenessBarrier.READY,
                complete_through_block=None,
            )

        with self.assertRaises(
            ValueError, msg="Ready with complete_through_block < block_number must fail"
        ):
            StateVersion(
                chain_id=1,
                block_number=1000,
                block_hash=self.block_hash_alpha,
                received_at_ms=1700000000000,
                completeness=CompletenessBarrier.READY,
                complete_through_block=999,
            )

        ready_state = StateVersion(
            chain_id=1,
            block_number=1000,
            block_hash=self.block_hash_alpha,
            received_at_ms=1700000000000,
            completeness=CompletenessBarrier.READY,
            complete_through_block=1000,
        )
        self.assertTrue(ready_state.is_ready())

    def test_block_domain_l1_l2_separation(self) -> None:
        """Verify block_domain accepts l1 and l2, and rejects unknown domains."""
        l1_state = StateVersion(
            chain_id=1,
            block_number=1000,
            block_hash=self.block_hash_alpha,
            received_at_ms=1700000000000,
            block_domain="l1",
        )
        self.assertEqual(l1_state.block_domain, "l1")

        with self.assertRaises(ValueError):
            StateVersion(
                chain_id=1,
                block_number=1000,
                block_hash=self.block_hash_alpha,
                received_at_ms=1700000000000,
                block_domain="l3",
            )

    def test_defensive_tuple_copying_and_immutability(self) -> None:
        """Verify coverage and stale_reasons are defensively copied to immutable tuples."""
        coverage_list = ["range_0_100", "pool_v3_usdc_eth"]
        stale_list = ["rpc_lag"]
        state = StateVersion(
            chain_id=1,
            block_number=1000,
            block_hash=self.block_hash_alpha,
            received_at_ms=1700000000000,
            coverage=coverage_list,
            stale_reasons=stale_list,
        )
        self.assertEqual(state.coverage, ("range_0_100", "pool_v3_usdc_eth"))
        coverage_list.append("range_100_200")
        self.assertEqual(state.coverage, ("range_0_100", "pool_v3_usdc_eth"))

        with self.assertRaises((FrozenInstanceError, AttributeError)):
            state.block_number = 1001  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
