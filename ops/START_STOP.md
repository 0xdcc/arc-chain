# ARC SERVICE START & STOP RUNBOOK (T41)

- **Plan ID**: `ARC-4B-v3.1-20260911-13817f4`
- **Application**: `apps/arc_collect.py` & `arc_runtime`

---

## 1. Safe Startup Procedure (Offline Record Mode)

```bash
cd /root/projects/crypto/arc-chain

# 1. Pre-flight verification
./venv/bin/python -c "import sys; assert sys.version_info >= (3, 12)"
mkdir -p runtime-data/ingest runtime-data/ledgers

# 2. Run record collector in offline fixture mode
./venv/bin/python apps/arc_collect.py \
  --chain-id 5042 \
  --from-block 100 \
  --to-block 150 \
  --output-dir runtime-data/ingest \
  --fixture-mode
```

---

## 2. Safe Shutdown & Process Management Rules
1. **Never use wildcard `pkill python` or `killall`**:
   - Multiple background agents and services run on this host.
   - Always target the specific PID recorded in `runtime-data/arc.lock` or the explicit foreground process.
2. **Graceful Termination**:
   - Send `SIGTERM` (kill <PID>) to allow `InstanceLock.release()` and atomic cursor flushing.
3. **Lock Recovery**:
   - If a process dies abruptly, `InstanceLock` verifies `os.kill(pid, 0)`. If the PID is dead, the stale lock is safely replaced on the next start without manual deletion.
