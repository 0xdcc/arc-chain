# ARC FOUR-BRAIN COORDINATION RUNBOOK (v3.1)

- **Plan ID**: `ARC-4B-v3.1-20260911-13817f4`
- **Coordination Root**: `/root/projects/crypto/arc-chain/.coord`
- **Evidence Root**: `/root/projects/crypto/arc-chain/.evidence`

## 1. Role Division
- **M1 (Controller & Integration)**: Sole code merger, contract steward, CLI and reporting owner (`arbitrage_contracts`, `arc_runtime`, `apps/arc_*`, `.coord`).
- **M2 (Data & Markets)**: Ingest, RPC transport, USDC dual views, V3/V4 catalog and StateView (`market_catalog`, `arc_readiness`, `arc_ingest`, `arc_markets`).
- **M3 (Strategy & Simulation)**: Quotes, economic calculations, readonly simulation, historical attribution (`opportunities`, `state_graph`, `atomic_execution`, `arc_opportunities`, `arc_research`).
- **M4 (Independent Reviewer)**: Upstream obligation parity, F01-F07 migrations, chaos recovery, final release audit (`tests/arc_v3/independent/`, `docs/acceptance/`).

## 2. Dispatch & Lease Protocol
1. Every task execution must have an explicit dispatch configuration adhering to `templates/DISPATCH.json`.
2. `exact_write_allowlist` must contain relative file paths strictly disjoint from other active leases.
3. Leases are registered in `.coord/LEASES.json` by M1.
4. If a task requires creating new `__init__.py`, adding dependencies, or modifying shared contracts, a Change Request (CR) must be filed with M1.

## 3. Outbox Handover & Reduction Protocol
1. Upon completing a task, the subagent/brain writes a sealed RESULT file to its assigned outbox:
   `.coord/outbox/<Brain>/RESULT-<TaskID>-attempt<Attempt>.json`
2. M1 invokes `python3 tools/coord/reduce_outbox.py` to:
   - Validate changed files against the granted lease allowlist.
   - Update `.coord/TASK_BOARD.json`.
   - Release the active lease in `.coord/LEASES.json`.
   - Append the result commit to `.coord/MERGE_QUEUE.json`.
3. Outbox message processing is idempotent: duplicate deliveries are ignored safely.

## 4. Session Resumption & Crash Recovery
- If a brain window is interrupted, reload `.coord/SESSION_INDEX.json` and `.coord/TASK_BOARD.json`.
- Do not assume other brains have finished tasks based on chat context; read disk state directly.
- Expired TTL does not mean the previous process is dead. Always audit `ps aux` and `git status` before reissuing a task with an incremented `lease_epoch`.
