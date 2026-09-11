# W7 RWA Research Test Fixtures (v1)

This directory contains deterministic test fixtures and mock models for W7 RWA research, oracle observation, and discrete liquidity valuation.

- `manifest.json`: Cryptographic integrity manifest indexing all fixture files in this directory.
- `models.json`: Synthetic baseline records covering full, partial, and minimal `RwaResearchRecord` representations.
- `normalize.json`: Test vectors for corporate action multiplier, UI scaling, and price conversion.
- `validity.json`: Test cases for oracle staleness, session flags, and sequencer uptime barriers.
- `quotes.json`: Synthetic discrete quotes and multi-tier liquidity curves for RWA tokens.
- `e2e.json`: End-to-end observation bundles for CLI evaluation and replay testing.
