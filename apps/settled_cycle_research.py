"""Thin, offline CLI for W3 settled-cycle coverage and reporting."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, NoReturn

from research.settled_cycles.models import SettledCycleRecord
from research.settled_cycles.report import OutputDirectoryConflictError, write_outputs

EXIT_COMPLETE = 0
EXIT_PARTIAL = 2
EXIT_USAGE = 3
EXIT_CONFLICT = 4
_RECORD_KEYS = {
    "schema_id",
    "schema_version",
    "tx_hash",
    "chain_id",
    "block_number",
    "block_hash",
    "transaction_index",
    "timestamp_s",
    "subjects",
    "actions",
    "subject_deltas",
    "cost_breakdown",
    "attribution_status",
    "economic_status",
    "attributed_net_atoms",
    "attributed_net_usd",
    "rejection_or_unknown_reasons",
    "provenance",
    "data_mode",
    "verified",
}
_TRUNCATION_KEYS = {"truncated", "reason"}
_REGISTRY_SCHEMA = "w3-pool-registry/1.0.0"


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_registry(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    if not isinstance(value, dict):
        raise ValueError("Registry must be a JSON object")
    return value


def _load_records(path: Path) -> tuple[list[SettledCycleRecord], int, int, int, bool, str | None]:
    records: list[SettledCycleRecord] = []
    rejected = 0
    unhandled = 0
    truncated = False
    termination_reason: str | None = None
    with path.open("r", encoding="utf-8") as handle:
        for _line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line, object_pairs_hook=_reject_duplicate_keys)
            except (json.JSONDecodeError, UnicodeError, ValueError) as error:
                unhandled += 1
                _ = error
                continue
            if not isinstance(value, dict):
                rejected += 1
                continue
            if set(value) == _TRUNCATION_KEYS and value.get("truncated") is True:
                reason = value.get("reason")
                if not isinstance(reason, str) or not reason:
                    raise ValueError("Truncation marker requires a non-empty reason")
                truncated = True
                termination_reason = reason
                continue
            if set(value) != _RECORD_KEYS:
                rejected += 1
                continue
            try:
                records.append(SettledCycleRecord.from_dict(value))
            except (TypeError, ValueError):
                rejected += 1
                continue
    accepted = len(records)
    unique: dict[str, SettledCycleRecord] = {}
    for record in records:
        previous = unique.get(record.tx_hash)
        if previous is not None and previous.to_dict() != record.to_dict():
            rejected += 1
            continue
        unique.setdefault(record.tx_hash, record)
    return list(unique.values()), accepted, rejected, unhandled, truncated, termination_reason


class SchemaMismatchError(ValueError):
    """Raised when the registry schema does not match the expected version."""


class UsageError(ValueError):
    """Raised for command-line usage failures that must exit with code 3."""


class ResearchArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise UsageError(message)


def build_parser() -> argparse.ArgumentParser:
    """Build the isolated research CLI parser."""
    parser = ResearchArgumentParser(description="Offline W3 settled-cycle research report")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--mode", required=True)
    return parser


def run(args: argparse.Namespace) -> int:
    input_path = args.input
    if not input_path.is_file():
        raise FileNotFoundError(f"Input file does not exist: {input_path}")
    input_sha256 = hashlib.sha256(input_path.read_bytes()).hexdigest()
    registry_sha256 = hashlib.sha256(args.registry.read_bytes()).hexdigest()
    records, accepted, rejected, unhandled, truncated, termination_reason = _load_records(input_path)
    registry = _load_registry(args.registry)
    if registry.get("schema") != _REGISTRY_SCHEMA:
        raise SchemaMismatchError(f"Registry schema must be {_REGISTRY_SCHEMA}")
    if args.mode != "offline":
        raise UsageError("--mode must be offline")
    exit_code = EXIT_COMPLETE
    if rejected or unhandled or truncated:
        exit_code = EXIT_PARTIAL
    try:
        write_outputs(
            records,
            args.output_dir,
            input_sha256=input_sha256,
            registry_sha256=registry_sha256,
            registry=registry,
            accepted_count=accepted,
            rejected_count=rejected,
            unhandled_count=unhandled,
            truncated=truncated,
            termination_reason=termination_reason,
        )
    except OutputDirectoryConflictError:
        raise
    return exit_code


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and translate bounded failure modes to documented exit codes."""
    try:
        parser = build_parser()
        args = parser.parse_args(argv)
        return run(args)
    except UsageError:
        print("Usage or input error", file=sys.stderr)
        return EXIT_USAGE
    except OutputDirectoryConflictError:
        print("Output directory conflict", file=sys.stderr)
        return EXIT_CONFLICT
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError, PermissionError, OSError):
        print("Usage or input error", file=sys.stderr)
        return EXIT_USAGE
    except (TypeError, ValueError, json.JSONDecodeError):
        print("Usage or input error", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
