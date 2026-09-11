# Arc Execution Binding & Decoupling Matrix (T25)

## 1. Scope & Objective
This document outlines the architectural decoupling of W5 Execution Planning and Calldata Encoding from Robinhood (4663) and hardcoded contracts, binding it exclusively to reviewed and verified Arc deployments (Chain ID 5042 mainnet, 5042002 testnet).

## 2. Decoupling Call-Chain Matrix

| Layer | Robinhood Legacy (Banned on Arc) | Arc Architecture (Enforced by T25) | Verification Mechanism |
|---|---|---|---|
| **Chain ID** | `4663` | `5042` (mainnet) or `5042002` (testnet) | `ArcExecutionDeploymentBinding.__post_init__` rejects 4663 |
| **Router** | `0x8876789976dEcBfCbBbe364623C63652db8C0904` | Verified Arc UniversalRouter | Rejects known Robinhood addresses, enforces 42-char EVM hex |
| **Permit2** | `0x000000000022D473030F116dDEE9F6B43aC78BA3` | Arc reviewed Permit2 (or None) | Rejects Robinhood addresses |
| **Value (Native)** | Value > 0 allowed on Robinhood | Strictly `value_atoms = 0` on Arc | `ArcExecutionPlan` raises on non-zero value |
| **Execution Authorization** | `ARMED` flag required | `can_atomic_execute` permanently `False` | Immutable property, raises if set to `True` |

## 3. Command Security & Zero-Revert Invariants
1. **Bit 7 (0x80 / allow_revert) Strictly Unset**:
   - Universal Router command byte mask enforces `cmd & 0x80 == 0`.
   - Partial hop failure causes atomic contract rollback; no partial trades are tolerated.
2. **Intermediate vs. Final Hop Routing**:
   - Intermediate swap outputs settle at `ADDRESS_THIS` (Router internal balance).
   - Final hop output is strictly directed to `MSG_SENDER` (the calling account).
3. **Strict Principal Floor**:
   - Final hop output minimum is bound to `plan.min_amount_out` (which satisfies `amount_in + gas_atoms + 1`).
