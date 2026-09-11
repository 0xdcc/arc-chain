# ARC DEFERRED SCOPE MANIFEST (T06)

- **Plan ID**: `ARC-4B-v3.1-20260911-13817f4`
- **Target Network**: Arc Mainnet (`5042`)
- **Authority**: `M1` (Integration & Coordination)

---

## 1. Deferred Tasks & Justifications

### A. Task T12: G1-LIVE Readonly Ingest from Real RPC
- **Lead Brain**: `M2`
- **Phase**: `G1-LIVE`
- **Status**: `DEFERRED / PENDING_AUTHORIZATION`
- **Reason**:
  - Live node polling requires explicit, user-authorized RPC endpoints and rate limit quotas.
  - Per the execution mandate: *"只读RPC仅使用已有明确获准的端点与配额，缺授权只阻断对应分支。"*
  - The offline and fixture capabilities of the ingest engine (`apps/arc_collect.py`) are 100% verified (35+ unit and integration tests passing). Live network calls remain deferred until dedicated RPC credentials/proxies are supplied.

### B. Task T30: Execution Reconciliation and Circuit Breaker
- **Lead Brain**: `M3`
- **Phase**: `G3-PREP`
- **Status**: `ACTIVE_LEASE / G3_STAGING`
- **Reason**:
  - Pertains to live transaction reconciliation, post-flight balance adjustments, and emergency kill-switches.
  - Under G2 Readonly scope, live execution is permanently disabled. T30 serves as pre-flight scaffolding for future G3 phases and is being finalized in an isolated branch without blocking G2_CODE release.

---

## 2. Prohibited & Permanently Disabled Capabilities

The following actions are strictly non-authorized and physically barred across all runtime entrypoints:
1. **Real Funds Trading & Signing**: No private keys are loaded; all execution pipelines reject mutating flags.
2. **Contract Deployment**: No contracts can be deployed to Arc Mainnet or Testnet.
3. **Transaction Broadcasting**: `eth_sendRawTransaction` and `eth_sendTransaction` are blocked at the RPC transport layer.
4. **Production QQ Robot Notification**: All notifications are routed to `FakeArcNotifier` with `[FAKE_QQ]` logs.
5. **Remote Git Push**: No code is pushed to remote repositories; all work remains local to `/root/projects/crypto/arc-chain`.
