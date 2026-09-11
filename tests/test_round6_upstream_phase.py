"""Real MonitorRpc call/session IO and runtime estimate/route phase probes."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests
from web3.exceptions import ContractLogicError

from arbitrage.monitor_rpc import MonitorRpc, MonitorRpcHalted
from execution.funds_ledger import FundsLedger
from execution.protocols import ROUTER, V3_QUOTER
from execution.public_runtime import build_public_runtime
from tests.test_round4_feedback import PATHS, invoke, setup_case


def wire_monitor(ctx, daemon, monkeypatch, stage, kind, *, fail_after=0):
    """Only the HTTP/session and Web3 IO edges are synthetic; no admission stub."""
    rpc = MonitorRpc(throttle=0, urls=["https://fixture.invalid"])
    reached = []
    data = {"fixture": "opaque rejection", "return": "0xdeadbeef"}

    def post(url, *, json, headers, timeout):
        method, params = json["method"], json["params"]
        if method == "eth_estimateGas":
            assert params[0]["to"].lower() == ROUTER
            assert params[0]["data"].startswith("0x3593564c") or params[0]["data"].startswith(
                "0x24856bc3"
            )
            result = "0x186a0"
        else:
            result = ctx.request(method, params)
        target = stage == "estimate" and method == "eth_estimateGas"
        target |= (
            stage in {"router", "quoter"}
            and method == "eth_call"
            and params[0]["to"].lower() == (ROUTER if stage == "router" else V3_QUOTER)
        )
        if target:
            reached.append((method, params))
        if target and len(reached) > fail_after:
            if kind == "transport":
                raise requests.Timeout("execution reverted: deliberately misleading transport text")
            code = {"revert": 3, "generic": -32000, "malformed": "3"}[kind]
            body = {"id": 1, "error": {"code": code, "data": data, "message": "opaque fixture"}}
        else:
            body = {"id": 1, "result": result}
        return SimpleNamespace(status_code=200, json=lambda: body)

    rpc._session = MagicMock()
    rpc._session.post.side_effect = post
    runtime = build_public_runtime(
        ctx.profile,
        rpc.call,
        ctx.ledger.path,
        lambda: (
            daemon.running and not rpc.circuit_open,
            daemon.auto_execute,
            daemon.one_shot_probe,
        ),
        Decimal(30),
    )
    ctx.runtime = daemon.funds_runtime = ctx.executor.funds_runtime = runtime
    daemon.reader._rpc = rpc
    ctx.w3.eth.estimate_gas.side_effect = lambda tx: int(
        rpc.call("eth_estimateGas", [tx])["result"], 16
    )
    return rpc, reached, data


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("stage", ("estimate", "router"))
def test_real_upstream_revert_preserves_phase(tmp_path, monkeypatch, path, stage):
    ctx, daemon, alert, events, _, _ = setup_case(
        tmp_path, monkeypatch, triangle="triangle" in path
    )
    if path.startswith("burst"):
        daemon._execute_admitted_plan(ctx.plan)
    sends = ctx.w3.eth.send_raw_transaction.call_count
    before = ctx.ledger.status(4663, ctx.wallet, "WETH")
    rpc, reached, data = wire_monitor(ctx, daemon, monkeypatch, stage, "revert")
    invoke(path, daemon, alert, ctx)
    assert len(reached) == 1, "ROUND6_UPSTREAM_STAGE_NOT_REACHED"
    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == "SIMULATION_REVERTED", "ROUND6_UPSTREAM_PHASE_LOST"
    assert event["phase"] == ("estimate_gas" if stage == "estimate" else "router_simulation")
    assert event["rpc_method"] == reached[0][0] and event["rpc_code"] == 3
    assert event["broadcast"] is False and event["actual_gas_usd"] == "0"
    assert "tx_hash" not in event and "net_profit_usd" not in event
    assert daemon.consecutive_snipe_fails == daemon.data_error_count == 0
    assert rpc.metrics()["consecutive_failures"] == 1 and not rpc.circuit_open
    assert ctx.ledger.status(4663, ctx.wallet, "WETH") == before
    assert ctx.w3.eth.account.sign_transaction.call_count == sends
    assert ctx.w3.eth.send_raw_transaction.call_count == sends


@pytest.mark.parametrize("stage", ("estimate", "router", "quoter"))
@pytest.mark.parametrize("kind", ("transport", "generic", "malformed"))
def test_non_reverts_never_claim_simulation_or_gas(tmp_path, monkeypatch, stage, kind):
    ctx, daemon, alert, events, _, _ = setup_case(tmp_path, monkeypatch)
    before = ctx.ledger.status(4663, ctx.wallet, "WETH")
    rpc, reached, _ = wire_monitor(ctx, daemon, monkeypatch, stage, kind)
    invoke("spread", daemon, alert, ctx)
    assert len(reached) == 1
    assert events and not any(event["event_type"] == "SIMULATION_REVERTED" for event in events)
    assert all("actual_gas_usd" not in event and "net_profit_usd" not in event for event in events)
    assert ctx.ledger.status(4663, ctx.wallet, "WETH") == before
    assert rpc.metrics()["consecutive_failures"] == 1
    ctx.w3.eth.account.sign_transaction.assert_not_called()
    ctx.w3.eth.send_raw_transaction.assert_not_called()


def test_quoter_code_three_is_not_full_router_rejection(tmp_path, monkeypatch):
    ctx, daemon, alert, events, _, _ = setup_case(tmp_path, monkeypatch)
    _, reached, _ = wire_monitor(ctx, daemon, monkeypatch, "quoter", "revert")
    invoke("spread", daemon, alert, ctx)
    assert len(reached) == 1
    assert not any(event["event_type"] == "SIMULATION_REVERTED" for event in events)
    ctx.w3.eth.send_raw_transaction.assert_not_called()


@pytest.mark.parametrize("batch", (False, True))
def test_error_envelope_and_third_failure_latch(batch):
    rpc = MonitorRpc(throttle=0, urls=["https://fixture.invalid"])
    data = {"opaque": ["0xdeadbeef", 17]}
    failure = {
        "id": 9 if batch else 1,
        "error": {"code": 3, "data": data, "message": "private provider detail"},
    }
    body = [failure, {"id": 4, "result": "0xa"}] if batch else failure
    rpc._session = MagicMock()
    rpc._session.post.return_value = SimpleNamespace(status_code=200, json=lambda: body)

    def call():
        if batch:
            return rpc.call_batch(
                [
                    {"id": 4, "method": "eth_blockNumber", "params": []},
                    {"id": 9, "method": "eth_estimateGas", "params": [{}]},
                ]
            )
        return rpc.call("eth_estimateGas", [{}])

    for n in (1, 2):
        with pytest.raises(RuntimeError) as error:
            call()
        assert getattr(error.value, "rpc_method", None) == "eth_estimateGas", (
            "ROUND6_RPC_STRUCTURE_LOST"
        )
        assert error.value.rpc_code == 3 and error.value.rpc_data == data
        assert "private provider detail" not in str(error.value)
        assert rpc.metrics()["consecutive_failures"] == n
    with pytest.raises(MonitorRpcHalted) as halted:
        call()
    assert halted.value.__cause__.rpc_data == data
    assert rpc.circuit_open and rpc._session.post.call_count == 3
    with pytest.raises(MonitorRpcHalted):
        call()
    assert rpc._session.post.call_count == 3
    assert rpc.metrics()["outcomes"] == {"rpc_error": 3}


def test_estimate_web3_typed_error_keeps_phase(tmp_path, monkeypatch):
    ctx, daemon, alert, events, _, _ = setup_case(tmp_path, monkeypatch)
    ctx.w3.eth.estimate_gas.side_effect = ContractLogicError("opaque", data="0xdeadbeef")
    invoke("spread", daemon, alert, ctx)
    assert len(events) == 1
    assert events[0]["event_type"] == "SIMULATION_REVERTED", "ROUND6_ESTIMATE_TYPED_PHASE_LOST"
    assert events[0]["phase"] == "estimate_gas" and events[0]["rpc_method"] == "eth_estimateGas"
    ctx.w3.eth.account.sign_transaction.assert_not_called()
    ctx.w3.eth.send_raw_transaction.assert_not_called()


def test_broadcast_timeout_is_unknown_with_persisted_hash(tmp_path, monkeypatch):
    ctx, daemon, alert, events, _, _ = setup_case(tmp_path, monkeypatch)
    # The router/quoter/estimate paths succeed through the real upstream client.
    rpc, reached, _ = wire_monitor(ctx, daemon, monkeypatch, "none", "transport")
    ctx.w3.eth.send_raw_transaction.side_effect = requests.Timeout("execution reverted")
    invoke("spread", daemon, alert, ctx)
    assert not reached and not rpc.circuit_open
    assert len(events) == 1 and events[0]["event_type"] == "RECONCILIATION_REQUIRED"
    assert events[0]["status"] == "UNKNOWN" and "actual_gas_usd" not in events[0]
    assert "net_profit_usd" not in events[0]
    assert FundsLedger(ctx.ledger.path).pending_intent(4663, ctx.wallet)["state"] == "UNKNOWN"
    ctx.w3.eth.account.sign_transaction.assert_called_once()
    ctx.w3.eth.send_raw_transaction.assert_called_once()


@pytest.mark.parametrize("path", ("burst_spread", "burst_triangle"))
@pytest.mark.parametrize("stage", ("estimate", "router"))
def test_success_then_upstream_revert_retains_phase_summary(tmp_path, monkeypatch, path, stage):
    ctx, daemon, alert, events, messages, _ = setup_case(
        tmp_path, monkeypatch, triangle="triangle" in path
    )
    first = daemon._execute_admitted_plan(ctx.plan)["broadcast"]
    rpc, reached, _ = wire_monitor(ctx, daemon, monkeypatch, stage, "revert", fail_after=1)
    assert invoke(path, daemon, alert, ctx, initial_profit_usd=float(first["net_profit_usd"])) == 1
    assert len(reached) == 2 and rpc.metrics()["consecutive_failures"] == 1
    assert ctx.w3.eth.send_raw_transaction.call_count == 2
    assert ctx.w3.eth.account.sign_transaction.call_count == 2
    assert [e["event_type"] for e in events] == ["BURST_CHOMP_SUCCESS", "SIMULATION_REVERTED"]
    phase = "estimate_gas" if stage == "estimate" else "router_simulation"
    summary = next(msg for msg in messages if "累计净到手 USD" in msg)
    assert phase in summary and reached[-1][0] in summary, "ROUND6_BURST_SUMMARY_PHASE_LOST"
    total = Decimal(str(first["net_profit_usd"])) + Decimal(str(events[0]["profit_usd"]))
    assert f"+${total:.2f} USD" in summary
    assert events[1]["actual_gas_usd"] == "0" and events[1]["broadcast"] is False
    assert ctx.ledger.status(4663, ctx.wallet, "WETH")["pending"] is None
