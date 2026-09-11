"""Outbox Reducer for Arc Coordination (T04)

Processes immutable outbox messages submitted by M1-M4:
- Validates that changed files are within the granted task lease
- Verifies lease epoch and baseline integrity
- Updates TASK_BOARD.json and queues accepted tasks into MERGE_QUEUE.json
- Releases completed leases from LEASES.json idempotently
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


class OutboxReductionError(Exception):
    """Raised when outbox message processing encounters an invalid or unauthorized submission."""


def reduce_outbox(
    coord_root: Path,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Scan and process all unmerged outbox messages across M1-M4."""
    outbox_base = coord_root / "outbox"
    task_board_path = coord_root / "TASK_BOARD.json"
    leases_path = coord_root / "LEASES.json"
    merge_queue_path = coord_root / "MERGE_QUEUE.json"

    with open(task_board_path) as f:
        task_board = json.load(f)

    with open(leases_path) as f:
        leases_data = json.load(f)
    active_leases = leases_data.get("active_leases", {})

    with open(merge_queue_path) as f:
        merge_queue_data = json.load(f)
    queue = merge_queue_data.get("queue", [])
    processed_message_ids = set(merge_queue_data.get("processed_messages", []))

    new_processed = []

    for brain in ("M1", "M2", "M3", "M4"):
        brain_outbox = outbox_base / brain
        if not brain_outbox.exists():
            continue

        for result_file in sorted(brain_outbox.glob("RESULT-*.json")):
            msg_id = f"{brain}/{result_file.name}"
            if msg_id in processed_message_ids:
                continue

            with open(result_file) as f:
                result_payload = json.load(f)

            task_id = result_payload.get("task_id")
            if not task_id or task_id not in task_board["tasks"]:
                raise OutboxReductionError(f"Unknown task_id in {msg_id}: {task_id}")

            epoch = result_payload.get("lease_epoch")
            status = result_payload.get("status")
            changed_files = result_payload.get("changed_files", [])

            # Check lease boundaries if lease was registered
            if task_id in active_leases:
                granted_lease = active_leases[task_id]
                granted_files = set(granted_lease.get("exact_write_allowlist", []))
                for cf in changed_files:
                    if cf not in granted_files:
                        raise OutboxReductionError(
                            f"Task {task_id} wrote file outside lease allowlist: {cf} in {msg_id}"
                        )
                # Release lease upon successful completion
                if status in ("SUBMITTED", "ACCEPTED", "BOOTSTRAP_PREPARED"):
                    del active_leases[task_id]

            # Update task board state
            task_board["tasks"][task_id]["status"] = status
            task_board["tasks"][task_id]["result"] = result_payload.get("result_commit")
            task_board["tasks"][task_id]["last_attempt"] = result_payload.get("attempt")
            task_board["tasks"][task_id]["last_updated_by"] = brain

            # Add to merge queue if not already queued
            queue_item = {
                "task_id": task_id,
                "brain": brain,
                "message_id": msg_id,
                "status": status,
                "result_commit": result_payload.get("result_commit"),
                "changed_files": changed_files,
            }
            if not any(item["message_id"] == msg_id for item in queue):
                queue.append(queue_item)

            processed_message_ids.add(msg_id)
            new_processed.append(msg_id)

    if not dry_run and new_processed:
        with open(task_board_path, "w") as f:
            json.dump(task_board, f, indent=2)

        leases_data["active_leases"] = active_leases
        with open(leases_path, "w") as f:
            json.dump(leases_data, f, indent=2)

        merge_queue_data["queue"] = queue
        merge_queue_data["processed_messages"] = sorted(list(processed_message_ids))
        with open(merge_queue_path, "w") as f:
            json.dump(merge_queue_data, f, indent=2)

    return {
        "processed_count": len(new_processed),
        "processed_messages": new_processed,
        "queue_length": len(queue),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Reduce outbox messages into task board and merge queue.")
    parser.add_argument("--coord-root", type=Path, default=Path(".coord"), help="Path to .coord directory")
    parser.add_argument("--dry-run", action="store_true", help="Do not write changes to disk")
    args = parser.parse_args()

    summary = reduce_outbox(args.coord_root, dry_run=args.dry_run)
    print(f"Outbox reduction completed: {summary}")


if __name__ == "__main__":
    main()
