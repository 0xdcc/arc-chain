"""Non-peg, non-default price probes with real planning, admission and receipt ledger."""

import time
from decimal import Decimal

import pytest

from arbitrage.spread_monitor import PoolSpec, PriceQuote, SpreadAlert
from arbitrage.triangular import DirectedEdge, calculate_triangular_path
from core.wallet_guard import ExcessiveAmountError
from execution.funds import BASES, FundsError, hex_value, validate_plan_value
from execution.funds_ledger import ExecutionLatched, FundsLedger
from monitors.daemons.arbitrage_daemon import ArbitrageDaemon
from tests.receipt_fixture import make_receipt_fixture
from tests.test_round4_feedback import DAEMON, PATHS


def price_case(
    tmp_path,
    monkeypatch,
    *,
    triangle=False,
    base="WETH",
    native_price="4000",
    usdg_price="0.8",
    one_shot=True,
):
    """Construct coherent market data; every economic check remains real."""
    ctx = make_receipt_fixture(
        tmp_path,
        monkeypatch,
        base=base,
        gain=(500_000 if base == "USDG" else 2 * 10**14),
        gas_used=40_000,
        triangle=triangle,
        native_price=Decimal(native_price),
        usdg_price=Decimal(usdg_price),
    )
    daemon = ArbitrageDaemon(
        auto_execute=False, funds_runtime=ctx.runtime, max_burst_rounds=1, one_shot_probe=one_shot
    )
    daemon.executor = ctx.executor
    daemon.running = daemon.auto_execute = True
    ctx.w3.eth.get_block.return_value = {"baseFeePerGas": 1}
    ctx.w3.eth.estimate_gas.return_value = 40_000
    events, messages, opportunities = [], [], []
    monkeypatch.setattr(DAEMON + "notify_chain_auditor", events.append)
    monkeypatch.setattr(DAEMON + "send_qq_notification", lambda msg, target: messages.append(msg))
    monkeypatch.setattr(daemon, "record_opportunity", lambda *args: opportunities.append(args))
    prices = {
        hex_value(BASES["WETH"][0], 20): Decimal(native_price),
        hex_value(BASES["USDG"][0], 20): Decimal(usdg_price),
        hex_value("0x" + "bb" * 20, 20): Decimal(1),
    }
    decimals = {
        hex_value(BASES["WETH"][0], 20): 18,
        hex_value(BASES["USDG"][0], 20): 6,
        hex_value("0x" + "bb" * 20, 20): 6,
    }
    if triangle:
        rates = tuple(
            float(prices[hex_value(leg.from_token, 20)] / prices[hex_value(leg.to_token, 20)])
            * (1.05 if i == 2 else 1)
            for i, leg in enumerate(ctx.plan.legs)
        )
        edges, quotes = [], []
        for leg, rate in zip(ctx.plan.legs, rates, strict=True):
            from_key = hex_value(leg.from_token, 20)
            to_key = hex_value(leg.to_token, 20)
            pool = PoolSpec(
                address=leg.pool_address,
                label="round4-v3",
                fee_bps=leg.pool_fee / 100,
                token0=leg.from_token,
                token1=leg.to_token,
                dec0=decimals[from_key],
                dec1=decimals[to_key],
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
            buy_price=float(Decimal(usdg_price) / Decimal(native_price)),
            sell_price=float(Decimal(usdg_price) / Decimal(native_price) * Decimal("1.05")),
            gross_spread_pct=5,
            total_fee_pct=0.06,
            net_spread_pct=4.94,
            fee_pct=0.06,
            max_capacity_usd=10,
            optimal_size_usd=10,
            max_profit_usd=0.494,
            profit_at_500u=24.7,
            base_token=base,
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


def enter(path, ctx, daemon, alert):
    """Use actual initial/burst entry; stale supplied price must not govern a new round."""
    if path.startswith("burst"):
        method = (
            daemon._run_burst_chomp_spread if path == "burst_spread" else daemon._run_burst_chomp
        )
        return method(
            alert,
            wallet_addr=ctx.wallet,
            weth_price_est=2500,
            is_usdg=ctx.plan.base_symbol == "USDG",
        )
    method = daemon._try_auto_snipe_spread if path == "spread" else daemon._try_auto_snipe_triangle
    return method(alert)


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("base", ("WETH", "USDG"))
@pytest.mark.parametrize("usdg_price", ("0.8", "1.25"))
def test_price_sizing_reaches_real_admission(tmp_path, monkeypatch, path, base, usdg_price):
    ctx, daemon, alert, events, messages, _ = price_case(
        tmp_path,
        monkeypatch,
        triangle="triangle" in path,
        base=base,
        usdg_price=usdg_price,
        one_shot=not path.startswith("burst"),
    )
    if path.startswith("burst"):
        daemon._execute_admitted_plan(ctx.plan)
        assert ctx.ledger.status(4663, ctx.wallet, base)["mode"] == "NORMAL"
    records = []
    original = ctx.executor.execute

    def capture(*args, **kwargs):
        records.append(kwargs["plan"])
        return original(*args, **kwargs)

    monkeypatch.setattr(ctx.executor, "execute", capture)
    sends = ctx.w3.eth.send_raw_transaction.call_count
    enter(path, ctx, daemon, alert)
    assert len(records) == 1, "ROUND6_PRICE_PLAN_NOT_REACHED"
    plan = records[0]
    price = Decimal(usdg_price) if base == "USDG" else Decimal(4000)
    actual = Decimal(plan.amount_in_weth) / 10**plan.decimals * price
    assert abs(actual - Decimal(str(plan.amount_usd))) <= price / 10**plan.decimals, (
        "ROUND6_PRICE_ATOMS_MISMATCH"
    )
    assert ctx.w3.eth.send_raw_transaction.call_count == sends + 1, (
        "ROUND6_PRICE_ADMISSION_NOT_REACHED"
    )
    debit, credit = (int(log["data"], 16) for log in ctx.receipt["logs"])
    gas = Decimal(ctx.gas_used * ctx.gas_price) / 10**18 * 4000
    assert (
        Decimal(daemon.last_execution["net_profit_usd"])
        == Decimal(credit - debit) / 10**plan.decimals * price - gas
    )
    assert ctx.ledger.status(4663, ctx.wallet, base)["spent_usd"] == 0
    assert events[-1]["event_type"] in {"LIVE_EXECUTION_SUCCESS", "BURST_CHOMP_SUCCESS"}


@pytest.mark.parametrize("path", ("spread", "triangle"))
@pytest.mark.parametrize("one_shot", (False, True))
def test_probe_label_and_tradeable_state(tmp_path, monkeypatch, path, one_shot):
    # Isolate phase from price sabotage: original 2500/1 data are intentional here.
    ctx, daemon, alert, _, messages, _ = price_case(
        tmp_path,
        monkeypatch,
        triangle=path == "triangle",
        native_price="2500",
        usdg_price="1",
        one_shot=one_shot,
    )
    daemon.max_burst_rounds = 0
    enter(path, ctx, daemon, alert)
    assert ctx.w3.eth.send_raw_transaction.call_count == 1, "ROUND6_PHASE_RECEIPT_NOT_REACHED"
    assert any("阶段: 首笔探路 成功成交" in msg for msg in messages), "ROUND6_PROBE_LABEL_LOST"
    state = ctx.ledger.status(4663, ctx.wallet, "WETH")
    assert daemon.scaled_up == (
        state["mode"] == "NORMAL" and not state["paused"] and not state["pending"]
    ), "ROUND6_PAUSED_SCALED_UP"
    if one_shot:
        assert state["paused"] and state["mode"] == "RECONCILED_PROFIT"
        daemon.running = True
        ctx.runtime.ledger = FundsLedger(ctx.ledger.path)
        with pytest.raises(ExecutionLatched):
            daemon._execute_admitted_plan(ctx.plan)
        assert ctx.w3.eth.send_raw_transaction.call_count == 1
    else:
        assert state["mode"] == "NORMAL" and daemon.running
        assert ctx.ledger.status(4663, ctx.wallet, "USDG")["mode"] == "PROBE"


@pytest.mark.parametrize("path", PATHS)
def test_discount_wallet_balance_is_valued_in_usd(tmp_path, monkeypatch, path):
    ctx, daemon, alert, _, _, _ = price_case(
        tmp_path,
        monkeypatch,
        triangle="triangle" in path,
        base="USDG",
        one_shot=not path.startswith("burst"),
    )
    if path.startswith("burst"):
        daemon._execute_admitted_plan(ctx.plan)
    # 8 tokens at $0.80; 95% usable = $6.08. All route inputs remain canonical.
    monkeypatch.setattr(ctx.executor, "get_usdg_balance", lambda _: 8_000_000)
    plans = []
    original = ctx.executor.execute

    def capture(*args, **kwargs):
        plans.append(kwargs["plan"])
        return original(*args, **kwargs)

    monkeypatch.setattr(ctx.executor, "execute", capture)
    enter(path, ctx, daemon, alert)
    assert len(plans) == 1, "ROUND6_BALANCE_PLAN_NOT_REACHED"
    assert plans[0].amount_usd == pytest.approx(6.08), "ROUND6_USDG_BALANCE_PEG"
    assert plans[0].amount_in_weth <= 7_600_000
    assert ctx.w3.eth.send_raw_transaction.call_count == (2 if path.startswith("burst") else 1)


@pytest.mark.parametrize("stale", (True, False))
def test_price_age_and_missing_runtime_still_block(tmp_path, monkeypatch, stale):
    ctx, daemon, alert, _, _, _ = price_case(tmp_path, monkeypatch)
    if stale:
        ctx.block["timestamp"] = hex(int(ctx.block["timestamp"], 16) - 60)
        with pytest.raises(FundsError):
            ctx.runtime.prepare(ctx.executor, ctx.plan, ctx.wallet)
    else:
        daemon.funds_runtime = None
        with pytest.raises(FundsError):
            daemon._execute_admitted_plan(ctx.plan)
    ctx.w3.eth.account.sign_transaction.assert_not_called()
    ctx.w3.eth.send_raw_transaction.assert_not_called()


@pytest.mark.parametrize("price", ("0.8", "1.25"))
@pytest.mark.parametrize("sizing", ("usd", "atoms"))
def test_manual_usdg_planner_uses_bound_price(tmp_path, monkeypatch, price, sizing):
    ctx = make_receipt_fixture(tmp_path, monkeypatch, base="USDG", usdg_price=Decimal(price))
    kwargs = {"amount_usd": 10} if sizing == "usd" else {"amount_in_weth": 20_000_000}
    plan = ctx.executor.plan_two_hop("WETH", 100, 500, fee_unit="raw", base_token="USDG", **kwargs)
    expected_atoms = int(Decimal(10) / Decimal(price) * 10**6) if sizing == "usd" else 20_000_000
    assert plan.amount_in_weth == expected_atoms, "ROUND6_MANUAL_USDG_PEG"
    expected_usd = Decimal(expected_atoms) / 10**6 * Decimal(price)
    assert Decimal(str(plan.amount_usd)) == expected_usd, "ROUND6_MANUAL_USDG_PEG"
    evidence = ctx.runtime.read_price(ctx.token, ctx.block)
    assert validate_plan_value(plan, evidence, time.time(), ctx.runtime.max_age) == expected_usd
    with pytest.raises(ExcessiveAmountError):
        ctx.executor.plan_two_hop(
            "WETH",
            100,
            500,
            fee_unit="raw",
            base_token="USDG",
            amount_usd=1,
            amount_in_weth=1_000_000_000,
        )
    ctx.w3.eth.account.sign_transaction.assert_not_called()
    ctx.w3.eth.send_raw_transaction.assert_not_called()


@pytest.mark.parametrize("base", ("WETH", "USDG"))
def test_same_block_stale_price_is_not_refreshed_by_planning(tmp_path, monkeypatch, base):
    ctx = make_receipt_fixture(tmp_path, monkeypatch, base=base, age_seconds=30)
    with pytest.raises(FundsError, match="Stale price"):
        ctx.runtime.planning_prices(base)
    with pytest.raises(FundsError, match="Stale price"):
        ctx.runtime.prepare(ctx.executor, ctx.plan, ctx.wallet)
    assert ctx.ledger.status(4663, ctx.wallet, base)["spent_usd"] == 0
    ctx.w3.eth.account.sign_transaction.assert_not_called()
    ctx.w3.eth.send_raw_transaction.assert_not_called()


def test_weth_atomic_floor_avoids_float_roundoff(tmp_path, monkeypatch):
    ctx = make_receipt_fixture(tmp_path, monkeypatch, base="WETH", native_price=Decimal("2673.09"))
    plan = ctx.executor.plan_two_hop(
        "USDG", 100, 500, fee_unit="raw", amount_usd=500, weth_price_usd=2673.09
    )
    actual = Decimal(plan.amount_in_weth) / 10**18 * Decimal("2673.09")
    assert 0 <= Decimal(500) - actual < Decimal("2673.09") / 10**18, "ROUND6_WETH_FLOAT_ATOMS"
    evidence = ctx.runtime.read_price(ctx.token, ctx.block)
    assert validate_plan_value(plan, evidence, time.time(), ctx.runtime.max_age) == actual
    ctx.w3.eth.account.sign_transaction.assert_not_called()
    ctx.w3.eth.send_raw_transaction.assert_not_called()
