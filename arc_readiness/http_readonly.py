"""Limited HTTP implementation of ReadOnlyRpcTransport with explicit proxy and circuit breaker."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from arc_readiness.errors import ArcValidationError
from arc_readiness.rpc_readonly import (
    ReadOnlyRpcTransport,
    validate_batch_methods,
    validate_rpc_method,
)


class ArcCircuitBreakerTrippedError(ArcValidationError):
    """Raised when consecutive RPC failures exceed the circuit breaker limit."""


class HttpReadOnlyRpcTransport(ReadOnlyRpcTransport):
    """Production-grade read-only HTTP transport for Arc RPC queries.

    Enforces:
    - Zero network access unless allow_network is explicitly enabled.
    - Zero implicit environment/local proxy sniffing: only explicit proxy_url is honored.
    - Strict method allowlist with fail-closed checks on single and batch calls.
    - Circuit breaker: trips after max_consecutive_failures (default 3).
    """

    def __init__(
        self,
        endpoint_url: str,
        timeout_seconds: float = 10.0,
        max_consecutive_failures: int = 3,
        proxy_url: str | None = None,
        allow_network: bool = False,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        super().__init__(endpoint_url=endpoint_url)
        self.timeout_seconds = timeout_seconds
        self.max_consecutive_failures = max_consecutive_failures
        self.proxy_url = proxy_url
        self.allow_network = allow_network
        self._consecutive_failures = 0
        self._is_tripped = False

        if opener is not None:
            self._opener = opener
        else:
            self._opener = self._build_explicit_opener(proxy_url)

    @staticmethod
    def _build_explicit_opener(proxy_url: str | None) -> urllib.request.OpenerDirector:
        """Build an OpenerDirector strictly isolated from system environment proxies."""
        handlers: list[urllib.request.BaseHandler] = []
        if proxy_url:
            # Explicit proxy handler only
            handlers.append(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
        else:
            # Empty proxy handler blocks all environment (HTTP_PROXY, ALL_PROXY, etc.) sniffing
            handlers.append(urllib.request.ProxyHandler({}))
        return urllib.request.build_opener(*handlers)

    @property
    def is_circuit_broken(self) -> bool:
        return self._is_tripped

    def reset_circuit_breaker(self) -> None:
        self._consecutive_failures = 0
        self._is_tripped = False

    def _sanitize_error(self, err: Any) -> str:
        """Sanitize error message to ensure endpoint credentials/secrets are not leaked."""
        err_msg = str(err)
        if not self.endpoint_url:
            return err_msg

        try:
            parsed = urlsplit(self.endpoint_url)
            if parsed.password:
                err_msg = err_msg.replace(parsed.password, "<REDACTED>")
            if parsed.username and parsed.username not in ("http", "https"):
                if parsed.password and f"{parsed.username}:{parsed.password}" in err_msg:
                    err_msg = err_msg.replace(f"{parsed.username}:{parsed.password}", "<REDACTED>")
                elif f"{parsed.username}:<REDACTED>" in err_msg:
                    pass
                else:
                    err_msg = err_msg.replace(parsed.username, "<REDACTED>")

            if parsed.netloc and "@" in parsed.netloc:
                netloc_user, netloc_host = parsed.netloc.rsplit("@", 1)
                safe_netloc = f"<REDACTED>@{netloc_host}"
                safe_url = urlunsplit(
                    (parsed.scheme, safe_netloc, parsed.path, parsed.query, parsed.fragment)
                )
                err_msg = err_msg.replace(self.endpoint_url, safe_url)
        except Exception:
            pass

        return err_msg

    def request(self, method: str, params: Sequence[Any] | None = None) -> Any:
        """Execute a single JSON-RPC read-only request."""
        validate_rpc_method(method)

        if self._is_tripped:
            raise ArcCircuitBreakerTrippedError(
                f"RPC Circuit Breaker is TRIPPED ({self._consecutive_failures} consecutive failures). "
                "Refusing to loop indefinitely."
            )

        if not self.allow_network:
            raise ArcValidationError(
                "Network access not authorized: HttpReadOnlyRpcTransport is in offline-default mode. "
                "Explicit authorization required to perform outbound RPC calls."
            )

        safe_params = list(params) if params is not None else []
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": safe_params,
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url=self.endpoint_url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Arc-Readiness-ReadOnly/1.0",
            },
            method="POST",
        )

        try:
            with self._opener.open(req, timeout=self.timeout_seconds) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
            self._consecutive_failures = 0  # reset on success
            if "error" in resp_data:
                err = resp_data["error"]
                raise ArcValidationError(f"RPC server returned error: {err}")
            return resp_data.get("result")
        except Exception as e:
            self._consecutive_failures += 1
            safe_err = self._sanitize_error(e)
            if self._consecutive_failures >= self.max_consecutive_failures:
                self._is_tripped = True
                raise ArcCircuitBreakerTrippedError(
                    f"RPC failure {self._consecutive_failures}/{self.max_consecutive_failures}: {safe_err}. "
                    "Circuit Breaker TRIPPED. Halting repeated requests."
                ) from e
            raise ArcValidationError(f"RPC request failed: {safe_err}") from e

    def request_batch(
        self,
        calls: Sequence[tuple[str, Sequence[Any]]],
    ) -> list[Any]:
        """Execute a batch of read-only calls, rejecting the entire batch if any method is prohibited."""
        if not calls:
            raise ArcValidationError("Batch calls list cannot be empty")

        for idx, call in enumerate(calls):
            if not isinstance(call, (tuple, list)) or len(call) < 2:
                raise ArcValidationError(
                    f"Invalid batch call format at index {idx}: expected (method, params) sequence"
                )

        methods = [call[0] for call in calls]
        validate_batch_methods(methods)

        if self._is_tripped:
            raise ArcCircuitBreakerTrippedError(
                f"RPC Circuit Breaker is TRIPPED ({self._consecutive_failures} consecutive failures)."
            )

        if not self.allow_network:
            raise ArcValidationError("Network access not authorized in offline mode.")

        batch_payload = [
            {
                "jsonrpc": "2.0",
                "id": idx + 1,
                "method": call[0],
                "params": list(call[1]) if call[1] else [],
            }
            for idx, call in enumerate(calls)
        ]
        body = json.dumps(batch_payload).encode("utf-8")
        req = urllib.request.Request(
            url=self.endpoint_url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Arc-Readiness-ReadOnly/1.0",
            },
            method="POST",
        )

        try:
            with self._opener.open(req, timeout=self.timeout_seconds) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))

            if not isinstance(resp_data, list):
                raise ArcValidationError("Unexpected batch response format: expected list")

            expected_ids = set(range(1, len(calls) + 1))
            responses_by_id: dict[int, dict[str, Any]] = {}

            for elem in resp_data:
                if not isinstance(elem, dict):
                    raise ArcValidationError(
                        f"Malformed batch response element: expected JSON-RPC object, got {type(elem).__name__}"
                    )

                if "id" not in elem:
                    raise ArcValidationError("Malformed batch response element: missing 'id' field")

                elem_id = elem["id"]
                # Guard against bool being treated as int in Python (isinstance(True, int) is True)
                if isinstance(elem_id, bool) or not isinstance(elem_id, int):
                    raise ArcValidationError(
                        f"Invalid batch response id type: expected int, got {type(elem_id).__name__}"
                    )

                if elem_id not in expected_ids:
                    raise ArcValidationError(f"Unknown batch response id: {elem_id}")

                if elem_id in responses_by_id:
                    raise ArcValidationError(f"Duplicate batch response id: {elem_id}")

                responses_by_id[elem_id] = elem

            missing_ids = expected_ids - set(responses_by_id.keys())
            if missing_ids:
                raise ArcValidationError(
                    f"Missing batch response for ids: {sorted(missing_ids)}"
                )

            # Reconstruct responses in the exact original request order
            results: list[Any] = []
            for idx in range(len(calls)):
                req_id = idx + 1
                item = responses_by_id[req_id]

                # Fail-closed on error sub-item according to single request contract
                if "error" in item:
                    err = item["error"]
                    raise ArcValidationError(f"RPC server returned error: {err}")

                # Distinguish legal result: null from missing result field
                if "result" not in item:
                    raise ArcValidationError(
                        f"Batch response missing 'result' field for id {req_id}"
                    )

                results.append(item["result"])

            self._consecutive_failures = 0
            return results
        except Exception as e:
            self._consecutive_failures += 1
            safe_err = self._sanitize_error(e)
            if self._consecutive_failures >= self.max_consecutive_failures:
                self._is_tripped = True
                raise ArcCircuitBreakerTrippedError(
                    f"Batch RPC failure {self._consecutive_failures}/{self.max_consecutive_failures}: {safe_err}. "
                    "Circuit Breaker TRIPPED. Halting repeated requests."
                ) from e
            raise ArcValidationError(f"Batch RPC request failed: {safe_err}") from e
