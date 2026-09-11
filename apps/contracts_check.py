"""Pure file I/O CLI for contract schema validation and legacy record adaptation."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from arbitrage_contracts.legacy_adapter import (
    AdaptationResult,
    LegacyContext,
    adapt_legacy_record,
)
from arbitrage_contracts.serialization import (
    ContractRecord,
    decode_record_json,
    encode_record_json,
)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key detected: {key!r}")
        result[key] = value
    return result


def _reject_constant(val: str) -> None:
    raise ValueError(f"Prohibited non-finite JSON constant: {val!r}")


def _parse_input_records(input_path: Path) -> list[tuple[int, str, dict[str, Any]]]:
    if not input_path.is_file():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    content = input_path.read_text(encoding="utf-8")
    if not content.strip():
        raise ValueError("Input file is empty")

    lines = [line.strip() for line in content.splitlines() if line.strip()]
    records: list[tuple[int, str, dict[str, Any]]] = []

    first_err: Exception | None = None
    parsed_line_by_line = True
    for idx, line in enumerate(lines):
        try:
            raw_dict = json.loads(
                line,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_constant,
            )
            if not isinstance(raw_dict, Mapping):
                raise ValueError(f"Record on line {idx + 1} must be a JSON object mapping")
            records.append((idx + 1, line, {str(k): v for k, v in raw_dict.items()}))
        except Exception as exc:
            first_err = exc
            parsed_line_by_line = False
            break

    if parsed_line_by_line and records:
        return records

    try:
        single_obj = json.loads(
            content,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
        if isinstance(single_obj, Mapping):
            return [(1, content, {str(k): v for k, v in single_obj.items()})]
        elif isinstance(single_obj, list):
            if not single_obj:
                raise ValueError("JSON array input is empty")
            list_records: list[tuple[int, str, dict[str, Any]]] = []
            for idx, item in enumerate(single_obj):
                if not isinstance(item, Mapping):
                    raise ValueError(f"Item {idx + 1} in JSON array must be an object")
                list_records.append(
                    (idx + 1, json.dumps(item), {str(k): v for k, v in item.items()})
                )
            return list_records
    except Exception:
        pass

    if first_err is not None:
        raise first_err
    raise ValueError("Failed to parse input file as valid JSON or JSONL")


def run_validate_mode(records: list[tuple[int, str, dict[str, Any]]], output_path: Path) -> int:
    total = len(records)
    accepted_records: list[ContractRecord] = []
    errors: list[str] = []

    for line_no, raw_text, _raw_dict in records:
        try:
            record = decode_record_json(raw_text)
            accepted_records.append(record)
        except Exception as exc:
            errors.append(f"Line {line_no}: Validation failed: {exc}")

    if errors:
        print(
            f"[FAIL] Validation failed. Read: {total}, Accepted: {len(accepted_records)}, Rejected: {len(errors)}",
            file=sys.stderr,
        )
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canonical_lines = [encode_record_json(record) for record in accepted_records]
    output_path.write_text("\n".join(canonical_lines) + "\n", encoding="utf-8")

    print(
        f"[SUCCESS] Validation passed. Read: {total}, Accepted: {len(accepted_records)}, Rejected: 0"
    )
    return 0


def run_adapt_legacy_mode(
    records: list[tuple[int, str, dict[str, Any]]],
    output_path: Path,
    context: LegacyContext,
) -> int:
    total = len(records)
    adapted_records: list[ContractRecord] = []
    errors: list[str] = []

    for line_no, _raw_text, raw_dict in records:
        try:
            res: AdaptationResult = adapt_legacy_record(raw_dict, context)
            if not res.success or res.record is None:
                reasons_str = (
                    "; ".join(res.unresolved_reasons) if res.unresolved_reasons else "Unknown"
                )
                missing_str = ", ".join(res.missing_fields) if res.missing_fields else "None"
                errors.append(
                    f"Line {line_no}: status={res.status}, missing=[{missing_str}], reasons=[{reasons_str}]"
                )
            else:
                adapted_records.append(res.record)
        except Exception as exc:
            errors.append(f"Line {line_no}: Adaptation raised exception: {exc}")

    if errors:
        print(
            f"[FAIL] Legacy adaptation failed. Read: {total}, Accepted: {len(adapted_records)}, Rejected: {len(errors)}",
            file=sys.stderr,
        )
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canonical_lines = [encode_record_json(record) for record in adapted_records]
    output_path.write_text("\n".join(canonical_lines) + "\n", encoding="utf-8")

    print(
        f"[SUCCESS] Legacy adaptation passed. Read: {total}, Accepted: {len(adapted_records)}, Rejected: 0"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="contracts_check",
        description="Pure file I/O validator and legacy adapter for arbitrage contracts.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="Path to input JSON/JSONL contract file",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Path to destination canonical JSONL file",
    )
    parser.add_argument(
        "--mode",
        choices=["validate", "adapt-legacy"],
        required=True,
        help="Execution mode: validate standard contracts or adapt legacy records",
    )
    parser.add_argument(
        "--context",
        type=Path,
        default=None,
        help="Optional path to legacy migration context JSON file",
    )

    args = parser.parse_args(argv)

    try:
        records = _parse_input_records(args.input)
    except Exception as exc:
        print(f"[FAIL] Input read error: {exc}", file=sys.stderr)
        return 1

    context = LegacyContext()
    if args.context is not None:
        try:
            if not args.context.is_file():
                print(f"[FAIL] Context file not found: {args.context}", file=sys.stderr)
                return 1
            ctx_dict = json.loads(args.context.read_text(encoding="utf-8"))
            if not isinstance(ctx_dict, Mapping):
                print("[FAIL] Context file must contain a JSON object", file=sys.stderr)
                return 1
            context = LegacyContext(**ctx_dict)
        except Exception as exc:
            print(f"[FAIL] Context load error: {exc}", file=sys.stderr)
            return 1

    if args.mode == "validate":
        return run_validate_mode(records, args.output)
    elif args.mode == "adapt-legacy":
        return run_adapt_legacy_mode(records, args.output, context)
    else:
        print(f"[FAIL] Unknown mode: {args.mode}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
