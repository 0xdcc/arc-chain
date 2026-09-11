"""Canonical JSONL and human-readable report generation for W3-E."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

from .coverage import CoverageGap, classify_coverage, gap_counts
from .models import SCHEMA_VERSION, SettledCycleRecord

REPORT_SCHEMA = "w3-settled-research-report/1.0.0"
_PROVENANCE_PATH_FIELDS = frozenset({"source_file", "path", "file_path"})
_SOURCE_PATHS = (
    "apps/settled_cycle_research.py",
    "research/settled_cycles/coverage.py",
    "research/settled_cycles/models.py",
    "research/settled_cycles/report.py",
    "research/settled_cycles/schema-v1.json",
)


class OutputDirectoryConflictError(ValueError):
    """Raised when report generation would overwrite or merge into artifacts."""


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def _canonical_record_json(record: SettledCycleRecord) -> str:
    data = record.to_dict()
    data["provenance"] = {
        key: value
        for key, value in data["provenance"].items()
        if key not in _PROVENANCE_PATH_FIELDS
    }
    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    )


def _reject_non_empty_directory(out_dir: Path) -> None:
    if out_dir.exists() and not out_dir.is_dir():
        raise ValueError(f"Output path is not a directory: {out_dir}")
    if out_dir.exists() and any(out_dir.iterdir()):
        raise OutputDirectoryConflictError(f"Output directory is not empty: {out_dir}")


def _distribution(records: Iterable[SettledCycleRecord], field_name: str) -> dict[str, int]:
    return dict(
        sorted(
            Counter(getattr(record, field_name).value for record in records).items(),
        )
    )


def _unknown_top(records: tuple[SettledCycleRecord, ...], gaps: tuple[CoverageGap, ...]) -> list[dict[str, Any]]:
    reasons = Counter(record.rejection_or_unknown_reasons for record in records)
    reason_counts = Counter(reason for reasons_tuple, count in reasons.items() for reason in reasons_tuple for _ in range(count))
    reason_counts.update(gap.gap_kind.value for gap in gaps)
    return [
        {"reason": reason, "count": count}
        for reason, count in sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))[:10]
    ]


def _unexplained_summary(records: tuple[SettledCycleRecord, ...]) -> dict[str, Any]:
    affected = []
    reasons: Counter[str] = Counter()
    for record in records:
        record_reasons = tuple(
            reason for reason in record.rejection_or_unknown_reasons if "unexplained" in reason.lower()
        )
        if record_reasons:
            affected.append(record.tx_hash)
            reasons.update(record_reasons)
    return {
        "affected_transactions": len(affected),
        "reasons": dict(sorted(reasons.items())),
    }


def write_outputs(
    records: Iterable[SettledCycleRecord],
    out_dir: Path | str,
    *,
    input_sha256: str | None = None,
    registry_sha256: str | None = None,
    registry: Mapping[str, Any] | None = None,
    accepted_count: int | None = None,
    rejected_count: int | None = None,
    unhandled_count: int | None = None,
    truncated: bool = False,
    termination_reason: str | None = None,
    exit_semantics: str = "0=complete_success;2=partial_invalid_or_truncated;3=usage_or_input_error;4=output_conflict",
) -> dict[str, str]:
    """Write canonical, relocation-stable research artifacts without overwriting.

    Path-like provenance is excluded from canonical identity by the existing model.
    Manifests deliberately exclude absolute paths and elapsed-time values.
    """
    materialized = tuple(records)
    if not all(isinstance(record, SettledCycleRecord) for record in materialized):
        raise TypeError("records must contain only SettledCycleRecord values")
    output = Path(out_dir)
    _reject_non_empty_directory(output)
    output.mkdir(parents=True, exist_ok=True)

    lines = tuple((_canonical_record_json(record), record) for record in materialized)
    records_path = output / "records.jsonl"
    report_path = output / "report.md"
    manifest_path = output / "run-manifest.json"
    with records_path.open("x", encoding="utf-8", newline="\n") as handle:
        for line, _ in lines:
            handle.write(line + "\n")

    gaps = tuple(gap for _, record in lines for gap in classify_coverage(record, registry, None))
    historical_count = sum(record.data_mode == "historical" for record in materialized)
    synthetic_count = sum(record.data_mode == "synthetic" for record in materialized)
    report = [
        "# W3-E Settled Cycle Report",
        "",
        "## Sample Separation",
        f"- historical/real: {historical_count}",
        f"- synthetic (never enters real-sample denominator): {synthetic_count}",
        "",
        "## Attribution Distribution",
    ]
    report.extend(f"- {key}: {value}" for key, value in _distribution(materialized, "attribution_status").items())
    report.extend(["", "## Economic Distribution"])
    report.extend(f"- {key}: {value}" for key, value in _distribution(materialized, "economic_status").items())
    report.extend(["", "## Unknown Reasons (Top)"])
    for item in _unknown_top(materialized, gaps):
        report.append(f"- {item['reason']}: {item['count']}")
    report.extend(["", "## Coverage Gaps"])
    for key, value in gap_counts(gaps).items():
        report.append(f"- {key}: {value}")
    report.extend(["", "## Unexplained Flows"])
    unexplained = _unexplained_summary(materialized)
    report.append(f"- affected transactions: {unexplained['affected_transactions']}")
    report.extend(f"- {reason}: {count}" for reason, count in unexplained["reasons"].items())
    if truncated:
        report.extend(["", "## Truncation"])
        report.append("- truncated: true")
        report.append(f"- termination reason: {termination_reason or 'unspecified'}")
    with report_path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(report) + "\n")

    records_sha256 = hashlib.sha256(
        "".join(line + "\n" for line, _ in lines).encode("utf-8")
    ).hexdigest()
    manifest = {
        "schema": REPORT_SCHEMA,
        "records_schema_version": SCHEMA_VERSION,
        "code_identity": {
            "implementation": "w3-e-offline-coverage-report-cli",
            "version": "1.0.0",
            "source_sha256": {
                relative: hashlib.sha256(
                    (Path(__file__).parents[2] / relative).read_bytes()
                ).hexdigest()
                for relative in _SOURCE_PATHS
            },
        },
        "input_file_sha256": input_sha256,
        "registry_file_sha256": registry_sha256,
        "counts": {
            "records": len(materialized),
            "accepted": accepted_count,
            "rejected": rejected_count,
            "unhandled": unhandled_count,
            "historical": historical_count,
            "synthetic": synthetic_count,
        },
        "coverage_gap_counts": gap_counts(gaps),
        "attribution_distribution": _distribution(materialized, "attribution_status"),
        "economic_distribution": _distribution(materialized, "economic_status"),
        "records_jsonl_sha256": records_sha256,
        "truncated": truncated,
        "termination_reason": termination_reason,
        "excluded_from_cross_path_identity": [
            "absolute_paths",
            "elapsed_time",
            "filesystem_metadata",
        ],
        "exit_semantics": exit_semantics,
    }
    with manifest_path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(manifest, handle, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        handle.write("\n")
    return {
        "records.jsonl": str(records_path),
        "report.md": str(report_path),
        "run-manifest.json": str(manifest_path),
    }
