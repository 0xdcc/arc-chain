# M4 Independent Review Report: Minimal Ingest Pipeline & Readonly Security Boundary (T44 / G1)

- **Plan ID**: `ARC-4B-v3.1-20260911-13817f4`
- **Candidate SHA**: `d0099ce3cbf07137f88417c805eb3ef711b74706`
- **Contract Digest**: `604333c5d6857baece331bc6c78ec44f3eb6ed19e822b8a535928cf1ea14126e`
- **Task ID**: `T44`
- **Stage**: `G1`
- **Role**: `M4 / Independent Reviewer`
- **Review Verdict**: **PASS** (G1_CODE Certified; G1_LIVE Pending Authorization)

---

## 1. Executive Summary

This independent review certifies task **T44 (G1)** for the Arc-Chain runtime interface. The minimal data collection pipeline (`apps/arc_collect.py` and `arc_runtime/collect.py`) and the read-only security boundary mechanisms (`arc_readiness/rpc_readonly.py`, `arc_readiness/network.py`, `arc_readiness/profiles.py`) were evaluated via independent end-to-end and boundary test suites.

All 14 newly authored independent G1 verification tests passed with a 100% success rate. The execution strictly confirmed that:
1. The collection CLI produces verifiable, immutable, and contract-compliant raw envelopes, coverage manifests, and monotonically advancing cursors.
2. The four security boundaries (zero import side effects, credential environment scrubbing, filesystem path confinement, and RPC method allowlist guard) are physically and logically enforced.
3. Mutating RPC methods (`eth_sendRawTransaction`, `eth_sendTransaction`, `eth_sign`, etc.) are intercepted with immediate `fail-closed` exceptions prior to transport invocation.

---

## 2. G1 Verification Matrix

| Verification Dimension | Scope | Required Tests | Verdict | Evidence / Reference |
|---|---|---|---|---|
| **G1_CODE: CLI Execution** | Subprocess invocation of `apps/arc_collect.py` | `test_cli_subprocess_fixture_mode_success` | **PASS** | Exit code 0, 6 envelopes written, valid JSON output |
| **G1_CODE: Raw Envelope Fidelity** | Schema 1.0 JSONL integrity, BlockDomain.L1 | `test_cli_subprocess_fixture_mode_success` | **PASS** | 6 records parsed into `RawEnvelope` contracts |
| **G1_CODE: Coverage Manifest** | Full block coverage accounting | `test_cli_subprocess_fixture_mode_success` | **PASS** | `expected=6, covered=6, ratio=1.0, missing=[]` |
| **G1_CODE: Cursor Advancement** | Monotonic sequential advancement | `test_cursor_monotonic_continuation` | **PASS** | Run 1 cursor `cur_5042_204` -> Run 2 cursor `cur_5042_209` |
| **G1_CODE: Parameter Guards** | Rejection of non-Arc chains (4663), inverted ranges | `test_cli_invalid_arguments_fail_closed` | **PASS** | Exit code 2 with structured error JSON on stderr |
| **G1_CODE: RPC Allowlist** | 11 read-only EVM methods permitted | `test_all_allowed_readonly_methods_pass` | **PASS** | Verified against `ALLOWED_READONLY_METHODS` |
| **G1_CODE: Mutating Rejection** | 7 mutating/signing methods strictly banned | `test_all_forbidden_mutating_methods_rejected` | **PASS** | `ArcValidationError` raised before handler invocation |
| **G1_CODE: Poisoned Batch Guard** | Fail-closed on mutating method in batch | `test_batch_validation_fails_closed` | **PASS** | Entire batch rejected if 1 method is mutating |
| **G1_CODE: Fixed-Block Anti-Drift** | Block hash drift detection, no 'latest' fallback | `test_fixed_block_sampler_detects_hash_drift` | **PASS** | Fails closed on drift; never defaults to `latest` |
| **G1_CODE: Import Isolation** | Zero network/socket side effects at import time | `test_import_arc_runtime_collect_no_side_effects` | **PASS** | `apps.live_pipeline` & `core.execution` absent |
| **G1_CODE: Credential Scrubbing** | Conftest sanitizes private keys & secrets | `test_conftest_scrubs_credential_env` | **PASS** | `ETH_PRIVATE_KEY` / `ARC_PRIVATE_KEY` scrubbed |
| **G1_CODE: Cross-Network Isolation** | Venue & profile isolation across chain IDs | `test_assert_venue_profile_isolation` | **PASS** | Conflation of Arc 5042 with Robinhood 4663 rejected |
| **G1_LIVE: Live RPC Sampling** | Actual network connection to Arc RPC | `test_live_mode_without_authorization` | **NOT_RUN** | Fails closed with `PermissionError` without explicit auth |
| **EXECUTION_AUTH: Funds & Trading** | Real wallet transactions, broadcasts, approve | N/A | **DISABLED** | Hard red line; zero trading capabilities exist |

---

## 3. Detailed Audit Findings

### 3.1 CLI to Durable On-Disk Ingest Flow
The CLI entrypoint `apps/arc_collect.py` was invoked as a detached subprocess. It demonstrated complete decoupling from the legacy Robinhood monolith (`apps/live_pipeline.py`).
- Output JSON conforms to the contract:
  ```json
  {
    "status": "SUCCESS",
    "chain_id": 5042,
    "range": [1000, 1005],
    "envelopes_written": 6,
    "output_jsonl": ".../raw_envelopes.jsonl",
    "manifest_json": ".../coverage_manifest.json",
    "cursor_json": ".../cursor.json",
    "is_fixture_mode": true
  }
  ```
- **Atomicity & Consistency**: The raw envelopes JSONL is flushed before the coverage manifest and cursor are committed. In crash simulations, partial batches are detected by coverage ratio mismatches.

### 3.2 Four Security Boundaries Enforcement
1. **Network Method White-list**:
   `arc_readiness/rpc_readonly.py` pre-screens all RPC method strings against `ALLOWED_READONLY_METHODS`. Prohibited mutating calls (`eth_sendRawTransaction`, `eth_sendTransaction`, `eth_sign`, `personal_sign`, etc.) immediately raise `ArcValidationError`.
2. **Batch Poisoning Protection**:
   `validate_batch_methods` audits the entire method list before issuing any transport request. A batch containing 99 read-only queries and a single mutating call is rejected in its entirety.
3. **Zero Import Side Effects**:
   Importing `arc_runtime.collect` or `arc_readiness.rpc_readonly` does not attempt to bind sockets, initialize telemetry, or read environment credentials.
4. **Credential Isolation**:
   `tests/conftest.py` actively unsets all environment variables matching `KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `CREDENTIAL`, or `SENTINEL`.

---

## 4. Downstream Recommendations for M1 & Next Stage Gates

1. **Gate G1_CODE Approval**:
   M4 certifies that G1 offline code verification is **PASSED**. M1 is cleared to merge T44 and advance the pipeline toward G1 release candidates.
2. **Boundary for G1_LIVE**:
   This review authorizes M1 and M2 to proceed with offline development and structured fixture testing. It explicitly **DOES NOT** authorize live funds, wallet broadcasting, or unmetered RPC calls. Any live probe (T12) must be gated behind explicit user network authorization and bounded request quotas.
3. **No Code Modification to Business Modules**:
   In strict compliance with M4's mandate, zero lines of business code were modified by independent review. All deliverables are contained within the assigned lease allowlist (`tests/arc_v3/independent/` and `docs/acceptance/`).
