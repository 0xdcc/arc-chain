# Read-only monitor import migration

Status: VERIFIED — local incremental repair.

Only imports in apps/monitor/service.py and tests/test_readonly_monitor_app.py moved to the already verified research namespaces. Non-import AST and all eight test bodies preserved, independently reviewed at /tmp/arc-readonly-monitor-admission/RESULT.md.

Integration evidence: /tmp/arc-readonly-monitor-integrate/.
- Joint monitor/reporting/strategies/domain/manifest suites: 104 passed, exit 0.
- Full Ruff exit 0; full Mypy exit 0 (470 source files).
- Upstream audit exit 0. Audit still reports legacy run_layers and obligation-mapping informational defects; those are not fixed here.
- Collection 2360 tests, 28 errors, exit 2. Exactly tests/test_readonly_monitor_app.py removed from the previous 29-error set, no new errors.

Original eight monitor tests now collect and run; whole repository is not yet green. No push, commit, service changes, live RPC or funds operations.
