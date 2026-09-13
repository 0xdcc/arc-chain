"""Round4 feedback through real planners, runtime, receipt verifier and durable ledger.

All transport/signing is inert fixture IO; event and notification delivery is memory-only.
"""

from decimal import Decimal

import pytest
from arbitrage.spread_monitor import PoolSpec, PriceQuote, SpreadAlert
from arbitrage.triangular import DirectedEdge, calculate_triangular_path
from execution.funds import BASES
from execution.funds_ledger import ExecutionLatched, FundsLedger
from execution.funds_runtime import PostBroadcastUnresolved, SettledLoss
from monitors.daemons.arbitrage_daemon import ArbitrageDaemon

from tests.receipt_fixture import make_receipt_fixture

DAEMON = "monitors.daemons.arbitrage_daemon."
PATHS = ("spread", "triangle", "burst_spread", "burst_triangle")


def setup_case(tmp_path, monkeypatch, *, triangle=False, gain=2 * 10**14, gas_used=40_000):
    """Construct coherent market data; every economic check remains real."""
    ctx = make_receipt_fixture(
        tmp_path, monkeypatch, base="WETH", gain=gain, gas_used=gas_used, triangle=triangle
    )
    daemon = ArbitrageDaemon(auto_execute=False, funds_runtime=ctx.runtime, max_burst_rounds=2)
    daemon.executor = ctx.executor
    daemon.running = daemon.auto_execute = True
    ctx.w3.eth.get_block.return_value = {"baseFeePerGas": 1}
    events, messages, opportunities = [], [], []
    monkeypatch.setattr(DAEMON + "notify_chain_auditor", events.append)
    monkeypatch.setattr(DAEMON + "send_qq_notification", lambda msg, target: messages.append(msg))
    monkeypatch.setattr(daemon, "record_opportunity", lambda *args: opportunities.append(args))
    if triangle:
        rates = (2500, 1, 0.00042)
        edges, quotes = [], []
        for leg, rate in zip(ctx.plan.legs, rates, strict=True):
            pool = PoolSpec(
                address=leg.pool_address,
                label="round4-v3",
                fee_bps=leg.pool_fee / 100,
                token0=leg.from_token,
                token1=leg.to_token,
                dec0=18 if leg.from_token == ctx.token else 6,
                dec1=18 if leg.to_token == ctx.token else 6,
            )
            edges.append(
                DirectedEdge(
                    from_token=leg.from_token,
                    to_token=leg.to_token,
                    pool=pool,
                    rate=rate,
                    fee_bps=pool.fee_bps,
                    effective_rate=rate * (1 - pool.fee_bps / 10_000),
                    weight=0,
                    block_number=10,
                )
            )
            quotes.append(
                PriceQuote(
                    pool=pool,
                    base=leg.from_token,
                    quote=leg.to_token,
                    price=rate,
                    raw_price_t1_per_t0=rate,
                    block_number=10,
                    block_hash=ctx.block["hash"],
                    ts=float(int(ctx.block["timestamp"], 16)),
                )
            )
        alert = calculate_triangular_path(*edges, slippage_buffer_pct=0.1)
        assert alert is not None
        alert.max_capacity_usd = 10
    else:
        pools = [
            PoolSpec(
                address=leg.pool_address,
                label="round4-v3",
                fee_bps=leg.pool_fee / 100,
                token0=BASES["USDG"][0],
                token1=BASES["WETH"][0],
                dec0=6,
                dec1=18,
            )
            for leg in ctx.plan.legs
        ]
        alert = SpreadAlert(
            base=BASES["USDG"][0],
            quote=BASES["WETH"][0],
            buy_pool=pools[0],
            sell_pool=pools[1],
            buy_price=0.0004,
            sell_price=0.00042,
            gross_spread_pct=5,
            total_fee_pct=0.06,
            net_spread_pct=4.94,
            fee_pct=0.06,
            max_capacity_usd=10,
            optimal_size_usd=10,
            max_profit_usd=0.494,
            profit_at_500u=24.7,
            base_token="WETH",
            ts=float(int(ctx.block["timestamp"], 16)),
        )
        quotes = [
            PriceQuote(
                pool=pool,
                base=alert.base,
                quote=alert.quote,
                price=price,
                raw_price_t1_per_t0=price,
                block_number=10,
                block_hash=ctx.block["hash"],
                ts=alert.ts,
            )
            for pool, price in zip(pools, (alert.buy_price, alert.sell_price), strict=True)
        ]
    by_pool = {quote.pool.address: quote for quote in quotes}
    monkeypatch.setattr(daemon.reader, "quote", lambda pool: by_pool[pool.address])
    return ctx, daemon, alert, events, messages, opportunities


def invoke(path, daemon, alert, ctx, **kwargs):
    """Enter the actual public initial/burst method with public wallet identity."""
    if path == "spread":
        return daemon._try_auto_snipe_spread(alert)
    if path == "triangle":
        return daemon._try_auto_snipe_triangle(alert)
    method = daemon._run_burst_chomp_spread if path == "burst_spread" else daemon._run_burst_chomp
    return method(alert, wallet_addr=ctx.wallet, weth_price_est=2500, **kwargs)


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("outcome", ("loss", "unknown"))
def test_four_paths_pause_once_with_receipt_or_pending(tmp_path, monkeypatch, path, outcome):
    ctx, daemon, alert, events, _, _ = setup_case(
        tmp_path, monkeypatch, triangle="triangle" in path
    )
    if path.startswith("burst"):
        daemon._execute_admitted_plan(ctx.plan)
        assert ctx.ledger.status(4663, ctx.wallet, "WETH")["mode"] == "NORMAL"
    before = ctx.w3.eth.send_raw_transaction.call_count
    # A real status=0 receipt is a definite loss, including in NORMAL mode.
    # It has no transfer logs and charges its actual gas, not an invented token loss.
    ctx.receipt_status = 0
    if outcome == "unknown":
        ctx.w3.eth.send_raw_transaction.side_effect = TimeoutError("inert send timeout")
    caught = []
    execute = ctx.executor.execute

    def capture_execution(*args, **kwargs):
        try:
            return execute(*args, **kwargs)
        except (SettledLoss, PostBroadcastUnresolved) as exc:
            caught.append((args, kwargs, exc))
            raise

    monkeypatch.setattr(ctx.executor, "execute", capture_execution)
    daemon.consecutive_snipe_fails = 2
    invoke(path, daemon, alert, ctx)
    assert len(caught) == 1, "ROUND4_EXECUTION_SEAM_NOT_REACHED"
    assert not daemon.running and not daemon.scaled_up
    assert ctx.w3.eth.send_raw_transaction.call_count == before + 1
    event = events[-1]
    assert len(events) == 1, "ROUND4_DUPLICATE_OUTCOME_EVENT"
    assert event["tx_hash"] == ctx.digest
    assert event["chain_id"] == 4663 and event["wallet"] == ctx.wallet
    assert event["base_symbol"] == "WETH" and event["verification_status"] == "pending"
    assert event["source"] == "daemon_self_report"
    state = ctx.ledger.status(4663, ctx.wallet, "WETH")
    if outcome == "loss":
        assert event["event_type"] == "SETTLED_LOSS_PAUSED", "ROUND4_MISSING_SETTLED_LOSS_EVENT"
        assert daemon.consecutive_snipe_fails == 3
        assert Decimal(event["net_profit_usd"]) == Decimal("-.10")
        assert Decimal(event["token_delta_usd"]) == 0
        assert Decimal(event["actual_gas_usd"]) == Decimal(".10")
        assert event["receipt_status"] == "0" and event["block_hash"] == ctx.block["hash"]
        assert state["paused"] and state["pending"] is None
        assert Decimal(event["spent_usd"]) == Decimal(".10")
        assert Decimal(event["remaining_usd"]) == Decimal(".90")
    else:
        assert event["event_type"] == "RECONCILIATION_REQUIRED"
        assert daemon.consecutive_snipe_fails == 2
        assert event["status"] == "UNKNOWN"
        assert "net_profit_usd" not in event and "receipt_status" not in event
        assert state["pending"] == event["intent_id"] and not state["paused"]
        assert ctx.ledger.pending_intent(4663, ctx.wallet)["state"] == "UNKNOWN"
        assert state["spent_usd"] == 0
    snapshot = dict(daemon.last_execution)
    daemon._report_paused_execution(caught[0][1]["plan"], caught[0][2])
    assert daemon.last_execution == snapshot and len(events) == 1
    assert daemon.consecutive_snipe_fails == (3 if outcome == "loss" else 2)
    reopened = FundsLedger(ctx.ledger.path)
    for base in ("WETH", "USDG"):
        other = reopened.status(4663, ctx.wallet, base)
        assert other["pending"] == state["pending"] and other["paused"] == state["paused"]
        assert other["spent_usd"] == state["spent_usd"]
    invoke(path, daemon, alert, ctx)
    daemon.running = True  # restart cannot bypass persisted wallet admission
    with pytest.raises(ExecutionLatched):
        daemon._execute_admitted_plan(ctx.plan)
    assert ctx.w3.eth.send_raw_transaction.call_count == before + 1
    assert ctx.w3.eth.account.sign_transaction.call_count == before + 1


@pytest.mark.parametrize("spent", (".05", "1.00"))
@pytest.mark.parametrize("delivery_fails", (False, True))
def test_loss_budget_classification_and_delivery_failure(
    tmp_path, monkeypatch, spent, delivery_fails
):
    # Probe quote remains profitable; a canonical revert charges only receipt gas.
    ctx, daemon, _, events, _, _ = setup_case(tmp_path, monkeypatch)
    ctx.receipt_status = 0
    if spent == "1.00":
        # 100,000 gas at the admitted 4 gwei cap is exactly the $1 budget.
        ctx.w3.eth.gas_price = 3_333_333_333
        ctx.w3.eth.estimate_gas.return_value = 83_333
        ctx.gas_price = 4_000_000_000
        ctx.gas_used = 100_000
    else:
        ctx.gas_used = 20_000
    original_event = events.append

    def delivery(event):
        original_event(event)
        if delivery_fails:
            raise RuntimeError("inert event sink failure")

    monkeypatch.setattr(DAEMON + "notify_chain_auditor", delivery)
    with pytest.raises(SettledLoss) as raised:
        daemon._execute_admitted_plan(ctx.plan)
    assert not daemon.running and not daemon.scaled_up
    assert ctx.gas_used <= ctx.signed_payloads[-1]["gas"]
    assert ctx.gas_price <= ctx.signed_payloads[-1]["maxFeePerGas"]
    assert daemon.consecutive_snipe_fails == 1
    assert Decimal(daemon.last_execution["net_profit_usd"]) == -Decimal(spent)
    assert len(events) == 1
    assert events[0]["event_type"] == (
        "EMERGENCY_BREAKER_TRIGGERED" if Decimal(spent) >= 1 else "SETTLED_LOSS_PAUSED"
    )
    assert Decimal(events[0]["spent_usd"]) == Decimal(spent)
    assert Decimal(events[0]["remaining_usd"]) == max(0, 1 - Decimal(spent))
    daemon._report_paused_execution(ctx.plan, raised.value)
    assert len(events) == 1 and daemon.consecutive_snipe_fails == 1


@pytest.mark.parametrize("path", ("burst_spread", "burst_triangle"))
def test_burst_two_serial_receipts_report_net_and_total(tmp_path, monkeypatch, path):
    ctx, daemon, alert, events, messages, opportunities = setup_case(
        tmp_path, monkeypatch, triangle="triangle" in path
    )
    first = daemon._execute_admitted_plan(ctx.plan)["broadcast"]
    assert first["net_profit_usd"] == Decimal(".40")
    records = []
    execute = ctx.executor.execute

    def record_execution(*args, **kwargs):
        result = execute(*args, **kwargs)
        records.append((kwargs["plan"], result["broadcast"], dict(ctx.receipt)))
        return result

    monkeypatch.setattr(ctx.executor, "execute", record_execution)
    rounds = invoke(path, daemon, alert, ctx, initial_profit_eth=0.0002, initial_profit_usd=0.40)
    assert rounds == 2 and len(records) == 2, "ROUND4_NORMAL_TWO_SERIAL_SENDS_MISSING"
    assert daemon.successful_snipes == 2 and ctx.w3.eth.send_raw_transaction.call_count == 3
    assert len({first["tx_hash"], *(record[1]["tx_hash"] for record in records)}) == 3
    assert [payload["nonce"] for payload in ctx.signed_payloads] == [1, 2, 3]
    total = Decimal(".40")
    burst_events = [event for event in events if event["event_type"] == "BURST_CHOMP_SUCCESS"]
    assert len(burst_events) == 2
    for index, ((plan, report, receipt), event) in enumerate(
        zip(records, burst_events, strict=True)
    ):
        debit, credit = (int(log["data"], 16) for log in receipt["logs"])
        assert debit == plan.amount_in_weth
        token_usd = Decimal(credit - debit) / 10**18 * 2500
        gas_usd = (
            Decimal(int(receipt["gasUsed"], 16) * int(receipt["effectiveGasPrice"], 16))
            / 10**18
            * 2500
        )
        net = token_usd - gas_usd
        assert report["net_profit_usd"] == net and net > 0
        assert Decimal(str(event["profit_usd"])) == net, "ROUND4_BURST_GROSS_REPORTED_AS_NET"
        total += net
        assert Decimal(str(event["total_profit_usd"])) == total
        assert event["combo"] == index + 2
        assert Decimal(str(event["real_profit_eth"])) == Decimal(credit - debit) / 10**18
        assert opportunities[index][-1]["profit_usd"] == float(net)
        assert any(f"单笔净赚: +${net:.2f} USD" in message for message in messages)
    assert any(f"累计净到手 USD: +${total:.2f} USD" in message for message in messages)
    assert ctx.ledger.status(4663, ctx.wallet, "WETH")["pending"] is None


@pytest.mark.parametrize("path", ("burst_spread", "burst_triangle"))
@pytest.mark.parametrize("invalid", (None, "NaN"))
def test_missing_burst_accounting_is_post_execution_and_never_gross(
    tmp_path, monkeypatch, path, invalid
):
    ctx, daemon, alert, events, _, _ = setup_case(
        tmp_path, monkeypatch, triangle="triangle" in path
    )
    daemon._execute_admitted_plan(ctx.plan)
    execute = ctx.executor.execute
    returned = []

    def corrupt_return_after_real_reconciliation(*args, **kwargs):
        result = execute(*args, **kwargs)
        if invalid is None:
            result["broadcast"].pop("net_profit_usd")
        else:
            result["broadcast"]["net_profit_usd"] = invalid
        returned.append(result["broadcast"])
        return result

    monkeypatch.setattr(ctx.executor, "execute", corrupt_return_after_real_reconciliation)
    assert invoke(path, daemon, alert, ctx) == 0
    assert len(returned) == 1 and ctx.w3.eth.send_raw_transaction.call_count == 2
    assert not daemon.running and not daemon.scaled_up
    assert daemon.consecutive_snipe_fails == 0
    assert len(events) == 1 and events[0]["event_type"] == "RECONCILIATION_REQUIRED"
    assert events[0]["tx_hash"] == ctx.digest
    assert events[0]["phase"] == "post_execution_accounting"
    assert events[0]["pending"] is None and "profit_usd" not in events[0]
    assert ctx.ledger.status(4663, ctx.wallet, "WETH")["mode"] == "NORMAL"
    daemon._report_paused_execution(ctx.plan, PostBroadcastUnresolved("same result"), returned[0])
    assert len(events) == 1 and daemon.consecutive_snipe_fails == 0


@pytest.mark.parametrize("delivery_fails", (False, True))
def test_unknown_delivery_and_later_loss_reconciliation(tmp_path, monkeypatch, delivery_fails):
    ctx, daemon, _, events, _, _ = setup_case(tmp_path, monkeypatch)
    ctx.receipt_status = 0
    send = ctx.w3.eth.send_raw_transaction.side_effect

    def lost_response(raw):
        send(raw)  # fixture receipt exists, but the caller receives only a transport timeout
        raise TimeoutError("inert lost response")

    def delivery(event):
        events.append(event)
        if delivery_fails:
            raise RuntimeError("inert delivery failure")

    ctx.w3.eth.send_raw_transaction.side_effect = lost_response
    monkeypatch.setattr(DAEMON + "notify_chain_auditor", delivery)
    with pytest.raises(PostBroadcastUnresolved):
        daemon._execute_admitted_plan(ctx.plan)
    assert not daemon.running and not daemon.scaled_up
    assert len(events) == 1 and events[0]["event_type"] == "RECONCILIATION_REQUIRED"
    assert daemon.consecutive_snipe_fails == 0
    assert FundsLedger(ctx.ledger.path).pending_intent(4663, ctx.wallet)["hash"] == ctx.digest
    # Explicit read-only recovery in this test, never a daemon retry or new send.
    with pytest.raises(SettledLoss) as settled:
        ctx.runtime.reconcile_pending(ctx.wallet)
    daemon._report_paused_execution(ctx.plan, settled.value)
    daemon._report_paused_execution(ctx.plan, settled.value)
    assert len(events) == 2 and events[1]["event_type"] == "SETTLED_LOSS_PAUSED"
    assert daemon.consecutive_snipe_fails == 1
    assert not daemon.running and not daemon.scaled_up
    assert ctx.ledger.status(4663, ctx.wallet, "WETH")["pending"] is None
    ctx.w3.eth.send_raw_transaction.assert_called_once()
    ctx.w3.eth.account.sign_transaction.assert_called_once()
