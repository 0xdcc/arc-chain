"""1B counterexamples; artificial amounts/receipts, no transport or signing."""

from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.config import load_safe_config
from core.wallet_guard import ExcessiveAmountError, InvalidSlippageError, WalletGuard
from execution.funds import (
    BASES,
    TRANSFER_TOPIC,
    FundsError,
    PriceEvidence,
    output_floor,
    receipt_accounting,
    validate_plan_structure,
    validate_plan_value,
)
from execution.funds_ledger import ExecutionLatched, FundsLedger
from execution.weth_arbitrage_executor import WethArbitrageExecutor

WALLET = "0x" + "11" * 20
ROUTER = "0x" + "22" * 20
TX = "0x" + "33" * 32
BLOCK = "0x" + "44" * 32


def plan(base="USDG", amount=10_000_000, usd=10):
    token, decimals = BASES[base]
    other = BASES["WETH" if base == "USDG" else "USDG"][0]
    return SimpleNamespace(
        base_symbol=base,
        base_token=token,
        decimals=decimals,
        amount_in_weth=amount,
        expected_amount_out=amount * 2,
        amount_out_min=amount,
        amount_usd=usd,
        slippage_pct=1,
        legs=[
            SimpleNamespace(from_token=token, to_token=other),
            SimpleNamespace(from_token=other, to_token=token),
        ],
    )


def price(base="USDG", value="1"):
    return PriceEvidence(BASES[base][0], D(value), 10, BLOCK, 100, "fixture-reader")


def transfer(sender, recipient, amount, index):
    return {
        "address": BASES["USDG"][0],
        "transactionHash": TX,
        "blockHash": BLOCK,
        "logIndex": index,
        "removed": False,
        "topics": [TRANSFER_TOPIC, "0x" + "0" * 24 + sender[2:], "0x" + "0" * 24 + recipient[2:]],
        "data": "0x" + f"{amount:064x}",
    }


def receipt(status=1):
    return {
        "transactionHash": TX,
        "blockHash": BLOCK,
        "blockNumber": 10,
        "status": status,
        "gasUsed": 100_000,
        "effectiveGasPrice": 10**9,
        "gasUsedForL1": 25_000,
        "logs": [transfer(WALLET, ROUTER, 10_000_000, 0), transfer(ROUTER, WALLET, 10_100_000, 1)]
        if status
        else [],
    }


def account(data):
    return receipt_accounting(
        data,
        tx_hash=TX,
        wallet=WALLET,
        base_symbol="USDG",
        counterparties={ROUTER},
        base_price=D(1),
        native_price=D(2500),
        fee_model="arbitrum_nitro_total",
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), True])
def test_guard_rejects_nonfinite_and_boolean(value):
    with pytest.raises(ValueError):
        WalletGuard().validate_amount(value)
    with pytest.raises(InvalidSlippageError):
        WalletGuard().validate_slippage(value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("decimals", 18),
        ("base_symbol", "ETH"),
        ("amount_in_weth", True),
        ("amount_usd", float("nan")),
        ("base_token", BASES["WETH"][0]),
        ("amount_out_min", 0),
        ("amount_in_weth", 1.5),
    ],
)
def test_invalid_plan_rejected(field, value):
    candidate = plan()
    setattr(candidate, field, value)
    with pytest.raises(FundsError):
        validate_plan_structure(candidate)


def test_disconnected_middle_leg_rejected():
    candidate = plan()
    candidate.legs[1].from_token = ROUTER
    with pytest.raises(FundsError, match="Disconnected"):
        validate_plan_structure(candidate)


def test_atomic_amount_beats_forged_nominal():
    with pytest.raises(ExcessiveAmountError):
        validate_plan_value(plan("WETH", 10**18, 10), price("WETH", "2500"), 101, 5)
    with pytest.raises(FundsError, match="Nominal"):
        validate_plan_value(plan(usd=1), price(), 101, 5)
    assert validate_plan_value(plan(), price(), 101, 5) == D(10)
    with pytest.raises(FundsError, match="Stale"):
        validate_plan_value(plan(), price(), 106, 5)


def test_principal_floor_includes_gas_without_double_dex_fee():
    assert output_floor(100_000_000, 101_000_000, 99_000_000, 6, D(1), D(".25")) == 100_250_001
    assert output_floor(10_000_000, 10_100_000, 8_000_000, 6, D(1), D(".25"), D(1)) == 9_250_000
    with pytest.raises(FundsError):
        output_floor(100_000_000, 100_100_000, 99_000_000, 6, D(1), D(".25"))
    with pytest.raises(FundsError):
        output_floor(10_000_000, 10_100_000, 8_000_000, 6, D(1), D(".25"), D(".20"))


def test_actual_net_loss_and_revert_gas_are_not_success():
    result = account(receipt())
    assert result["token_delta_usd"] == D(".10")
    assert result["actual_gas_usd"] == D(".25")
    assert result["net_profit_usd"] == D("-.15")
    assert account(receipt(0))["net_profit_usd"] == D("-.25")


@pytest.mark.parametrize("mutation", ["missing_gas", "external", "duplicate", "removed", "l1fee"])
def test_incomplete_or_unattributed_receipt_cannot_claim_profit(mutation):
    data = receipt()
    if mutation == "missing_gas":
        del data["effectiveGasPrice"]
    elif mutation == "external":
        data["logs"].append(transfer("0x" + "55" * 20, WALLET, 10**6, 2))
    elif mutation == "duplicate":
        data["logs"].append(data["logs"][0])
    elif mutation == "removed":
        data["logs"][0]["removed"] = True
    else:
        data["l1Fee"] = 1
    with pytest.raises(FundsError):
        account(data)


def reserve(ledger, intent="first", base="USDG", nonce=1, loss=D(1), one_shot=False):
    ledger.reserve(
        intent_id=intent,
        chain=4663,
        wallet=WALLET,
        base=base,
        nonce=nonce,
        payload={"to": ROUTER, "data": "0x1234"},
        worst_loss_usd=loss,
        running=True,
        auto_execute=True,
        one_shot=one_shot,
    )


def reconcile(ledger, net=D("-.6")):
    ledger.mark_signed("first", TX)
    ledger.reconcile(
        "first",
        tx_hash=TX,
        block_hash=BLOCK,
        net_profit_usd=net,
        actual_gas_usd=D(".25"),
        receipt_status=1,
    )


def test_pending_survives_restart_and_blocks_other_base(tmp_path):
    ledger = FundsLedger(tmp_path / "ledger.sqlite")
    reserve(ledger)
    ledger.mark_signed("first", TX)
    ledger.mark_unknown("first")
    restarted = FundsLedger(tmp_path / "ledger.sqlite")
    with pytest.raises(ExecutionLatched):
        reserve(restarted, "second", "WETH", 2)
    with pytest.raises(ExecutionLatched):
        restarted.cancel_before_signing("first")
    assert restarted.status(4663, WALLET, "WETH")["pending"] == "first"


def test_loss_is_wallet_global_durable_and_duplicate_receipt_idempotent(tmp_path):
    ledger = FundsLedger(tmp_path / "ledger.sqlite")
    reserve(ledger)
    reconcile(ledger)
    ledger.reconcile(
        "first",
        tx_hash=TX,
        block_hash=BLOCK,
        net_profit_usd=D("-.6"),
        actual_gas_usd=D(".25"),
        receipt_status=1,
    )
    restarted = FundsLedger(tmp_path / "ledger.sqlite")
    status = restarted.status(4663, WALLET, "WETH")
    assert status["remaining_usd"] == D(".4")
    assert status["paused"] is True
    with pytest.raises(ExecutionLatched):
        reserve(restarted, "second", "WETH", 2, D(".5"))


def test_positive_receipt_needs_explicit_upgrade_for_only_its_base(tmp_path):
    ledger = FundsLedger(tmp_path / "ledger.sqlite")
    reserve(ledger)
    reconcile(ledger, D(".1"))
    with pytest.raises(ExecutionLatched):
        reserve(ledger, "second", nonce=2)
    ledger.promote(4663, WALLET, "USDG")
    assert ledger.status(4663, WALLET, "USDG")["mode"] == "NORMAL"
    assert ledger.status(4663, WALLET, "WETH")["mode"] == "PROBE"
    reserve(ledger, "second", nonce=2)


def test_one_shot_profit_still_latches_and_presign_failure_can_cancel(tmp_path):
    ledger = FundsLedger(tmp_path / "ledger.sqlite")
    reserve(ledger)
    ledger.cancel_before_signing("first")
    reserve(ledger, one_shot=True)
    reconcile(ledger, D(1))
    with pytest.raises(ExecutionLatched):
        ledger.promote(4663, WALLET, "USDG")


@pytest.mark.parametrize(
    "simulate,fail,status",
    [
        (True, True, "SIMULATION_FAILED"),
        (False, False, "NOT_SIMULATED"),
        (True, False, "DRY_RUN_SUCCESS"),
    ],
)
def test_dry_run_status_reflects_actual_simulation(simulate, fail, status):
    executor = WethArbitrageExecutor(config=load_safe_config(_env_file=None), w3=MagicMock())
    candidate = executor.plan_two_hop(
        token_b="USDG", pool1_fee=1, pool2_fee=1, amount_usd=10, expected_multiplier=1.01
    )
    executor.preflight_check = MagicMock(
        return_value={"base_balance": 10**18, "has_allowance": True}
    )
    executor.build_swap_tx = MagicMock(
        return_value={
            "tx": {"to": ROUTER, "data": "0x1234"},
            "calldata_details": {"calldata": "0x1234", "selector": "0x1234"},
        }
    )
    executor.simulate_swap = MagicMock(
        side_effect=ValueError("fixture revert") if fail else None, return_value={"success": True}
    )
    result = executor.execute(candidate, wallet_address=WALLET, dry_run=True, simulate=simulate)
    assert result["status"] == status
    assert result["mev_protected"] is False


@pytest.mark.parametrize("kind", ["spread", "triangle"])
@pytest.mark.parametrize("running,enabled", [(False, True), (True, False)])
def test_queued_or_monitor_only_entry_never_reaches_executor(kind, running, enabled):
    from monitors.daemons.arbitrage_daemon import ArbitrageDaemon

    daemon = ArbitrageDaemon(auto_execute=False)
    daemon.executor = MagicMock()
    daemon.running = running
    daemon.auto_execute = enabled
    getattr(daemon, "_try_auto_snipe_" + kind)(MagicMock())
    daemon.executor.execute.assert_not_called()


def test_budget_is_reserved_before_any_broadcast_and_visible_across_bases(tmp_path):
    ledger = FundsLedger(tmp_path / "ledger.sqlite")
    reserve(ledger, loss=D(".6"))
    other_base = FundsLedger(tmp_path / "ledger.sqlite").status(4663, WALLET, "WETH")
    assert other_base["spent_usd"] == 0
    assert other_base["reserved_usd"] == D(".6")
    assert other_base["remaining_usd"] == D(".4")
    # Already RESERVED, before mark_signed or any network dispatch.
    with pytest.raises(ExecutionLatched):
        reserve(ledger, "second", "WETH", 2, D(".5"))


def test_idempotence_compares_decimal_value_and_receipt_status(tmp_path):
    ledger = FundsLedger(tmp_path / "ledger.sqlite")
    reserve(ledger)
    reconcile(ledger, D("-.25"))
    ledger.reconcile(
        "first",
        tx_hash=TX,
        block_hash=BLOCK,
        net_profit_usd=D("-.2500"),
        actual_gas_usd=D(".2500"),
        receipt_status=1,
    )
    assert ledger.status(4663, WALLET, "USDG")["spent_usd"] == D(".25")
    with pytest.raises(ExecutionLatched, match="Conflicting"):
        ledger.reconcile(
            "first",
            tx_hash=TX,
            block_hash=BLOCK,
            net_profit_usd=D("-.25"),
            actual_gas_usd=D(".25"),
            receipt_status=0,
        )


def test_normal_mode_does_not_get_an_extra_wallet_loss_budget(tmp_path):
    ledger = FundsLedger(tmp_path / "ledger.sqlite")
    reserve(ledger)
    reconcile(ledger, D(".1"))
    ledger.promote(4663, WALLET, "USDG")
    assert ledger.status(4663, WALLET, "USDG")["mode"] == "NORMAL"
    with pytest.raises(ExecutionLatched, match="Wallet-global loss budget"):
        reserve(ledger, "normal", "USDG", 2, D("1.01"))
    assert ledger.status(4663, WALLET, "WETH")["remaining_usd"] == D(1)
