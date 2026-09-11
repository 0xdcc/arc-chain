# W5-B Input Gate Synthetic Test Fixtures (v1)

This directory contains pure offline test fixtures for the W5 atomic execution input gate
(`atomic_execution/inputs.py`) and domain envelopes (`atomic_execution/models.py`).
All addresses, quotes, and state proofs in this directory are synthetic; they do not represent
live on-chain positions or broadcasted transactions.

## Files

- `input-cases.jsonl`: Discrete JSONL test cases for gate validation, covering positive 2/3-hop
  cycles and negative test vectors across acceptance categories C01 through C05.
- `manifest.json`: Cryptographic integrity manifest recording partition SHA-256 digests and
  case classification summaries.

## Covered Gates

- **C01 (Format and Types)**: Missing fields, boolean masquerading as integer atoms, uint256 overflows.
- **C02 (Chain and Loop Closure)**: Unsupported chains, non-WETH/USDG base assets, open currency exposure.
- **C03 (Hop Count and Topology)**: 1-hop routes, unsupported 4-hop routes, duplicate pools, Uniswap V2 protocol.
- **C04 (Eligibility and Capabilities)**: V4 pools with non-zero hooks, dynamic fee models, unapproved assets.
- **C05 (State and Timing)**: Non-QUOTED quote status, unready StateVersion, mixed block hashes.

## Verification

The test suite in `tests/atomic_execution/test_inputs.py` reads `input-cases.jsonl` and validates
that every case reproduces the expected gate outcome and rejection attribution.
