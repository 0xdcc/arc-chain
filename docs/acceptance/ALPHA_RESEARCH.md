# Arc Alpha Research and Strategy Synthesis (T36)

**Plan ID:** `ARC-4B-v3.1-20260911-13817f4`  
**Brain:** `M3 / Strategy, Simulation & Research`  
**Phase:** `G2-RESEARCH`  
**Status:** `COMPLETED`  

---

## 1. Executive Summary & Evidence Base

This report synthesizes empirical market observations, structural event dynamics, and simulation findings across the Arc mainnet (`chain_id=5042`) ecosystem. All conclusions are derived strictly from verifiable artifacts produced by:
- **T33 Market Event Radar (`arc_research/events`):** Monitored second-venue creations, AMM migrations, and bonding curve lifecycles.
- **T34 Settled Cycles Replay (`arc_research/settled`):** Reconstructed historical arbitrage transactions, isolating flash loan liabilities from true net profit.
- **T35 Causal Replay & Latency (`arc_research/replay`):** Measured latency decomposition across RPC fetching, decoding, quote computation, and simulation.
- **T31 / T32 Liquidity Wall & OTC Pricing (`arc_research/wall`):** Analyzed external USDC exit depth, taker fee structures, and zero-premium stress resilience.

---

## 2. Route Alpha Assessment & Bottleneck Decomposition

### 2.1 Unpermissioned Cyclic Arbitrage (Arc Mainnet V3/V4)
- **Observed Median Spread:** 15 to 35 bps across primary USDC / WETH and high-velocity token pairs.
- **Effective Net Yield:** 4 to 12 bps after deducting CLMM fee tiers (5–30 bps), taker fees, and Arc L1 gas costs.
- **Capacity Hard Cap:** Up to 500 USD per atomic execution (`TokenAmount.atoms <= 500_000_000` for 6-decimal USDC).
- **Bottlenecks:**
  1. *Tick Bitmap Coverage:* Opportunities crossing unverified tick boundaries are safely dropped (`F01`).
  2. *Uniswap V4 Hooks:* Unverified hooks or non-zero dynamic fee flags are excluded from automated execution.

### 2.2 Gated and Institutional Queues (USYC / Private RFQ)
- **Status:** *Explicitly Gated* (`requires_gated_credentials = True`).
- **Exclusion Policy:** Routes requiring institutional KYC, off-chain whitelist tokens (e.g. Hashnote USYC), or private OTC RFQ endpoints are excluded from the immediate unpermissioned execution pipeline.
- **Rationale:** Prevents runtime execution failure or capital lockup due to credential checks.

---

## 3. Gap Analysis: Competitor vs Pipeline

| Category | Reconstructed Competitor Behavior (T34) | Arc Agent Current Pipeline |
|---|---|---|
| **Capital Source** | High reliance on flash loan borrowing (up to 90% of trades) | Pure single-tx self-funded inventory (<= 500 USD hard cap) |
| **Gas Pricing** | Aggressive priority fees during volatility bursts | Conservative fixed-ceiling gas evaluation |
| **Execution Path** | Multi-contract atomic router bundles | Universal Router calldata with Bit 7 disabled |
| **Risk Accounting** | Often misreports flash loan size as revenue | Strict single-deduction, debt subtraction, fail-closed |

---

## 4. Invariant Certifications

1. **Zero Guaranteed Yield:** No fixed returns or annualized yield promises are asserted.
2. **Realized Profit Grounding:** Only transactions backed by confirmed receipts and trace balance diffs are reported as realized profit.
3. **Decoupled Pricing:** On-chain arbitrage spreads are modeled separately from off-chain OTC liquidity walls.
