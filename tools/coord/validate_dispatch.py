"""Dispatch Validation Engine for Arc Coordination (T04)

Enforces:
- Exact write path disjointness across active leases
- Mandatory baseline SHA, contract digest, and lease epoch
- Ban on forbidden capabilities (sign, broadcast, transfer, production keys)
- Path traversal and root contamination prevention
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

FORBIDDEN_CAPABILITIES = frozenset(
    {
        "production_secret_read",
        "sign",
        "broadcast",
        "transfer",
        "approve",
        "contract_deploy",
        "real_qq_push",
        "production_deploy",
        "service_restart",
        "remote_push",
    }
)


class DispatchValidationError(Exception):
    """Raised when a task dispatch order violates safety or coordination rules."""


def validate_dispatch(
    dispatch: dict[str, Any],
    active_leases: dict[str, Any],
    ownership_paths: dict[str, Any] | None = None,
) -> tuple[bool, str | None]:
    """Validate a candidate dispatch order against active leases and boundaries."""
    # 1. Mandatory schema fields
    required_fields = (
        "plan_id",
        "task_id",
        "attempt",
        "lease_epoch",
        "lead_brain",
        "worker_id",
        "baseline_sha",
        "contract_digest",
        "exact_write_allowlist",
    )
    for field_name in required_fields:
        if field_name not in dispatch or dispatch[field_name] is None:
            raise DispatchValidationError(f"Missing mandatory dispatch field: {field_name}")

    if not isinstance(dispatch["exact_write_allowlist"], list):
        raise DispatchValidationError("exact_write_allowlist must be a list of relative file paths")

    if not dispatch["exact_write_allowlist"]:
        raise DispatchValidationError("exact_write_allowlist cannot be empty")

    # 2. Check forbidden capabilities
    requested_caps = set(dispatch.get("requested_capabilities", []))
    violating_caps = requested_caps.intersection(FORBIDDEN_CAPABILITIES)
    if violating_caps:
        raise DispatchValidationError(f"Dispatch requests forbidden capabilities: {sorted(violating_caps)}")

    # 3. Path safety checks
    for p_str in dispatch["exact_write_allowlist"]:
        p = Path(p_str)
        if p.is_absolute():
            raise DispatchValidationError(f"Absolute paths forbidden in exact_write_allowlist: {p_str}")
        if ".." in p.parts:
            raise DispatchValidationError(f"Path traversal '..' forbidden: {p_str}")
        if p.parts and p.parts[0] in (".git", ".env", "venv", ".secrets"):
            raise DispatchValidationError(f"Protected system path forbidden: {p_str}")

    # 4. Lease conflict & disjointness check
    target_files = set(dispatch["exact_write_allowlist"])
    task_id = dispatch["task_id"]

    for active_tid, lease in active_leases.items():
        if active_tid == task_id:
            # Re-dispatch or attempt replacement requires matching or higher epoch
            curr_epoch = lease.get("lease_epoch", 0)
            if dispatch["lease_epoch"] <= curr_epoch:
                raise DispatchValidationError(
                    f"Lease epoch {dispatch['lease_epoch']} is not strictly greater than active epoch {curr_epoch}"
                )
            continue

        active_files = set(lease.get("exact_write_allowlist", []))
        overlap = target_files.intersection(active_files)
        if overlap:
            raise DispatchValidationError(
                f"File collision with active lease {active_tid}: overlapping paths {sorted(overlap)}"
            )

    return True, None


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate dispatch order against coordination leases.")
    parser.add_argument("dispatch_file", type=Path, help="Path to dispatch JSON file")
    parser.add_argument("--leases", type=Path, default=Path(".coord/LEASES.json"), help="Path to LEASES.json")
    args = parser.parse_args()

    if not args.dispatch_file.exists():
        print(f"Error: Dispatch file not found: {args.dispatch_file}", file=sys.stderr)
        sys.exit(1)

    with open(args.dispatch_file) as f:
        dispatch_data = json.load(f)

    leases_data = {}
    if args.leases.exists():
        with open(args.leases) as f:
            leases_data = json.load(f).get("active_leases", {})

    try:
        validate_dispatch(dispatch_data, leases_data)
        print(f"SUCCESS: Dispatch {dispatch_data.get('task_id')} is valid and disjoint.")
    except DispatchValidationError as err:
        print(f"FAILED: {err}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
