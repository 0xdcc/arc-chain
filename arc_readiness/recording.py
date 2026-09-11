"""Sanitization and structured failure classification for Arc read-only RPC recordings."""

from __future__ import annotations

from typing import Any

SENSITIVE_KEYS = frozenset(
    {
        "private_key",
        "privatekey",
        "secret",
        "bearer",
        "authorization",
        "mnemonic",
        "password",
    }
)


def sanitize_rpc_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Recursively redact any potential private keys, bearer tokens, or sensitive credentials."""
    sanitized: dict[str, Any] = {}
    for key, value in payload.items():
        key_lower = str(key).lower()
        if any(sens in key_lower for sens in SENSITIVE_KEYS):
            sanitized[key] = "<REDACTED>"
        elif isinstance(value, dict):
            sanitized[key] = sanitize_rpc_payload(value)
        elif isinstance(value, list):
            sanitized[key] = [
                sanitize_rpc_payload(item) if isinstance(item, dict) else item for item in value
            ]
        else:
            sanitized[key] = value
    return sanitized


def classify_rpc_failure(
    status_code: int | None = None,
    rpc_error_code: int | None = None,
    error_message: str | None = None,
) -> str:
    """Classify RPC and transport failures into explicit deterministic error domains.

    Prevents confusing network rate limits or invalid RPC methods with smart contract reverts.
    """
    msg = (error_message or "").lower()

    if status_code == 429 or "rate limit" in msg or "too many requests" in msg:
        return "RATE_LIMITED"

    if status_code == 408 or "timeout" in msg or "timed out" in msg:
        return "TIMEOUT"

    if rpc_error_code == -32601 or "method not found" in msg:
        return "METHOD_NOT_FOUND"

    if rpc_error_code == 3 or "execution reverted" in msg or "revert" in msg:
        return "EXECUTION_REVERTED"

    return "TRANSPORT_FAILURE"
