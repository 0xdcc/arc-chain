"""Independent HTTP RPC Batch Contract and ID Correlation Test Suite.

Verifies strict security and correctness invariants for HttpReadOnlyRpcTransport.request_batch:
1. Pre-flight atomic rejection: any prohibited method (first, middle, last), empty calls,
   malformed calls, offline mode, or tripped circuit breaker halts with zero opener calls.
2. Exact ID correlation: response elements are mapped by ID to reconstruct original request
   order, and valid out-of-order responses are accepted and reordered properly.
3. Malformed responses: non-list responses, non-dict elements, missing ID, duplicate ID,
   unknown ID, or non-integer / bool IDs fail closed.
4. Error handling: sub-item RPC errors fail closed according to the single-request contract
   and are not silently swallowed as None; missing 'result' keys are distinguished from
   valid 'result: null'.
5. Circuit breaker & security: consecutive failures trip the breaker, and error messages
   never leak sensitive endpoint credentials.
"""

from __future__ import annotations

import json
import urllib.error
from typing import Any
from unittest.mock import MagicMock

import pytest

from arc_readiness.errors import ArcValidationError
from arc_readiness.http_readonly import (
    ArcCircuitBreakerTrippedError,
    HttpReadOnlyRpcTransport,
)


def _build_mock_opener(
    response_payload: Any = None,
    side_effect: Exception | None = None,
) -> MagicMock:
    """Build a mock OpenerDirector returning a JSON-RPC response or raising an exception."""
    mock_opener = MagicMock()
    if side_effect is not None:
        mock_opener.open.side_effect = side_effect
    elif response_payload is not None:
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_payload).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = False
        mock_opener.open.return_value = mock_resp
    return mock_opener


class TestHttpBatchPreflightRejections:
    """Verify pre-flight validation blocks prohibited or invalid batches before network dispatch."""

    def test_batch_empty_calls_rejected_zero_opener_call(self) -> None:
        """Empty calls sequence must be rejected immediately without calling opener."""
        mock_opener = _build_mock_opener()
        transport = HttpReadOnlyRpcTransport(
            endpoint_url="https://rpc.arc.io",
            allow_network=True,
            opener=mock_opener,
        )

        with pytest.raises(ArcValidationError, match="Batch calls list cannot be empty"):
            transport.request_batch([])

        mock_opener.open.assert_not_called()

    def test_batch_malformed_call_tuple_rejected_zero_opener_call(self) -> None:
        """Malformed call elements (< 2 items) must fail closed before calling opener."""
        mock_opener = _build_mock_opener()
        transport = HttpReadOnlyRpcTransport(
            endpoint_url="https://rpc.arc.io",
            allow_network=True,
            opener=mock_opener,
        )

        malformed_calls: Any = [("eth_chainId",)]
        with pytest.raises(ArcValidationError, match="Invalid batch call format"):
            transport.request_batch(malformed_calls)

        mock_opener.open.assert_not_called()

    def test_batch_prohibited_method_at_beginning_zero_opener_call(self) -> None:
        """A prohibited mutating method at index 0 must reject the entire batch with zero opener calls."""
        mock_opener = _build_mock_opener()
        transport = HttpReadOnlyRpcTransport(
            endpoint_url="https://rpc.arc.io",
            allow_network=True,
            opener=mock_opener,
        )

        calls = [
            ("eth_sendRawTransaction", ["0x1234"]),
            ("eth_chainId", []),
            ("eth_gasPrice", []),
        ]
        with pytest.raises(ArcValidationError, match="Prohibited mutating or non-EVM RPC method"):
            transport.request_batch(calls)

        mock_opener.open.assert_not_called()

    def test_batch_prohibited_method_in_middle_zero_opener_call(self) -> None:
        """A prohibited mutating method in the middle must reject the entire batch with zero opener calls."""
        mock_opener = _build_mock_opener()
        transport = HttpReadOnlyRpcTransport(
            endpoint_url="https://rpc.arc.io",
            allow_network=True,
            opener=mock_opener,
        )

        calls = [
            ("eth_chainId", []),
            ("eth_sendTransaction", [{"from": "0x1", "to": "0x2"}]),
            ("eth_gasPrice", []),
        ]
        with pytest.raises(ArcValidationError, match="Prohibited mutating or non-EVM RPC method"):
            transport.request_batch(calls)

        mock_opener.open.assert_not_called()

    def test_batch_prohibited_method_at_end_zero_opener_call(self) -> None:
        """A prohibited method at the last position must reject the entire batch with zero opener calls."""
        mock_opener = _build_mock_opener()
        transport = HttpReadOnlyRpcTransport(
            endpoint_url="https://rpc.arc.io",
            allow_network=True,
            opener=mock_opener,
        )

        calls = [
            ("eth_chainId", []),
            ("eth_gasPrice", []),
            ("personal_sign", ["0xmsg", "0xaddr"]),
        ]
        with pytest.raises(ArcValidationError, match="Prohibited mutating or non-EVM RPC method"):
            transport.request_batch(calls)

        mock_opener.open.assert_not_called()

    def test_batch_unknown_method_zero_opener_call(self) -> None:
        """An unknown or unapproved RPC method must reject the entire batch with zero opener calls."""
        mock_opener = _build_mock_opener()
        transport = HttpReadOnlyRpcTransport(
            endpoint_url="https://rpc.arc.io",
            allow_network=True,
            opener=mock_opener,
        )

        calls: list[tuple[str, list[Any]]] = [("eth_nonExistentReadMethod", [])]
        with pytest.raises(ArcValidationError, match="Prohibited RPC method"):
            transport.request_batch(calls)

        mock_opener.open.assert_not_called()

    def test_batch_offline_mode_rejected_zero_opener_call(self) -> None:
        """Offline-default mode (allow_network=False) must reject batch with zero opener calls."""
        mock_opener = _build_mock_opener()
        transport = HttpReadOnlyRpcTransport(
            endpoint_url="https://rpc.arc.io",
            allow_network=False,
            opener=mock_opener,
        )

        calls: list[tuple[str, list[Any]]] = [("eth_chainId", []), ("eth_gasPrice", [])]
        with pytest.raises(ArcValidationError, match="Network access not authorized in offline mode"):
            transport.request_batch(calls)

        mock_opener.open.assert_not_called()

    def test_batch_tripped_circuit_breaker_rejected_zero_opener_call(self) -> None:
        """A tripped circuit breaker must reject batch requests with zero opener calls."""
        mock_opener = _build_mock_opener()
        transport = HttpReadOnlyRpcTransport(
            endpoint_url="https://rpc.arc.io",
            allow_network=True,
            opener=mock_opener,
        )
        transport._is_tripped = True
        transport._consecutive_failures = 3

        calls: list[tuple[str, list[Any]]] = [("eth_chainId", [])]
        with pytest.raises(ArcCircuitBreakerTrippedError, match="RPC Circuit Breaker is TRIPPED"):
            transport.request_batch(calls)

        mock_opener.open.assert_not_called()


class TestHttpBatchSuccessAndOrdering:
    """Verify successful batch execution, request order reconstruction, and result: null support."""

    def test_batch_in_order_success(self) -> None:
        """Responses returning in sequential ID order must be mapped accurately."""
        payload = [
            {"jsonrpc": "2.0", "id": 1, "result": "0x13b2"},
            {"jsonrpc": "2.0", "id": 2, "result": "0x3b9aca00"},
            {"jsonrpc": "2.0", "id": 3, "result": "0x01"},
        ]
        mock_opener = _build_mock_opener(response_payload=payload)
        transport = HttpReadOnlyRpcTransport(
            endpoint_url="https://rpc.arc.io",
            allow_network=True,
            opener=mock_opener,
        )

        calls = [
            ("eth_chainId", []),
            ("eth_gasPrice", []),
            ("eth_getBalance", ["0x1111111111111111111111111111111111111111", "latest"]),
        ]
        results = transport.request_batch(calls)

        assert results == ["0x13b2", "0x3b9aca00", "0x01"]
        assert not transport.is_circuit_broken
        assert mock_opener.open.call_count == 1

    def test_batch_out_of_order_restored_to_request_order(self) -> None:
        """Responses returning out of order (e.g. 3, 1, 2) must be restored to original request order."""
        payload = [
            {"jsonrpc": "2.0", "id": 3, "result": "result_third"},
            {"jsonrpc": "2.0", "id": 1, "result": "result_first"},
            {"jsonrpc": "2.0", "id": 2, "result": "result_second"},
        ]
        mock_opener = _build_mock_opener(response_payload=payload)
        transport = HttpReadOnlyRpcTransport(
            endpoint_url="https://rpc.arc.io",
            allow_network=True,
            opener=mock_opener,
        )

        calls = [
            ("eth_chainId", []),
            ("eth_gasPrice", []),
            ("eth_getBlockByNumber", ["latest", False]),
        ]
        results = transport.request_batch(calls)

        assert results == ["result_first", "result_second", "result_third"]
        assert not transport.is_circuit_broken
        assert mock_opener.open.call_count == 1

    def test_batch_legal_result_null_preserved(self) -> None:
        """A JSON-RPC response with result: null must be preserved as None and not treated as missing."""
        payload = [
            {"jsonrpc": "2.0", "id": 1, "result": None},
        ]
        mock_opener = _build_mock_opener(response_payload=payload)
        transport = HttpReadOnlyRpcTransport(
            endpoint_url="https://rpc.arc.io",
            allow_network=True,
            opener=mock_opener,
        )

        calls = [("eth_getTransactionByHash", ["0x0000000000000000000000000000000000000000000000000000000000000000"])]
        results = transport.request_batch(calls)

        assert results == [None]
        assert not transport.is_circuit_broken

    def test_batch_request_payload_format(self) -> None:
        """Dispatched payload must be a JSON array with sequential integer IDs and method/params."""
        mock_opener = _build_mock_opener(
            response_payload=[
                {"jsonrpc": "2.0", "id": 1, "result": "0x1"},
                {"jsonrpc": "2.0", "id": 2, "result": "0x2"},
            ]
        )
        transport = HttpReadOnlyRpcTransport(
            endpoint_url="https://rpc.arc.io",
            allow_network=True,
            opener=mock_opener,
        )

        transport.request_batch([("eth_chainId", []), ("eth_gasPrice", [])])

        assert mock_opener.open.call_count == 1
        req = mock_opener.open.call_args[0][0]
        assert req.get_method() == "POST"
        assert req.headers["Content-type"] == "application/json"
        assert req.headers["User-agent"] == "Arc-Readiness-ReadOnly/1.0"

        dispatched_body = json.loads(req.data.decode("utf-8"))
        assert isinstance(dispatched_body, list)
        assert len(dispatched_body) == 2
        assert dispatched_body[0] == {"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}
        assert dispatched_body[1] == {"jsonrpc": "2.0", "id": 2, "method": "eth_gasPrice", "params": []}


class TestHttpBatchMalformedResponses:
    """Verify strict fail-closed handling on corrupt or non-compliant JSON-RPC batch responses."""

    def test_batch_non_list_response_fails(self) -> None:
        """A single JSON object returned instead of a batch list must fail closed."""
        mock_opener = _build_mock_opener(response_payload={"jsonrpc": "2.0", "id": 1, "result": "0x1"})
        transport = HttpReadOnlyRpcTransport(
            endpoint_url="https://rpc.arc.io",
            allow_network=True,
            opener=mock_opener,
        )

        with pytest.raises(ArcValidationError, match="expected list"):
            transport.request_batch([("eth_chainId", [])])

    def test_batch_non_object_element_fails(self) -> None:
        """A primitive element inside the response list must fail closed."""
        mock_opener = _build_mock_opener(response_payload=["not_a_dict"])
        transport = HttpReadOnlyRpcTransport(
            endpoint_url="https://rpc.arc.io",
            allow_network=True,
            opener=mock_opener,
        )

        with pytest.raises(ArcValidationError, match="expected JSON-RPC object"):
            transport.request_batch([("eth_chainId", [])])

    def test_batch_missing_id_in_element_fails(self) -> None:
        """A response element missing the 'id' field must fail closed."""
        mock_opener = _build_mock_opener(response_payload=[{"jsonrpc": "2.0", "result": "0x1"}])
        transport = HttpReadOnlyRpcTransport(
            endpoint_url="https://rpc.arc.io",
            allow_network=True,
            opener=mock_opener,
        )

        with pytest.raises(ArcValidationError, match="missing 'id' field"):
            transport.request_batch([("eth_chainId", [])])

    def test_batch_bool_id_rejected_as_type_error(self) -> None:
        """A boolean id (True/False) must not pass as an integer id (bool is int subclass)."""
        mock_opener = _build_mock_opener(response_payload=[{"jsonrpc": "2.0", "id": True, "result": "0x1"}])
        transport = HttpReadOnlyRpcTransport(
            endpoint_url="https://rpc.arc.io",
            allow_network=True,
            opener=mock_opener,
        )

        with pytest.raises(ArcValidationError, match="Invalid batch response id type: expected int, got bool"):
            transport.request_batch([("eth_chainId", [])])

    def test_batch_string_id_rejected_as_type_error(self) -> None:
        """A string id must be rejected since batch dispatch generates integer IDs."""
        mock_opener = _build_mock_opener(response_payload=[{"jsonrpc": "2.0", "id": "1", "result": "0x1"}])
        transport = HttpReadOnlyRpcTransport(
            endpoint_url="https://rpc.arc.io",
            allow_network=True,
            opener=mock_opener,
        )

        with pytest.raises(ArcValidationError, match="Invalid batch response id type: expected int, got str"):
            transport.request_batch([("eth_chainId", [])])

    def test_batch_unknown_id_fails(self) -> None:
        """An ID outside the expected dispatched range must fail closed."""
        payload = [
            {"jsonrpc": "2.0", "id": 1, "result": "0x1"},
            {"jsonrpc": "2.0", "id": 99, "result": "0x2"},
        ]
        mock_opener = _build_mock_opener(response_payload=payload)
        transport = HttpReadOnlyRpcTransport(
            endpoint_url="https://rpc.arc.io",
            allow_network=True,
            opener=mock_opener,
        )

        with pytest.raises(ArcValidationError, match="Unknown batch response id: 99"):
            transport.request_batch([("eth_chainId", []), ("eth_gasPrice", [])])

    def test_batch_duplicate_id_fails(self) -> None:
        """Duplicate IDs in the response batch must fail closed to prevent ambiguous results."""
        payload = [
            {"jsonrpc": "2.0", "id": 1, "result": "0x1"},
            {"jsonrpc": "2.0", "id": 1, "result": "0x2"},
        ]
        mock_opener = _build_mock_opener(response_payload=payload)
        transport = HttpReadOnlyRpcTransport(
            endpoint_url="https://rpc.arc.io",
            allow_network=True,
            opener=mock_opener,
        )

        with pytest.raises(ArcValidationError, match="Duplicate batch response id: 1"):
            transport.request_batch([("eth_chainId", []), ("eth_gasPrice", [])])

    def test_batch_missing_id_in_results_fails(self) -> None:
        """If response omits one of the dispatched IDs, batch must fail closed."""
        payload = [
            {"jsonrpc": "2.0", "id": 1, "result": "0x1"},
        ]
        mock_opener = _build_mock_opener(response_payload=payload)
        transport = HttpReadOnlyRpcTransport(
            endpoint_url="https://rpc.arc.io",
            allow_network=True,
            opener=mock_opener,
        )

        calls: list[tuple[str, list[Any]]] = [("eth_chainId", []), ("eth_gasPrice", [])]
        with pytest.raises(ArcValidationError, match="Missing batch response for ids: \\[2\\]"):
            transport.request_batch(calls)

    def test_batch_error_sub_item_not_swallowed(self) -> None:
        """RPC error in a response sub-item must fail closed matching single-request contract, not return None."""
        payload = [
            {"jsonrpc": "2.0", "id": 1, "result": "0x5042"},
            {"jsonrpc": "2.0", "id": 2, "error": {"code": -32000, "message": "execution reverted"}},
        ]
        mock_opener = _build_mock_opener(response_payload=payload)
        transport = HttpReadOnlyRpcTransport(
            endpoint_url="https://rpc.arc.io",
            allow_network=True,
            opener=mock_opener,
        )

        calls: list[tuple[str, list[Any]]] = [("eth_chainId", []), ("eth_gasPrice", [])]
        with pytest.raises(ArcValidationError, match="RPC server returned error: {'code': -32000, 'message': 'execution reverted'}"):
            transport.request_batch(calls)

    def test_batch_missing_result_key_fails_distinguished_from_null(self) -> None:
        """A response element with neither 'error' nor 'result' key must fail closed as malformed."""
        payload = [
            {"jsonrpc": "2.0", "id": 1},
        ]
        mock_opener = _build_mock_opener(response_payload=payload)
        transport = HttpReadOnlyRpcTransport(
            endpoint_url="https://rpc.arc.io",
            allow_network=True,
            opener=mock_opener,
        )

        with pytest.raises(ArcValidationError, match="missing 'result' field"):
            transport.request_batch([("eth_chainId", [])])


class TestHttpBatchCircuitBreakerAndSecurity:
    """Verify circuit breaker trips on consecutive batch failures and credentials are sanitized."""

    def test_batch_circuit_breaker_trips_after_consecutive_failures(self) -> None:
        """Three consecutive batch failures must trip the circuit breaker and halt further attempts."""
        mock_opener = _build_mock_opener(
            side_effect=urllib.error.URLError("Connection refused"),
        )
        transport = HttpReadOnlyRpcTransport(
            endpoint_url="https://rpc.arc.io",
            max_consecutive_failures=3,
            allow_network=True,
            opener=mock_opener,
        )

        calls: list[tuple[str, list[Any]]] = [("eth_chainId", [])]

        # Failure 1
        with pytest.raises(ArcValidationError, match="Batch RPC request failed"):
            transport.request_batch(calls)
        assert not transport.is_circuit_broken

        # Failure 2
        with pytest.raises(ArcValidationError, match="Batch RPC request failed"):
            transport.request_batch(calls)
        assert not transport.is_circuit_broken

        # Failure 3: trips circuit breaker
        with pytest.raises(ArcCircuitBreakerTrippedError, match="Circuit Breaker TRIPPED"):
            transport.request_batch(calls)
        assert transport.is_circuit_broken

        # Attempt 4: instant halt without invoking opener
        mock_opener.open.reset_mock()  # type: ignore[unreachable]
        with pytest.raises(ArcCircuitBreakerTrippedError, match="RPC Circuit Breaker is TRIPPED"):
            transport.request_batch(calls)
        mock_opener.open.assert_not_called()

    def test_batch_error_sanitization_does_not_leak_credentials(self) -> None:
        """Exceptions mentioning endpoint URLs with credentials must be redacted in error output."""
        sensitive_url = "https://svc_account:super_secret_token_8888@rpc.arc.io/v1/auth"
        mock_opener = _build_mock_opener(
            side_effect=urllib.error.URLError(
                f"Failed to connect to {sensitive_url}: network timeout"
            ),
        )
        transport = HttpReadOnlyRpcTransport(
            endpoint_url=sensitive_url,
            allow_network=True,
            opener=mock_opener,
        )

        calls: list[tuple[str, list[Any]]] = [("eth_chainId", [])]
        with pytest.raises(ArcValidationError) as exc_info:
            transport.request_batch(calls)

        err_str = str(exc_info.value)
        assert "super_secret_token_8888" not in err_str
        assert "<REDACTED>" in err_str
