"""Router-phase feedback with real admission and memory-only transport/delivery."""

from decimal import Decimal

import pytest
from web3.exceptions import ContractLogicError

from execution.protocols import ROUTER, V3_QUOTER, RouterSimulationRevertError
from tests.test_round4_feedback import DAEMON, PATHS, invoke, setup_case


def inject_failure(ctx, monkeypatch, *, location="router", kind="structured"):
    """Fail only after the real fixture has validated the targeted RPC input."""
    original = ctx.runtime.verifier.request
    reached = []

    def request(method, params):
        result = original(method, params)
        target = ROUTER if location == "router" else V3_QUOTER
        if method == "eth_call" and params[0]["to"].lower() == target:
            reached.append((method, params))
            if kind == "typed":
                raise ContractLogicError("opaque fixture rejection")
            if kind == "transport":
                raise TimeoutError("execution reverted")  # text is deliberately misleading
            if kind == "generic":
                return {"error": {"code": -32000, "message": "execution reverted"}}
            if kind == "malformed":
                return {"error": {"code": "3", "message": "execution reverted"}}
            return {"error": {"code": 3, "message": "opaque fixture rejection", "data": "0x"}}
        return result

    monkeypatch.setattr(ctx.runtime.verifier, "request", request)
    return reached


def prime_burst(path, ctx, daemon):
    """Obtain NORMAL through an actual positive synthetic receipt, without promotion mocks."""
    if path.startswith("burst"):
        daemon._execute_admitted_plan(ctx.plan)
        assert ctx.ledger.status(4663, ctx.wallet, "WETH")["mode"] == "NORMAL"


def assert_no_funds_effect(ctx, before, sends):
    """The failed attempt cannot reserve, sign, send, spend, pause or change base mode."""
    assert ctx.ledger.status(4663, ctx.wallet, "WETH") == before
    assert before["pending"] is None and before["reserved_usd"] == 0
    assert before["spent_usd"] == Decimal(0)
    assert ctx.w3.eth.account.sign_transaction.call_count == sends
    assert ctx.w3.eth.send_raw_transaction.call_count == sends


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("kind", ("structured", "typed"))
def test_router_revert_keeps_phase_and_budget(tmp_path, monkeypatch, path, kind):
    ctx, daemon, alert, events, _, _ = setup_case(
        tmp_path, monkeypatch, triangle="triangle" in path
    )
    prime_burst(path, ctx, daemon)
    events.clear()
    before = ctx.ledger.status(4663, ctx.wallet, "WETH")
    sends = ctx.w3.eth.send_raw_transaction.call_count
    reached = inject_failure(ctx, monkeypatch, kind=kind)
    invoke(path, daemon, alert, ctx)
    assert len(reached) == 1, "ROUND5_ROUTER_STAGE_NOT_REACHED"
    assert_no_funds_effect(ctx, before, sends)
    assert daemon.running
    assert daemon.consecutive_snipe_fails == daemon.data_error_count == 0
    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == "SIMULATION_REVERTED", "ROUND5_ROUTER_PHASE_LOST"
    assert event["phase"] == "router_simulation" and event["rpc_method"] == "eth_call"
    assert event["rpc_code"] == (3 if kind == "structured" else None)
    assert event["broadcast"] is False and event["actual_gas_usd"] == "0"
    assert event["verification_status"] == "not_broadcast"
    assert "tx_hash" not in event and "net_profit_usd" not in event
    assert daemon.last_execution["phase"] == "router_simulation"


@pytest.mark.parametrize(
    "location,kind",
    (
        ("quoter", "structured"),
        ("router", "transport"),
        ("router", "generic"),
        ("router", "malformed"),
    ),
)
def test_other_failures_do_not_claim_router_revert(tmp_path, monkeypatch, location, kind):
    ctx, daemon, alert, events, _, _ = setup_case(tmp_path, monkeypatch)
    before = ctx.ledger.status(4663, ctx.wallet, "WETH")
    reached = inject_failure(ctx, monkeypatch, location=location, kind=kind)
    invoke("spread", daemon, alert, ctx)
    assert len(reached) == 1
    assert_no_funds_effect(ctx, before, 0)
    assert not any(event["event_type"] == "SIMULATION_REVERTED" for event in events)
    expected = "EXECUTION_EXCEPTION" if kind == "transport" else "ARBITRAGE_DATA_ERROR"
    assert [event["event_type"] for event in events] == [expected]


def test_feedback_failure_preserves_original_typed_outcome(tmp_path, monkeypatch):
    ctx, daemon, _, _, _, _ = setup_case(tmp_path, monkeypatch)
    before = ctx.ledger.status(4663, ctx.wallet, "WETH")
    reached = inject_failure(ctx, monkeypatch)

    def failed_delivery(event):
        raise RuntimeError("inert delivery failure")

    monkeypatch.setattr(DAEMON + "notify_chain_auditor", failed_delivery)
    with pytest.raises(RouterSimulationRevertError) as caught:
        daemon._execute_admitted_plan(ctx.plan)
    assert caught.value.rpc_code == 3
    assert len(reached) == 1
    assert_no_funds_effect(ctx, before, 0)
    assert daemon.last_execution["phase"] == "router_simulation"
    assert daemon.consecutive_snipe_fails == daemon.data_error_count == 0


@pytest.mark.parametrize("path", ("burst_spread", "burst_triangle"))
def test_success_then_revert_preserves_burst_net_summary(tmp_path, monkeypatch, path):
    ctx, daemon, alert, events, messages, _ = setup_case(
        tmp_path, monkeypatch, triangle="triangle" in path
    )
    first = daemon._execute_admitted_plan(ctx.plan)["broadcast"]
    assert first["net_profit_usd"] == Decimal(".40")
    original = ctx.runtime.verifier.request
    reached = []

    def request(method, params):
        result = original(method, params)
        if method == "eth_call" and params[0]["to"].lower() == ROUTER:
            reached.append(method)
            if len(reached) == 2:
                return {"error": {"code": 3, "message": "opaque rejection"}}
        return result

    monkeypatch.setattr(ctx.runtime.verifier, "request", request)
    assert invoke(path, daemon, alert, ctx, initial_profit_eth=0.0002, initial_profit_usd=0.40) == 1
    assert reached == ["eth_call", "eth_call"]
    assert ctx.w3.eth.send_raw_transaction.call_count == 2
    assert ctx.w3.eth.account.sign_transaction.call_count == 2
    assert daemon.successful_snipes == 1 and daemon.consecutive_snipe_fails == 0
    debit, credit = (int(log["data"], 16) for log in ctx.receipt["logs"])
    gas = Decimal(ctx.gas_used * ctx.gas_price) / 10**18 * 2500
    net = Decimal(credit - debit) / 10**18 * 2500 - gas
    assert [event["event_type"] for event in events] == [
        "BURST_CHOMP_SUCCESS",
        "SIMULATION_REVERTED",
    ]
    assert Decimal(str(events[0]["profit_usd"])) == net
    total = Decimal(".40") + net
    assert any(f"累计净到手 USD: +${total:.2f} USD" in msg for msg in messages), (
        "ROUND5_REVERT_DROPPED_BURST_SUMMARY"
    )
    state = ctx.ledger.status(4663, ctx.wallet, "WETH")
    assert state["pending"] is None and state["reserved_usd"] == state["spent_usd"] == 0
