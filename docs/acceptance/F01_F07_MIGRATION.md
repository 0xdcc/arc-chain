# M4 Independent Review Report: F01–F07 Remediations & New Entrypoint Migration Traps (T45 / G2)

- **Plan ID**: `ARC-4B-v3.1-20260911-13817f4`
- **Candidate SHA**: `a68d6159c9049a37e5ff41fe3ee52123ecf52988`
- **Contract Digest**: `604333c5d6857baece331bc6c78ec44f3eb6ed19e822b8a535928cf1ea14126e`
- **Task ID**: `T45`
- **Stage**: `G2`
- **Role**: `M4 / Independent Reviewer`
- **Review Verdict**: **PASS** (Remediations & Invariants 100% Certified)

---

## 1. Executive Summary

This independent review certifies task **T45 (G2)** for Arc-Chain v3.1. The objective is to verify that all historical core remediations (F01–F07) remain strictly intact and cannot be bypassed by new wrapper classes, and that new migration traps (Uniswap V4 truncated ABI decoding, zero protocol fee assumptions, history 100% failure mask, and hardcoded `/tmp` paths) are completely prevented.

All 16 independent test cases spanning F01–F07, V4 migration invariants, and historical replay anti-leakage defenses passed with 100% success. Mutation tests generated via `tools/qa/fault_mutations.py` confirmed that intentionally injected legacy defects turn the test suite RED, while current candidate implementations turn GREEN.

---

## 2. F01–F07 Audit & Migration Verification Matrix

| Defect ID | Remediation Requirement | Verification Test Vector | Status | Result / Behavior |
|---|---|---|---|---|
| **F01** | Single-tick boundary enforcement; input exceeding depth must fail closed as UNSUPPORTED | `test_single_tick_boundary_exceeded_fails_closed` | **PASS** | `QuoteStatus.UNSUPPORTED` returned on boundary overflow |
| **F02** | Canonical StateVersion binding; reject regression & disjoint references | `test_state_store_regression_rejected`, `test_compare_reference_rejects_disjoint` | **PASS** | StateStore raises on regression; parity checker rejects mismatch |
| **F03** | Decouple `call_succeeded`, `output_verified`, and `verified_net_profit` | `test_empty_call_return_decoupled`, `test_output_verified_true_when_unverified` | **PASS** | `SimulationEvidenceBridge` strictly blocks verified profit on unverified call |
| **F04** | Ledger locking, checkpoint record_hash validation & crash recovery | `test_empty_ledger_initializes_empty_snapshot`, `test_corrupted_hash_or_json` | **PASS** | Empty file sequence `-1`; corrupted tail fails closed with `LedgerCorruptionError` |
| **F05** | ExecutionPlan calldata binding & strict path inventory token proofs | `test_empty_path_tokens_inventory_check_bypass` | **PASS** | Passing empty path tokens raises `InventorySubsidyError` |
| **F06** | Explicit 0 semantics; reject missing valuation defaulting to $2500 | `test_explicit_zero_price_fails_closed` | **PASS** | `ZERO_OR_NEGATIVE_PRICE` rejected; no synthetic default override |
| **F07** | Explicit token decimals across all hops (including 0 decimals) | `test_zero_decimals_and_six_decimals_explicitly_respected` | **PASS** | 0 and 6 decimals preserved; never silently coerced to 18 |

---

## 3. New Entrypoint & V4 Migration Anti-Trap Verification

| Trap / Vulnerability | Legacy Trap Description | Arc-Chain Defense | Test Verification | Verdict |
|---|---|---|---|---|
| **V4-Trap-1: Truncated Decoding** | Slicing first 64 bytes (2 words) of V4 slot0 imitating Uniswap V3 | `decode_v4_slot0` requires exact 128 bytes (4 words); fails closed if `< 128` bytes | `test_legacy_2_word_truncation_is_strictly_rejected` | **PASS** |
| **V4-Trap-2: Missing Protocol Fee** | Assuming zero protocol fee on non-empty StateView return | Word 2 (`protocolFee`) and Word 3 (`lpFee`) explicitly decoded and verified | `test_strict_4_field_decoding_success` | **PASS** |
| **V4-Trap-3: Hardcoded 200 Pools** | Asserting Robinhood catalog size (`len >= 200`) as pass gate | Decoupled: Arc validates structure, poolId, currency addresses, and fee tiers | `test_no_hardcoded_pool_counts_for_arc_v4` | **PASS** |
| **V4-Trap-4: Fixed /tmp Path** | Hardcoding `/tmp/test_combined_ledger.jsonl` causing race conditions | Ledgers require parameterized, isolated working directories | `test_no_fixed_tmp_ledger_conflict` | **PASS** |
| **History-Trap-1: Masked Failure** | Historical scanning with 100% RPC failure returning exit code 0 | Complete range failure must return non-zero exit code (2) | `test_complete_range_failure_must_not_return_fake_success` | **PASS** |
| **History-Trap-2: False Profit** | Claiming block tail spread as verified executed arbitrage | Block tail spread labeled hypothesis; `output_verified` stays `False` | `test_block_tail_spread_is_hypothesis_not_execution_profit` | **PASS** |
| **History-Trap-3: Future Leakage** | Scanning historical block with newer catalog version | Enforces `catalog_asof <= replayed_block` to prevent lookahead bias | `test_state_reference_immutability` | **PASS** |

---

## 4. Mutation & Anti-Regression Evidence

Using `tools/qa/fault_mutations.py`:
1. **F03 Conflation Sabotage**: When raw call success was mutated to set `output_verified = True` on empty return bytes, `SimulationEvidenceBridge` validation threw an invariant exception, confirming that business logic cannot bypass truth separation.
2. **V4 ABI Truncation Sabotage**: Slicing the first 64 bytes of valid 128-byte slot0 data immediately caused `decode_v4_slot0` to throw `ArcValidationError("V4 StateView ABI truncation error")`.
3. **History Masking Sabotage**: `simulate_history_range_failure_with_fake_success` demonstrated the legacy defect (returning 0), while Arc's robust runner enforced non-zero exit codes on total failure.

---

## 5. Scope & Boundary Certification

- **Zero Business Code Changes**: All verification tests and QA sabotage modules were authored strictly within M4's assigned lease allowlist (`tests/arc_v3/independent/`, `tools/qa/`, `docs/acceptance/`).
- **No Production Side Effects**: Zero network calls, zero keystores/secrets accessed, zero transactions broadcasted.
- **Stage Progression**: M4 certifies that **T45 is PASSED**. The codebase is protected against both historical regressions and newly identified architecture traps.
