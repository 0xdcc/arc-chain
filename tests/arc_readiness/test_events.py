"""Tests covering Arc event log normalization, EIP-7708 deduplication, and movement journal (C07-C11)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arc_readiness.errors import ArcEventDeduplicationError, ArcValidationError
from arc_readiness.events import (
    ARC_SYSTEM_TRANSFER_EMITTER,
    ARC_USDC_ERC20_ADDRESS,
    TRANSFER_TOPIC,
    ZERO_ADDRESS,
    ArcEventJournal,
    deduplicate_transaction_events,
    is_ignorable_zero_or_self_transfer,
    parse_raw_event_log,
)
from arc_readiness.models import ARC_TESTNET_CHAIN_ID, ArcEventRecordDraft

TEST_TX_HASH = "0x" + "bb" * 32
TEST_BLOCK_HASH = "0x" + "aa" * 32
ADDR_ALICE = "0x" + "11" * 20
ADDR_BOB = "0x" + "22" * 20


# ==============================================================================
# C07: Dual EIP-7708 System Log and ERC-20 Deduplication
# ==============================================================================


def test_c07_dual_emitted_erc20_transfer_deduplication() -> None:
    """C07: A single ERC-20 transfer emits both 18d system and 6d contract logs. Collapses to 1 18d event."""
    # 1. System emitter log: 1.0 USDC (10^18 atoms)
    system_log = ArcEventRecordDraft(
        chain_id=ARC_TESTNET_CHAIN_ID,
        block_number=100,
        block_hash=TEST_BLOCK_HASH,
        tx_hash=TEST_TX_HASH,
        log_index=0,
        emitter_address=ARC_SYSTEM_TRANSFER_EMITTER,
        event_type="Transfer",
        from_address=ADDR_ALICE,
        to_address=ADDR_BOB,
        raw_value_atoms=10**18,
        decimals_view=18,
        is_system_emitter=True,
    )

    # 2. ERC-20 contract log: 1.0 USDC (10^6 atoms)
    erc20_log = ArcEventRecordDraft(
        chain_id=ARC_TESTNET_CHAIN_ID,
        block_number=100,
        block_hash=TEST_BLOCK_HASH,
        tx_hash=TEST_TX_HASH,
        log_index=1,
        emitter_address=ARC_USDC_ERC20_ADDRESS,
        event_type="Transfer",
        from_address=ADDR_ALICE,
        to_address=ADDR_BOB,
        raw_value_atoms=10**6,
        decimals_view=6,
        is_system_emitter=False,
    )

    raw_events = [system_log, erc20_log]
    deduped = deduplicate_transaction_events(raw_events)

    assert len(deduped) == 1, "Must collapse paired system and ERC-20 logs into exactly 1 event"
    retained = deduped[0]
    assert retained.is_system_emitter is True
    assert retained.decimals_view == 18
    assert retained.raw_value_atoms == 10**18
    assert retained.from_address == ADDR_ALICE
    assert retained.to_address == ADDR_BOB

    # Anti-double-counting assertion:
    # A naive engine summing raw_value_atoms from both logs would falsely report 10^18 + 10^6 atoms
    naive_sum = sum(e.raw_value_atoms for e in raw_events)
    assert naive_sum != 10**18, "Un-deduplicated logs must fail sanity check"


def test_c07_unrecognized_emitter_skipped() -> None:
    """C07: Events from unknown emitters with the same Transfer topic are not recognized as USDC."""
    fake_log = {
        "address": "0x" + "99" * 20,
        "topics": [TRANSFER_TOPIC, "0x" + "00" * 12 + "11" * 20, "0x" + "00" * 12 + "22" * 20],
        "data": "0x100",
    }
    res = parse_raw_event_log(fake_log, chain_id=ARC_TESTNET_CHAIN_ID)
    assert res is None


# ==============================================================================
# C08: Two Legitimate Identical Transfers in Same Tx & Idempotent Journal
# ==============================================================================


def test_c08_two_identical_transfers_in_same_tx_preserved() -> None:
    """C08: Two distinct transfers of identical amount from Alice to Bob in one tx must both be kept."""
    tx_hash = "0x" + "cc" * 32

    # Two legitimate transfers of 50 USDC in the same transaction
    tx_event_1 = ArcEventRecordDraft(
        chain_id=ARC_TESTNET_CHAIN_ID,
        block_number=200,
        block_hash=TEST_BLOCK_HASH,
        tx_hash=tx_hash,
        log_index=0,
        emitter_address=ARC_SYSTEM_TRANSFER_EMITTER,
        event_type="Transfer",
        from_address=ADDR_ALICE,
        to_address=ADDR_BOB,
        raw_value_atoms=50 * 10**18,
        decimals_view=18,
        is_system_emitter=True,
    )
    tx_event_2 = ArcEventRecordDraft(
        chain_id=ARC_TESTNET_CHAIN_ID,
        block_number=200,
        block_hash=TEST_BLOCK_HASH,
        tx_hash=tx_hash,
        log_index=1,
        emitter_address=ARC_SYSTEM_TRANSFER_EMITTER,
        event_type="Transfer",
        from_address=ADDR_ALICE,
        to_address=ADDR_BOB,
        raw_value_atoms=50 * 10**18,
        decimals_view=18,
        is_system_emitter=True,
    )

    deduped = deduplicate_transaction_events([tx_event_1, tx_event_2])
    assert len(deduped) == 2, "Must preserve both distinct transfers with different log_index"
    assert deduped[0].log_index == 0
    assert deduped[1].log_index == 1


def test_c08_idempotent_journal_replay_rejection() -> None:
    """C08: Replaying identical event records into the journal produces zero increments."""
    journal = ArcEventJournal()

    event = ArcEventRecordDraft(
        chain_id=ARC_TESTNET_CHAIN_ID,
        block_number=201,
        block_hash=TEST_BLOCK_HASH,
        tx_hash="0x" + "dd" * 32,
        log_index=0,
        emitter_address=ARC_SYSTEM_TRANSFER_EMITTER,
        event_type="Transfer",
        from_address=ADDR_ALICE,
        to_address=ADDR_BOB,
        raw_value_atoms=10**18,
        decimals_view=18,
        is_system_emitter=True,
    )

    # First record succeeds
    assert journal.record_event(event, strict_raise=True) is True
    assert journal.total_records == 1

    # Second record with strict_raise raises error
    with pytest.raises(ArcEventDeduplicationError, match="Duplicate event rejected"):
        journal.record_event(event, strict_raise=True)

    # Batch record skips duplicates safely
    added = journal.record_batch([event, event], strict_raise=False)
    assert added == 0
    assert journal.total_records == 1


# ==============================================================================
# C09: Corrupt / Reorg Event Rejection
# ==============================================================================


def test_c09_removed_log_rejected() -> None:
    """C09: Logs flagged with removed=True (chain reorganization) fail-closed."""
    corrupt_log = {
        "removed": True,
        "address": ARC_SYSTEM_TRANSFER_EMITTER,
        "topics": [TRANSFER_TOPIC, "0x" + "00" * 12 + "11" * 20, "0x" + "00" * 12 + "22" * 20],
        "data": "0x100",
    }
    with pytest.raises(ArcValidationError, match="Removed event log detected"):
        parse_raw_event_log(corrupt_log, chain_id=ARC_TESTNET_CHAIN_ID)


# ==============================================================================
# C10: Zero-value and Self-transfer Logless Rules
# ==============================================================================


def test_c10_zero_and_self_transfers() -> None:
    """C10: In Arc protocol, native zero-value transfers and self-transfers emit no log."""
    assert is_ignorable_zero_or_self_transfer(ADDR_ALICE, ADDR_ALICE, 1000) is True
    assert is_ignorable_zero_or_self_transfer(ADDR_ALICE, ADDR_BOB, 0) is True
    assert is_ignorable_zero_or_self_transfer(ADDR_ALICE, ADDR_BOB, 1000) is False


# ==============================================================================
# C11: Mint and Burn Semantics
# ==============================================================================


def test_c11_mint_and_burn_identification() -> None:
    """C11: Mint and Burn are identified by 0x0 address and must not be counted as arbitrage profit."""
    mint_log = {
        "address": ARC_SYSTEM_TRANSFER_EMITTER,
        "topics": [
            TRANSFER_TOPIC,
            "0x" + "00" * 32,  # From 0x0
            "0x" + "00" * 12 + "11" * 20,  # To Alice
        ],
        "data": hex(10**18),
        "blockNumber": "0x10",
        "blockHash": TEST_BLOCK_HASH,
        "transactionHash": TEST_TX_HASH,
        "logIndex": "0x0",
    }
    parsed_mint = parse_raw_event_log(mint_log, chain_id=ARC_TESTNET_CHAIN_ID)
    assert parsed_mint is not None
    assert parsed_mint.event_type == "Mint"
    assert parsed_mint.from_address == ZERO_ADDRESS

    burn_log = {
        "address": ARC_SYSTEM_TRANSFER_EMITTER,
        "topics": [
            TRANSFER_TOPIC,
            "0x" + "00" * 12 + "11" * 20,  # From Alice
            "0x" + "00" * 32,  # To 0x0
        ],
        "data": hex(10**18),
        "blockNumber": "0x10",
        "blockHash": TEST_BLOCK_HASH,
        "transactionHash": TEST_TX_HASH,
        "logIndex": "0x1",
    }
    parsed_burn = parse_raw_event_log(burn_log, chain_id=ARC_TESTNET_CHAIN_ID)
    assert parsed_burn is not None
    assert parsed_burn.event_type == "Burn"
    assert parsed_burn.to_address == ZERO_ADDRESS


# ==============================================================================
# Fixture-driven Verification
# ==============================================================================


def test_fixture_driven_event_vectors() -> None:
    """Verify test scenarios loaded from event_vectors.json fixture."""
    fixture_path = (
        Path(__file__).resolve().parents[1] / "fixtures" / "arc_readiness" / "event_vectors.json"
    )
    data = json.loads(fixture_path.read_text(encoding="utf-8"))

    for vec in data["vectors"]:
        parsed_logs: list[ArcEventRecordDraft] = []
        for raw in vec["logs"]:
            parsed = parse_raw_event_log(raw, chain_id=ARC_TESTNET_CHAIN_ID)
            assert parsed is not None, f"Failed to parse log in {vec['id']}"
            parsed_logs.append(parsed)

        deduped = deduplicate_transaction_events(parsed_logs)
        if "expected_collapsed_count" in vec:
            assert len(deduped) == vec["expected_collapsed_count"], f"Mismatch in {vec['id']}"
        if "expected_raw_value_atoms" in vec:
            assert deduped[0].raw_value_atoms == vec["expected_raw_value_atoms"], (
                f"Value mismatch in {vec['id']}"
            )
