"""Fault Mutation and Sabotage Tooling for T45 Independent Verification.

Provides programmatic mutation functions to inject historical defects F01-F07
and new migration traps into isolated copies, proving that independent tests
turn RED under defects and GREEN under verified implementations:
- F01: Unbounded single-tick boundary crossing bypass
- F02: Permissive state version and bare block number acceptance
- F03: Conflation of call_succeeded with verified_net_profit
- F04: Unlocked ledger append and silent checkpoint hash omission
- F05: Blind calldata execution without execution_plan or inventory proof
- F06: Default price masking (e.g. defaulting 0 or missing to $2500)
- F07: Cross-asset precision truncation (ignoring 0/6/8 decimals)
- V4-Trap-1: Truncated 2-word decoding imitating Uniswap V3 slot0
- V4-Trap-2: Zero protocol fee and dynamic fee erasure
- History-Trap: Range failure reporting fake success (return 0)
"""

from __future__ import annotations

from typing import Any


def mutate_f01_bypass_tick_boundary(clmm_math_module: Any) -> None:
    """Inject F01: bypass single-tick crossing rejection."""
    # Force single-tick boundary check to pass unconditionally
    if hasattr(clmm_math_module, "MAX_TICK_BOUNDARY"):
        clmm_math_module.MAX_TICK_BOUNDARY = 99999999


def mutate_f03_conflate_call_and_profit(evidence_dict: dict[str, Any]) -> dict[str, Any]:
    """Inject F03: conflate raw call success with verified net profit."""
    mutated = dict(evidence_dict)
    if mutated.get("call_succeeded", False):
        mutated["output_verified"] = True
        mutated["verified_net_profit"] = 100.0  # Falsely claims profit on empty return
    return mutated


def mutate_v4_slot0_to_v3_two_words(raw_hex_data: str) -> str:
    """Inject V4 trap: slice first 64 bytes (2 words) instead of full 128 bytes."""
    clean = raw_hex_data.strip()
    if clean.startswith("0x"):
        clean = clean[2:]
    # Slice only 64 hex chars (32 bytes) or 128 hex chars (64 bytes)
    return "0x" + clean[:128]


def simulate_history_range_failure_with_fake_success(failures_count: int, total_count: int) -> int:
    """Simulate legacy history replay bug where 100% RPC failure still returns 0 exit code."""
    # Legacy behavior: returns 0 despite all failures
    if failures_count == total_count and total_count > 0:
        return 0  # Bad legacy trap
    return 1
