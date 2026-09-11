"""Pure read-only atomic simulation CLI entrypoint.

Assembles:
Input Gate -> Policy Check -> Plan Assembly -> Encoding -> Simulation -> Result Export.

Enforces:
- C21: Strictly offline mode only; fail-closed rejection of any --live/--send/--broadcast flags.
  Zero keystores, zero private keys, zero broadcasting components imported or loaded.
- C22: O_NOFOLLOW safe exports with traversal and symlink prevention.
- C23: Full end-to-end processing of stream fixtures with strict conservation laws.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

FORBIDDEN_DISPATCH_FLAGS: frozenset[str] = frozenset(
    {
        "--live",
        "--send",
        "--broadcast",
        "--approve",
        "--transact",
        "--write",
        "--sign",
        "--execute",
    }
)

DEFAULT_CALLER_WALLET: str = "0x39dBED3a2bd333467115dE45665cC57F813C4571"


class CliSecurityError(Exception):
    """Raised when non-offline or destructive CLI flags are detected under C21."""


def _ensure_repo_root_in_sys_path() -> None:
    """Ensure project root is in sys.path for direct CLI invocation."""
    repo_root = str(Path(__file__).resolve().parent.parent)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)


def _assert_readonly_invocation_flags(raw_args: Sequence[str]) -> None:
    """Intercept destructive or live broadcast attempts fail-closed per C21."""
    for index, raw_arg in enumerate(raw_args):
        flag_candidate = raw_arg.strip().split("=")[0].lower()
        if flag_candidate in FORBIDDEN_DISPATCH_FLAGS:
            raise CliSecurityError(
                f"Destructive or state-changing parameter {raw_arg!r} is strictly prohibited under C21. "
                "Only pure read-only offline simulation is permitted."
            )

        if flag_candidate == "--mode":
            mode_val = None
            if "=" in raw_arg:
                mode_val = raw_arg.split("=", 1)[1].strip().lower()
            elif index + 1 < len(raw_args) and not raw_args[index + 1].startswith("-"):
                mode_val = raw_args[index + 1].strip().lower()

            if mode_val is not None and mode_val != "offline":
                raise CliSecurityError(
                    f"Unsupported mode {mode_val!r}. Only mode 'offline' is permitted under C21."
                )


def build_argument_parser() -> argparse.ArgumentParser:
    """Construct command-line argument parser for atomic execution simulator."""
    parser = argparse.ArgumentParser(
        prog="atomic_simulate",
        description="Pure read-only atomic execution simulation entrypoint (C21~C24).",
        add_help=True,
    )
    parser.add_argument(
        "--mode",
        type=str,
        default="offline",
        help="Execution mode (strictly offline; live execution is forbidden).",
    )
    parser.add_argument(
        "--input",
        "-i",
        type=str,
        required=True,
        help="Path to JSONL stream containing candidate opportunities to simulate.",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default=None,
        help="Path to output JSONL destination for structured simulation results.",
    )
    parser.add_argument(
        "--summary-output",
        "-s",
        type=str,
        default=None,
        help="Optional path to output summary JSON.",
    )
    parser.add_argument(
        "--caller",
        type=str,
        default=DEFAULT_CALLER_WALLET,
        help=f"Authorized caller wallet address (defaults to {DEFAULT_CALLER_WALLET}).",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose output logging.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI main routine returning integer exit code."""
    raw_args = list(sys.argv[1:] if argv is None else argv)

    # C21 Early Interception: fail-closed before any argument parsing or module execution
    try:
        _assert_readonly_invocation_flags(raw_args)
    except CliSecurityError as sec_err:
        sys.stderr.write(f"FATAL SECURITY VIOLATION: {sec_err}\n")
        return 1

    _ensure_repo_root_in_sys_path()

    from atomic_execution.export import (
        ExportError,
        ExportPathSecurityError,
        export_records_to_jsonl,
        export_summary_to_json,
    )
    from atomic_execution.pipeline import (
        PipelineConfig,
        PipelineError,
        PipelineInputError,
        PipelineSecurityError,
        execute_pipeline,
    )

    parser = build_argument_parser()

    try:
        parsed_args = parser.parse_args(raw_args)
    except SystemExit as exit_exc:
        return exit_exc.code if isinstance(exit_exc.code, int) else 2

    if parsed_args.mode.lower() != "offline":
        sys.stderr.write(
            f"FATAL: Unsupported mode {parsed_args.mode!r}. "
            "Only mode 'offline' is permitted under C21.\n"
        )
        return 1

    config = PipelineConfig(
        caller_wallet=parsed_args.caller,
    )

    try:
        results, summary = execute_pipeline(
            parsed_args.input,
            config=config,
        )
    except PipelineInputError as input_err:
        sys.stderr.write(f"INPUT ERROR: {input_err}\n")
        return 2
    except PipelineSecurityError as sec_err:
        sys.stderr.write(f"SECURITY ERROR: {sec_err}\n")
        return 1
    except PipelineError as pipe_err:
        sys.stderr.write(f"PIPELINE ERROR: {pipe_err}\n")
        return 1
    except Exception as unexpected_err:
        sys.stderr.write(f"UNEXPECTED FAILURE: {unexpected_err}\n")
        return 1

    if parsed_args.output is not None:
        try:
            export_records_to_jsonl(results, parsed_args.output)
        except ExportPathSecurityError as sec_err:
            sys.stderr.write(f"EXPORT SECURITY ERROR: {sec_err}\n")
            return 1
        except ExportError as exp_err:
            sys.stderr.write(f"EXPORT ERROR: {exp_err}\n")
            return 1

    if parsed_args.summary_output is not None:
        try:
            export_summary_to_json(summary, parsed_args.summary_output)
        except ExportError as exp_err:
            sys.stderr.write(f"SUMMARY EXPORT ERROR: {exp_err}\n")
            return 1

    if parsed_args.verbose or parsed_args.output is None:
        sys.stdout.write(
            json.dumps(
                summary.to_dict(),
                sort_keys=True,
                indent=2,
                ensure_ascii=False,
            )
            + "\n"
        )

    # Preserve all rejected rows and the summary, while distinguishing a file
    # containing only malformed records from a completed business evaluation.
    if results and all(
        item.stage == "INPUT_GATE" and item.rejection_reason in ("MALFORMED_INPUT", "DUPLICATE_KEY")
        for item in results
    ):
        sys.stderr.write("INPUT ERROR: all candidate rows are malformed; see exported results\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
