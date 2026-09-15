# Indicative quote capacity migration

Status: VERIFIED local increment; full repository incomplete.

Independent review: /tmp/arc-quote-capacity-admission/RESULT.md. Internal arithmetic uses Fraction; float exists only at the historical return boundary. The retained function name does not mean realized PnL. Gas remains unaccounted for; these estimates never authorize execution or prove liquidity.

Original round2 tests: seven parametrized nodes preserved with import-only changes. Boundary cases: 39 nodes. Freshness guard removal made both outside-window cases fail. Integration with manifest/import checks: 62 passed, exit 0. Full Ruff and Mypy passed (472 files), audit exited 0. Collection: 2406 nodes, 27 errors, exit 2; exactly test_round2_regressions.py removed from the former 28-error set. No new collection errors. Source manifest has 536 files. Logs: /tmp/arc-quote-capacity-integrate/.

No commit/push, live RPC, service or funds operations. Other legacy runtime/collection obligations remain unresolved.
