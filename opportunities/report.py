"""Local, fail-closed reporting for append-only W2 shadow ledgers."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any

from opportunities.lifecycle import LifecycleEvent, LifecyclePolicy, ObservationMerger
from opportunities.store import AppendOnlyLedger, LedgerCorruptionError

REPORT_SCHEMA_ID = "w2-shadow-report-v1"
SCHEMA_IDS = frozenset({"w2-shadow-event-v1", "w2-shadow-rejection-v1"})
_REJECT_RESULTS = frozenset({"pool_unsupported"})
_POSITIVE_RESULTS = frozenset({"profitable", "positive"})
_AMOUNT_FIELDS = ("delta_atoms", "net_atoms", "gas_cost_atoms")
W2_GAP_LIMIT_MS = 3_000
W2_TARGET_DELAY_MS = 250


@dataclass(frozen=True, slots=True)
class ReportPayload:
    """JSON-safe report derived exclusively from a validated ledger snapshot."""

    schema_id: str
    ledger: dict[str, Any]
    totals: dict[str, Any]
    denominators: dict[str, Any]
    rejection_reasons: dict[str, Any]
    amount_groups: tuple[dict[str, Any], ...]
    timeline: dict[str, Any]
    consistency: dict[str, Any]
    limitations: tuple[str, ...]

    def to_json_dict(self) -> dict[str, Any]:
        """Return the canonical payload with tuples represented as JSON arrays."""
        return {
            "schema_id": self.schema_id,
            "ledger": self.ledger,
            "totals": self.totals,
            "denominators": self.denominators,
            "rejection_reasons": self.rejection_reasons,
            "amount_groups": list(self.amount_groups),
            "timeline": self.timeline,
            "consistency": self.consistency,
            "limitations": list(self.limitations),
        }


def _stable_integer(value: object, field_name: str) -> int:
    if type(value) is not int:
        raise LedgerCorruptionError(f"ledger {field_name} must be an integer")
    return value


def _stable_boolean(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise LedgerCorruptionError(f"ledger {field_name} must be a boolean")
    return value


def _stable_text(value: object, field_name: str) -> str:
    if type(value) is not str or not value:
        raise LedgerCorruptionError(f"ledger {field_name} must be a non-empty string")
    return value


def _event_id_set_sha256(event_ids: set[str]) -> str:
    return hashlib.sha256("\n".join(sorted(event_ids)).encode("utf-8")).hexdigest()


def _parse_event(payload: dict[str, Any], sequence: int) -> LifecycleEvent:
    raw_event = payload.get("event")
    if not isinstance(raw_event, dict):
        raise LedgerCorruptionError(f"ledger entry {sequence} has no event object")
    event = LifecycleEvent.from_mapping(raw_event, sequence)
    if event.ledger_sequence != sequence or raw_event.get("event_id") != event.event_id:
        raise LedgerCorruptionError(f"ledger event identity mismatch at sequence {sequence}")
    return event


def _denominator(
    name: str,
    *,
    event_ids: set[str],
    sequences: list[int],
) -> dict[str, Any]:
    return {
        "name": name,
        "count": len(sequences),
        "unique_event_ids": len(event_ids),
        "event_id_set_sha256": _event_id_set_sha256(event_ids),
        "sequence_sum": sum(sequences),
        "recomputed_count": len(event_ids),
        "matches_ledger_count": len(sequences) == len(event_ids),
    }


def _new_amount_state(
    route_id: str,
    amount_atoms: str,
    decimals: int,
    *,
    sim_available: bool,
    truncated: bool,
    halted: bool,
) -> dict[str, Any]:
    return {
        "route_id": route_id,
        "amount_atoms": amount_atoms,
        "decimals": decimals,
        "event_count": 0,
        "event_id_set": set[str](),
        "sequence_set": set[int](),
        "delta_nonzero": 0,
        "delta_unknown": 0,
        "delta_positive": 0,
        "delta_negative": 0,
        "delta_zero": 0,
        "net_positive": 0,
        "net_negative": 0,
        "net_zero": 0,
        "net_unknown": 0,
        "net_values": set[int](),
        "sim_available": sim_available,
        "sim_all_available": sim_available,
        "truncated": truncated,
        "halted": halted,
    }


def _build_report(
    events: tuple[dict[str, Any], ...],
    *,
    confirmed_sequence: int,
    head_hash: str,
    file_sha256: str,
) -> ReportPayload:
    parsed_events: list[LifecycleEvent] = []
    for sequence, payload in enumerate(events):
        schema_id = payload.get("schema_id")
        if schema_id not in SCHEMA_IDS:
            raise LedgerCorruptionError(f"unknown ledger schema at sequence {sequence}")
        parsed_events.append(_parse_event(payload, sequence))

    all_ids: set[str] = set()
    candidate_ids: set[str] = set()
    attempted_ids: set[str] = set()
    quoted_ids: set[str] = set()
    rejected_ids: set[str] = set()
    unknown_ids: set[str] = set()
    candidate_sequences: list[int] = []
    attempted_sequences: list[int] = []
    quoted_sequences: list[int] = []
    rejected_sequences: list[int] = []
    unknown_sequences: list[int] = []
    rejection_counts: Counter[str] = Counter()
    reason_ids: dict[str, set[str]] = {}
    amount_states: dict[tuple[str, str, int], dict[str, Any]] = {}
    data_modes: set[str] = set()
    streams: set[str] = set()
    payload_truncations: list[bool] = []
    sim_flags: set[bool] = set()

    for sequence, (payload, event) in enumerate(zip(events, parsed_events, strict=True)):
        event_id = _stable_text(event.event_id, "event.event_id")
        if event_id in all_ids:
            raise LedgerCorruptionError(f"duplicate event_id at sequence {sequence}")
        all_ids.add(event_id)
        schema_id = _stable_text(payload.get("schema_id"), "schema_id")
        candidate = _stable_boolean(event.candidate, "event.candidate")
        truncated = _stable_boolean(event.truncated, "event.truncated")
        data_modes.add(_stable_text(event.data_mode, "event.data_mode"))
        streams.add(_stable_text(event.stream_id, "event.stream_id"))

        quote_status: str | None = None
        amounts: dict[str, int | None] = dict.fromkeys(_AMOUNT_FIELDS)
        payload_truncated = False
        halted = False
        if schema_id == "w2-shadow-event-v1":
            quote_status = _stable_text(payload.get("quote_status"), "quote_status")
            for field_name in _AMOUNT_FIELDS:
                value = payload.get(field_name)
                amounts[field_name] = None if value is None else _stable_integer(value, field_name)
            halted = _stable_boolean(payload.get("halted"), "halted")
            payload_truncated = _stable_boolean(payload.get("truncated"), "truncated")

        sim_available = _stable_boolean(payload.get("sim_available"), "sim_available")
        sim_flags.add(sim_available)
        quoted = schema_id == "w2-shadow-event-v1" and quote_status == "quoted"
        rejected = schema_id == "w2-shadow-rejection-v1" or event.result in _REJECT_RESULTS
        attempted = candidate or quoted or rejected
        if candidate:
            candidate_ids.add(event_id)
            candidate_sequences.append(sequence)
        if attempted:
            attempted_ids.add(event_id)
            attempted_sequences.append(sequence)
        if quoted:
            quoted_ids.add(event_id)
            quoted_sequences.append(sequence)
        if rejected:
            rejected_ids.add(event_id)
            rejected_sequences.append(sequence)
            reason = _stable_text(payload.get("reason", event.result), "reason")
            rejection_counts[reason] += 1
            reason_ids.setdefault(reason, set()).add(event_id)
        if not candidate and not quoted and not rejected:
            unknown_ids.add(event_id)
            unknown_sequences.append(sequence)

        amount_key = (event.route_id, event.amount_atoms, event.decimals)
        state = amount_states.setdefault(
            amount_key,
            _new_amount_state(
                event.route_id,
                event.amount_atoms,
                event.decimals,
                sim_available=sim_available,
                truncated=truncated or payload_truncated,
                halted=halted,
            ),
        )
        state["event_count"] += 1
        state["event_id_set"].add(event_id)
        state["sequence_set"].add(sequence)
        delta_atoms = amounts["delta_atoms"]
        if delta_atoms is None:
            state["delta_unknown"] += 1
        else:
            state[
                "delta_positive"
                if delta_atoms > 0
                else "delta_negative"
                if delta_atoms < 0
                else "delta_zero"
            ] += 1
            if delta_atoms != 0:
                state["delta_nonzero"] += 1
        net_atoms = amounts["net_atoms"]
        if net_atoms is None:
            state["net_unknown"] += 1
        else:
            state["net_values"].add(net_atoms)
            state[
                "net_positive" if net_atoms > 0 else "net_negative" if net_atoms < 0 else "net_zero"
            ] += 1
        state["sim_available"] = state["sim_available"] or sim_available
        state["sim_all_available"] = state["sim_all_available"] and sim_available
        state["truncated"] = state["truncated"] or truncated or payload_truncated
        state["halted"] = state["halted"] or halted
        payload_truncations.append(payload_truncated or truncated)

    if data_modes != {"synthetic"}:
        raise LedgerCorruptionError("report only supports one synthetic data mode")
    if len(streams) != 1:
        raise LedgerCorruptionError("ledger must contain exactly one stream_id")
    if False in sim_flags and True in sim_flags:
        raise LedgerCorruptionError("mixed sim_available values are not reportable")

    merger = ObservationMerger(
        LifecyclePolicy(gap_limit_ms=W2_GAP_LIMIT_MS, target_delay_ms=W2_TARGET_DELAY_MS)
    )
    for event in parsed_events:
        merger.process(event)
    lifecycle = merger.finalize()

    reason_rows: list[dict[str, Any]] = []
    for reason, count in sorted(rejection_counts.items()):
        event_ids = reason_ids[reason]
        reason_rows.append(
            {
                "reason": reason,
                "count": count,
                "event_id_set_sha256": _event_id_set_sha256(event_ids),
                "recomputed_count": len(event_ids),
                "matches_ledger_count": len(event_ids) == count,
            }
        )

    amount_rows: list[dict[str, Any]] = []
    for state in sorted(
        amount_states.values(),
        key=lambda item: (item["route_id"], item["amount_atoms"], item["decimals"]),
    ):
        event_ids = state.pop("event_id_set")
        sequences = state.pop("sequence_set")
        net_values = state.pop("net_values")
        state["event_id_set_sha256"] = _event_id_set_sha256(event_ids)
        state["recomputed_event_count"] = len(event_ids)
        state["event_count_matches_ledger_count"] = state["event_count"] == len(event_ids)
        state["sequence_sum"] = sum(sequences)
        state["net_atoms_values"] = [str(value) for value in sorted(net_values)]
        amount_rows.append(state)

    positive_times = [
        event.observed_at_ms for event in parsed_events if event.result in _POSITIVE_RESULTS
    ]
    nonpositive_times = [
        event.observed_at_ms for event in parsed_events if event.result not in _POSITIVE_RESULTS
    ]
    positive_boundary = (
        {"first": min(positive_times), "last": max(positive_times)} if positive_times else None
    )
    nonpositive_boundary = (
        {"first": min(nonpositive_times), "last": max(nonpositive_times)}
        if nonpositive_times
        else None
    )
    all_nonpositive = bool(parsed_events) and all(
        event.result not in _POSITIVE_RESULTS
        for event, payload in zip(parsed_events, events, strict=True)
        if payload.get("schema_id") == "w2-shadow-event-v1"
    )
    timeline = {
        "episodes_total": len(lifecycle.episodes),
        "episodes_open": sum(not episode.right_censored for episode in lifecycle.episodes),
        "episodes_closed": sum(episode.right_censored for episode in lifecycle.episodes),
        "episodes_censored": sum(episode.right_censored for episode in lifecycle.episodes),
        "continuity_unknown_count": sum(
            episode.continuity_unknown for episode in lifecycle.episodes
        ),
        "parent_link_count": sum(
            episode.parent_opportunity_id is not None for episode in lifecycle.episodes
        ),
        "last_positive_observed_at_ms": max(positive_times, default=None),
        "first_nonpositive_observed_at_ms": min(nonpositive_times, default=None),
        "positive_observation_boundary": positive_boundary,
        "nonpositive_observation_boundary": nonpositive_boundary,
        "gap_limit_ms": W2_GAP_LIMIT_MS,
        "gap_limit_source": "docs/w2/POLICY.md",
        "gap_exceeded_count": sum(episode.continuity_unknown for episode in lifecycle.episodes),
        "sampling_first_observed_at_ms": min(
            (event.observed_at_ms for event in parsed_events), default=None
        ),
        "sampling_last_observed_at_ms": max(
            (event.observed_at_ms for event in parsed_events), default=None
        ),
        "sampling_last_available_at_ms": max(
            (event.available_at_ms for event in parsed_events), default=None
        ),
        "decision_watermark_ms": lifecycle.decision_watermark_ms,
        "duplicate_event_count": lifecycle.duplicate_event_count,
        "late_revision_count": lifecycle.late_revision_count,
        "missed_due_count": lifecycle.missed_due_count,
        "all_nonpositive_complete_ledger": all_nonpositive,
    }

    denominator_data = {
        "candidate": _denominator(
            "candidate", event_ids=candidate_ids, sequences=candidate_sequences
        ),
        "attempted_quote": _denominator(
            "attempted_quote", event_ids=attempted_ids, sequences=attempted_sequences
        ),
        "quoted": _denominator("quoted", event_ids=quoted_ids, sequences=quoted_sequences),
        "rejected": _denominator("rejected", event_ids=rejected_ids, sequences=rejected_sequences),
        "unknown": _denominator("unknown", event_ids=unknown_ids, sequences=unknown_sequences),
    }
    limitations = list(lifecycle.limitations)
    limitations.extend(
        name for name, item in denominator_data.items() if not item["matches_ledger_count"]
    )

    consistency = {
        "event_count": len(events),
        "confirmed_sequence": confirmed_sequence,
        "confirmed_sequence_matches_last_index": confirmed_sequence == len(events) - 1,
        "unique_event_ids": len(all_ids),
        "recomputed_event_count": len(all_ids),
        "event_count_matches_ledger_count": len(events) == len(all_ids),
        "denominator_sum_matches_total": (
            len(quoted_ids) + len(rejected_ids) + len(unknown_ids) == len(events)
        ),
        "head_hash_matches_last_event": head_hash == events[-1]["_ledger_hash"]
        if events
        else head_hash == "0" * 64,
        "file_sha256_is_input_hash": bool(file_sha256),
    }
    if confirmed_sequence != len(events) - 1:
        limitations.append("confirmed_sequence_mismatch")

    return ReportPayload(
        schema_id=REPORT_SCHEMA_ID,
        ledger={
            "head_hash": head_hash,
            "event_count": len(events),
            "file_sha256": file_sha256,
            "confirmed_sequence": confirmed_sequence,
            "tail_truncated": False,
        },
        totals={
            "events": len(events),
            "unique_event_ids": len(all_ids),
            "streams": sorted(streams),
            "data_mode": "synthetic",
            "sim_available": all(sim_flags),
            "truncated": any(payload_truncations),
            "all_nonpositive": all_nonpositive,
            "business_exit_code": 0,
        },
        denominators=denominator_data,
        rejection_reasons={
            "rows": reason_rows,
            "sum": sum(row["count"] for row in reason_rows),
            "event_count": len(rejected_ids),
            "exclusive_sum": sum(row["count"] for row in reason_rows) == len(rejected_ids),
        },
        amount_groups=tuple(amount_rows),
        timeline=timeline,
        consistency=consistency,
        limitations=tuple(limitations),
    )


def build_report_from_ledger(ledger_path: Path) -> ReportPayload:
    """Load and independently reconcile one append-only ledger into a report."""
    file_bytes = ledger_path.read_bytes()
    ledger = AppendOnlyLedger(ledger_path)
    snapshot = ledger.load()
    if snapshot.truncated_tail is not None:
        raise LedgerCorruptionError("ledger tail is truncated")
    return _build_report(
        snapshot.events,
        confirmed_sequence=snapshot.confirmed_sequence,
        head_hash=snapshot.head_hash,
        file_sha256=hashlib.sha256(file_bytes).hexdigest(),
    )


def _md(value: object) -> str:
    return escape(str(value), quote=True)


def _md_int(value: object) -> str:
    return "N/A" if value is None else _md(value)


def render_markdown(payload: ReportPayload) -> str:
    """Render a deterministic Markdown report with ledger-derived strings escaped."""
    raw = payload.to_json_dict()
    lines = [
        "# W2 Shadow Report — synthetic",
        "",
        "- Ledger head hash: `" + _md(raw["ledger"]["head_hash"]) + "`",
        "- Ledger events: `" + _md(raw["ledger"]["event_count"]) + "`",
        "- Ledger file SHA-256: `" + _md(raw["ledger"]["file_sha256"]) + "`",
        "",
        "## Denominator reconciliation — synthetic",
        "",
        "| denominator | ledger count | recomputed | IDs SHA-256 |",
        "|---|---:|---:|---|",
    ]
    for name in ("candidate", "attempted_quote", "quoted", "rejected", "unknown"):
        item = raw["denominators"][name]
        lines.append(
            f"| {_md(name)} | {_md(item['count'])} | {_md(item['recomputed_count'])} | "
            + f"`{_md(item['event_id_set_sha256'])}` |"
        )
    lines.extend(
        [
            "",
            "## Rejection reasons — synthetic",
            "",
            "| reason | count | IDs SHA-256 |",
            "|---|---:|---|",
        ]
    )
    if raw["rejection_reasons"]["rows"]:
        for row in raw["rejection_reasons"]["rows"]:
            lines.append(
                f"| {_md(row['reason'])} | {_md(row['count'])} | "
                + f"`{_md(row['event_id_set_sha256'])}` |"
            )
    else:
        lines.append("| N/A | N/A | N/A |")
    lines.extend(
        [
            "",
            "## Amount groups — synthetic",
            "",
            "| route_id | amount_atoms | decimals | delta +/0/-/unknown | net +/0/-/unknown | sim_available |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    if raw["amount_groups"]:
        for row in raw["amount_groups"]:
            lines.append(
                f"| {_md(row['route_id'])} | `{_md(row['amount_atoms'])}` | {_md(row['decimals'])} | "
                + f"{_md(row['delta_positive'])}/{_md(row['delta_zero'])}/{_md(row['delta_negative'])}/{_md(row['delta_unknown'])} | "
                + f"{_md(row['net_positive'])}/{_md(row['net_zero'])}/{_md(row['net_negative'])}/{_md(row['net_unknown'])} | "
                + f"{_md(row['sim_all_available'])} |"
            )
    else:
        lines.append("| N/A | N/A | N/A | N/A | N/A |")
    timeline = raw["timeline"]
    lines.extend(
        [
            "",
            "## Timeline and censoring — synthetic",
            "",
            f"- Episodes: {_md(timeline['episodes_total'])} total, {_md(timeline['episodes_open'])} open, "
            + f"{_md(timeline['episodes_closed'])} closed, {_md(timeline['episodes_censored'])} censored.",
            f"- Continuity unknown: {_md(timeline['continuity_unknown_count'])}; parent links: "
            + f"{_md(timeline['parent_link_count'])}.",
            f"- Sampling: {_md(timeline['sampling_first_observed_at_ms'])}..{_md(timeline['sampling_last_observed_at_ms'])} ms; "
            + f"decision watermark {_md(timeline['decision_watermark_ms'])} ms.",
            f"- Positive boundary: {_md(timeline['positive_observation_boundary'])}; nonpositive boundary: "
            + f"{_md(timeline['nonpositive_observation_boundary'])}.",
            "",
            "## Consistency — synthetic",
            "",
            f"- Event count vs recomputed IDs: `{_md(raw['consistency']['event_count'])}` / "
            + f"`{_md(raw['consistency']['recomputed_event_count'])}`; match="
            + f"`{_md(raw['consistency']['event_count_matches_ledger_count'])}`.",
            f"- Limitations: {_md(', '.join(raw['limitations']) or 'none')}.",
            "",
            "No theoretical aggregate profit is reported.",
            "",
        ]
    )
    return "\n".join(lines)


def write_report(ledger_path: Path, output_root: Path) -> tuple[Path, Path]:
    """Build and atomically write JSON and Markdown report files."""
    payload = build_report_from_ledger(ledger_path)
    json_payload = json.dumps(payload.to_json_dict(), sort_keys=True, allow_nan=False) + "\n"
    markdown_payload = render_markdown(payload)
    output_root.mkdir(parents=True, exist_ok=False)
    json_path = output_root / "report.json"
    markdown_path = output_root / "report.md"
    for path, content in ((json_path, json_payload), (markdown_path, markdown_payload)):
        with path.open("x", encoding="utf-8") as output:
            output.write(content)
    return json_path, markdown_path
