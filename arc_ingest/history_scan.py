"""Historical continuous block range scanner with RPC budget guards and circuit breakers."""

from __future__ import annotations

import json
import time
from typing import Any

from arbitrage_contracts.arc_extensions import BlockDomain, RawEnvelope
from arc_ingest.coverage import CoverageManifest
from arc_ingest.recorder import RawDataRecorder
from arc_readiness.errors import ArcValidationError
from arc_readiness.http_readonly import ArcCircuitBreakerTrippedError
from arc_readiness.rpc_readonly import ReadOnlyRpcTransport

MAX_BATCH_BLOCKS: int = 100
MAX_CONSECUTIVE_FAILURES: int = 3


class HistoricalBlockScanner:
    """Scans and records historical block intervals with explicit batching and rate guards."""

    def __init__(
        self,
        transport: ReadOnlyRpcTransport,
        recorder: RawDataRecorder,
        coverage: CoverageManifest | None = None,
        max_batch_size: int = MAX_BATCH_BLOCKS,
    ) -> None:
        self.transport = transport
        self.recorder = recorder
        self.coverage = coverage or CoverageManifest(chain_id=recorder.chain_id)
        self.max_batch_size = max_batch_size
        self._consecutive_failures = 0

    def scan_block(self, block_number: int) -> dict[str, Any]:
        """Fetch and record a single block header and its logs."""
        hex_block = hex(block_number)
        received_at = time.time()  # Real current observation time

        try:
            block_data = self.transport.request("eth_getBlockByNumber", [hex_block, False])
            if not block_data or not isinstance(block_data, dict):
                raise ArcValidationError(f"Null or empty block returned for height {block_number}")

            logs_data = self.transport.request(
                "eth_getLogs",
                [{"fromBlock": hex_block, "toBlock": hex_block}],
            )
            logs = logs_data if isinstance(logs_data, list) else []

            # Tag envelope with metadata
            block_data["_received_at"] = received_at
            b_hash = str(block_data.get("hash", "")).lower()

            curr_cursor = self.recorder.cursor_store.current
            if curr_cursor is not None and block_number < curr_cursor.block_number:
                # Historical gap insertion: write directly to segment without regressing head cursor
                cursor_str = f"arc:{self.recorder.chain_id}:{block_number}:{b_hash[:10]}"
                b_ts = int(block_data["timestamp"], 16) if isinstance(block_data["timestamp"], str) else int(block_data["timestamp"])
                raw_payload = {
                    "header": block_data,
                    "logs": list(logs),
                    "logs_count": len(logs),
                    "is_empty_block": len(logs) == 0,
                }
                serialized = json.dumps(raw_payload, separators=(",", ":"))
                envelope = RawEnvelope(
                    chain_id=self.recorder.chain_id,
                    block_domain=BlockDomain.L1,
                    block_number=block_number,
                    block_hash=b_hash,
                    cursor=cursor_str,
                    received_at=float(b_ts),
                    payload_type="block_with_logs",
                    raw_payload=serialized,
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
                self.recorder.segment_manager.append_record(block_number, record_dict)
            else:
                self.recorder.record_block(block_data, logs)

            self.coverage.add_block(block_number, b_hash)

            self._consecutive_failures = 0
            return block_data

        except Exception as e:
            self._consecutive_failures += 1
            if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                raise ArcCircuitBreakerTrippedError(
                    f"Historical scanner halted: {self._consecutive_failures} consecutive RPC failures. "
                    f"Last error: {e}"
                ) from e
            raise

    def scan_range(self, from_block: int, to_block: int) -> int:
        """Scan a continuous range of blocks [from_block, to_block].

        Invariants:
        - from_block >= 0
        - to_block >= from_block
        - total blocks <= batch budget
        - 3 consecutive RPC failures halts with ArcCircuitBreakerTrippedError
        """
        if from_block < 0:
            raise ArcValidationError(f"from_block cannot be negative: {from_block}")
        if to_block < from_block:
            raise ArcValidationError(
                f"Invalid scan range: from_block ({from_block}) > to_block ({to_block})"
            )

        count = to_block - from_block + 1
        if count > self.max_batch_size:
            raise ArcValidationError(
                f"Scan range {count} blocks exceeds max batch limit {self.max_batch_size}"
            )

        scanned_count = 0
        for b in range(from_block, to_block + 1):
            self.scan_block(b)
            scanned_count += 1

        return scanned_count
