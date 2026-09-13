"""Arc Data Collection Runtime (T37)

Provides record-only ingest logic for Arc Chain (5042 L1).
Decoupled from trading, signing, or monolithic pipeline code.
Enforces:
- Zero import side effects
- Explicit runtime profiles and block ranges
- Emits RawEnvelope and CoverageManifest contracts
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from arbitrage_contracts.arc_extensions import (
    BlockDomain,
    CoverageManifest,
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
            raise ValueError(
                f"from_block ({self.from_block}) cannot exceed to_block ({self.to_block})"
            )
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


def _reject_path_symlinks(path: Path) -> None:
    """Ensure neither the target path nor any ancestor directory is a symlink."""
    abs_norm = (Path.cwd() / path) if not path.is_absolute() else path
    abs_norm = Path(os.path.normpath(str(abs_norm)))
    for part in [abs_norm] + list(abs_norm.parents):
        if part.is_symlink() or os.path.islink(part):
            raise PermissionError(f"Symlink detected in path component: {part}")


@contextmanager
def _locked_output_dir(dir_path: Path):
    """Acquire an exclusive process lock on an output directory."""
    _reject_path_symlinks(dir_path)
    dir_path.mkdir(parents=True, exist_ok=True)
    lock_file = dir_path / ".lock"
    _reject_path_symlinks(lock_file)
    fd = os.open(lock_file, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def execute_collection(config: CollectorConfig) -> CollectorSummary:
    """Execute block ingest and write raw envelopes with coverage proofs."""
    start_time = time.monotonic()

    if not config.fixture_mode:
        # Reject unapproved live mode before *any* filesystem mutation. This is
        # intentionally earlier than mkdir()/ledger setup so a denied live
        # probe cannot leave evidence-looking directories behind.
        raise PermissionError(
            "Live RPC collection requires verified endpoint authorization (G1_LIVE). Use --fixture-mode for G1_CODE."
        )

    _reject_path_symlinks(config.output_dir)

    with _locked_output_dir(config.output_dir):
        envelopes_file = config.output_dir / "raw_envelopes.jsonl"
        manifest_file = config.output_dir / "coverage_manifest.json"
        cursor_file = config.output_dir / "cursor.json"
        protected_outputs = (envelopes_file, manifest_file, cursor_file)

        for p in protected_outputs:
            if p.is_symlink() or os.path.islink(p):
                raise FileExistsError(f"Refusing to overwrite existing collection evidence: {p}")

        existing_outputs = [p for p in protected_outputs if os.path.lexists(p)]

        is_continuation = False
        initial_from_block = config.from_block
        accumulated_covered_blocks = 0

        if len(existing_outputs) == 3 and all(p.is_file() for p in existing_outputs):
            # All 3 files exist as regular files; validate complete continuity and chain consistency
            try:
                with open(cursor_file, encoding="utf-8") as f:
                    cursor_data = json.load(f)
                if not isinstance(cursor_data, dict):
                    raise ValueError("cursor.json is not a valid JSON object")

                if cursor_data.get("chain_id") != config.chain_id:
                    raise ValueError(
                        f"Cursor chain_id mismatch: {cursor_data.get('chain_id')} != {config.chain_id}"
                    )
                if "block_domain" in cursor_data and cursor_data["block_domain"] != str(
                    config.block_domain
                ):
                    raise ValueError(
                        f"Cursor block_domain mismatch: {cursor_data.get('block_domain')} != {config.block_domain}"
                    )
                if (
                    "is_fixture_mode" in cursor_data
                    and cursor_data["is_fixture_mode"] != config.fixture_mode
                ):
                    raise ValueError("Cursor fixture_mode mismatch")

                last_block = cursor_data.get("last_block")
                if not isinstance(last_block, int):
                    raise ValueError("Cursor last_block is not an integer")

                if config.from_block != last_block + 1:
                    raise ValueError(
                        f"Non-monotonic range continuation: expected from_block {last_block + 1}, got {config.from_block}"
                    )

                with open(manifest_file, encoding="utf-8") as f:
                    manifest_data = json.load(f)
                if not isinstance(manifest_data, dict):
                    raise ValueError("coverage_manifest.json is not a valid JSON object")

                if manifest_data.get("chain_id") != config.chain_id:
                    raise ValueError("Manifest chain_id mismatch")

                m_from = manifest_data.get("from_block")
                m_to = manifest_data.get("to_block")
                m_covered = manifest_data.get("covered_blocks")
                m_expected = manifest_data.get("expected_blocks")

                if not isinstance(m_from, int) or not isinstance(m_to, int):
                    raise ValueError("Manifest block range values are not integers")
                if m_to != last_block:
                    raise ValueError(f"Manifest to_block {m_to} != cursor last_block {last_block}")
                if m_from > m_to:
                    raise ValueError(f"Invalid manifest range: {m_from}..{m_to}")
                if m_expected != (m_to - m_from + 1):
                    raise ValueError("Manifest expected_blocks does not match range")
                if m_covered != m_expected:
                    raise ValueError("Manifest covered_blocks does not match expected_blocks")

                # Verify actual raw_envelopes.jsonl lines
                lines = [
                    line
                    for line in envelopes_file.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                if len(lines) != m_covered:
                    raise ValueError(
                        f"Envelope lines count ({len(lines)}) does not match manifest covered_blocks ({m_covered})"
                    )

                first_env = json.loads(lines[0])
                if first_env.get("block_number") != m_from:
                    raise ValueError(
                        f"First envelope block_number {first_env.get('block_number')} != manifest from_block {m_from}"
                    )
                if first_env.get("chain_id") != config.chain_id:
                    raise ValueError("Envelope chain_id mismatch")
                if first_env.get("block_domain") != str(config.block_domain):
                    raise ValueError("Envelope block_domain mismatch")

                last_env = json.loads(lines[-1])
                if last_env.get("block_number") != m_to:
                    raise ValueError(
                        f"Last envelope block_number {last_env.get('block_number')} != manifest to_block {m_to}"
                    )
                if (
                    cursor_data.get("last_cursor")
                    and last_env.get("cursor") != cursor_data["last_cursor"]
                ):
                    raise ValueError("Last envelope cursor mismatch with cursor.json")

                is_continuation = True
                initial_from_block = m_from
                accumulated_covered_blocks = m_covered
            except Exception as exc:
                raise FileExistsError(
                    f"Refusing to overwrite existing collection evidence: invalid continuation state ({exc})"
                ) from exc
        elif len(existing_outputs) > 0:
            # Partial / incomplete set of outputs exists on disk; fail closed
            collisions = [str(p) for p in existing_outputs]
            raise FileExistsError(
                "Refusing to overwrite existing collection evidence: " + ", ".join(collisions)
            )

        # Generate envelopes
        expected_count = config.to_block - config.from_block + 1
        envelopes: list[RawEnvelope] = []
        covered_blocks: list[int] = []

        if config.fixture_mode:
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

        # Prepare Manifest and Cursor payloads
        if is_continuation:
            manifest_from = initial_from_block
            manifest_to = config.to_block
            manifest_covered = accumulated_covered_blocks + len(envelopes)
            manifest_expected = manifest_to - manifest_from + 1
        else:
            manifest_from = config.from_block
            manifest_to = config.to_block
            manifest_covered = len(covered_blocks)
            manifest_expected = expected_count

        manifest = CoverageManifest(
            chain_id=config.chain_id,
            from_block=manifest_from,
            to_block=manifest_to,
            expected_blocks=manifest_expected,
            covered_blocks=manifest_covered,
            missing_blocks=(),
            coverage_ratio=1.0,
            verified_at=time.time(),
        )
        manifest_payload = {
            "chain_id": manifest.chain_id,
            "from_block": manifest.from_block,
            "to_block": manifest.to_block,
            "expected_blocks": manifest.expected_blocks,
            "covered_blocks": manifest.covered_blocks,
            "missing_blocks": list(manifest.missing_blocks),
            "coverage_ratio": manifest.coverage_ratio,
            "verified_at": manifest.verified_at,
        }

        cursor_data = {
            "chain_id": config.chain_id,
            "block_domain": str(config.block_domain),
            "last_block": config.to_block,
            "last_cursor": envelopes[-1].cursor if envelopes else None,
            "is_fixture_mode": config.fixture_mode,
            "updated_at": time.time(),
        }

        # Write safely via unique tempfiles and fsync
        temp_manifest: Path | None = None
        temp_cursor: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                dir=config.output_dir,
                prefix=".manifest_",
                suffix=".tmp",
                delete=False,
                encoding="utf-8",
            ) as tf_m:
                temp_manifest = Path(tf_m.name)
                json.dump(manifest_payload, tf_m, indent=2)
                tf_m.flush()
                os.fsync(tf_m.fileno())

            with tempfile.NamedTemporaryFile(
                mode="w",
                dir=config.output_dir,
                prefix=".cursor_",
                suffix=".tmp",
                delete=False,
                encoding="utf-8",
            ) as tf_c:
                temp_cursor = Path(tf_c.name)
                json.dump(cursor_data, tf_c, indent=2)
                tf_c.flush()
                os.fsync(tf_c.fileno())

            if is_continuation:
                fd = os.open(envelopes_file, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
                with os.fdopen(fd, "a", encoding="utf-8") as f:
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
                    f.flush()
                    os.fsync(f.fileno())
            else:
                fd = os.open(
                    envelopes_file,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                    0o644,
                )
                with os.fdopen(fd, "w", encoding="utf-8") as f:
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
                    f.flush()
                    os.fsync(f.fileno())

            os.replace(temp_manifest, manifest_file)
            temp_manifest = None
            os.replace(temp_cursor, cursor_file)
            temp_cursor = None
        finally:
            if temp_manifest and temp_manifest.exists():
                try:
                    temp_manifest.unlink()
                except OSError:
                    pass
            if temp_cursor and temp_cursor.exists():
                try:
                    temp_cursor.unlink()
                except OSError:
                    pass

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
