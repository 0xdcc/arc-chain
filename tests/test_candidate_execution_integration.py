"""Candidate-only execution counterexamples; all signing and RPC are inert stubs.

Run exclusively through the approved namespace layer on the host review tree.
No external calls, real account construction, signed bytes, or notifications.
"""

import copy
import sqlite3
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from core.config import load_safe_config
from core.wallet_guard import WalletGuard
from execution.audit_verifier import ReceiptAuditor
from execution.funds import BASES, TRANSFER_TOPIC, FundsError, PriceEvidence, hex_value
from execution.funds_ledger import ExecutionLatched, FundsLedger
from execution.funds_runtime import FundsRuntime, PostBroadcastUnresolved, SettledLoss
from execution.protocols import ROUTER, RouteVerifier
from execution.weth_arbitrage_executor import ArbitrageLeg, ArbitragePlan, WethArbitrageExecutor
from web3 import Web3

WALLET = "0x" + "11" * 20
BLOCK = "0x" + "44" * 32
RAW_STUB = b"inert fixture bytes; not an EVM transaction"
DIGEST = hex_value(Web3.keccak(RAW_STUB), 32)


def make_plan(base="USDG"):
    token, decimals = BASES[base]
    other = BASES["WETH" if base == "USDG" else "USDG"][0]
    amount = 10_000_000 if base == "USDG" else 4 * 10**15
    return ArbitragePlan(
        legs=[
            ArbitrageLeg(token, other, 100, pool_address="0x" + "55" * 20),
            ArbitrageLeg(other, token, 500, pool_address="0x" + "66" * 20),
        ],
        amount_in_weth=amount,
        expected_amount_out=amount * 101 // 100,
        amount_out_min=amount * 99 // 100,
        amount_usd=10,
        slippage_pct=1,
        base_symbol=base,
        base_token=token,
        decimals=decimals,
    )


def transfer(sender, recipient, amount, index):
    return {
        "address": BASES["USDG"][0],
        "transactionHash": DIGEST,
        "blockHash": BLOCK,
        "logIndex": hex(index),
        "topics": [TRANSFER_TOPIC, "0x" + "0" * 24 + sender[2:], "0x" + "0" * 24 + recipient[2:]],
        "data": "0x" + f"{amount:064x}",
    }


@pytest.fixture
def integration(tmp_path, monkeypatch):
    monkeypatch.setattr("execution.funds_runtime.time.time", lambda: 100.0)
    monkeypatch.setattr("eth_account.Account.from_key", lambda _: SimpleNamespace(address=WALLET))
    w3 = MagicMock()
    w3.eth.gas_price = 10**9
    w3.eth.estimate_gas.return_value = 100_000
    w3.eth.get_transaction_count.return_value = 1
    w3.eth.get_balance.return_value = 10**18
    w3.eth.contract.return_value.functions.balanceOf.return_value.call.return_value = 10**20
    w3.eth.contract.return_value.functions.allowance.return_value.call.return_value = 10**20
    w3.eth.account.sign_transaction.return_value = SimpleNamespace(raw_transaction=RAW_STUB)
    w3.eth.send_raw_transaction.return_value = Web3.to_bytes(hexstr=DIGEST)
    block = {"number": "0xa", "hash": BLOCK, "timestamp": "0x64"}
    receipt = {
        "transactionHash": DIGEST,
        "blockHash": BLOCK,
        "blockNumber": "0xa",
        "status": "0x1",
        "gasUsed": hex(100_000),
        "effectiveGasPrice": hex(10**9),
        "gasUsedForL1": hex(25_000),
        "logs": [transfer(WALLET, ROUTER, 10_000_000, 0), transfer(ROUTER, WALLET, 10_100_000, 1)],
    }
    tx = {
        "hash": DIGEST,
        "chainId": "0x1237",
        "from": WALLET,
        "to": ROUTER,
        "value": "0x0",
        "blockNumber": "0xa",
        "blockHash": BLOCK,
    }
    responses = {
        "eth_chainId": "0x1237",
        "eth_getTransactionByHash": tx,
        "eth_getTransactionReceipt": receipt,
        "eth_getBlockByNumber": block,
        "eth_blockNumber": "0xb",
    }

    def request(method, params):
        assert method in responses
        return copy.deepcopy(responses[method])

    def price_reader(token, at_block):
        return PriceEvidence(
            token,
            D(2500) if token == BASES["WETH"][0] else D(1),
            int(at_block["number"], 16),
            at_block["hash"],
            100,
            "fixture-reader",
        )

    auditor = ReceiptAuditor(
        request, wallet=WALLET, router=ROUTER, counterparties={ROUTER}, price_reader=price_reader
    )
    verifier = MagicMock(spec=RouteVerifier)
    verifier.block.return_value = block
    ledger = FundsLedger(tmp_path / "wallet.sqlite")
    runtime = FundsRuntime(ledger, verifier, auditor, price_reader, lambda: (True, True, False))
    cfg = load_safe_config(
        _env_file=None,
        PRIVATE_KEY="not-a-key; from_key is stubbed",
        DRY_RUN=False,
        UNIVERSAL_ROUTER_ADDRESS=ROUTER,
    )
    executor = WethArbitrageExecutor(
        config=cfg, guard=WalletGuard(dry_run=False), w3=w3, funds_runtime=runtime
    )
    return SimpleNamespace(
        executor=executor,
        runtime=runtime,
        ledger=ledger,
        w3=w3,
        responses=responses,
        receipt=receipt,
        verifier=verifier,
    )


def broadcast(ctx, plan=None):
    candidate = make_plan() if plan is None else plan
    return ctx.executor.broadcast_swap(
        tx={"data": "0xdeadbeef"},
        wallet_address=WALLET,
        base_symbol=candidate.base_symbol,
        base_token=candidate.base_token,
        plan=candidate,
    )


def test_broadcast_reserves_before_signing_and_retains_actual_loss(integration):
    ctx = integration

    def sign_stub(payload, private_key):
        status = ctx.ledger.status(4663, WALLET, "WETH")
        assert status["pending"]
        assert 0 < status["reserved_usd"] <= 1
        assert status["remaining_usd"] == 1 - status["reserved_usd"]
        assert payload["data"] != "0xdeadbeef"  # Exact plan is rebuilt, arbitrary tx ignored.
        return SimpleNamespace(raw_transaction=RAW_STUB)

    ctx.w3.eth.account.sign_transaction.side_effect = sign_stub
    with pytest.raises(SettledLoss) as error:
        broadcast(ctx)
    facts = error.value.report
    assert facts["token_delta_usd"] == D(".10")
    assert facts["actual_gas_usd"] == D(".25")
    assert facts["net_profit_usd"] == D("-.15")
    assert facts["real_profit_usd"] == -0.15
    assert ctx.ledger.status(4663, WALLET, "WETH")["paused"] is True
    assert ctx.ledger.status(4663, WALLET, "USDG")["spent_usd"] == D(".15")
    with pytest.raises(ExecutionLatched):
        broadcast(ctx, make_plan("WETH"))
    assert ctx.w3.eth.send_raw_transaction.call_count == 1


def test_timeout_restart_cross_base_cannot_send_twice(integration):
    ctx = integration
    ctx.w3.eth.wait_for_transaction_receipt.side_effect = TimeoutError("fixture timeout")
    with pytest.raises(PostBroadcastUnresolved):
        broadcast(ctx)
    ctx.runtime.ledger = FundsLedger(ctx.ledger.path)
    with pytest.raises(ExecutionLatched):
        broadcast(ctx, make_plan("WETH"))
    assert ctx.w3.eth.send_raw_transaction.call_count == 1
    assert ctx.runtime.ledger.status(4663, WALLET, "WETH")["pending"]


def test_remaining_point_four_rejects_point_five_before_signing(integration):
    ctx = integration
    # Inject persisted historical accounting for an independently resumed wallet.
    # Production API deliberately does not automatically reopen a loss-paused wallet.
    with sqlite3.connect(ctx.ledger.path) as db:
        db.execute("INSERT INTO wallets(chain,wallet,spent) VALUES(4663,?,'0.6')", (WALLET,))
    ctx.w3.eth.estimate_gas.return_value = 140_000  # Conservative gas >0.50 USD.
    with pytest.raises(FundsError, match="Gas alone exceeds"):
        broadcast(ctx)
    assert ctx.ledger.status(4663, WALLET, "WETH")["remaining_usd"] == D(".4")
    ctx.w3.eth.account.sign_transaction.assert_not_called()
    ctx.w3.eth.send_raw_transaction.assert_not_called()


def test_reverted_receipt_keeps_actual_gas_and_pauses(integration):
    ctx = integration
    ctx.receipt["status"] = "0x0"
    ctx.receipt["logs"] = []
    with pytest.raises(SettledLoss) as error:
        broadcast(ctx)
    assert error.value.report["receipt_status"] == 0
    assert error.value.report["net_profit_usd"] == D("-.25")
    assert ctx.ledger.status(4663, WALLET, "USDG")["paused"]


def test_zero_missing_gas_and_reorg_never_upgrade(integration):
    ctx = integration
    del ctx.receipt["effectiveGasPrice"]
    with pytest.raises(PostBroadcastUnresolved):
        broadcast(ctx)
    state = ctx.ledger.status(4663, WALLET, "USDG")
    assert state["mode"] == "PROBE" and state["pending"]


def test_profitable_usdg_does_not_promote_weth(integration):
    ctx = integration
    ctx.receipt["logs"][1] = transfer(ROUTER, WALLET, 12_500_000, 1)
    result = broadcast(ctx)
    assert result["real_profit_usdg"] == 2.5
    assert result["real_profit_usd"] == 2.25
    assert ctx.ledger.status(4663, WALLET, "USDG")["mode"] == "NORMAL"
    assert ctx.ledger.status(4663, WALLET, "WETH")["mode"] == "PROBE"


def test_pre_sign_failure_releases_without_spending(integration):
    ctx = integration
    ctx.w3.eth.account.sign_transaction.side_effect = ValueError("stub refuses signing")
    with pytest.raises(ValueError, match="stub refuses"):
        broadcast(ctx)
    status = ctx.ledger.status(4663, WALLET, "USDG")
    assert status["pending"] is None
    assert status["reserved_usd"] == status["spent_usd"] == 0
    ctx.w3.eth.send_raw_transaction.assert_not_called()


def test_actual_execute_reaches_reserved_boundary(integration, monkeypatch):
    ctx = integration
    monkeypatch.setattr(ctx.executor.guard, "assert_tokens_tax_free", lambda _: None)
    # This assertion targets wiring from execute, not solely the accounting helper.
    with pytest.raises(SettledLoss):
        ctx.executor.execute(make_plan(), wallet_address=WALLET, dry_run=False)
    assert ctx.verifier.verify.call_count == 1
    assert ctx.w3.eth.send_raw_transaction.call_count == 1


def test_missing_runtime_is_not_a_legacy_send_fallback(integration):
    ctx = integration
    ctx.executor.funds_runtime = None
    with pytest.raises(FundsError, match="requires reviewed funds runtime"):
        broadcast(ctx)
    ctx.w3.eth.send_raw_transaction.assert_not_called()


def test_pending_reconciliation_after_restart_never_resends(integration):
    ctx = integration
    ctx.w3.eth.wait_for_transaction_receipt.side_effect = TimeoutError("fixture")
    with pytest.raises(PostBroadcastUnresolved):
        broadcast(ctx)
    ctx.runtime.ledger = FundsLedger(ctx.ledger.path)
    with pytest.raises(SettledLoss):
        ctx.runtime.reconcile_pending(WALLET)
    assert ctx.w3.eth.account.sign_transaction.call_count == 1
    assert ctx.w3.eth.send_raw_transaction.call_count == 1
    assert ctx.runtime.ledger.status(4663, WALLET, "WETH")["paused"]


def test_daemon_shared_execution_boundary_retains_loss_and_stops(integration, monkeypatch):
    from monitors.daemons.arbitrage_daemon import ArbitrageDaemon

    ctx = integration
    monkeypatch.setattr(ctx.executor.guard, "assert_tokens_tax_free", lambda _: None)
    daemon = ArbitrageDaemon(auto_execute=False, funds_runtime=ctx.runtime)
    daemon.executor = ctx.executor
    daemon.auto_execute = daemon.running = True
    with pytest.raises(SettledLoss):
        daemon._execute_admitted_plan(make_plan())
    assert daemon.running is False
    assert D(daemon.last_execution["actual_gas_usd"]) == D(".25")
    assert D(daemon.last_execution["net_profit_usd"]) == D("-.15")
    with pytest.raises(ExecutionLatched):
        daemon._execute_admitted_plan(make_plan("WETH"))
    assert ctx.w3.eth.send_raw_transaction.call_count == 1


def test_configured_probe_amount_cannot_be_bypassed_by_other_base(integration):
    ctx = integration
    ctx.runtime.probe_amount_usd = D(5)
    for base in ("USDG", "WETH"):
        with pytest.raises(ExecutionLatched, match="configured probe amount"):
            broadcast(ctx, make_plan(base))
    ctx.w3.eth.account.sign_transaction.assert_not_called()
    ctx.w3.eth.send_raw_transaction.assert_not_called()


def test_route_verification_rejects_unknown_transfer_semantics():
    def request(method, params):
        assert method == "eth_chainId"
        return "0x1237"

    verifier = RouteVerifier(request, {})
    with pytest.raises(FundsError, match="Unreviewed token transfer"):
        verifier.verify(
            make_plan(),
            {"from": WALLET, "to": ROUTER, "value": 0},
            WALLET,
            now=100,
            block={"number": "0xa", "hash": BLOCK, "timestamp": "0x64"},
        )


def test_route_rejects_router_balance_subsidy():
    from eth_abi import encode

    def request(method, params):
        if method == "eth_chainId":
            return "0x1237"
        if method == "eth_getCode":
            return "0x01"
        assert method == "eth_call"
        return "0x" + encode(["uint256"], [1]).hex()

    tokens = {entry[0] for entry in BASES.values()}
    digest = hex_value(Web3.keccak(b"\x01"), 32)
    verifier = RouteVerifier(request, {token: digest for token in tokens}, transfer_tokens=tokens)
    with pytest.raises(FundsError, match="pre-existing"):
        verifier.verify(
            make_plan(),
            {"from": WALLET, "to": ROUTER, "value": 0},
            WALLET,
            now=100,
            block={"number": "0xa", "hash": BLOCK, "timestamp": "0x64"},
        )


def test_daemon_rpc_latch_blocks_the_actual_execution_boundary(integration):
    import requests
    from arbitrage.monitor_rpc import MonitorRpcHalted
    from monitors.daemons.arbitrage_daemon import ArbitrageDaemon

    from tests.test_remaining_monitor_rpc import client

    ctx = integration
    daemon = ArbitrageDaemon(auto_execute=False, funds_runtime=ctx.runtime)
    daemon.executor = ctx.executor
    daemon.auto_execute = daemon.running = True
    rpc = client()
    rpc._session.post.side_effect = requests.Timeout("fixture")
    daemon.reader._rpc = rpc
    for _ in range(3):
        with pytest.raises((requests.Timeout, MonitorRpcHalted)):
            rpc.call("eth_blockNumber")
    with pytest.raises(MonitorRpcHalted):
        daemon._execute_admitted_plan(make_plan())
    assert daemon.running is False
    ctx.w3.eth.account.sign_transaction.assert_not_called()
    ctx.w3.eth.send_raw_transaction.assert_not_called()


def test_normal_mode_budget_checked_before_second_signature(integration):
    ctx = integration
    ctx.receipt["logs"][1] = transfer(ROUTER, WALLET, 12_500_000, 1)
    broadcast(ctx)
    assert ctx.ledger.status(4663, WALLET, "USDG")["mode"] == "NORMAL"
    ctx.w3.eth.get_transaction_count.return_value = 2
    ctx.w3.eth.estimate_gas.return_value = 400_000  # >1 USD conservative gas risk.
    candidate = make_plan()
    candidate.expected_amount_out = 20_000_000  # Positive net does not enlarge risk budget.
    with pytest.raises(ExecutionLatched, match="Wallet-global loss budget"):
        broadcast(ctx, candidate)
    assert ctx.w3.eth.account.sign_transaction.call_count == 1
    assert ctx.w3.eth.send_raw_transaction.call_count == 1
