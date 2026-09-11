# Arc Launchpad Evidence and Verification Matrix

## 1. Mechanism Taxonomy

Arc token launches are partitioned into three verifiable operational states:

1. **DIRECT_V3_LISTING**:
   - Liquidity is provided directly into a verified Uniswap V3 (or V4) pool.
   - Quote Eligibility: **Eligible** immediately upon pool initialization and non-zero active liquidity.
   - Safety Barrier: Standard token decimal checks and contract deployment verification apply.

2. **BONDING_CURVE_PRE_GRADUATION**:
   - Token is traded on an isolated internal bonding curve (virtual reserves).
   - Quote Eligibility: **Strictly Ineligible** for CLMM arbitrage graph.
   - Safety Barrier: Any routing attempt through pre-graduation bonding curves fails closed with `ArcMarketIneligibleError`.

3. **MIGRATED_POST_GRADUATION**:
   - Token has accumulated sufficient reserves, triggered graduation, and migrated liquidity into a verified CLMM pool.
   - Quote Eligibility: **Eligible** ONLY when anchored by verified `target_clmm_pool`, non-null `graduation_tx_hash`, and valid `graduation_block`.

---

## 2. Unsupported Venues and Missing Evidence Protocol

In accordance with T17 specifications, any candidate venue lacking reproducible bytecode, public ABI, or verified transaction traces is classified as `UNSUPPORTED_MISSING_EVIDENCE`.

| Candidate Venue | Classification | Missing Evidence Detail |
|---|---|---|
| `pump_fun_clone_unverified` | UNSUPPORTED_MISSING_EVIDENCE | Missing verified deployment address and public ABI on Arc testnet/mainnet. |
| `moonshot_portal_v1` | UNSUPPORTED_MISSING_EVIDENCE | Missing transaction trace samples of migration execution to Uniswap V3. |
| `anonymous_fair_launch` | UNSUPPORTED_MISSING_EVIDENCE | Bytecode unverified on block explorer; no source proof. |

Synthesizing mock adapters for venues in `UNSUPPORTED_MISSING_EVIDENCE` status is strictly prohibited.
