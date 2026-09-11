# ARC ENVIRONMENT & ISOLATION SPECIFICATION (T41)

- **Plan ID**: `ARC-4B-v3.1-20260911-13817f4`
- **Project Root**: `/root/projects/crypto/arc-chain`
- **Dedicated Virtualenv**: `/root/projects/crypto/arc-chain/venv`
- **Runtime Data Root**: `/root/projects/crypto/arc-chain/runtime-data`

---

## 1. Environment Isolation Mandates
1. **No Production Monolith Cross-Contamination**:
   - `arc-chain` strictly operates out of its dedicated repository and virtual environment.
   - `sys.path` must not contain `/root/projects/crypto/dex-sniper-engine` or any legacy monolith paths.
   - Zero imports from `core`, `chains`, `monitors`, or `execution`.
2. **Credential and Secret Scrubbing**:
   - No `.env`, `.env.production`, `.key`, or keystore files are permitted in the project tree.
   - All runtime test fixtures and processes scrub environment variables matching `*KEY*`, `*SECRET*`, `*TOKEN*`, `*PASSWORD*`, `*CREDENTIAL*`, `*AUTH*`.
3. **Dedicated Data & Ledgers**:
   - Ingest outputs: `runtime-data/ingest/` (raw envelopes JSONL, coverage manifests, cursors).
   - Opportunity ledgers: `runtime-data/ledgers/` (hash-chained opportunity records).
   - Temporary test caches: Redirected strictly to `/tmp/` (`-o cache_dir=/tmp/pytest_cache`).

---

## 2. Capability Matrix
| Capability | Status | Authorization Requirement |
|---|---|---|
| Offline Ingest CLI (`--fixture-mode`) | Enabled | Pre-approved in G1 |
| Discrete Quoting & Economics | Enabled | Pre-approved in G1/G2 |
| Readonly Live RPC Polling | Pending | Requires explicit G1_LIVE endpoint approval |
| Real Transaction Signing & Broadcast | Blocked | Physical barrier; forbidden in G1/G2 |
| Systemd Service Restart | Blocked | Requires manual host command authorization |
