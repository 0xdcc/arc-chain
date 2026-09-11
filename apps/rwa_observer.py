"""Standalone read-only CLI tool for RWA research evaluation and reporting."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import NoReturn

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from rwa_research import (  # noqa: E402
    classify_research_record,
    from_dict,
    generate_jsonl_records,
    generate_markdown_report,
)


def _fail(message: str, exit_code: int = 2) -> NoReturn:
    sys.stderr.write(f"ERROR: {message}\n")
    sys.exit(exit_code)


def main() -> None:
    parser = argparse.ArgumentParser(description="W7 RWA Research Observer CLI")
    parser.add_argument("--input", type=str, required=True, help="Path to input JSON bundle")
    parser.add_argument("--as-of-ms", type=int, required=True, help="Explicit as-of evaluation timestamp in ms")
    parser.add_argument("--output", type=str, required=True, help="Directory to save generated reports")
    parser.add_argument("--quote-currency", type=str, default="USDG", help="Quote currency symbol (default: USDG)")

    args = parser.parse_args()

    input_path = Path(args.input)
    if input_path.is_symlink():
        _fail(f"Input path cannot be a symlink: {args.input}", exit_code=2)
    if not input_path.exists() or not input_path.is_file():
        _fail(f"Input file not found or is not a regular file: {args.input}", exit_code=2)

    try:
        raw_bytes = input_path.read_bytes()
    except Exception as exc:
        _fail(f"Failed to read input file: {exc}", exit_code=2)

    # C28: Reject empty or whitespace-only inputs
    if not raw_bytes or not raw_bytes.strip():
        _fail("Input file is empty or contains only whitespace", exit_code=2)

    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except Exception as exc:
        _fail(f"Input file contains invalid JSON: {exc}", exit_code=2)

    if not isinstance(data, dict):
        _fail("Root JSON element must be an object", exit_code=2)

    if data.get("format") != "w7-rwa-e2e-bundle-v1":
        _fail(f"Unsupported or missing bundle format: {data.get('format')!r}", exit_code=2)

    raw_records = data.get("records")
    if not isinstance(raw_records, list) or len(raw_records) == 0:
        _fail("Bundle contains zero records or records is not a list", exit_code=2)

    # Deserialize records
    parsed_records = []
    classifications = []
    for idx, r_dict in enumerate(raw_records):
        try:
            record = from_dict(r_dict)
        except Exception as exc:
            _fail(f"Record at index {idx} failed deserialization: {exc}", exit_code=2)

        # C31: Enforce as-of timing barrier
        if record.as_of_ms > args.as_of_ms:
            _fail(
                f"Record {record.record_id} as_of_ms ({record.as_of_ms}) is in the future relative to CLI as-of ({args.as_of_ms})",
                exit_code=2,
            )

        clf = classify_research_record(record, quote_currency=args.quote_currency)
        parsed_records.append(record)
        classifications.append(clf)

    # Prepare output directory
    out_dir = Path(args.output)
    if out_dir.is_symlink():
        _fail("Output directory cannot be a symlink", exit_code=2)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Generate records.jsonl
    jsonl_content = generate_jsonl_records(parsed_records)
    jsonl_path = out_dir / "records.jsonl"
    jsonl_path.write_text(jsonl_content, encoding="utf-8")

    # 2. Generate report.md
    md_sections = []
    md_sections.append(f"# RWA Observer Multi-Record Report (As-Of: {args.as_of_ms})")
    md_sections.append(f"- Evaluated Records Count: {len(parsed_records)}")
    md_sections.append(f"- Quote Currency: `{args.quote_currency}`\n")
    for rec, clf in zip(parsed_records, classifications, strict=True):
        md_sections.append(generate_markdown_report(clf, rec))
        md_sections.append("\n---\n")

    report_content = "\n".join(md_sections)
    report_path = out_dir / "report.md"
    report_path.write_text(report_content, encoding="utf-8")

    # 3. Generate manifest.json
    artifacts = ["records.jsonl", "report.md"]
    manifest_entries = []
    for art in artifacts:
        p = out_dir / art
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        manifest_entries.append({
            "relative_path": art,
            "sha256": h,
            "size_bytes": p.stat().st_size,
        })

    manifest_data = {
        "format": "w7-rwa-run-manifest-v1",
        "as_of_ms": args.as_of_ms,
        "input_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "records_count": len(parsed_records),
        "artifacts": manifest_entries,
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    sys.stdout.write(f"Successfully processed {len(parsed_records)} records -> {out_dir}\n")
    sys.exit(0)


if __name__ == "__main__":
    main()
