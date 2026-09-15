"""Read-only monitoring RPC: one attempt per call, shared three-failure latch."""

from __future__ import annotations

import math
import threading
import time
from collections import Counter, deque
from typing import Any

import requests


class UnknownRpcMethodError(ValueError):
    """Method outside this research transport read-only policy."""

READ_METHODS = frozenset(
    {
        "eth_chainId",
        "eth_blockNumber",
        "eth_call",
        "eth_getCode",
        "eth_getStorageAt",
        "eth_getBlockByNumber",
        "eth_getLogs",
        "eth_getBalance",
        "eth_getTransactionCount",
        "eth_getTransactionByHash",
        "eth_getTransactionReceipt",
        "eth_gasPrice",
        "eth_estimateGas",
    }
)


class MonitorRpcError(RuntimeError):
    """Structured read-only rejection; provider payload is never formatted into logs."""

    def __init__(self, method: str, error: dict[str, Any]) -> None:
        super().__init__("Read-only monitoring RPC rejected a request")
        self.rpc_method = method
        self.rpc_code = error.get("code")
        self.rpc_data = error.get("data")


class MonitorRpcHalted(RuntimeError):
    """Three failed requests close the monitor; only an explicit new run can restart."""


class MonitorRpc:
    """Share one failure latch across metadata, multicall and concurrent fallback reads.

    The monitor serializes transport attempts so queued calls cannot start after
    the third failure. Each call has one HTTP attempt; no internal retry sleeps,
    automatic node rotation, sends, overrides, signing or approval operations.
    Historical/backtest clients keep their separate existing retry behavior.
    """

    def __init__(self, throttle: float = 0.0, urls: list[str] | None = None,
                 *, session: Any = None, user_agent: str = "Arc-Monitor-ReadOnly/1.0") -> None:
        if not isinstance(urls, list) or not urls or any(not isinstance(url, str) or not url.strip() for url in urls):
            raise ValueError("Explicit nonempty endpoints required")
        if not math.isfinite(throttle) or throttle < 0:
            raise ValueError("Invalid throttle interval")
        self.active_url = urls[0]
        self._session = session
        self.user_agent = user_agent
        self.throttle = throttle
        self._last_request: float | None = None
        self._request_lock = threading.RLock()
        self._monitor_failures = 0
        self._monitor_halted = False
        self._request_counts: Counter[str] = Counter()
        self._latencies: deque[float] = deque(maxlen=256)

    def _wait_throttle(self) -> None:
        now = time.monotonic()
        if self._last_request is not None and self.throttle:
            delay = self.throttle - (now - self._last_request)
            if delay > 0:
                time.sleep(delay)
        self._last_request = time.monotonic()

    @property
    def circuit_open(self) -> bool:
        """Expose the latched state without clearing it or issuing any request."""
        with self._request_lock:
            return self._monitor_halted

    def metrics(self) -> dict[str, Any]:
        """Return measured request outcomes/latencies, with None when no sample exists."""
        with self._request_lock:
            samples = sorted(self._latencies)
            return {
                "halted": self._monitor_halted,
                "consecutive_failures": self._monitor_failures,
                "outcomes": dict(self._request_counts),
                "latency_sample_count": len(samples),
                "latency_p50_seconds": samples[(len(samples) - 1) // 2] if samples else None,
                "latency_p95_seconds": samples[(95 * len(samples) + 99) // 100 - 1]
                if samples
                else None,
            }

    def _admit_method(self, method: str) -> None:
        if not isinstance(method, str) or method not in READ_METHODS:
            raise UnknownRpcMethodError("Monitor does not admit this RPC method")

    def _request_once(self, payload: Any, *, batch: bool = False) -> Any:
        with self._request_lock:
            if self._monitor_halted:
                raise MonitorRpcHalted("Monitor RPC halted after three consecutive failures")
            if self._session is None:
                raise ValueError("Explicit offline session injection required")
            self._wait_throttle()
            started = time.monotonic()
            outcome = "transport_error"
            try:
                response = self._session.post(
                    self.active_url,
                    json=payload,
                    headers={"User-Agent": self.user_agent, "Content-Type": "application/json"},
                    timeout=20,
                )
                if response.status_code != 200:
                    outcome = f"http_{response.status_code}"
                    raise RuntimeError(f"Monitor HTTP request failed ({response.status_code})")
                outcome = "malformed_response"
                raw = response.json()
                results = raw if batch and isinstance(raw, list) else [raw]
                if batch and (not isinstance(raw, list) or len(raw) != len(payload)):
                    raise RuntimeError("Monitor batch response count mismatch")
                expected_ids = {(type(item["id"]), item["id"]) for item in payload} if batch else {(int, 1)}
                if any(not isinstance(item, dict) or type(item.get("id")) not in (int, str) for item in results):
                    raise RuntimeError("Monitor response identity type mismatch")
                if {(type(item["id"]), item["id"]) for item in results} != expected_ids:
                    raise RuntimeError("Monitor response identity mismatch")
                for item in results:
                    if "jsonrpc" in item and item["jsonrpc"] != "2.0":
                        raise RuntimeError("Invalid JSON-RPC version")
                    if not isinstance(item, dict) or "result" not in item or "error" in item:
                        outcome = "rpc_error"
                        if isinstance(item, dict) and isinstance(item.get("error"), dict):
                            requests_by_id = (
                                {entry["id"]: entry for entry in payload} if batch else {1: payload}
                            )
                            method = requests_by_id[item["id"]]["method"]
                            raise MonitorRpcError(method, item["error"])
                        raise RuntimeError("Monitor RPC returned an error or missing result")
                self._monitor_failures = 0
                self._request_counts["success"] += 1
                return raw
            except Exception as exc:
                if isinstance(exc, requests.Timeout):
                    outcome = "timeout"
                if isinstance(exc, requests.Timeout):
                    exc = requests.Timeout("Monitor transport timeout")
                elif isinstance(exc, requests.RequestException):
                    exc = requests.RequestException("Monitor transport failure")
                elif not isinstance(exc, MonitorRpcError):
                    exc = RuntimeError("Monitor request failed validation")
                self._monitor_failures += 1
                self._request_counts[outcome] += 1
                if self._monitor_failures >= 3:
                    self._monitor_halted = True
                    raise MonitorRpcHalted(
                        "Monitor RPC halted after three consecutive failures"
                    ) from exc
                raise exc from None
            finally:
                self._latencies.append(time.monotonic() - started)

    def call(self, method: str, params: list[Any] | None = None) -> dict[str, Any]:
        """Issue one admitted read and flatten block fields as the original client does."""
        self._admit_method(method)
        if params is not None and not isinstance(params, list):
            raise ValueError("RPC params must be a list")
        if method in {"eth_call", "eth_estimateGas"} and params is not None and len(params) > 2:
            raise ValueError("Monitor state overrides are not admitted")
        raw = self._request_once(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": method,
                "params": [] if params is None else params,
            }
        )
        result = dict(raw)
        if isinstance(raw["result"], dict):
            for key, value in raw["result"].items():
                result.setdefault(key, value)
        return result

    def call_batch(self, calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Read a batch once, validating every method before transport."""
        if not isinstance(calls, list):
            raise ValueError("RPC batch must be a list")
        if not calls:
            return []
        for item in calls:
            if not isinstance(item, dict) or not isinstance(item.get("params", []), list):
                raise ValueError("RPC batch entries require list params")
            method = item.get("method")
            if not isinstance(method, str):
                raise UnknownRpcMethodError("Monitor batch method missing")
            self._admit_method(method)
            if method in {"eth_call", "eth_estimateGas"} and len(item.get("params", [])) > 2:
                raise ValueError("Monitor state overrides are not admitted")
        if any(type(item.get("id")) not in (int, str) for item in calls) or len({(type(item["id"]), item["id"]) for item in calls}) != len(calls):
            raise ValueError("Monitor batch requires unique request IDs")
        return self._request_once(calls, batch=True)
