# ARC MAINNET CHANGE & PUBLIC OPENING CHECKLIST (T42)

- **Plan ID**: `ARC-4B-v3.1-20260911-13817f4`
- **Target Network**: Arc Mainnet (`5042`)

---

## 🔴 1. Public Opening Date Non-Authorization Rule
**The arrival of September 16, 2026 (public launch date) DOES NOT grant automatic trading authorization.**
- The system remains in `ARMED = False` (readonly simulation) mode by default.
- Real funds execution, wallet initialization, or transaction broadcasting requires separate, explicit, user-authorized activation.

---

## 2. Mainnet Deployment Ingestion Protocol
When official Uniswap V3 / V4 or bridge deployments occur on Arc Mainnet:
1. **Never Hardcode in Core**:
   - New venue addresses must be added to `configs/arc/venues.json` or registered via `arc_markets.deployments.ArcDeploymentsRegistry`.
2. **Review Lifecycle Bridge**:
   - Newly discovered deployments enter `pending_review` state.
   - Requires bytecode verification and chain ID (5042) confirmation before promotion to `verified`.
3. **Foreign Chain Exclusion**:
   - Robinhood (4663) addresses are physically rejected with `ForeignChainIdError`.

---

## 3. Real Funds Pre-Flight Checklist (Future G3 Gate)
Before any live execution can ever be considered:
- [ ] Explicit user prompt with authorized amount budget recorded in `.coord/`
- [ ] Pathfinder single-shot mode active: initial transaction capped at $\le 1.0\text{ USD}$
- [ ] Single-shot in-flight latch active: wallet locks until on-chain receipt confirms
- [ ] AST zero-float check verified across financial calculation paths
- [ ] Realized net profit calculation requires granular ERC-20 Transfer log netting
- [ ] Output floor guarantee enforced: $\text{floor} = \max(\text{min\_out}, \text{in} + \lceil\text{gas}\rceil + 1\text{ atom})$
- [ ] Hard maximum single trade cap locked at $\le 500.0\text{ USD}$
