# M4 Independent Review Report: Concurrency, Lease Coordination & Storage Crash Recovery (T47 / G2)

- **Plan ID**: `ARC-4B-v3.1-20260911-13817f4`
- **Candidate SHA**: `16daab8` (Base of Batch 6)
- **Reviewer**: M4 (Independent Review & Quality Assurance)
- **Scope**: `G2_CONCURRENCY_LEASES_AND_STORAGE_RECOVERY_AUDIT`
- **Verdict**: **ACCEPTED / PASS**

---

## 1. Executive Summary

This independent review audits multi-mastermind concurrency, lease isolation, and storage recovery mechanisms implemented across T04, T11, T23, and T40. All tests were executed in the isolated M4 review worktree (`.worktrees/T47-attempt1-m4`).

Key audit confirmations:
1. **Multi-Writer & Lease Protection**:
   - Every writer possesses its own isolated git worktree and temporary working directory.
   - Tasks may only write files explicitly authorized in `LEASES.json`. Writing outside the allowlist fails closed via `OutboxReductionError` without corrupting `TASK_BOARD.json` or `MERGE_QUEUE.json`.
   - `reduce_outbox` is completely idempotent: re-processing already reduced outbox messages is a strict no-op. Unknown task IDs or unassigned tasks fail closed.
2. **Mastermind Interruption Tolerance**:
   - Masterminds M2, M3, and M4 write solely to their append-only local outboxes (`.coord/outbox/{brain}/`).
   - Pausing or restarting M1 does not disrupt active workers or cause write lock contention. When M1 resumes, `reduce_outbox` absorbs pending submissions in order.
3. **Storage Crash & Torn Write Recovery**:
   - **Raw Ingest Segments (T11)**: Severed or truncated JSON lines resulting from sudden process interruption (`SIGKILL` / power loss) are safely skipped during coverage manifest reconstruction, correctly indexing valid prefixes and computing gap ranges for backfill.
   - **Opportunity Ledger (T23 / F04)**: Corrupted trailing records without newline or with severed JSON are identified as `truncated_tail` by `_read_snapshot`. Opening `ArcOpportunityLedger` on such corrupted tails fails closed with `LedgerCorruptionError`, preventing invalid state transitions until the corrupted tail is pruned. Following truncation, monotonic sequence continuation proceeds seamlessly.
4. **Health & Lifecycle Observability (T40)**:
   - Resource health checks continuously monitor filesystem free bytes, percentage used, and 1-minute load averages without initiating network requests.

---

## 2. Independent Verification Test Suite

The verification suite consists of 8 dedicated chaos and recovery test cases:

| Test File | Cases | Focus | Verdict |
|---|---|---|---|
| `tests/arc_v3/independent/test_coordination_chaos.py` | 4 | Lease boundary enforcement, outbox reduction idempotency, unauthorized write rejection, unknown task fail-closed | **PASS (4/4)** |
| `tests/arc_v3/independent/test_storage_chaos.py` | 4 | Ingest JSONL truncation skip, gap backfill planning, ledger torn tail fail-closed & recovery, resource health metrics | **PASS (4/4)** |

Execution stats: **collected 8, passed 8, failed 0, skipped 0** in 0.45s.

---

## 3. Findings and Invariant Matrix

| Invariant ID | Target Rule | Audit Observation | Enforcement Status |
|---|---|---|---|
| **CHAOS-01** | Lease Allowlist Guard | Files modified outside lease allowlist rejected with `OutboxReductionError` | **VERIFIED** |
| **CHAOS-02** | Reducer Idempotency | Re-executing `reduce_outbox` on existing messages leaves state invariant | **VERIFIED** |
| **CHAOS-03** | Reducer Fail-Closed | Unknown `task_id` in outbox payload rejected; state preserved | **VERIFIED** |
| **CHAOS-04** | Ingest Crash Tolerance | Severed tail lines in `.jsonl` skipped; coverage rebuilt accurately | **VERIFIED** |
| **CHAOS-05** | Gap Planning Accuracy | Disjoint block intervals identified into exact `[start, end]` ranges | **VERIFIED** |
| **CHAOS-06** | Ledger Torn-Tail Guard | Severed tail identified via `truncated_tail`; ledger init fails closed | **VERIFIED** |
| **CHAOS-07** | Monotonic Continuation | Post-truncation ledger recovery appends subsequent sequence monotonically | **VERIFIED** |
| **CHAOS-08** | Resource Health Headroom | Disk usage & load average monitored; critical flag raised under stress | **VERIFIED** |

---

## 4. Residual Risks and Gate Recommendations

1. **Residual Risks**:
   - Distributed file locking (`flock`) relies on POSIX advisory locks on local filesystems; shared NFS/network filesystems require network lock managers (NLM).
   - Ingest recovery relies on valid block numbers in raw lines; Byzantine payload corruption with valid JSON structure requires signature verification.

2. **Gate Recommendation**:
   - Coordination chaos and storage recovery pass all criteria for `G2_CODE`.
   - M4 approves integrating T47 into the candidate baseline.
