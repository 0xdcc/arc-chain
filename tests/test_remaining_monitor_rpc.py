"""Real monitor transport entry with inert HTTP fixtures; never connects to RPC."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests

from arbitrage.monitor_rpc import MonitorRpc, MonitorRpcHalted
from core.rpc_policy import UnknownRpcMethodError
from monitors.daemons.arbitrage_daemon import ArbitrageDaemon


def client():
    rpc = MonitorRpc(throttle=0, urls=["https://fixture.invalid"])
    rpc._session = MagicMock()
    return rpc


def success(value="0xa"):
    return SimpleNamespace(
        status_code=200, json=lambda: {"jsonrpc": "2.0", "id": 1, "result": value}
    )


def test_third_actual_failure_stops_queued_requests_without_retry():
    rpc = client()
    rpc._session.post.side_effect = requests.Timeout("inert fixture timeout")

    def read(_):
        try:
            rpc.call("eth_blockNumber")
        except (requests.Timeout, MonitorRpcHalted):
            return
        raise AssertionError("Failed transport unexpectedly succeeded")

    with ThreadPoolExecutor(max_workers=6) as workers:
        list(workers.map(read, range(6)))
    assert rpc._session.post.call_count == 3
    assert rpc.circuit_open
    assert rpc.metrics()["outcomes"] == {"timeout": 3}
    assert rpc.metrics()["latency_sample_count"] == 3
    with pytest.raises(MonitorRpcHalted):
        rpc.call("eth_getCode", ["0x" + "11" * 20, "latest"])
    assert rpc._session.post.call_count == 3


def test_success_resets_consecutive_failure_count_but_not_history():
    rpc = client()
    assert rpc.metrics()["latency_p95_seconds"] is None
    rpc._session.post.side_effect = [
        requests.Timeout("fixture"),
        success(),
        requests.Timeout("fixture"),
    ]
    with pytest.raises(requests.Timeout):
        rpc.call("eth_blockNumber")
    assert rpc.call("eth_blockNumber")["result"] == "0xa"
    with pytest.raises(requests.Timeout):
        rpc.call("eth_blockNumber")
    assert rpc.metrics()["consecutive_failures"] == 1
    assert rpc.metrics()["outcomes"] == {"timeout": 2, "success": 1}
    assert not rpc.circuit_open


def test_write_or_override_refused_before_transport():
    rpc = client()
    with pytest.raises(UnknownRpcMethodError):
        rpc.call("eth_sendRawTransaction", ["0x00"])
    with pytest.raises(ValueError, match="overrides"):
        rpc.call("eth_call", [{}, "latest", {}])
    with pytest.raises(UnknownRpcMethodError):
        rpc.call_batch([{"id": 1, "method": "eth_sendTransaction", "params": []}])
    rpc._session.post.assert_not_called()


def test_http_and_rpc_errors_are_measured_without_hidden_retries():
    rpc = client()
    rpc._session.post.side_effect = [
        SimpleNamespace(status_code=429),
        SimpleNamespace(status_code=503),
        SimpleNamespace(status_code=200, json=lambda: {"id": 1, "error": {"code": -32000}}),
    ]
    for _ in range(2):
        with pytest.raises(RuntimeError):
            rpc.call("eth_blockNumber")
    with pytest.raises(MonitorRpcHalted):
        rpc.call("eth_blockNumber")
    assert rpc._session.post.call_count == 3
    assert rpc.metrics()["outcomes"] == {"http_429": 1, "http_503": 1, "rpc_error": 1}


def test_daemon_halts_before_alerts_when_reader_rpc_latched():
    rpc = client()
    rpc._session.post.side_effect = requests.Timeout("fixture")
    for _ in range(3):
        with pytest.raises((requests.Timeout, MonitorRpcHalted)):
            rpc.call("eth_blockNumber")
    daemon = ArbitrageDaemon(auto_execute=False)
    daemon.reader._rpc = rpc
    daemon.reader.batch_quote = MagicMock(return_value=[MagicMock(), MagicMock()])
    daemon.handle_spread_alert = MagicMock()
    daemon.running = True
    with pytest.raises(MonitorRpcHalted):
        daemon.scan_round()
    assert daemon.running is False
    daemon.handle_spread_alert.assert_not_called()
