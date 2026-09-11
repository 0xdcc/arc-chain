# M4 Independent Review Report: Upstream Import Parity & Test Obligations (T43 / G0)

- **Plan ID**: `ARC-4B-v3.1-20260911-13817f4`
- **Source SHA**: `13817f4e027375dd59cc7a202ae641c068525f53`
- **Contract Digest**: `604333c5d6857baece331bc6c78ec44f3eb6ed19e822b8a535928cf1ea14126e`
- **Task ID**: `T43`
- **Stage**: `G0`
- **Role**: `M4 / Independent Reviewer`
- **Review Verdict**: **PASS**

---

## 1. Executive Summary

This independent review report audits the baseline import fidelity, security boundaries, upstream test obligations, and data vs code separation for Arc-Chain v3.1. All 281 files imported from upstream `13817f4e027375dd59cc7a202ae641c068525f53` have been verified. Strict security exclusions (zero private keys, zero `.env*`, zero Robinhood pool catalogs, zero legacy monolithic pipelines) are fully enforced. The requirements of `07_本地池目录变化处理规则.md` have been implemented and independently verified with 100% test pass rate across all positive, boundary, corruption, and cross-chain injection test vectors.

---

## 2. Import Manifest Parity Audit (281 Files)

As specified in `docs/reuse/IMPORT_MANIFEST.json`:
- **Total Expected Files**: 281
- **Total Found Files**: 281 (100% presence)
- **Missing Files**: 0
- **Unexpected Empty Files**: 0 (2 zero-byte files match upstream git empty blobs: `tests/__init__.py` and `tests/fixtures/catalog/v1/synthetic_quotes.jsonl`)
- **Exact SHA-256 Matches**: 278
- **Documented Arc Adaptations**: 3 (0 unexpected mismatches)

### Documented Adaptations Audit

Three files differ from upstream git blobs by design to establish the Arc-Chain architecture and safety boundaries:

| File Path | Upstream SHA-256 | Arc Worktree SHA-256 | Justification & Verification |
|---|---|---|---|
| `AGENTS.md` | `68bf099d...` | `84a52af6...` | Replaced Robinhood rules with Arc-Chain v3.1 engineering convention, M1–M4 role definitions, and non-negotiable safety red lines (no funds, armed=false, 500u limit). |
| `arbitrage_contracts/__init__.py` | `dc64b32d...` | `fa8f45b9...` | Re-exports Arc domain extension types (`BlockDomain`, `NetworkProfile`, `OtcQuote`, `RawEnvelope`, `SimulationEvidenceBridge`, `TickCoverage`) frozen in T03. |
| `tests/conftest.py` | `9a1ba847...` | `a197cc1d...` | Added graceful fallback for `core.config` when core package is omitted (modular decoupling), strict `/tmp` pytest cache redirection, and external production path banning. |

All other 278 reusable source, contract, market catalog, state graph, opportunities, and test fixture files match upstream commit `13817f4` byte-for-byte.

---

## 3. Exclusion Manifest & Security Boundary Enforcement

As specified in `docs/reuse/EXCLUSION_MANIFEST.json` and project safety rules:
- **Total Exclusion Rules Checked**: 7
- **Violations**: 0

| Rule / Pattern | Category | Verification Result | Status |
|---|---|---|---|
| `.env*`, `*.pem`, `*.key`, `*keystore*` | `SECURITY_RED_LINE` | Full filesystem rglob confirmed 0 credentials, secrets, or keystores in repository. | PASS |
| `apps/live_pipeline.py` | `ARCHITECTURE_ISOLATION` | Verified absent. Monolithic Robinhood pipeline replaced by modular `apps/arc_collect.py` and `apps/arc_shadow.py`. | PASS |
| `data/v3_pools_live_catalog.json` | `CHAIN_ID_ISOLATION` | Verified absent. Robinhood 4663 pool catalog is strictly excluded from Arc production registry. | PASS |
| `data/v4_pools_live_catalog.json` | `CHAIN_ID_ISOLATION` | Verified absent. Robinhood 4663 pool catalog is strictly excluded from Arc production registry. | PASS |
| `tools/update_v3_catalog.py` | `TOOL_DEPENDENCY` | Verified absent. Legacy script with hardcoded Robinhood paths excluded. | PASS |
| `tools/export_v4_catalog.py` | `TOOL_DEPENDENCY` | Verified absent. Legacy export script excluded. | PASS |
| `.github/workflows/*` | `NO_AUTO_PUSH` | Verified absent. No automated remote push or unreviewed CI actions exist. | PASS |
| `sys.path` Isolation | `ENVIRONMENT_ISOLATION` | Verified that neither `/root/projects/crypto/dex-sniper-engine` nor `/root/.secrets` is present in `sys.path`. | PASS |

---

## 4. Upstream Test Obligations & Architectural Evolution

Upstream Robinhood tests were evaluated against Arc-Chain's modular architecture:

```
Upstream Monolithic Test Map               Arc-Chain Modular Test Suite
---------------------------------          ---------------------------------
tests/test_arbitrage_contracts.py  ----->  tests/contracts/ (10 test modules)
tests/test_market_catalog.py       ----->  tests/catalog/ (9 test modules)
tests/test_state_graph.py          ----->  tests/state_graph/ (12 test modules)
tests/test_opportunities.py        ----->  tests/opportunities/ (17 test modules)
tests/test_settled_cycles.py       ----->  tests/settled_cycles/ (8 test modules)
tests/test_v4_pipeline_integration ----->  tests/arc_v3/independent/ (M4 decoupled suite)
```

The modular packages significantly expand test coverage and isolate concerns compared to the legacy monolithic test entry points.

---

## 5. Verification of 07 Rule: Data vs Code Separation

Complying with `docs/plan_v3/07_本地池目录变化处理规则.md`:

1. **Positive Test (`DATA_ONLY_DIFFERENCE`)**:
   - When only registered pool catalog JSON (`data/v3_pools_live_catalog.json` or `data/v4_pools_live_catalog.json`) changes while code digest is identical, the system classifies the change as `DATA_ONLY_DIFFERENCE`.
   - `is_blocker = False`, `fast_path_allowed = True`, `requires_clean = False`, `requires_git_push = False`.
   - Local Robinhood pool additions do not halt or block Arc engineering.

2. **Negative/Boundary Test (`CODE_OR_POLICY_DIFFERENCE`)**:
   - Modifications to python code (`arbitrage_contracts/`, `core/`, models, algorithms, permissions, or hardcoded addresses) trigger `CODE_OR_POLICY_DIFFERENCE`.
   - `is_blocker = True`, `fast_path_allowed = False`.
   - Code changes cannot disguise themselves as data-only changes.

3. **Corruption Fail-Closed Test (`DATA_SNAPSHOT_UNAVAILABLE` / `UNCLASSIFIED`)**:
   - Truncated, malformed, or non-UTF8 JSON files trigger `DATA_SNAPSHOT_UNAVAILABLE`.
   - Scalar/unclassified structures trigger `UNCLASSIFIED`.
   - In both cases, the system fails closed (`is_blocker = True`) and refuses to fall back to an empty table.

4. **Cross-Chain Protection Test (`CROSS_CHAIN_INJECTION_REJECTED`)**:
   - Candidate pools specifying Robinhood `chain_id = 4663` or Robinhood factory addresses (`0x8bceaa...`, `0x1f7d75...`, `0x1ac9db...`, `0xe0c4ce...`, `0x8366a3...`) are explicitly rejected for the Arc 5042 registry.

5. **Pool Count Decoupling Test**:
   - Arc validation gates test structural integrity, schema, token keys, fee tiers, and state consistency.
   - Assertions do not hardcode upstream Robinhood snapshot counts (137 V3 or 200 V4 pools).

---

## 6. Upstream Defects Discovered & Documented

During this independent review, four upstream defects and configuration drifts were identified:

1. **Defect 1: Outdated Test Paths in `tools/checks/run_layers.py`**
   - *Issue*: `run_layers.py` attempts to run `tests/test_arbitrage_contracts.py`, `tests/test_state_graph.py`, `tests/test_market_catalog.py`, and `tests/test_opportunities.py` as single flat files.
   - *Impact*: In the modular architecture, these tests have been split into modular subdirectories (`tests/contracts/`, `tests/catalog/`, `tests/state_graph/`, `tests/opportunities/`). Running `run_layers.py` directly results in `file not found` errors.
   - *Recommendation*: M1 should patch `run_layers.py` to point to the modular test directories or invoke `tools/qa/upstream_obligations.py`.

2. **Defect 2: Non-Portable Virtualenv Path in `tools/checks/run_layers.py`**
   - *Issue*: `run_layers.py` assumes `root / "venv/bin/python"` exists relative to `__file__`.
   - *Impact*: In git worktrees (`.worktrees/T43-attempt1-m4`), the virtualenv lives in the main repository root (`/root/projects/crypto/arc-chain/venv`), not inside each worktree.
   - *Recommendation*: M1 should update the virtualenv discovery to check both worktree root and main repo root or use `sys.executable`.

3. **Defect 3: Outdated Manifest in `tools/checks/test_obligations.json`**
   - *Issue*: Legacy file lists flat test files instead of the modular directories or the official `docs/reuse/TEST_OBLIGATION_MAP.json`.
   - *Impact*: Misleading test obligation report if checked by legacy scripts.

4. **Defect 4: Hardcoded Robinhood Catalog Counts in Upstream Integration Tests**
   - *Issue*: Upstream `tests/test_v4_pipeline_integration.py` hardcoded assertions `assert v3_cnt == 137` and `>= 200 V4 pools`.
   - *Impact*: Artificially couples test suite to a specific snapshot in Robinhood, failing if local pool catalog adds pools.
   - *Status in Arc*: Fixed via decoupling tests in `test_source_data_separation.py` and test obligation migration.

---

## 7. Residual Risks & Next Steps

1. **Residual Risks**:
   - **Arc Live Deployment Evidence**: G0 review validates contracts, schemas, isolation, and data separation in offline mode. G1 live sampling (T12) will be required to verify actual Arc 5042 node endpoints and contract addresses.
   - **Legacy Test Tooling Update**: `tools/checks/run_layers.py` should be updated by M1 prior to final G1 integration to avoid confusion.

2. **Recommended Next Steps**:
   - M1 to merge `review/T43-attempt1` into the integration queue.
   - M2 to proceed with T44 (minimal collection and permission isolation).
