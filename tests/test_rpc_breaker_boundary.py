"""Independent boundary tests for RPC pool circuit breaker loop enforcement,

response ID validation, and credential error sanitization.
"""

from __future__ import annotations

from typing import Any

import pytest
import requests

from research.market_data.rpc_pool import (
    ArcCircuitBreakerTrippedError,
    RpcPoolClient,
)


class Counting503Session:
    """Session recording exact HTTP POST invocation count."""

    def __init__(self) -> None:
        self.post_count: int = 0
        self.called_urls: list[str] = []

    def post(self, url: str, **kwargs: Any) -> Any:
        self.post_count += 1
        self.called_urls.append(url)

        class Fake503:
            status_code = 503
            text = "Service Unavailable"

            def json(self) -> dict[str, Any]:
                return {}

        return Fake503()


class MockSession:
    """Mock session returning configurable JSON payload."""

    def __init__(self, data: Any, status_code: int = 200) -> None:
        self.data = data
        self.status_code = status_code
        self.post_count: int = 0
        self.called_urls: list[str] = []

    def post(self, url: str, **kwargs: Any) -> Any:
        self.post_count += 1
        self.called_urls.append(url)

        class MockResp:
            status_code = self.status_code
            text = str(self.data)

            def json(inner_self) -> Any:
                return self.data

        return MockResp()


class TestRpcBreakerLoopAndSanitizationBoundary:
    """Boundary tests for circuit breaker inside retry loops and error sanitization."""

    def test_single_node_max_retries_loop_breaker_strict_3_posts(self) -> None:
        """Single node with max_retries=8 must trip after exactly 3 POST calls, not 8."""
        session = Counting503Session()
        client = RpcPoolClient(url="https://node.test", session=session, max_retries=8)

        with pytest.raises(
            ArcCircuitBreakerTrippedError,
            match=r"Circuit breaker tripped: all 1 nodes reached 3 consecutive failures",
        ):
            client.call("eth_blockNumber")

        # Must be strictly 3 POSTs; max_retries=8 must not break through threshold
        assert session.post_count == 3
        assert len(session.called_urls) == 3
        assert client.health_status["https://node.test"]["consecutive_fails"] == 3

    def test_batch_max_retries_loop_breaker_strict_3_posts(self) -> None:
        """Batch call with max_retries=8 must trip after exactly 3 POST calls, not 8."""
        session = Counting503Session()
        client = RpcPoolClient(url="https://node.test", session=session, max_retries=8)

        batch = [{"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1}]
        with pytest.raises(
            ArcCircuitBreakerTrippedError,
            match=r"Circuit breaker tripped: all 1 nodes reached 3 consecutive failures",
        ):
            client.call_batch(batch)

        assert session.post_count == 3
        assert len(session.called_urls) == 3
        assert client.health_status["https://node.test"]["consecutive_fails"] == 3

    def test_multi_node_circuit_breaker_exhaustion_strict_post_count(self) -> None:
        """3-node pool with max_retries=8 must execute strictly 3 calls per node (9 total), then trip."""
        session = Counting503Session()
        nodes = ["https://node1.test", "https://node2.test", "https://node3.test"]
        client = RpcPoolClient(urls=nodes, session=session, max_retries=8)

        with pytest.raises(
            ArcCircuitBreakerTrippedError,
            match=r"Circuit breaker tripped: all 3 nodes reached 3 consecutive failures",
        ):
            client.call("eth_blockNumber")

        # 3 nodes * 3 consecutive failures = 9 POST attempts total (NOT 3 * 8 = 24)
        assert session.post_count == 9
        for u in nodes:
            assert session.called_urls.count(u) == 3
            assert client.health_status[u]["consecutive_fails"] == 3

    def test_isolated_tripped_node_failover_to_healthy_node(self) -> None:
        """Node reaching 3 consecutive failures is isolated; subsequent calls skip it to healthy node."""
        post_log: list[str] = []

        class MixedSession:
            def post(self, url: str, **kwargs: Any) -> Any:
                post_log.append(url)
                if url == "https://node-failing.test":
                    class Resp500:
                        status_code = 500
                        text = "Internal Error"
                        def json(self) -> dict[str, Any]:
                            return {}
                    return Resp500()
                class Resp200:
                    status_code = 200
                    text = "OK"
                    def json(self) -> dict[str, Any]:
                        return {"jsonrpc": "2.0", "result": "0xabc", "id": 1}
                return Resp200()

        nodes = ["https://node-failing.test", "https://node-healthy.test"]
        client = RpcPoolClient(urls=nodes, session=MixedSession(), max_retries=1)

        # Fail node-failing 3 times manually
        for _ in range(3):
            client._mark_failure("https://node-failing.test", "HTTP 500 Server Error")
        assert client.health_status["https://node-failing.test"]["consecutive_fails"] == 3

        # Active index points to node-failing, but it must be skipped immediately
        client._active_index = 0
        post_log.clear()

        res = client.call("eth_blockNumber")
        assert res.get("result") == "0xabc"
        # Zero calls to node-failing; routed directly to node-healthy
        assert post_log == ["https://node-healthy.test"]
        assert client.active_url == "https://node-healthy.test"

    def test_single_call_response_id_validation_rejects_missing_id(self) -> None:
        """Single call rejects response missing 'id' key."""
        session = MockSession({"jsonrpc": "2.0", "result": "0x1"})
        client = RpcPoolClient(url="https://node.test", session=session)

        with pytest.raises(RuntimeError, match="RPC response missing 'id'"):
            client.call("eth_blockNumber", id=1)

    def test_single_call_response_id_validation_rejects_mismatched_id(self) -> None:
        """Single call rejects response with mismatched 'id'."""
        session = MockSession({"jsonrpc": "2.0", "result": "0x1", "id": 999})
        client = RpcPoolClient(url="https://node.test", session=session)

        with pytest.raises(RuntimeError, match=r"RPC response ID mismatch: expected 1, got 999"):
            client.call("eth_blockNumber", id=1)

    def test_single_call_response_id_validation_rejects_bool_id(self) -> None:
        """Single call rejects response and request with boolean id."""
        # Request with bool id rejected
        session = MockSession({"jsonrpc": "2.0", "result": "0x1", "id": 1})
        client = RpcPoolClient(url="https://node.test", session=session)
        with pytest.raises(ValueError, match="RPC request ID cannot be a bool"):
            client.call("eth_blockNumber", id=True)

        # Response with bool id rejected
        session_bool_resp = MockSession({"jsonrpc": "2.0", "result": "0x1", "id": True})
        client2 = RpcPoolClient(url="https://node.test", session=session_bool_resp)
        with pytest.raises(RuntimeError, match="Invalid RPC response ID: bool not allowed"):
            client2.call("eth_blockNumber", id=1)

    def test_single_call_response_structure_rejects_non_dict(self) -> None:
        """Single call rejects response that is not a dictionary."""
        session_list = MockSession(["not", "a", "dict"])
        client = RpcPoolClient(url="https://node.test", session=session_list)

        with pytest.raises(RuntimeError, match="Invalid RPC response type: expected dict, got list"):
            client.call("eth_blockNumber")

    def test_batch_rejects_bool_id(self) -> None:
        """Batch call rejects bool id in request and response."""
        session = MockSession([{"jsonrpc": "2.0", "result": "0x1", "id": 1}])
        client = RpcPoolClient(url="https://node.test", session=session)

        # Request ID is bool
        with pytest.raises(ValueError, match="Batch request ID cannot be a bool"):
            client.call_batch([{"method": "eth_blockNumber", "id": True}])

        # Response ID is bool
        session_bool = MockSession([{"jsonrpc": "2.0", "result": "0x1", "id": True}])
        client2 = RpcPoolClient(url="https://node.test", session=session_bool)
        with pytest.raises(RuntimeError, match="Invalid response ID in batch: bool not allowed"):
            client2.call_batch([{"method": "eth_blockNumber", "id": 1}])

    def test_error_sanitization_sentinel_credential_never_leaked(self) -> None:
        """Verify sentinel credentials, URLs, query parameters and bodies are never reflected."""
        sentinel = "SUPER_SECRET_TOKEN_SENTINEL_XYZ_987654321"

        # 1. ConnectionError with credentials in URL and message
        class LeakyConnectionErrorSession:
            def post(self, url: str, **kwargs: Any) -> Any:
                raise requests.exceptions.ConnectionError(
                    f"Connection failed to https://user:{sentinel}@internal-node.test/path?key={sentinel}"
                )

        client1 = RpcPoolClient(url="https://node.test", session=LeakyConnectionErrorSession(), max_retries=1)
        with pytest.raises(RuntimeError) as exc_info:
            client1.call("eth_blockNumber")

        err_msg = str(exc_info.value)
        last_err = str(client1.health_status["https://node.test"]["last_error"])
        assert sentinel not in err_msg, f"Sentinel leaked in exception: {err_msg}"
        assert sentinel not in last_err, f"Sentinel leaked in last_error: {last_err}"

        # 2. HTTP 500 error with secret in response text
        class Leaky500BodySession:
            def post(self, url: str, **kwargs: Any) -> Any:
                class Fake500:
                    status_code = 500
                    text = f"Internal crash dump containing secret token {sentinel}"
                    def json(self) -> dict[str, Any]:
                        return {}
                return Fake500()

        client2 = RpcPoolClient(url="https://node.test", session=Leaky500BodySession(), max_retries=1)
        with pytest.raises(RuntimeError) as exc_info2:
            client2.call("eth_blockNumber")

        err_msg2 = str(exc_info2.value)
        last_err2 = str(client2.health_status["https://node.test"]["last_error"])
        assert sentinel not in err_msg2, f"Sentinel leaked in exception: {err_msg2}"
        assert sentinel not in last_err2, f"Sentinel leaked in last_error: {last_err2}"
