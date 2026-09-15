# Frozen historical catalog topology migration

Status: VERIFIED local historical-observation scope only.

Independent approval: /tmp/arc-historical-topology-admission/RESULT.md. The original two tests assert catalog schema, 137 V3 pools, at least 200 V4 pools, >5000 cycles and >=500 two-hop cycles. Restored exact git 13817f4 catalog blobs into tests/fixtures/historical/robinhood, never the Arc registry. Actual counts: 137 V3 + 209 V4, 5668 cycles including 548 two-hop cycles. No fabricated factory, PoolDescriptor, transport, executable capability or ledger.

Integration: 28 joint tests passed (two historical, ten small-graph, sixteen manifest/import), exit 0. Full Ruff passed; full Mypy passed (474 source files). Upstream audit exit 0. Collection: 2418 tests, 26 errors, exit 2; exactly test_v4_pipeline_integration.py removed, no new errors. Full repository still incomplete.

Logs: /tmp/arc-historical-topology-integrate/. Archived JSON files retain exact source bytes and hash provenance in the independent report. No commit/push, real RPC, funds or service operations.
