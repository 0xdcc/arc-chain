"""Arc Data Collection Runtime (T37)

Provides record-only ingest logic for Arc Chain (5042 L1).
Decoupled from trading, signing, or monolithic pipeline code.
Enforces:
- Zero import side effects
- Explicit runtime profiles and block ranges
- Emits RawEnvelope and CoverageManifest contracts
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from arbitrage_contracts.arc_extensions import (
    BlockDomain,
    CoverageManifest,
    NetworkProfile,
    RawEnvelope,
)


@dataclass(frozen=True)
class CollectorConfig:
    """Configuration for arc_collect execution."""

    chain_id: int
    block_domain: BlockDomain
    from_block: int
    to_block: int
    output_dir: Path
    fixture_mode: bool = False
    fixture_path: Path | None = None
    rpc_endpoint: str | None = None
    schema_version: str = "1.0"

    def __post_init__(self) -> None:
        if self.chain_id not in (5042, 5042002):
            raise ValueError(f"Invalid chain_id: {self.chain_id}")
        if self.block_domain != BlockDomain.L1:
            raise ValueError(f"block_domain must be l1, got {self.block_domain}")
        if self.from_block > self.to_block:
            raise ValueError(f"from_block ({self.from_block}) cannot exceed to_block ({self.to_block})")
        if self.to_block - self.from_block + 1 > 1000:
            raise ValueError("Max block batch range is 1000 blocks per collect run")
        if not self.fixture_mode and not self.rpc_endpoint:
            raise ValueError("Real collection requires an explicit rpc_endpoint")


@dataclass(frozen=True)
class CollectorSummary:
    """Execution summary of collect operation."""

    chain_id: int
    from_block: int
    to_block: int
    envelopes_written: int
    output_jsonl: Path
    manifest_json: Path
    cursor_json: Path
    elapsed_seconds: float
    is_fixture_mode: bool


def execute_collection(config: CollectorConfig) -> CollectorSummary:
    """Execute block ingest and write raw envelopes with coverage proofs."""
    start_time = time.monotonic()
    config.output_dir.mkdir(parents=True, exist_ok=True)

    envelopes_file = config.output_dir / "raw_envelopes.jsonl"
    manifest_file = config.output_dir / "coverage_manifest.json"
    cursor_file = config.output_dir / "cursor.json"

    expected_count = config.to_block - config.from_block + 1
    envelopes: list[RawEnvelope] = []
    covered_blocks: list[int] = []

    if config.fixture_mode:
        # Generate or load deterministic synthetic fixture data
        for b_num in range(config.from_block, config.to_block + 1):
            block_hash = f"0x{b_num:064x}"
            cursor_str = f"cur_{config.chain_id}_{b_num}"
            received_at = time.time()
            raw_payload = json.dumps(
                {
                    "number": b_num,
                    "hash": block_hash,
                    "timestamp": int(received_at),
                    "transactions_count": 0,
                    "data_mode": "SYNTHETIC_FIXTURE",
                }
            )
            env = RawEnvelope(
                chain_id=config.chain_id,
                block_domain=config.block_domain,
                block_number=b_num,
                block_hash=block_hash,
                cursor=cursor_str,
                received_at=received_at,
                payload_type="block_summary",
                raw_payload=raw_payload,
                schema_version=config.schema_version,
            )
            envelopes.append(env)
            covered_blocks.append(b_num)
    else:
        # Real collection: requires authorized endpoint
        # For G1_CODE offline test, real execution without authorized live probe is rejected
        raise PermissionError(
            "Live RPC collection requires verified endpoint authorization (G1_LIVE). Use --fixture-mode for G1_CODE."
        )

    # 1. Write envelopes JSONL
    with open(envelopes_file, "w", encoding="utf-8") as f:
        for env in envelopes:
            record = {
                "chain_id": env.chain_id,
                "block_domain": str(env.block_domain),
                "block_number": env.block_number,
                "block_hash": env.block_hash,
                "cursor": env.cursor,
                "received_at": env.received_at,
                "payload_type": env.payload_type,
                "raw_payload": env.raw_payload,
                "schema_version": env.schema_version,
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # 2. Write Coverage Manifest
    manifest = CoverageManifest(
        chain_id=config.chain_id,
        from_block=config.from_block,
        to_block=config.to_block,
        expected_blocks=expected_count,
        covered_blocks=len(covered_blocks),
        missing_blocks=(),
        coverage_ratio=1.0,
        verified_at=time.time(),
    )
    with open(manifest_file, "w", encoding="utf-8") as f:
        json.dump(
            {
                "chain_id": manifest.chain_id,
                "from_block": manifest.from_block,
                "to_block": manifest.to_block,
                "expected_blocks": manifest.expected_blocks,
                "covered_blocks": manifest.covered_blocks,
                "missing_blocks": list(manifest.missing_blocks),
                "coverage_ratio": manifest.coverage_ratio,
                "verified_at": manifest.verified_at,
            },
            f,
            indent=2,
        )

    # 3. Write Durable Cursor
    cursor_data = {
        "chain_id": config.chain_id,
        "last_block": config.to_block,
        "last_cursor": envelopes[-1].cursor if envelopes else None,
        "updated_at": time.time(),
    }
    with open(cursor_file, "w", encoding="utf-8") as f:
        json.dump(cursor_data, f, indent=2)

    elapsed = time.monotonic() - start_time
    return CollectorSummary(
        chain_id=config.chain_id,
        from_block=config.from_block,
        to_block=config.to_block,
        envelopes_written=len(envelopes),
        output_jsonl=envelopes_file,
        manifest_json=manifest_file,
        cursor_json=cursor_file,
        elapsed_seconds=elapsed,
        is_fixture_mode=config.fixture_mode,
    )
