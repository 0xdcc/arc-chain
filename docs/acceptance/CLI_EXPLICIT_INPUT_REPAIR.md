# Explicit offline CLI input integration

Status: VERIFIED limited implementation; old no-input semantics remain PENDING.

Independent approval: /tmp/arc-cli-explicit-admission/RESULT.md. CLI simulate and trade --dry-run now delegate explicit --input JSONL to apps.atomic_simulate.main and the real offline execution pipeline. No fabricated default plan is used. Missing input returns INPUT_INSUFFICIENT / exit 1; RPC URLs, private keys and live trade are rejected.

Integration: manifest/import suites 16 passed. CLI combined suites 25 passed, 2 failed (unchanged old tests expecting success without any input). Full Ruff passed; Mypy passed (480 files). Synthetic fixture outcomes are structured pipeline results, never claimed as realized profit or live execution.

No changes to those two old expectations, no fake success, skip or deletion. Full repository not complete. Logs: /tmp/arc-cli-explicit-integrate/. No commit/push or live RPC/funds/service operations.
