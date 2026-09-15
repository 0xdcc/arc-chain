# Fee units integration verification

Status: COMPLETED — incremental module only.

The parent recovered the timed-out integration run and verified both installed file hashes against the independently approved R3 candidate. No rerun of unchanged passing checks was needed.

- Fee-unit tests: 58 passed, exit 0.
- Related joint regression: 95 passed, exit 0.
- Manifest tests: 16 passed, exit 0.
- Full Ruff: passed, exit 0.
- Full Mypy: passed, 468 source files, exit 0.
- Upstream obligation audit: exit 0; informational legacy defects in the audit output remain disclosed in the raw log.
- Collection: 2308 tests collected, 30 errors, exit 2. Error sets unchanged from the 2250/30 baseline. This is NOT whole-repository success; runtime results for the whole repository remain unverified.

Source SHA-256: d673ac5b9b6d2d1df8bd97ba92822e0292493eb001316c81112281022ad2c336.
Test SHA-256: 153754c46f62182e05145fbf4eeec2ababfce347e4dc70297c67614700ab5b5c.

Evidence: /tmp/arc-fee-unit-integrate/logs/ and /tmp/arc-fee-unit-r3-review/.
No commit, push, production service change, live RPC, or funds operation performed for this integration.
