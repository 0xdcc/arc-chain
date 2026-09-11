# ARC CONTRACT INTERFACES v1.0.0

- **Plan ID**: `ARC-4B-v3.1-20260911-13817f4`
- **Contract Digest**: `604333c5d6857baece331bc6c78ec44f3eb6ed19e822b8a535928cf1ea14126e`
- **Target Network**: Arc Mainnet (`5042`) & Testnet (`5042002`)
- **Block Domain**: L1 (`BlockDomain.L1`)

## 1. Core Principles
1. **Pure Data Containers**: Contracts are immutable standard-library dataclasses (`frozen=True`) with explicit validation in `__post_init__`. Business logic, gas calculation, and simulation state transitions are strictly decoupled.
2. **Fail-Closed Type Safety**: Zero implicit conversions. Decimal precision, integer atoms, and domain boundaries are strictly enforced.
3. **Dual-Interface Balance Invariant**: Arc USDC operates on a single underlying balance domain. Native 18-decimal view and ERC-20 6-decimal view must not be summed or treated as separate independent balances.

## 2. Extension Entities

### `NetworkProfile`
Defines runtime profile for Arc chain.
- `chain_id`: `5042` (mainnet) or `5042002` (testnet).
- `block_domain`: Must be `BlockDomain.L1`.
- `max_trade_usd`: Hard cap `500.0 USD`.

### `RawEnvelope`
Raw ingest envelope storing durable cursors.
- `block_number`, `block_hash`, `cursor`, `received_at`, `payload_type`, `raw_payload`.

### `CoverageManifest`
Proof of contiguous block coverage over a scan window.
- Invariant: `covered_blocks + len(missing_blocks) == expected_blocks`.

### `TickCoverage`
Liquidity tick bitmap coverage evidence for V3/V4 pools.

### `CostEvidence`
Financial cost evidence with explicit atom units, kind, and estimation status.

### `SimulationEvidenceBridge`
Normalized readonly simulation outcome.
- Enforces truth invariants: `output_verified=True` requires `call_succeeded=True`. `OUTPUT_UNVERIFIED` cannot claim verified positive profit.

### `MarketStructureEvent`
Canonical domain event for pool deployments, fee updates, and liquidity transitions.

### `OtcQuote`
Read-only off-chain OTC channel quote representation.
