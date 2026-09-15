# Coordinator legacy obligations — incremental coverage

Independent review: /tmp/arc-coordinator-coverage-review/RESULT.md. Integrated test SHA256 e39cd527977a13a005927cd6307beefcb1152385b9f59190cfa7324540456e40.

Four incremental cases cover same-base UNKNOWN exclusion across restart, non-positive minimum output, unsupported base rejection, and the original static AST audit patterns. DOGE fixture now has a valid floor, isolating only asset admission. Removing the coordinator guard still triggers the ledger guard: mutation failure establishes the outer error contract, not an end-to-end admission bypass.

Integration: 49 joint tests passed; 16 manifest/import tests passed; full Ruff passed; Mypy passed (489 source files). Collection: 2510 tests, 26 errors; error file set unchanged. Old tests/test_funds_coordinator.py remains byte-for-byte intact. Its four previously identified behavior conflicts remain unresolved; no claim of whole-file migration or whole-repository completion.

No production code changed in this integration. No commit/push, live RPC, funds or service operation. Logs: /tmp/arc-coordinator-coverage-integrate/.
