# Historical fixture staging closure

Status: VERIFIED incremental repair.

Independently approved two explicit PUBLIC_FILES additions at /tmp/arc-stage-fixture-review/RESULT.md. Integrated both paths into source_manifest.files and public_fixtures; total 543 entries. The helper is not tracked in the immutable import manifest. No generic path admission or symlink/hardlink guard changed.

Integrated manifest/import tests: 16 passed. Full Ruff and Mypy passed (477 files). Real stage_sources invocation inside the strict bwrap sandbox copied both exact git 13817f4 blobs; a consumer loading only staged JSON produced 346 pools / 5668 cycles. Independent review also demonstrated old helper rejects explicitly registered JSON and incomplete manifest omits files.

Evidence: /tmp/arc-stage-fixture-integrate/. Whole repository not green; no new aggregate runtime claim. No commit/push, live RPC, funds or service action.
