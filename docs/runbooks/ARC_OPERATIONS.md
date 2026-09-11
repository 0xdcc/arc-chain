# ARC OPERATIONS & RUNTIME PLAYBOOK (T42)

- **Plan ID**: `ARC-4B-v3.1-20260911-13817f4`
- **Target Network**: Arc Mainnet (`5042`)
- **Authority**: `M1` (Integration & Runtime Operations)

---

## 1. Runtime CLI Tools Reference

All CLI tools are 100% self-contained, run on Python 3.12, and require `./venv/bin/python`.

### A. Raw Ingest Collector (`apps/arc_collect.py`)
Record-only ingest emitting raw envelopes, durable cursors, and coverage manifests.
```bash
./venv/bin/python apps/arc_collect.py \
  --chain-id 5042 \
  --from-block 1000 \
  --to-block 1050 \
  --output-dir runtime-data/ingest \
  --fixture-mode
```

### B. Readonly Shadow Pipeline (`apps/arc_shadow.py`)
Executes full market -> discrete quote -> economics -> readonly simulation -> ledger pipeline.
```bash
./venv/bin/python apps/arc_shadow.py \
  --chain-id 5042 \
  --ledger-dir runtime-data/ledgers \
  --max-amount-usd 500.0 \
  --fixture-mode
```

### C. Historical Block Replay (`apps/arc_history.py`)
Consecutive block state scanner with explicit hypothesis labeling.
```bash
./venv/bin/python apps/arc_history.py \
  --chain-id 5042 \
  --from-block 1000 \
  --to-block 1020 \
  --output-dir runtime-data/history \
  --mode retrospective_state \
  --fixture-mode
```

---

## 2. Health & Process Management
- **Single-Instance Lock**: Protected by `arc_runtime.lifecycle.InstanceLock`. Stored at `runtime-data/arc.lock`. Auto-recovers if previous PID is dead.
- **Circuit Breaker**: Non-negotiable 3-failure trip. Terminating stops prevent endless retry storms.
- **Never Wildcard Kill**: Prohibited to use `pkill python` or `killall`. Target only specific PIDs.

---

## 3. Data Storage & Cursor Recovery
1. Cursors are stored in `runtime-data/ingest/cursor.json`.
2. Raw envelopes are appended to `runtime-data/ingest/raw_envelopes.jsonl`.
3. If an ingest job halts, the cursor records the highest fully validated block. Re-running with `--from-block <last_cursor_block>` resumes contiguously without gaps.
