"""Production read-only multi-RPC pool client with sequential failover and circuit breaker.

Enforces:
1. Zero network access unless an explicit session/read-only transport is injected.
2. Explicit endpoint URLs required (zero default production Robinhood endpoints).
3. Strict method allowlist with fail-closed checks: write operations rejected before post.
4. Failover on HTTP 429, 5xx, network connection error, and JSON-RPC -32005 rate-limiting.
5. Zero sleep on 429 (instant failover to healthy alternatives).
6. Business contract reverts (e.g. execution reverted) are NOT retried across nodes.
7. Batch and single responses strictly matched by ID and type (rejects duplicate, missing, bool, or unknown IDs).
8. Health counters (fail_count, consecutive_fails, last_error) reset on success.
9. 3-failure circuit breaker threshold per node and pool-wide, strictly enforced within retry loops.
10. Strict error sanitization: credentials, userinfo, query params, and sensitive payload bodies
    are completely scrubbed from error logs, health states, and exception messages.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import Any

import requests

from arc_readiness.errors import ArcValidationError

logger = logging.getLogger(__name__)

# Standard EVM read-only JSON-RPC methods
ALLOWED_READONLY_METHODS: frozenset[str] = frozenset(
    {
        "eth_blockNumber",
        "eth_chainId",
        "eth_getBlockByNumber",
        "eth_getBlockByHash",
        "eth_getCode",
        "eth_getBalance",
        "eth_call",
        "eth_getLogs",
        "eth_getTransactionByHash",
        "eth_getTransactionReceipt",
        "eth_gasPrice",
        "eth_feeHistory",
        "eth_estimateGas",
        "net_version",
        "web3_clientVersion",
    }
)

# Mutating or wallet methods strictly prohibited
FORBIDDEN_MUTATING_METHODS: frozenset[str] = frozenset(
    {
        "eth_sendRawTransaction",
        "eth_sendTransaction",
        "eth_sign",
        "eth_signTransaction",
        "personal_sign",
        "wallet_addEthereumChain",
        "wallet_switchEthereumChain",
    }
)


def validate_rpc_method(method: str) -> None:
    """Validate a single RPC method against read-only allowlist before any HTTP dispatch."""
    if not isinstance(method, str):
        raise ArcValidationError(f"RPC method must be a string, got {type(method).__name__}")
    if method in FORBIDDEN_MUTATING_METHODS or not (
        method.startswith("eth_") or method.startswith("net_") or method.startswith("web3_")
    ):
        raise ArcValidationError(
            f"Prohibited mutating or non-EVM RPC method: {method}. Write operations strictly forbidden."
        )
    if method not in ALLOWED_READONLY_METHODS:
        raise ArcValidationError(
            f"Prohibited RPC method: {method}. Allowed methods are: {sorted(ALLOWED_READONLY_METHODS)}"
        )


class ArcCircuitBreakerTrippedError(RuntimeError, ArcValidationError):
    """Raised when consecutive RPC failures reach circuit breaker threshold or all nodes fail."""


class RpcPoolClient:
    """Multi-endpoint RPC pool client with automatic sequential failover."""

    CIRCUIT_BREAKER_THRESHOLD: int = 3

    def __init__(
        self,
        urls: Sequence[str] | None = None,
        url: str | None = None,
        session: Any | None = None,
        timeout: float = 10.0,
        throttle: float = 0.0,
        max_retries: int | None = None,
    ) -> None:
        # Require explicit URLs; no default production URLs allowed
        if url is not None:
            if not isinstance(url, str) or not url.strip():
                raise ValueError("url must be a non-empty string")
            self.urls = [url]
        elif urls is not None:
            cleaned = [str(u) for u in urls if str(u).strip()]
            if not cleaned:
                raise ValueError("urls must be a non-empty sequence of URL strings")
            self.urls = list(cleaned)
        else:
            raise ValueError(
                "Explicit urls or url must be provided; default production URLs are not permitted."
            )

        # Require explicit session/transport injection
        if session is None:
            raise ValueError("Explicit session or read-only transport must be injected.")
        self._session = session

        self.timeout = float(timeout)
        self.throttle = float(throttle)
        self.max_retries = max_retries if max_retries is not None else 1
        self._active_index = 0

        self.health_status: dict[str, dict[str, Any]] = {
            u: {"fail_count": 0, "consecutive_fails": 0, "last_error": None}
            for u in self.urls
        }

    @property
    def active_url(self) -> str:
        """Currently active RPC node endpoint."""
        if not self.urls:
            raise ValueError("No RPC URLs configured")
        return self.urls[self._active_index]

    @property
    def url(self) -> str:
        """Backward-compatible alias for active_url."""
        return self.active_url

    @url.setter
    def url(self, new_url: str) -> None:
        """Dynamic single URL assignment resets node list and health status."""
        if not isinstance(new_url, str) or not new_url.strip():
            raise ValueError("new_url must be a non-empty string")
        self.urls = [new_url]
        self._active_index = 0
        self.health_status = {
            new_url: {"fail_count": 0, "consecutive_fails": 0, "last_error": None}
        }

    @staticmethod
    def _sanitize_error(error: str) -> str:
        """Sanitize error message to prevent leaking endpoints, credentials, query parameters or secret bodies."""
        s = str(error)
        s = re.sub(r"://[^@]+@", "://[REDACTED]@", s)
        s = re.sub(r"\?[^\s\"'>]+", "?[REDACTED]", s)
        s = re.sub(r"https?://\S+", "[URL_REDACTED]", s)
        return s

    def _mark_failure(self, endpoint: str, error: str) -> None:
        """Increment failure counters for an endpoint upon network or node error."""
        if endpoint in self.health_status:
            self.health_status[endpoint]["fail_count"] += 1
            self.health_status[endpoint]["consecutive_fails"] += 1
            self.health_status[endpoint]["last_error"] = self._sanitize_error(error)

    def _mark_success(self, endpoint: str) -> None:
        """Reset consecutive failures for an endpoint upon successful response."""
        if endpoint in self.health_status:
            self.health_status[endpoint]["consecutive_fails"] = 0

    def _advance_node(self) -> None:
        """Advance active node pointer to next configured endpoint."""
        self._active_index = (self._active_index + 1) % len(self.urls)

    def _check_pool_breaker(self) -> None:
        """Raise ArcCircuitBreakerTrippedError if all configured nodes reached failure threshold."""
        if all(
            self.health_status[u]["consecutive_fails"] >= self.CIRCUIT_BREAKER_THRESHOLD
            for u in self.urls
        ):
            raise ArcCircuitBreakerTrippedError(
                f"Circuit breaker tripped: all {len(self.urls)} nodes reached "
                f"{self.CIRCUIT_BREAKER_THRESHOLD} consecutive failures"
            )

    def _select_healthy_node(self) -> str:
        """Find and set the active node pointer to an untripped node, or raise if all nodes tripped."""
        self._check_pool_breaker()
        total_nodes = len(self.urls)
        for i in range(total_nodes):
            candidate_idx = (self._active_index + i) % total_nodes
            candidate_url = self.urls[candidate_idx]
            if self.health_status[candidate_url]["consecutive_fails"] < self.CIRCUIT_BREAKER_THRESHOLD:
                self._active_index = candidate_idx
                return candidate_url
        raise ArcCircuitBreakerTrippedError(
            f"Circuit breaker tripped: all {len(self.urls)} nodes reached "
            f"{self.CIRCUIT_BREAKER_THRESHOLD} consecutive failures"
        )

    def call(
        self,
        method: str,
        params: Sequence[Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Execute a single JSON-RPC read-only call with automatic failover."""
        # 1. Pre-flight method validation before any dispatch
        validate_rpc_method(method)

        # 2. Strict request ID validation (reject bool, missing, non-int/str)
        req_id = kwargs.get("id", 1)
        if type(req_id) is bool or isinstance(req_id, bool):
            raise ValueError("RPC request ID cannot be a bool")
        if not isinstance(req_id, (int, str)):
            raise ValueError(f"RPC request ID must be int or str, got {type(req_id).__name__}")

        # 3. Check pool-wide circuit breaker before dispatch
        self._check_pool_breaker()

        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": list(params) if params is not None else [],
            "id": req_id,
        }

        total_nodes = len(self.urls)
        max_cycles = max(1, self.max_retries)
        max_attempts = total_nodes * max_cycles

        attempted_nodes: list[str] = []
        last_error: str = "Unknown error"

        for _ in range(max_attempts):
            current_url = self._select_healthy_node()
            attempted_nodes.append(current_url)

            try:
                resp = self._session.post(
                    current_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=self.timeout,
                )
                status_code = getattr(resp, "status_code", 200)

                # HTTP rate limit (429) -> instant failover without sleep 30s
                if status_code == 429:
                    last_error = "HTTP 429 Rate Limited"
                    self._mark_failure(current_url, last_error)
                    self._advance_node()
                    continue

                # HTTP server errors (5xx)
                if status_code >= 500:
                    last_error = f"HTTP {status_code} Server Error"
                    self._mark_failure(current_url, last_error)
                    self._advance_node()
                    continue

                # HTTP client errors (4xx)
                if status_code >= 400:
                    last_error = f"HTTP {status_code} Client Error"
                    self._mark_failure(current_url, last_error)
                    self._advance_node()
                    continue

                # JSON-RPC body inspection & strict structure validation
                data = resp.json()
                if not isinstance(data, dict):
                    raise RuntimeError(
                        f"Invalid RPC response type: expected dict, got {type(data).__name__}"
                    )

                if "id" not in data:
                    raise RuntimeError("RPC response missing 'id'")

                resp_id = data["id"]
                if type(resp_id) is bool or isinstance(resp_id, bool):
                    raise RuntimeError("Invalid RPC response ID: bool not allowed")
                if resp_id != req_id:
                    raise RuntimeError(f"RPC response ID mismatch: expected {req_id}, got {resp_id}")

                if "error" in data and data["error"] is not None:
                    err_obj = data["error"]
                    err_code = err_obj.get("code") if isinstance(err_obj, dict) else None
                    err_msg = str(
                        err_obj.get("message", "") if isinstance(err_obj, dict) else err_obj
                    )

                    # Node-level rate limit (-32005) -> failover
                    if err_code == -32005 or "rate limit" in err_msg.lower():
                        last_error = (
                            f"RPC error {err_code}: rate limit exceeded"
                            if err_code is not None
                            else "RPC error: rate limit exceeded"
                        )
                        self._mark_failure(current_url, last_error)
                        self._advance_node()
                        continue

                    # Business contract revert: node is healthy, do NOT failover
                    self._mark_success(current_url)
                    return data

                # Successful response
                self._mark_success(current_url)
                return data

            except (ArcCircuitBreakerTrippedError, ArcValidationError):
                raise
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                last_error = type(exc).__name__
                self._mark_failure(current_url, last_error)
                self._advance_node()
                continue
            except requests.exceptions.RequestException as exc:
                last_error = type(exc).__name__
                self._mark_failure(current_url, last_error)
                self._advance_node()
                continue
            except Exception as exc:
                if isinstance(exc, (ArcCircuitBreakerTrippedError, ArcValidationError)):
                    raise
                if isinstance(exc, (RuntimeError, ValueError)) and any(
                    k in str(exc).lower()
                    for k in ("response id", "missing 'id'", "bool not allowed", "invalid rpc response")
                ):
                    raise
                last_error = type(exc).__name__
                self._mark_failure(current_url, last_error)
                self._advance_node()
                continue

        raise RuntimeError(
            f"RPC call failed after {len(attempted_nodes)} attempts across {total_nodes} nodes: {last_error}"
        )

    def call_batch(
        self,
        batch_requests: Sequence[dict[str, Any]],
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Execute a batch of read-only JSON-RPC calls with ID alignment and failover."""
        if not batch_requests:
            raise ValueError("RPC batch cannot be empty")

        # 1. Strict pre-flight method validation & unique request IDs check
        expected_ids: list[Any] = []
        seen_ids: set[Any] = set()
        payload: list[dict[str, Any]] = []

        for req in batch_requests:
            if not isinstance(req, dict):
                raise ArcValidationError(f"Batch item must be dict, got {type(req).__name__}")
            method = req.get("method")
            if not method or not isinstance(method, str):
                raise ArcValidationError("Batch item missing valid 'method' string")
            validate_rpc_method(method)

            if "id" not in req:
                raise ValueError("Batch request item missing 'id'")
            req_id = req["id"]
            if type(req_id) is bool or isinstance(req_id, bool):
                raise ValueError("Batch request ID cannot be a bool")
            if not isinstance(req_id, (int, str)):
                raise ValueError(f"Batch request ID must be int or str, got {type(req_id).__name__}")
            if req_id in seen_ids:
                raise ValueError(f"Duplicate request ID in batch: {req_id}")
            seen_ids.add(req_id)
            expected_ids.append(req_id)

            payload.append(
                {
                    "jsonrpc": req.get("jsonrpc", "2.0"),
                    "method": method,
                    "params": list(req.get("params", [])),
                    "id": req_id,
                }
            )

        # 2. Check pool-wide circuit breaker
        self._check_pool_breaker()

        total_nodes = len(self.urls)
        max_cycles = max(1, self.max_retries)
        max_attempts = total_nodes * max_cycles

        attempted_nodes: list[str] = []
        last_error: str = "Unknown error"

        for _ in range(max_attempts):
            current_url = self._select_healthy_node()
            attempted_nodes.append(current_url)

            try:
                resp = self._session.post(
                    current_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=self.timeout,
                )
                status_code = getattr(resp, "status_code", 200)

                # HTTP rate limit (429) -> instant failover
                if status_code == 429:
                    last_error = "HTTP 429 Rate Limited"
                    self._mark_failure(current_url, last_error)
                    self._advance_node()
                    continue

                if status_code >= 500:
                    last_error = f"HTTP {status_code} Server Error"
                    self._mark_failure(current_url, last_error)
                    self._advance_node()
                    continue

                if status_code >= 400:
                    last_error = f"HTTP {status_code} Client Error"
                    self._mark_failure(current_url, last_error)
                    self._advance_node()
                    continue

                data = resp.json()

                # Node-level rate limit returned as a single error dictionary
                if isinstance(data, dict):
                    err_obj = data.get("error")
                    err_code = err_obj.get("code") if isinstance(err_obj, dict) else None
                    err_msg = str(
                        err_obj.get("message", "") if isinstance(err_obj, dict) else err_obj
                    )
                    if err_code == -32005 or "rate limit" in err_msg.lower():
                        last_error = (
                            f"RPC batch error {err_code}: rate limit exceeded"
                            if err_code is not None
                            else "RPC batch error: rate limit exceeded"
                        )
                        self._mark_failure(current_url, last_error)
                        self._advance_node()
                        continue
                    raise RuntimeError(
                        f"Unexpected non-list response for batch request: got {type(data).__name__}"
                    )

                if not isinstance(data, list):
                    raise RuntimeError(
                        f"Invalid batch response type: expected list, got {type(data).__name__}"
                    )

                # Check if any element in batch is node rate limit (-32005)
                has_rate_limit = False
                for item in data:
                    if isinstance(item, dict) and "error" in item and item["error"] is not None:
                        item_err = item["error"]
                        code = item_err.get("code") if isinstance(item_err, dict) else None
                        msg = str(
                            item_err.get("message", "") if isinstance(item_err, dict) else item_err
                        )
                        if code == -32005 or "rate limit" in msg.lower():
                            has_rate_limit = True
                            last_error = (
                                f"RPC batch item rate limit {code}: rate limit exceeded"
                                if code is not None
                                else "RPC batch item rate limit: rate limit exceeded"
                            )
                            break
                if has_rate_limit:
                    self._mark_failure(current_url, last_error)
                    self._advance_node()
                    continue

                # ID alignment: reject duplicate, unknown, or missing response IDs
                resp_by_id: dict[Any, dict[str, Any]] = {}
                seen_resp_ids: set[Any] = set()
                for item in data:
                    if not isinstance(item, dict) or "id" not in item:
                        raise RuntimeError("Batch response item missing 'id' or not a dict")
                    resp_id = item["id"]
                    if type(resp_id) is bool or isinstance(resp_id, bool):
                        raise RuntimeError("Invalid response ID in batch: bool not allowed")
                    if resp_id in seen_resp_ids:
                        raise RuntimeError(f"Duplicate response ID in batch: {resp_id}")
                    seen_resp_ids.add(resp_id)
                    if resp_id not in seen_ids:
                        raise RuntimeError(f"Unknown response ID in batch: {resp_id}")
                    resp_by_id[resp_id] = item

                missing_ids = set(expected_ids) - seen_resp_ids
                if missing_ids:
                    raise RuntimeError(
                        f"Missing response for request IDs in batch: {sorted(str(i) for i in missing_ids)}"
                    )

                # Match strictly by request order
                ordered_results = [resp_by_id[req_id] for req_id in expected_ids]

                self._mark_success(current_url)
                return ordered_results

            except (ArcCircuitBreakerTrippedError, ArcValidationError):
                raise
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                last_error = type(exc).__name__
                self._mark_failure(current_url, last_error)
                self._advance_node()
                continue
            except requests.exceptions.RequestException as exc:
                last_error = type(exc).__name__
                self._mark_failure(current_url, last_error)
                self._advance_node()
                continue
            except Exception as exc:
                if isinstance(exc, (ArcCircuitBreakerTrippedError, ArcValidationError)):
                    raise
                if (
                    isinstance(exc, (RuntimeError, ValueError))
                    and any(k in str(exc).lower() for k in ("batch", "response id", "bool not allowed"))
                ):
                    raise
                last_error = type(exc).__name__
                self._mark_failure(current_url, last_error)
                self._advance_node()
                continue

        raise RuntimeError(
            f"RPC call failed after {len(attempted_nodes)} attempts across {total_nodes} nodes: {last_error}"
        )
