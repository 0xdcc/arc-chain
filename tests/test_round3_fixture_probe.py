"""Falsifiable round3 fixture probes; run only through the existing OS namespace."""

import json
import time
from decimal import ROUND_CEILING, Decimal

from arbitrage.spread_monitor import SpreadAlert

from tests.receipt_fixture import make_receipt_fixture


def test_fixture_preserves_wall_clock_and_default_factory(tmp_path, monkeypatch):
    """A fixture must not split the wall clock from a captured dataclass factory."""
    wall_clock = time.time
    factory = SpreadAlert.__dataclass_fields__["ts"].default_factory
    ctx = make_receipt_fixture(tmp_path, monkeypatch)
    alert_ts = factory()
    now = time.time()
    block_ts = int(ctx.block["timestamp"], 16)
    print(
        "ROUND3_CLOCK "
        + json.dumps(
            {
                "clock_identity_preserved": time.time is wall_clock,
                "now": now,
                "factory_ts": alert_ts,
                "quote_age": now - alert_ts,
                "block_ts": block_ts,
                "block_age": now - block_ts,
                "max_age": ctx.runtime.max_age,
            },
            sort_keys=True,
        )
    )
    assert time.time is wall_clock
    assert 0 <= now - alert_ts <= ctx.runtime.max_age
    assert 0 <= now - block_ts <= ctx.runtime.max_age
    ctx.w3.eth.account.sign_transaction.assert_not_called()
    ctx.w3.eth.send_raw_transaction.assert_not_called()


def test_loss_fixture_reaches_real_admission_and_reserves_budget(tmp_path, monkeypatch):
    """A valid loss quote must reach the unchanged floor and shared ledger checks."""
    ctx = make_receipt_fixture(tmp_path, monkeypatch, gain=-400_000, gas_used=40_000)
    plan = ctx.plan
    print(
        "ROUND3_MINOUT "
        + json.dumps(
            {
                "amount_in": plan.amount_in_weth,
                "expected_out": plan.expected_amount_out,
                "initial_min_out": plan.amount_out_min,
                "min_minus_quote": plan.amount_out_min - plan.expected_amount_out,
            },
            sort_keys=True,
        )
    )
    assert 0 < plan.amount_out_min <= plan.expected_amount_out
    before = ctx.ledger.status(4663, ctx.wallet, "USDG")
    admission = ctx.runtime.prepare(ctx.executor, plan, ctx.wallet)
    required_floor = plan.amount_in_weth + int(
        (
            (admission.gas_usd_bound - before["remaining_usd"]) * 10**plan.decimals / ctx.base_price
        ).to_integral_value(rounding=ROUND_CEILING)
    )
    minimum = admission.plan.amount_out_min
    token_loss = Decimal(plan.amount_in_weth - minimum) / 10**plan.decimals * ctx.base_price
    state = ctx.ledger.status(4663, ctx.wallet, "USDG")
    other = ctx.ledger.status(4663, ctx.wallet, "WETH")
    print(
        "ROUND3_RESERVATION "
        + json.dumps(
            {
                "admitted_min_out": minimum,
                "budget_floor": required_floor,
                "gas_bound_usd": str(admission.gas_usd_bound),
                "reserved_usd": str(state["reserved_usd"]),
                "other_base_reserved_usd": str(other["reserved_usd"]),
            },
            sort_keys=True,
        )
    )
    assert required_floor <= minimum <= plan.expected_amount_out
    assert admission.loss_usd_bound == max(
        admission.gas_usd_bound, token_loss + admission.gas_usd_bound
    )
    assert 0 < state["reserved_usd"] == admission.loss_usd_bound <= before["remaining_usd"] <= 1
    assert state["pending"] and other["pending"]
    assert other["reserved_usd"] == state["reserved_usd"]
    assert state["spent_usd"] == other["spent_usd"] == 0
    ctx.w3.eth.account.sign_transaction.assert_not_called()
    ctx.w3.eth.send_raw_transaction.assert_not_called()
