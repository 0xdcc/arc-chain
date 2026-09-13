"""Real provider/daemon/executor binding with only public RPC and signing stubs."""

from decimal import Decimal

import pytest
from arbitrage.monitor_rpc import MonitorRpc
from eth_abi import encode
from execution.funds import FundsError
from execution.funds_runtime import SettledLoss
from monitors.daemons.arbitrage_daemon import ArbitrageDaemon
from web3 import Web3

from tests.receipt_fixture import make_receipt_fixture


def bind_daemon(ctx, tmp_path, monkeypatch):
    daemon = ArbitrageDaemon(auto_execute=False)
    rpc = MonitorRpc(urls=["https://fixture.invalid"], throttle=0)
    monkeypatch.setattr(rpc, "call", ctx.request)
    daemon.reader._rpc = rpc
    daemon.executor = ctx.executor
    daemon.configure_public_runtime(ctx.profile, tmp_path / "daemon-ledger.sqlite")
    daemon.running = daemon.auto_execute = True
    return daemon


def test_public_profile_binds_actual_daemon_and_all_admission_checks(tmp_path, monkeypatch):
    ctx = make_receipt_fixture(tmp_path, monkeypatch)
    daemon = bind_daemon(ctx, tmp_path, monkeypatch)
    monkeypatch.setattr(
        ctx.executor.guard,
        "check_private_key_file",
        lambda *_: pytest.fail("Public planning read a key file"),
    )
    assert daemon._get_trade_amount_usd(1000, base_symbol="USDG") == 30
    report = daemon._execute_admitted_plan(ctx.plan)
    assert report["broadcast"]["net_profit_usd"] == Decimal("2.25")
    assert daemon.funds_runtime is ctx.executor.funds_runtime
    assert daemon.funds_runtime.ledger.status(4663, ctx.wallet, "USDG")["mode"] == "NORMAL"
    assert daemon.funds_runtime.ledger.status(4663, ctx.wallet, "WETH")["mode"] == "PROBE"
    assert any(
        method == "eth_call" and params[0]["to"].lower() == ctx.profile["router"]
        for method, params in ctx.calls
    )
    ctx.w3.eth.send_raw_transaction.assert_called_once()


def test_independent_oracle_failure_rejects_before_signing(tmp_path, monkeypatch):
    ctx = make_receipt_fixture(tmp_path, monkeypatch)
    original = ctx.runtime.verifier.request
    selector = "0x" + Web3.keccak(text="latestRoundData()")[:4].hex()

    def stale(method, params):
        if method == "eth_call" and params[0]["data"].startswith(selector):
            return (
                "0x"
                + encode(
                    ["uint80", "int256", "uint256", "uint256", "uint80"], [7, 10**8, 1, 1, 7]
                ).hex()
            )
        return original(method, params)

    ctx.runtime.verifier.request = stale
    with pytest.raises(FundsError, match="stale"):
        ctx.runtime.prepare(ctx.executor, ctx.plan, ctx.wallet)
    ctx.w3.eth.account.sign_transaction.assert_not_called()
    ctx.w3.eth.send_raw_transaction.assert_not_called()


def test_settled_loss_uses_real_receipt_and_pauses_daemon(tmp_path, monkeypatch):
    ctx = make_receipt_fixture(tmp_path, monkeypatch, gain=-400_000, gas_used=40_000)
    daemon = bind_daemon(ctx, tmp_path, monkeypatch)
    with pytest.raises(SettledLoss):
        daemon._execute_admitted_plan(ctx.plan)
    assert daemon.running is False
    assert not daemon.scaled_up
    assert Decimal(daemon.last_execution["net_profit_usd"]) == Decimal("-.50")
    assert Decimal(daemon.last_execution["actual_gas_usd"]) == Decimal(".10")
    state = daemon.funds_runtime.ledger.status(4663, ctx.wallet, "USDG")
    assert state["paused"] and state["mode"] == "PAUSED"
    assert state["spent_usd"] == Decimal(".50")


def test_public_probe_denies_writes_and_stops_at_first_transport_error(monkeypatch):
    from unittest.mock import MagicMock

    import requests

    from scripts.probe_public_runtime import PublicProbe

    probe = PublicProbe()
    post = MagicMock(side_effect=requests.Timeout("inert fixture"))
    monkeypatch.setattr(probe.session, "post", post)
    with pytest.raises(ValueError, match="method denied"):
        probe.request("eth_sendRawTransaction", ["0x"])
    post.assert_not_called()
    with pytest.raises(requests.Timeout):
        probe.request("eth_chainId", [])
    with pytest.raises(ValueError, match="closed"):
        probe.request("eth_chainId", [])
    post.assert_called_once()
