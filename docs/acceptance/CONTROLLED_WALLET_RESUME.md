# Controlled wallet resume — authorized offline integration

User approved explicit, audited recovery only when no unresolved transaction exists, ledger history is consistent and budget remains. Historical loss and bound policy must not reset.

Independent diff review: /tmp/arc-resume-r3-admission/RESULT.md. Recovery validates canonical paused state, terminal intent records and receipt identities, exact Fraction loss sum against spent, pending exclusion and positive remaining budget under the same BEGIN IMMEDIATE transaction. Success changes paused and PAUSED base modes to PROBE and inserts an audit record; no automatic promotion or live authorization.

Integration logs: /tmp/arc-controlled-resume-integrate/. Joint tests: 60 passed; manifest/import tests: 16 passed; full Ruff passed; full Mypy passed (491 files). Collection: 2521 tests, 26 errors, exit 2. Original unresolved legacy tests are retained; this is not whole-repository completion. Earlier R2 corruption regression produced 3 failures and 1 positive control; corrected implementation passes those frozen cases.

Local database consistency is not proof of chain authenticity or resistance to coordinated alteration of all database history. No real RPC, funds, deployment, commit or push was performed.
