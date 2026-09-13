# Arc Arbitrage Engine (v3.1 Modular)

- **Target Network**: Arc Mainnet (Chain ID: `5042`) & Testnet (`5042002`)
- **Plan ID**: `ARC-4B-v3.1-20260911-13817f4`
- **Current Gate**: `G2_CODE: PASS` | `G2_LIVE: PENDING_AUTHORIZATION`
- **Immutable Contract Digest**: `604333c5d6857baece331bc6c78ec44f3eb6ed19e822b8a535928cf1ea14126e`

---

## 1. Quickstart

### Virtual Environment Setup
- **Main Repository**:
  ```bash
  cd /root/projects/crypto/arc-chain
  source venv/bin/activate
  ```
- **Independent Worktrees** (e.g. `.worktrees/*`):
  Local `./venv` does not exist in worktrees; activate the main repository venv or invoke the absolute interpreter:
  ```bash
  source /root/projects/crypto/arc-chain/venv/bin/activate
  # or invoke directly: /root/projects/crypto/arc-chain/venv/bin/python
  ```

### Run Test Suite
```bash
/root/projects/crypto/arc-chain/venv/bin/python -m pytest tests/arc_v3/ -q -o cache_dir=/tmp/pytest_cache
# Arc modular test suite: 375 passed, 1 warning (exit 0)
# Manifest test suite: 5 passed (tests/contracts/test_manifest_coverage.py)
# RWA test suite: 44 passed (tests/rwa/)
# Offline gates: W5 offline check (exit 0), W7 offline check (exit 0)
# Mutation sabotages: W3 (5/5 passed), W5 (6/6 passed), W7 (5/5 passed)
# Note: Whole-repo baseline checks maintain historical failures (full collection: 1633 tests collected, 49 errors during collection from historical unimported arbitrage/core/backtest modules) and are NOT full-repo PASS.
```

### Sandboxed Execution (bwrap, Recommended)
> **Security Notice**: Bare command execution is not sandbox-safe. `tools/checks/arc_audit_local.py` is an application-level runner and process scheduler, not an OS-level isolation sandbox. Safe reproducible execution requires kernel namespace isolation via `bwrap`.
```bash
# Reproducible execution isolating host network, credentials, and write filesystem
bwrap \
  --unshare-net --unshare-pid --unshare-ipc --unshare-uts --clearenv \
  --setenv PATH /root/projects/crypto/arc-chain/venv/bin:/usr/bin:/bin \
  --setenv LANG C.UTF-8 --setenv LC_ALL C.UTF-8 \
  --setenv HOME /tmp --setenv TMPDIR /tmp \
  --setenv PYTHONDONTWRITEBYTECODE 1 --setenv PYTEST_DISABLE_PLUGIN_AUTOLOAD 1 \
  --tmpfs / --proc /proc --dev /dev --tmpfs /tmp \
  --ro-bind /usr /usr --ro-bind /lib /lib --ro-bind /lib64 /lib64 --ro-bind /bin /bin \
  --ro-bind /root/.local/share/uv/python /root/.local/share/uv/python \
  --ro-bind /root/projects/crypto/arc-chain/venv /root/projects/crypto/arc-chain/venv \
  --ro-bind $(pwd) /sandbox/src \
  --chdir /sandbox/src \
  -- /root/projects/crypto/arc-chain/venv/bin/python -m pytest tests/arc_v3/ -q -o cache_dir=/tmp/pytest_cache
```

### CLI Tooling
```bash
# Ingest Collector (Offline Fixture Mode)
/root/projects/crypto/arc-chain/venv/bin/python apps/arc_collect.py --chain-id 5042 --from-block 1000 --to-block 1010 --output-dir runtime-data/ingest --fixture-mode

# Shadow Opportunity Evaluator
/root/projects/crypto/arc-chain/venv/bin/python apps/arc_shadow.py --chain-id 5042 --ledger-dir runtime-data/ledgers --fixture-mode

# Historical Block Replay Scanner
/root/projects/crypto/arc-chain/venv/bin/python apps/arc_history.py --chain-id 5042 --from-block 1000 --to-block 1005 --output-dir runtime-data/history --fixture-mode

# Offline Local Audit Diagnostics Runner (Scheduler only; use bwrap for OS-level sandboxing)
/root/projects/crypto/arc-chain/venv/bin/python tools/checks/arc_audit_local.py --evidence-dir /tmp/arc-audit-local-evidence
```

---

## 2. Directory Layout

- `arbitrage_contracts/`: Immutable contract definitions and extensions for Arc.
- `arc_readiness/`: Network profiles, USDC dual-interface accounting, and eligibility rules.
- `arc_markets/`: Venue deployments registry, V3 discovery, and V4 StateView 4-word decoding.
- `arc_ingest/`: Ingest transport, fixed-block sampling, durable cursors, and raw recorder.
- `arc_opportunities/`: CLMM quote bridge, cost models, ledger, and shadow service.
- `arc_execution/`: Physical risk policy, authorization isolation, and Nonce journaling state machine.
- `arc_research/`: Liquidity walls, settled arbitrage event attribution, and latency causal replay.
- `arc_runtime/`: Single-instance lifecycle, circuit breaker health, and Hermes reporting.
- `docs/acceptance/`: Independent red-team review reports delivered by M4 (all PASS).
- `docs/coordination/`: Coordination manifests, release candidate declarations, and user delivery reports.

---

## 3. Operational Invariants & Safety Redlines

- **Zero Unapproved Trading**: Real execution, signing, and broadcasting are physically disabled in code.
- **USDC Single-Deduction**: 18d native accounting and 6d ERC-20 scaling factors are separated to prevent double-counting.
- **Uniswap V4 Slot0**: Strictly decodes 4 words (128 bytes) with two's complement tick parsing; fails closed on truncation.
- **Hard Cap**: Single trade cap strictly limited to $\le 500.0\text{ USD}$; pathfinder mode limited to $\le 1.0\text{ USD}$.
- **Public Launch Non-Authorization**: Arrival of September 16, 2026 does **not** grant automatic live trading authority.
- **Audit Runner Boundary**: `tools/checks/arc_audit_local.py` is an application-level runner and process scheduler, not an OS isolation sandbox. Safe offline execution relies on external containerization or `bwrap` kernel namespace isolation.
- **Fail-Closed on Data Disruption**: Existing corrupted or interrupted manifests fail closed to protect historical data integrity; manual remediation is required before appending to disrupted sessions.
- **Symlink TOCTOU Boundary**: Symlink traversal checks enforce ancestor inspection and `O_NOFOLLOW` leaf flags. Concurrent replacement of parent directories by local privileged processes constitutes an OS TOCTOU boundary.
