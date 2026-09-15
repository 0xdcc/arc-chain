# Explicit read-only RPC pool integration

Status: VERIFIED local module; repository incomplete.

Reviewed candidate hashes match installed source and two tests. Independent evidence: /tmp/arc-rpc-pool-r2-review/. R1 plus frozen boundaries: 10 failed / 20 passed; R2: 30 passed. Integration with domain and manifest suites: 89 passed. Full Ruff passed; full Mypy passed (479 files).

Collection: 2450 tests, 26 errors, exit 2. Exactly tests/test_multi_rpc.py removed from the previous 27-error set. This replaces the previously injected MagicMock with real, explicitly configured read-only client behavior. Historical URLs are only test fixtures; no production defaults were restored.

Full runtime totals require a refreshed aggregate run; do not derive a whole-repository pass count from these targeted suites. No live RPC, funds, service, commit or push operation.

Logs: /tmp/arc-rpc-pool-integrate/.
