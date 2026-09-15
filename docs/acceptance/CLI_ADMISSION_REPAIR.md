# CLI admission and imports — limited integration

Status: VERIFIED limited repair; simulation gaps OPEN.

Independent approval: /tmp/arc-cli-admission-review/RESULT.md. Research monitor/replay imports restored. Live trade and missing required key rejection precede legacy execution imports, RPC resolution, plan construction and ledger creation. No new signing or network capability.

Integrated checks: CLI suite and side-effect tests 14 passed, 2 failed (simulate and trade --dry-run still depend on retired execution modules). Manifest/import tests 16 passed. Full Ruff passed. Full Mypy passed, 477 source files. Full repository collection/runtime totals require a fresh aggregate run; no inference from these targeted counts.

Evidence: /tmp/arc-cli-admission-integrate/. No commit, push, live RPC or funds action.
