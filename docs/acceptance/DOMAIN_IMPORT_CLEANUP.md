# Domain test import cleanup integration

Status: VERIFIED test-environment repair; unresolved dependencies exposed honestly.

Candidate hash 446458e41f4bb6a957c0e862bb1599a555dbe28de546be48d0e9808bc468c76e, independently approved at /tmp/arc-domain-cleanup-review/RESULT.md. All test-function AST bodies equal the previous version. Removed only global external-path and backtest MagicMock injection plus unused imports/noqa.

Integration: domain plus manifest/import suites 59 passed, exit 0. Full Ruff and Mypy passed (477 source files). Independent fresh bwrap process importing domain tests produced no backtest modules or external repair path.

Collection now 2420 tests, 27 errors, exit 2. test_multi_rpc.py is newly visible as an actual missing backtest dependency rather than 11 false runtime Mock failures; this is not claimed as a reduction in global errors. The RPC pool candidate remains under repair. Full-runtime totals await another aggregate run.

Logs: /tmp/arc-domain-cleanup-integrate/. No real RPC/funds, commit/push, service or venv changes.
