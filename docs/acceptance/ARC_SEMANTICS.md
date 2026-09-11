# M4 Independent Review Report: Arc Accounting Semantics & Output Evidence Verification (T46 / G2)

- **Plan ID**: `ARC-4B-v3.1-20260911-13817f4`
- **Candidate SHA**: `16daab8` (Base of Batch 6)
- **Reviewer**: M4 (Independent Review & Quality Assurance)
- **Scope**: `G2_ARC_ACCOUNTING_AND_OUTPUT_EVIDENCE_AUDIT`
- **Verdict**: **ACCEPTED / PASS**

---

## 1. Executive Summary

This independent review evaluates the core Arc-specific accounting semantics, dual-interface USDC models, fee accounting, and output evidence certification implemented across T10, T14, and T27. All verification was conducted in the isolated M4 review worktree (`.worktrees/T46-attempt1-m4`), executing both forward validation and fault-injection boundary tests.

The audit establishes that:
1. **Dual-Interface USDC Reconciliation**: 18-decimal native USDC atoms and 6-decimal ERC-20 atoms are strictly governed by `reconcile_dual_interface_balance`. Double-counting (summing native and ERC-20 views) is explicitly barred by `prevent_balance_double_counting` with `ArcValidationError`. Single-sided observations strictly follow unidirectional inference (native derives ERC-20 + dust; ERC-20 bounds native while leaving dust explicitly unknown).
2. **Authority Decoupling**: Native transfer capability is an explicit protocol privilege and cannot be bypassed via ERC-20 allowance approvals (`validate_spending_authorization` raises `ArcValidationError` if ERC-20 allowance is asserted for native transfers).
3. **Event Deduplication**: Dual-emitted EIP-7708 system Transfer logs (18d) and ERC-20 contract Transfer logs (6d) are cleanly collapsed into single canonical 18-decimal system logs via `deduplicate_transaction_events`. Distinct multiple transfers to the same recipient in the same transaction are preserved with log index fidelity.
4. **Gas Accounting & Single-Deduction Netting**: Transaction fees are computed in 18d native atoms (`gas_used * effective_gas_price_wei`). Quoter DEX pool fees and gas costs are deducted strictly once; attempting double gas deduction or unhandled DEX fee configurations immediately fails closed.
5. **Output Proof Certification**: Secondary independent `balanceOf` polling is rejected as uncertified post-state (`INDEPENDENT_POLL_REJECTED`). Unrelated external balance changes are rejected as state pollution (`STATE_POLLUTION_DETECTED`). EVM call success without trace certification is strictly held at `OUTPUT_UNVERIFIED` (`output_verified=False`, `net_output_atoms=None`), preventing speculative profits from entering the ledger.

---

## 2. Independent Verification Test Suite

The verification suite consists of 25 dedicated test cases:

| Test File | Cases | Focus | Verdict |
|---|---|---|---|
| `tests/arc_v3/independent/test_arc_accounting.py` | 16 | Dual-interface USDC (18d/6d), anti-double-counting, dust bounds, EIP-7708 event dedup, single-deduction fee netting, and profile isolation | **PASS (16/16)** |
| `tests/arc_v3/independent/test_output_proof.py` | 9 | OutputEvidenceVerifier state-diff certification, polling rejection, anti-pollution, caller/recipient attribution, and ArcOutputAdapter decoupling | **PASS (9/9)** |

Execution stats: **collected 25, passed 25, failed 0, skipped 0** in 0.28s.

---

## 3. Findings and Invariant Matrix

| Invariant ID | Target Rule | Audit Observation | Enforcement Status |
|---|---|---|---|
| **ARC-ACC-01** | Dual-Interface Balance | Native 18d and ERC-20 6d scale by $10^{12}$; dust tracked explicitly | **VERIFIED** |
| **ARC-ACC-02** | Anti-Double-Counting | Adding native and ERC-20 views raises `ArcValidationError` | **VERIFIED** |
| **ARC-ACC-03** | Dust Indeterminacy | ERC-20 only view sets `dust_atoms=None`, bounds native $E \cdot 10^{12} \le N < (E+1)\cdot 10^{12}$ | **VERIFIED** |
| **ARC-ACC-04** | Allowance Decoupling | Native transfer authority cannot be synthesized from ERC-20 allowance | **VERIFIED** |
| **ARC-ACC-05** | EIP-7708 Deduplication | Dual-emitted transfers collapsed to 18d system log; multi-transfer preserved | **VERIFIED** |
| **ARC-ACC-06** | Single-Deduction Netting | Gas deducted exactly once; output floor requires principal + gas + 1 atom | **VERIFIED** |
| **ARC-PROOF-01** | Anti-Polling Guard | Independent `balanceOf` query fails as `INDEPENDENT_POLL_REJECTED` | **VERIFIED** |
| **ARC-PROOF-02** | Anti-Pollution Guard | Injection of funds from untracked third-party accounts detected and rejected | **VERIFIED** |
| **ARC-PROOF-03** | Profit Decoupling | Unverified trace locks `output_verified=False` and `net_output_atoms=None` | **VERIFIED** |

---

## 4. Residual Risks and Gate Recommendations

1. **Residual Risks**:
   - Trace generation in offline unit testing utilizes deterministic fixtures and synthetic state-diff entries; live trace format compatibility depends on specific node client RPC implementations (`debug_traceCall` vs `trace_call`).
   - Live network gas pricing remains subject to EIP-1559 base fee volatility between quote block and execution block.

2. **Gate Recommendation**:
   - The accounting and output proof invariants satisfy all requirements for `G2_CODE`.
   - M4 approves integrating T46 into the candidate baseline.
