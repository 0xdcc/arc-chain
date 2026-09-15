# Permit2 digest policy and remaining protocol migration

Status: VERIFIED locally; whole repository still incomplete.

Permit2 source and 18 digest tests independently approved at /tmp/arc-permit2-digest-admission/RESULT.md. Protocol migration independently approved at /tmp/arc-remaining-spec-final/RESULT.md. Installed candidates preserve the reviewed bytes.

Real bwrap checks: joint run 367 passed and 1 failed (stale encoding adaptation SHA). The failure was traced to the earlier protocol-whitelist integration omitting its adaptation hash update. Updated only that documented hash/reason to the already independently verified encoding candidate. Follow-up full manifest/import suites: 16 passed, exit 0. This is a follow-up closure, not a fabricated rerun of the original joint command.
Full Ruff exit 0; full Mypy exit 0, 470 source files. Audit command exit 0. Collection: 2352 tests, 29 errors, exit 2; removed only tests/test_remaining_protocols.py, no new errors. The full repository runtime failure count is still unverified.

Scope: explicit caller digest normalization and complete configuration checking; not independent certification of live deployed contracts. Legacy manual planner test now verifies explicit unit arithmetic, not restoration of a retired planner. Fork test proves label rejection, not factory/codehash identity.

Logs: /tmp/arc-protocol-digest-final/. No push/commit, network RPC, funds operation or service change.
