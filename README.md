# Arc Arbitrage Engine (v3.1 Modular)

- **Target Network**: Arc Mainnet (Chain ID: `5042`) & Testnet (`5042002`)
- **Plan ID**: `ARC-4B-v3.1-20260911-13817f4`
- **Current Gate**: `G2_CODE: PASS` | `G2_LIVE: PENDING_AUTHORIZATION`
- **Immutable Contract Digest**: `604333c5d6857baece331bc6c78ec44f3eb6ed19e822b8a535928cf1ea14126e`

---

## 1. Quickstart

### Virtual Environment Setup
```bash
cd /root/projects/crypto/arc-chain
source venv/bin/activate
```

### Run Test Suite
```bash
./venv/bin/python -m pytest tests/arc_v3/ -q -o cache_dir=/tmp/pytest_cache
# 321 passed, 100% test coverage across all domains
```

### CLI Tooling
```bash
# Ingest Collector (Offline Fixture Mode)
./venv/bin/python apps/arc_collect.py --chain-id 5042 --from-block 1000 --to-block 1010 --output-dir runtime-data/ingest --fixture-mode

# Shadow Opportunity Evaluator
./venv/bin/python apps/arc_shadow.py --chain-id 5042 --ledger-dir runtime-data/ledgers --fixture-mode

# Historical Block Replay Scanner
./venv/bin/python apps/arc_history.py --chain-id 5042 --from-block 1000 --to-block 1005 --output-dir runtime-data/history --fixture-mode
```

---

## 2. Directory Layout

- `arbitrage_contracts/`: Immutable contract definitions and extensions for Arc.
- `arc_readiness/`: Network profiles, USDC dual-interface accounting, and eligibility rules.
- `arc_markets/`: Venue deployments registry, V3 discovery, and V4 StateView 4-word decoding.
- `arc_ingest/`: Ingest transport, fixed-block sampling, durable cursors, and raw recorder.
- `arc_opportunities/`: CLMM quote bridge, cost models, ledger, and shadow service.
- `arc_execution/`: Physical risk policy, authorization isolation, and Nonce journaling state machine.
- `arc_research/`: Liquidity walls, settled arbitrage event attribution, and latency causal replay.
- `arc_runtime/`: Single-instance lifecycle, circuit breaker health, and Hermes reporting.
- `docs/acceptance/`: Independent red-team review reports delivered by M4 (all PASS).
- `docs/coordination/`: Coordination manifests, release candidate declarations, and user delivery reports.

---

## 3. Operational Invariants & Safety Redlines

- **Zero Unapproved Trading**: Real execution, signing, and broadcasting are physically disabled in code.
- **USDC Single-Deduction**: 18d native accounting and 6d ERC-20 scaling factors are separated to prevent double-counting.
- **Uniswap V4 Slot0**: Strictly decodes 4 words (128 bytes) with two's complement tick parsing; fails closed on truncation.
- **Hard Cap**: Single trade cap strictly limited to $\le 500.0\text{ USD}$; pathfinder mode limited to $\le 1.0\text{ USD}$.
- **Public Launch Non-Authorization**: Arrival of September 16, 2026 does **not** grant automatic live trading authority.
