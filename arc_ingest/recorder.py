"""Raw block and log recorder with strict write-then-cursor ordering and replay capabilities."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from arc_ingest.cursors import CursorStore, DurableCursor
from arc_ingest.segments import SegmentManager
from arc_readiness.errors import ArcValidationError
from arbitrage_contracts.arc_extensions import BlockDomain, RawEnvelope


class RawDataRecorder:
    """Ingests raw block headers and log events into bounded, fsynced segments.

    Enforces:
    - Data is written and confirmed on disk BEFORE advancing the durable cursor.
    - Zero data loss on partial writes or crashes.
    - Explicit envelope packaging with BlockDomain.L1 and durable cursor pointers.
    """

    def __init__(
        self,
        base_dir: str | Path,
        chain_id: int = 5042,
        segment_size_blocks: int = 1000,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.chain_id = chain_id
        self.segment_manager = SegmentManager(
            base_dir=self.base_dir / "segments",
            segment_size_blocks=segment_size_blocks,
        )
        self.cursor_store = CursorStore(
            cursor_file=self.base_dir / "cursor.json",
            chain_id=self.chain_id,
        )

    @property
    def latest_cursor(self) -> DurableCursor | None:
        return self.cursor_store.current

    def record_block(
        self,
        block_header: dict[str, Any],
        logs: Sequence[dict[str, Any]] = (),
        payload_type: str = "block_with_logs",
    ) -> str:
        """Record a raw block and its associated logs into segment storage, then advance cursor."""
        try:
            b_num = int(block_header["number"], 16) if isinstance(block_header["number"], str) else int(block_header["number"])
            b_hash = str(block_header["hash"]).lower()
            b_ts = int(block_header["timestamp"], 16) if isinstance(block_header["timestamp"], str) else int(block_header["timestamp"])
        except (KeyError, ValueError) as e:
            raise ArcValidationError(f"Invalid block_header fields: {e}") from e

        cursor_str = f"arc:{self.chain_id}:{b_num}:{b_hash[:10]}"

        raw_payload = {
            "header": block_header,
            "logs": list(logs),
            "logs_count": len(logs),
            "is_empty_block": len(logs) == 0,
        }
        serialized_payload = json.dumps(raw_payload, separators=(",", ":"))

        # 1. Instantiate immutable RawEnvelope validating invariants
        envelope = RawEnvelope(
            chain_id=self.chain_id,
            block_domain=BlockDomain.L1,
            block_number=b_num,
            block_hash=b_hash,
            cursor=cursor_str,
            received_at=float(b_ts),
            payload_type=payload_type,
            raw_payload=serialized_payload,
            schema_version="1.0",
        )

        record_dict = {
            "chain_id": envelope.chain_id,
            "block_domain": str(envelope.block_domain),
            "block_number": envelope.block_number,
            "block_hash": envelope.block_hash,
            "cursor": envelope.cursor,
            "received_at": envelope.received_at,
            "payload_type": envelope.payload_type,
            "raw_payload": envelope.raw_payload,
            "schema_version": envelope.schema_version,
        }

        # 2. Write to disk and fsync via SegmentManager
        seg_id = self.segment_manager.append_record(b_num, record_dict)

        # 3. Only after confirmed disk write, advance the persistent cursor
        self.cursor_store.advance(
            block_number=b_num,
            block_hash=b_hash,
            explicit_timestamp=float(b_ts),
        )

        return seg_id

    def read_records(self, seg_id: str) -> list[dict[str, Any]]:
        """Read and verify all records in a designated segment."""
        self.segment_manager.verify_segment_integrity(seg_id)
        file_path = self.base_dir / "segments" / f"{seg_id}.jsonl"
        records = []
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped:
                    records.append(json.loads(stripped))
        return records
