"""Arc event log normalization, EIP-7708 deduplication, and movement journal."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from arc_readiness.errors import ArcEventDeduplicationError, ArcValidationError
from arc_readiness.models import (
    ARC_SYSTEM_TRANSFER_EMITTER,
    ARC_USDC_ERC20_ADDRESS,
    ArcEventRecordDraft,
    validate_address,
)

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
SCALE_FACTOR = 1_000_000_000_000  # 10^12


def parse_raw_event_log(raw_log: Mapping[str, Any], chain_id: int) -> ArcEventRecordDraft | None:
    """Parse a raw JSON-RPC event log dictionary into an ArcEventRecordDraft.

    Returns None if the emitter or topic is not recognized as an Arc USDC event.
    """
    if raw_log.get("removed", False) is True:
        raise ArcValidationError(
            "Removed event log detected: reorganizations must not be parsed directly"
        )

    raw_addr = raw_log.get("address")
    if not isinstance(raw_addr, str):
        return None

    try:
        emitter = validate_address(raw_addr, "address")
    except ArcValidationError:
        return None

    # Only process recognized Arc USDC emitters
    if emitter not in (ARC_SYSTEM_TRANSFER_EMITTER, ARC_USDC_ERC20_ADDRESS):
        return None

    topics = raw_log.get("topics")
    if not isinstance(topics, (list, tuple)) or len(topics) < 3:
        return None

    topic0 = str(topics[0]).lower()
    if topic0 != TRANSFER_TOPIC:
        return None

    topic1 = str(topics[1]).lower()
    topic2 = str(topics[2]).lower()
    from_addr = "0x" + topic1[-40:]
    to_addr = "0x" + topic2[-40:]

    data_field = raw_log.get("data", "0x0")
    if isinstance(data_field, str):
        raw_val = int(data_field, 16) if data_field.startswith("0x") else int(data_field)
    elif isinstance(data_field, int):
        raw_val = data_field
    else:
        raise ArcValidationError(f"Invalid data field in raw log: {data_field!r}")

    is_system = emitter == ARC_SYSTEM_TRANSFER_EMITTER
    decimals_view = 18 if is_system else 6

    # Determine event semantic type
    if from_addr == ZERO_ADDRESS:
        event_type = "Mint"
    elif to_addr == ZERO_ADDRESS:
        event_type = "Burn"
    else:
        event_type = "Transfer"

    raw_block_num = raw_log.get("blockNumber", 0)
    if isinstance(raw_block_num, str) and raw_block_num.startswith("0x"):
        block_num = int(raw_block_num, 16)
    else:
        block_num = int(raw_block_num or 0)

    raw_log_index = raw_log.get("logIndex", 0)
    if isinstance(raw_log_index, str) and raw_log_index.startswith("0x"):
        log_index = int(raw_log_index, 16)
    else:
        log_index = int(raw_log_index or 0)

    block_hash = str(raw_log.get("blockHash", "0x" + "00" * 32))
    tx_hash = str(raw_log.get("transactionHash", "0x" + "00" * 32))

    return ArcEventRecordDraft(
        chain_id=chain_id,
        block_number=block_num,
        block_hash=block_hash,
        tx_hash=tx_hash,
        log_index=log_index,
        emitter_address=emitter,
        event_type=event_type,
        from_address=from_addr,
        to_address=to_addr,
        raw_value_atoms=raw_val,
        decimals_view=decimals_view,
        is_system_emitter=is_system,
    )


def is_ignorable_zero_or_self_transfer(from_addr: str, to_addr: str, value_atoms: int) -> bool:
    """Arc protocol rule: native zero-value transfers and self-transfers (from == to) emit no log."""
    return value_atoms == 0 or (from_addr.lower() == to_addr.lower())


def deduplicate_transaction_events(
    events: Sequence[ArcEventRecordDraft],
) -> list[ArcEventRecordDraft]:
    """Deduplicate dual-emitted Transfer events within transactions.

    Arc's EIP-7708 emits an 18-decimal system Transfer for EVERY USDC movement,
    while the ERC-20 contract additionally emits a 6-decimal Transfer for contract-level calls.
    This function collapses matched (system 18d, ERC-20 6d) pairs into the canonical 18-decimal system log.

    Preservation rule:
    Multiple identical transfers within the same transaction (e.g. paying the same address twice)
    are strictly preserved so long as their log indices / system emitters represent distinct actions.
    """
    system_events: list[ArcEventRecordDraft] = []
    erc20_events: list[ArcEventRecordDraft] = []

    for evt in events:
        if evt.is_system_emitter:
            system_events.append(evt)
        else:
            erc20_events.append(evt)

    # If there are no system events or no ERC20 events, no dual-logging collapse needed
    if not system_events:
        return list(events)
    if not erc20_events:
        return list(system_events)

    # Index system events by (tx_hash, from_address, to_address, erc20_equivalent_amount)
    unmatched_erc20: list[ArcEventRecordDraft] = []
    matched_system_indices: set[int] = set()

    for erc_evt in erc20_events:
        matched = False
        for sys_idx, sys_evt in enumerate(system_events):
            if sys_idx in matched_system_indices:
                continue
            if (
                sys_evt.tx_hash == erc_evt.tx_hash
                and sys_evt.from_address == erc_evt.from_address
                and sys_evt.to_address == erc_evt.to_address
                and (sys_evt.raw_value_atoms // SCALE_FACTOR) == erc_evt.raw_value_atoms
            ):
                # Matched: the system event supersedes the 6d ERC-20 log
                matched_system_indices.add(sys_idx)
                matched = True
                break
        if not matched:
            unmatched_erc20.append(erc_evt)

    # Canonical list contains all system events plus any unmatched standalone ERC-20 events
    result = list(system_events) + unmatched_erc20
    # Sort deterministically by block_number, log_index
    result.sort(key=lambda e: (e.block_number, e.log_index))
    return result


class ArcEventJournal:
    """Idempotent recording journal tracking processed events and rejecting duplicates."""

    def __init__(self) -> None:
        self._seen_keys: set[str] = set()
        self._records: list[ArcEventRecordDraft] = []

    @property
    def total_records(self) -> int:
        return len(self._records)

    def record_event(self, event: ArcEventRecordDraft, strict_raise: bool = True) -> bool:
        """Record an event idempotently. Returns True if accepted, False or raises if duplicate."""
        if event.idempotency_key in self._seen_keys:
            if strict_raise:
                raise ArcEventDeduplicationError(
                    f"Duplicate event rejected: idempotency_key={event.idempotency_key}"
                )
            return False

        self._seen_keys.add(event.idempotency_key)
        self._records.append(event)
        return True

    def record_batch(
        self, events: Sequence[ArcEventRecordDraft], strict_raise: bool = False
    ) -> int:
        """Record a batch of events. Returns the count of successfully recorded non-duplicate events."""
        added = 0
        for evt in events:
            if self.record_event(evt, strict_raise=strict_raise):
                added += 1
        return added

    def get_records(self) -> tuple[ArcEventRecordDraft, ...]:
        return tuple(self._records)
