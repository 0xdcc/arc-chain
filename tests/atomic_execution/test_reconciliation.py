"""Comprehensive unit and regression test suite for W5-F pure reconciliation and four-state verdict.

Verifies acceptance criteria C18 ~ C19:
- C18: Receipt & Four-State Verdict Core
  - VERIFIED: status=1, 100% hash & nonce match, logs prove net base token increase, counterparties whitelisted,
    exact gas cost deducted to determine net profit;
  - REVERTED: status=0, exact gas loss accounted for, no zero-gas spoofing, anomalous logs intercepted;
  - UNKNOWN: missing receipt, timeout, unfinalized/pending block, insufficient confirmations, missing logs,
    or missing price evidence; yields persistent latch decision (LATCH_REQUIRED) to prohibit retry/concurrency.
- C19: Fine-grained Transfer Log Auditing & Fraud Defense
  - Strict prohibition of crude balance diff (balance_after - balance_before);
  - FAKE_EVENT_REJECTED: forged hash, nonce mismatch, dry-run/test masquerade, non-target token transfers,
    external subsidy/donation, unauthorized debit, malformed log topics, or non-positive token delta.
  - Whitelisting of Universal Router & Permit2 counterparties.
"""

from __future__ import annotations

import ast
import json
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from arbitrage_contracts.identity import (
    Amount,
    AssetRef,
    FeeModel,
    PoolDescriptor,
    PoolKey,
    TokenKey,
)
from arbitrage_contracts.quote import HopRef, RouteRef
from atomic_execution.encoding import EncodedCalldata
from atomic_execution.inputs import (
    ROBINHOOD_CHAIN_ID,
    USDG_ADDRESS_4663,
    WETH_ADDRESS_4663,
    ZERO_ADDRESS,
)
from atomic_execution.planning import CANONICAL_UNIVERSAL_ROUTER, ExecutionPlan
from atomic_execution.policy import ExecutionPolicy
from atomic_execution.reconciliation import (
    CANONICAL_PERMIT2,
    DEFAULT_COUNTERPARTIES,
    TRANSFER_EVENT_TOPIC,
    ExecutionReconciler,
    LatchDecision,
    PureReconciler,
    ReconciliationAssessment,
    ReconciliationResult,
    ReconciliationStatus,
    reconcile,
    reconcile_execution,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "atomic_execution" / "v1"
RECEIPT_CASES_PATH = FIXTURES_DIR / "receipt-cases.jsonl"

TEST_WALLET = "0x39dBED3a2bd333467115dE45665cC57F813C4571"
TEST_ROUTER = CANONICAL_UNIVERSAL_ROUTER
TEST_PERMIT2 = CANONICAL_PERMIT2
TEST_TX_HASH = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TEST_BLOCK_HASH = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
TEST_BLOCK_NUMBER = 59255390


def _make_topic(address: str) -> str:
    clean = address.lower().replace("0x", "")
    return "0x" + "0" * 24 + clean


def _make_transfer_log(
    token: str,
    sender: str,
    recipient: str,
    amount: int,
    log_index: int = 0,
) -> dict[str, Any]:
    return {
        "address": token,
        "topics": [
            TRANSFER_EVENT_TOPIC,
            _make_topic(sender),
            _make_topic(recipient),
        ],
        "data": hex(amount),
        "logIndex": log_index,
        "transactionHash": TEST_TX_HASH,
        "blockNumber": TEST_BLOCK_NUMBER,
        "blockHash": TEST_BLOCK_HASH,
        "removed": False,
    }


def _make_asset(address: str) -> AssetRef:
    tk = TokenKey(chain_id=ROBINHOOD_CHAIN_ID, address=address)
    return AssetRef(interface_kind="erc20", chain_id=ROBINHOOD_CHAIN_ID, token_key=tk)


def _make_v3_hop(asset_in: AssetRef, asset_out: AssetRef, pool_id: str) -> HopRef:
    assert asset_in.token_key is not None
    assert asset_out.token_key is not None
    c0 = (
        asset_in
        if int(asset_in.token_key.address, 16) < int(asset_out.token_key.address, 16)
        else asset_out
    )
    c1 = asset_out if c0 == asset_in else asset_in
    direction = "zero_for_one" if c0 == asset_in else "one_for_zero"
    pk = PoolKey(
        chain_id=ROBINHOOD_CHAIN_ID,
        protocol_id="uniswap_v3",
        pool_id=pool_id,
        pool_id_kind="address",
        venue_address="0x1f7d7550b1b028f7571e69a784071f0205fd2efa",
        venue_kind="factory",
    )
    desc = PoolDescriptor(
        key=pk,
        currency0=c0,
        currency1=c1,
        fee_model=FeeModel.static(500),
        hooks=ZERO_ADDRESS,
    )
    return HopRef(
        pool_key=pk,
        asset_in=asset_in,
        asset_out=asset_out,
        direction=direction,
        pool_descriptor=desc,
    )


def _make_test_plan(
    base: str = "WETH",
    amount_in_atoms: int = 10**16,
    expected_out_atoms: int = int(10**16 + 5 * 10**14),
    min_amount_out_atoms: int = int(10**16 + 10**14),
    gas_usd: Decimal = Decimal("0.50"),
) -> ExecutionPlan:
    is_weth = base.upper() == "WETH"
    decimals = 18 if is_weth else 6
    base_addr = WETH_ADDRESS_4663 if is_weth else USDG_ADDRESS_4663
    alt_addr = USDG_ADDRESS_4663 if is_weth else WETH_ADDRESS_4663

    base_asset = _make_asset(base_addr)
    alt_asset = _make_asset(alt_addr)

    hop1 = _make_v3_hop(base_asset, alt_asset, "0x" + "33" * 20)
    hop2 = _make_v3_hop(alt_asset, base_asset, "0x" + "44" * 20)

    route_ref = RouteRef(
        chain_id=ROBINHOOD_CHAIN_ID,
        hops=(hop1, hop2),
        base_asset=base_asset,
    )

    amount_in = Amount(asset_ref=base_asset, atoms=amount_in_atoms, decimals=decimals)
    expected_out = Amount(asset_ref=base_asset, atoms=expected_out_atoms, decimals=decimals)
    min_amount_out = Amount(asset_ref=base_asset, atoms=min_amount_out_atoms, decimals=decimals)
    output_floor = Amount(asset_ref=base_asset, atoms=amount_in_atoms + 1, decimals=decimals)
    policy = ExecutionPolicy()

    base_price = Decimal("2500.0") if is_weth else Decimal("1.00")
    trade_usd = (Decimal(amount_in_atoms) / Decimal(10**decimals)) * base_price
    net_atoms = expected_out_atoms - amount_in_atoms
    net_usd = (Decimal(net_atoms) / Decimal(10**decimals)) * base_price - gas_usd

    return ExecutionPlan(
        plan_id=f"plan_recon_{base.lower()}",
        route_ref=route_ref,
        base_asset=base_asset,
        amount_in=amount_in,
        expected_out=expected_out,
        min_amount_out=min_amount_out,
        output_floor=output_floor,
        policy=policy,
        quoter_block=TEST_BLOCK_NUMBER,
        deadline=1800000000,
        target_router=TEST_ROUTER,
        estimated_gas_usd=gas_usd,
        gas_atoms=50000,
        net_atoms=net_atoms,
        net_profit_usd=net_usd,
        trade_amount_usd=trade_usd,
        base_asset_usd_price=base_price,
    )


# ==============================================================================
# 1. Test Fixtures Suite & Replay
# ==============================================================================


def test_fixtures_receipt_cases_file_integrity() -> None:
    """Validate that receipt-cases.jsonl exists and contains valid JSON records."""
    assert RECEIPT_CASES_PATH.exists(), f"Missing fixture file: {RECEIPT_CASES_PATH}"
    lines = [
        line.strip()
        for line in RECEIPT_CASES_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) >= 30, f"Expected >= 30 fixture cases, got {len(lines)}"

    case_ids: set[str] = set()
    for idx, line in enumerate(lines, start=1):
        data = json.loads(line)
        assert "case_id" in data, f"Line {idx} missing case_id"
        assert "category" in data, f"Line {idx} missing category"
        assert data["category"] in ("C18", "C19"), (
            f"Line {idx} invalid category: {data['category']}"
        )
        assert "expected_status" in data, f"Line {idx} missing expected_status"
        assert data["expected_status"] in (
            "VERIFIED",
            "REVERTED",
            "UNKNOWN",
            "FAKE_EVENT_REJECTED",
        ), f"Line {idx} unrecognized expected_status"
        assert "expected_latch" in data, f"Line {idx} missing expected_latch"
        assert data["expected_latch"] in ("LATCH_REQUIRED", "NONE"), f"Line {idx} invalid latch"

        cid = data["case_id"]
        assert cid not in case_ids, f"Duplicate case_id: {cid}"
        case_ids.add(cid)


def test_fixtures_receipt_cases_execution() -> None:
    """Replay all cases from receipt-cases.jsonl through pure reconcile_execution."""
    lines = [
        line.strip()
        for line in RECEIPT_CASES_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    for line in lines:
        case = json.loads(line)
        cid = case["case_id"]
        receipt = case.get("receipt")
        params = case.get("params", {})

        verdict = reconcile_execution(
            receipt=receipt,
            expected_tx_hash=params.get("expected_tx_hash"),
            expected_nonce=params.get("expected_nonce"),
            wallet_address=params.get("wallet_address"),
            base_asset_address=params.get("base_asset_address"),
            base_decimals=params.get("base_decimals"),
            base_asset_usd_price=params.get("base_asset_usd_price"),
            native_token_usd_price=params.get("native_token_usd_price"),
            current_block_number=params.get("current_block_number"),
            min_confirmations=params.get("min_confirmations", 1),
            timeout=params.get("timeout", False),
            is_dry_run_or_test=params.get("is_dry_run_or_test", False),
            event=params.get("event"),
        )

        assert verdict.status == case["expected_status"], (
            f"Case {cid} expected status {case['expected_status']}, got {verdict.status}: {verdict.reason}"
        )
        assert verdict.latch_decision == case["expected_latch"], (
            f"Case {cid} expected latch {case['expected_latch']}, got {verdict.latch_decision}"
        )

        assert verdict.is_verified == (case["expected_status"] == "VERIFIED")
        assert verdict.is_reverted == (case["expected_status"] == "REVERTED")
        assert verdict.is_unknown == (case["expected_status"] == "UNKNOWN")
        assert verdict.is_rejected == (case["expected_status"] == "FAKE_EVENT_REJECTED")
        assert verdict.is_latch_required == (case["expected_latch"] == "LATCH_REQUIRED")


# ==============================================================================
# 2. C18: Receipt & Four-State Verdict Core Tests
# ==============================================================================


def test_c18_verified_success_full_financial_accounting() -> None:
    """Status=1 receipt with genuine transfer logs yields VERIFIED with accurate gas & net profit."""
    amount_in = 10**16  # 0.01 WETH
    amount_out = int(10**16 + 5 * 10**14)  # 0.0105 WETH (+0.0005 WETH profit)
    token_delta = amount_out - amount_in  # 5 * 10**14 atoms

    logs = [
        _make_transfer_log(WETH_ADDRESS_4663, TEST_WALLET, TEST_ROUTER, amount_in, 0),
        _make_transfer_log(WETH_ADDRESS_4663, TEST_ROUTER, TEST_WALLET, amount_out, 1),
    ]
    receipt = {
        "transactionHash": TEST_TX_HASH,
        "nonce": 42,
        "blockNumber": TEST_BLOCK_NUMBER,
        "blockHash": TEST_BLOCK_HASH,
        "status": 1,
        "gasUsed": 80000,
        "effectiveGasPrice": 10 * 10**9,  # 10 Gwei
        "logs": logs,
    }

    verdict = reconcile_execution(
        receipt=receipt,
        expected_tx_hash=TEST_TX_HASH,
        expected_nonce=42,
        wallet_address=TEST_WALLET,
        base_asset_address=WETH_ADDRESS_4663,
        base_decimals=18,
        base_asset_usd_price=Decimal("2500"),
        native_token_usd_price=Decimal("2500"),
    )

    assert verdict.status == ReconciliationStatus.VERIFIED
    assert verdict.is_verified is True
    assert verdict.latch_decision == LatchDecision.NONE
    assert verdict.token_delta_atoms == token_delta

    # Token delta USD: 0.0005 * 2500 = $1.25 USD
    # Gas native: 80,000 * 10e9 / 1e18 = 0.0008 ETH
    # Gas USD: 0.0008 * 2500 = $2.00 USD
    # Net profit: $1.25 - $2.00 = -$0.75 USD
    assert verdict.token_delta_usd == Decimal("1.25")
    assert verdict.gas_used == 80000
    assert verdict.actual_gas_cost_native == Decimal("0.0008")
    assert verdict.actual_gas_cost_usd == Decimal("2.00")
    assert verdict.net_profit_usd == Decimal("-0.75")
    assert verdict.transfers_count == 2
    assert verdict.details is not None
    assert verdict.details["token_delta"] == token_delta


def test_c18_verified_usdg_6_decimals_accounting() -> None:
    """USDG 6-decimal cycle accurately scales token_delta_usd without precision distortion."""
    amount_in = 100_000_000  # 100 USDG
    amount_out = 105_000_000  # 105 USDG (+5 USDG profit)

    logs = [
        _make_transfer_log(USDG_ADDRESS_4663, TEST_WALLET, TEST_ROUTER, amount_in, 0),
        _make_transfer_log(USDG_ADDRESS_4663, TEST_ROUTER, TEST_WALLET, amount_out, 1),
    ]
    receipt = {
        "transactionHash": TEST_TX_HASH,
        "nonce": 12,
        "blockNumber": TEST_BLOCK_NUMBER,
        "blockHash": TEST_BLOCK_HASH,
        "status": 1,
        "gasUsed": 60000,
        "effectiveGasPrice": 5 * 10**9,  # 5 Gwei
        "logs": logs,
    }

    verdict = reconcile_execution(
        receipt=receipt,
        expected_tx_hash=TEST_TX_HASH,
        expected_nonce=12,
        wallet_address=TEST_WALLET,
        base_asset_address=USDG_ADDRESS_4663,
        base_decimals=6,
        base_asset_usd_price=Decimal("1.00"),
        native_token_usd_price=Decimal("2500.00"),
    )

    assert verdict.status == ReconciliationStatus.VERIFIED
    assert verdict.token_delta_atoms == 5_000_000
    assert verdict.token_delta_usd == Decimal("5.00")

    # Gas: 60,000 * 5e9 / 1e18 = 0.0003 ETH * $2500 = $0.75 USD
    assert verdict.actual_gas_cost_native == Decimal("0.0003")
    assert verdict.actual_gas_cost_usd == Decimal("0.75")
    assert verdict.net_profit_usd == Decimal("4.25")


def test_c18_revert_status_zero_records_gas_loss() -> None:
    """Status=0 EVM revert accurately accounts for gas loss and sets net profit to -gas_usd."""
    receipt = {
        "transactionHash": TEST_TX_HASH,
        "nonce": 42,
        "blockNumber": TEST_BLOCK_NUMBER,
        "blockHash": TEST_BLOCK_HASH,
        "status": 0,
        "gasUsed": 100000,
        "effectiveGasPrice": 20 * 10**9,  # 20 Gwei
        "logs": [],
    }

    verdict = reconcile_execution(
        receipt=receipt,
        expected_tx_hash=TEST_TX_HASH,
        expected_nonce=42,
        wallet_address=TEST_WALLET,
        native_token_usd_price=Decimal("2500"),
    )

    assert verdict.status == ReconciliationStatus.REVERTED
    assert verdict.is_reverted is True
    assert verdict.latch_decision == LatchDecision.NONE
    assert verdict.token_delta_atoms == 0
    assert verdict.token_delta_usd == Decimal("0")

    # Gas: 100,000 * 20e9 / 1e18 = 0.002 ETH * 2500 = $5.00 USD
    expected_gas_usd = Decimal("5.00")
    assert verdict.actual_gas_cost_usd == expected_gas_usd
    assert verdict.net_profit_usd == -expected_gas_usd
    assert "reverted (status=0)" in verdict.reason


def test_c18_revert_with_zero_gas_fails_to_unknown() -> None:
    """Reverted transaction with 0 gas cannot be accounted for and yields UNKNOWN with latch."""
    receipt = {
        "transactionHash": TEST_TX_HASH,
        "blockNumber": TEST_BLOCK_NUMBER,
        "blockHash": TEST_BLOCK_HASH,
        "status": 0,
        "gasUsed": 0,
        "effectiveGasPrice": 20 * 10**9,
    }
    verdict = reconcile_execution(
        receipt=receipt,
        expected_tx_hash=TEST_TX_HASH,
        wallet_address=TEST_WALLET,
        native_token_usd_price=Decimal("2500"),
    )
    assert verdict.status == ReconciliationStatus.UNKNOWN
    assert verdict.is_latch_required is True


def test_c18_revert_with_anomalous_logs_rejected() -> None:
    """Receipt claiming EVM status=0 while containing Transfer logs is rejected."""
    receipt = {
        "transactionHash": TEST_TX_HASH,
        "blockNumber": TEST_BLOCK_NUMBER,
        "blockHash": TEST_BLOCK_HASH,
        "status": 0,
        "gasUsed": 100000,
        "effectiveGasPrice": 20 * 10**9,
        "logs": [_make_transfer_log(WETH_ADDRESS_4663, TEST_WALLET, TEST_ROUTER, 1000, 0)],
    }
    verdict = reconcile_execution(
        receipt=receipt,
        expected_tx_hash=TEST_TX_HASH,
        wallet_address=TEST_WALLET,
        native_token_usd_price=Decimal("2500"),
    )
    assert verdict.status == ReconciliationStatus.FAKE_EVENT_REJECTED
    assert "anomalous" in verdict.reason


def test_c18_unknown_missing_receipt_none_triggers_latch() -> None:
    """None receipt yields UNKNOWN with LATCH_REQUIRED, prohibiting retry and concurrency."""
    verdict = reconcile_execution(
        receipt=None,
        expected_tx_hash=TEST_TX_HASH,
        expected_nonce=5,
        wallet_address=TEST_WALLET,
    )
    assert verdict.status == ReconciliationStatus.UNKNOWN
    assert verdict.is_unknown is True
    assert verdict.latch_decision == LatchDecision.LATCH_REQUIRED
    assert verdict.is_latch_required is True
    assert verdict.can_retry is False
    assert verdict.can_concurrent is False


def test_c18_unknown_timeout_triggers_latch() -> None:
    """Timeout flag yields UNKNOWN with LATCH_REQUIRED."""
    verdict = reconcile_execution(
        receipt=None,
        timeout=True,
        expected_tx_hash=TEST_TX_HASH,
        wallet_address=TEST_WALLET,
    )
    assert verdict.status == ReconciliationStatus.UNKNOWN
    assert verdict.is_latch_required is True
    assert "timed out" in verdict.reason


def test_c18_unknown_pending_block_triggers_latch() -> None:
    """Receipt belonging to pending block (null blockNumber) yields UNKNOWN with latch."""
    receipt = {
        "transactionHash": TEST_TX_HASH,
        "blockNumber": None,
        "blockHash": None,
        "status": 1,
    }
    verdict = reconcile_execution(
        receipt=receipt,
        expected_tx_hash=TEST_TX_HASH,
        wallet_address=TEST_WALLET,
    )
    assert verdict.status == ReconciliationStatus.UNKNOWN
    assert verdict.is_latch_required is True
    assert "pending" in verdict.reason


def test_c18_unknown_insufficient_confirmations_triggers_latch() -> None:
    """Block confirmations below minimum threshold yields UNKNOWN with latch."""
    receipt = {
        "transactionHash": TEST_TX_HASH,
        "blockNumber": 100,
        "blockHash": TEST_BLOCK_HASH,
        "status": 1,
        "gasUsed": 150000,
        "effectiveGasPrice": 10 * 10**9,
        "logs": [
            _make_transfer_log(WETH_ADDRESS_4663, TEST_WALLET, TEST_ROUTER, 100, 0),
            _make_transfer_log(WETH_ADDRESS_4663, TEST_ROUTER, TEST_WALLET, 200, 1),
        ],
    }
    verdict = reconcile_execution(
        receipt=receipt,
        expected_tx_hash=TEST_TX_HASH,
        wallet_address=TEST_WALLET,
        base_asset_address=WETH_ADDRESS_4663,
        base_decimals=18,
        base_asset_usd_price=Decimal("2500"),
        native_token_usd_price=Decimal("2500"),
        current_block_number=100,
        min_confirmations=3,
    )
    assert verdict.status == ReconciliationStatus.UNKNOWN
    assert verdict.is_latch_required is True
    assert "Insufficient block confirmations" in verdict.reason


def test_c18_unknown_missing_logs_on_status_one_triggers_latch() -> None:
    """Status=1 receipt with missing or empty Transfer logs yields UNKNOWN with latch."""
    receipt = {
        "transactionHash": TEST_TX_HASH,
        "blockNumber": TEST_BLOCK_NUMBER,
        "blockHash": TEST_BLOCK_HASH,
        "status": 1,
        "gasUsed": 150000,
        "effectiveGasPrice": 10 * 10**9,
        "logs": None,
    }
    verdict = reconcile_execution(
        receipt=receipt,
        expected_tx_hash=TEST_TX_HASH,
        wallet_address=TEST_WALLET,
        native_token_usd_price=Decimal("2500"),
    )
    assert verdict.status == ReconciliationStatus.UNKNOWN
    assert verdict.is_latch_required is True


def test_c18_unknown_missing_price_evidence_triggers_latch() -> None:
    """Missing native or base price evidence fails-closed to UNKNOWN with latch."""
    receipt = {
        "transactionHash": TEST_TX_HASH,
        "blockNumber": TEST_BLOCK_NUMBER,
        "blockHash": TEST_BLOCK_HASH,
        "status": 1,
        "gasUsed": 150000,
        "effectiveGasPrice": 10 * 10**9,
        "logs": [
            _make_transfer_log(USDG_ADDRESS_4663, TEST_WALLET, TEST_ROUTER, 100_000_000, 0),
            _make_transfer_log(USDG_ADDRESS_4663, TEST_ROUTER, TEST_WALLET, 102_000_000, 1),
        ],
    }
    verdict = reconcile_execution(
        receipt=receipt,
        expected_tx_hash=TEST_TX_HASH,
        wallet_address=TEST_WALLET,
        base_asset_address=USDG_ADDRESS_4663,
        base_decimals=6,
        base_asset_usd_price=Decimal("1.00"),
        native_token_usd_price=None,  # Intentionally omitted
    )
    assert verdict.status == ReconciliationStatus.UNKNOWN
    assert verdict.is_latch_required is True
    assert "native token USD price" in verdict.reason


# ==============================================================================
# 3. C19: Fine-grained Transfer Log Auditing & Fraud Defense
# ==============================================================================


def test_c19_strict_prohibition_of_crude_balance_diff() -> None:
    """Reconciler signature and implementation strictly prohibit crude balance diff parameters."""
    import inspect

    sig = inspect.signature(reconcile_execution)
    params = sig.parameters
    assert "balance_before" not in params
    assert "balance_after" not in params
    assert "pre_balance" not in params
    assert "post_balance" not in params

    # Also verify PureReconciler.reconcile
    sig_reconciler = inspect.signature(PureReconciler.reconcile)
    assert "balance_before" not in sig_reconciler.parameters
    assert "balance_after" not in sig_reconciler.parameters


def test_c19_fake_dry_run_or_test_event_rejected() -> None:
    """Dry-run or test flags in execution or event payload strictly rejected."""
    receipt = {"transactionHash": TEST_TX_HASH, "status": 1, "logs": []}

    v1 = reconcile_execution(receipt=receipt, is_dry_run_or_test=True)
    assert v1.status == ReconciliationStatus.FAKE_EVENT_REJECTED
    assert v1.is_rejected is True

    v2 = reconcile_execution(receipt=receipt, event={"dry_run": True})
    assert v2.status == ReconciliationStatus.FAKE_EVENT_REJECTED

    v3 = reconcile_execution(
        receipt={"transactionHash": TEST_TX_HASH, "status": 1, "dry_run": True}
    )
    assert v3.status == ReconciliationStatus.FAKE_EVENT_REJECTED


def test_c19_fake_tx_hash_mismatch_rejected() -> None:
    """Receipt transactionHash differing from expected hash rejected."""
    wrong_hash = "0x" + "99" * 32
    receipt = {
        "transactionHash": wrong_hash,
        "status": 1,
        "blockNumber": 100,
        "blockHash": TEST_BLOCK_HASH,
    }
    verdict = reconcile_execution(
        receipt=receipt,
        expected_tx_hash=TEST_TX_HASH,
        wallet_address=TEST_WALLET,
    )
    assert verdict.status == ReconciliationStatus.FAKE_EVENT_REJECTED
    assert "does not match expected" in verdict.reason


def test_c19_fake_nonce_mismatch_rejected() -> None:
    """Receipt nonce differing from expected nonce rejected."""
    receipt = {
        "transactionHash": TEST_TX_HASH,
        "nonce": 999,
        "blockNumber": 100,
        "blockHash": TEST_BLOCK_HASH,
        "status": 1,
    }
    verdict = reconcile_execution(
        receipt=receipt,
        expected_tx_hash=TEST_TX_HASH,
        expected_nonce=42,
        wallet_address=TEST_WALLET,
    )
    assert verdict.status == ReconciliationStatus.FAKE_EVENT_REJECTED
    assert "nonce mismatch" in verdict.reason


def test_c19_fake_non_target_token_touching_wallet_intercepted() -> None:
    """Transfer log on non-target token involving caller wallet is intercepted."""
    foreign_token = "0x" + "77" * 20
    logs = [_make_transfer_log(foreign_token, TEST_WALLET, TEST_ROUTER, 1000, 0)]
    receipt = {
        "transactionHash": TEST_TX_HASH,
        "blockNumber": TEST_BLOCK_NUMBER,
        "blockHash": TEST_BLOCK_HASH,
        "status": 1,
        "gasUsed": 150000,
        "effectiveGasPrice": 10 * 10**9,
        "logs": logs,
    }

    verdict = reconcile_execution(
        receipt=receipt,
        expected_tx_hash=TEST_TX_HASH,
        wallet_address=TEST_WALLET,
        base_asset_address=WETH_ADDRESS_4663,
        base_decimals=18,
        base_asset_usd_price=Decimal("2500"),
        native_token_usd_price=Decimal("2500"),
    )
    assert verdict.status == ReconciliationStatus.FAKE_EVENT_REJECTED
    assert "Non-target token transfer involving wallet intercepted" in verdict.reason


def test_c19_fake_unauthorized_counterparty_debit_intercepted() -> None:
    """Token debit to an unauthorized non-whitelisted address rejected."""
    unauthorized_recipient = "0x" + "88" * 20
    logs = [_make_transfer_log(WETH_ADDRESS_4663, TEST_WALLET, unauthorized_recipient, 1000, 0)]
    receipt = {
        "transactionHash": TEST_TX_HASH,
        "blockNumber": TEST_BLOCK_NUMBER,
        "blockHash": TEST_BLOCK_HASH,
        "status": 1,
        "gasUsed": 150000,
        "effectiveGasPrice": 10 * 10**9,
        "logs": logs,
    }

    verdict = reconcile_execution(
        receipt=receipt,
        expected_tx_hash=TEST_TX_HASH,
        wallet_address=TEST_WALLET,
        base_asset_address=WETH_ADDRESS_4663,
        base_decimals=18,
        base_asset_usd_price=Decimal("2500"),
        native_token_usd_price=Decimal("2500"),
        counterparties={TEST_ROUTER},
    )
    assert verdict.status == ReconciliationStatus.FAKE_EVENT_REJECTED
    assert "Unattributed token debit" in verdict.reason


def test_c19_fake_external_subsidy_injection_intercepted() -> None:
    """External transfer from unauthorized third-party to wallet rejected (subsidy defense)."""
    unauthorized_sender = "0x" + "66" * 20
    logs = [_make_transfer_log(WETH_ADDRESS_4663, unauthorized_sender, TEST_WALLET, 1000, 0)]
    receipt = {
        "transactionHash": TEST_TX_HASH,
        "blockNumber": TEST_BLOCK_NUMBER,
        "blockHash": TEST_BLOCK_HASH,
        "status": 1,
        "gasUsed": 150000,
        "effectiveGasPrice": 10 * 10**9,
        "logs": logs,
    }

    verdict = reconcile_execution(
        receipt=receipt,
        expected_tx_hash=TEST_TX_HASH,
        wallet_address=TEST_WALLET,
        base_asset_address=WETH_ADDRESS_4663,
        base_decimals=18,
        base_asset_usd_price=Decimal("2500"),
        native_token_usd_price=Decimal("2500"),
        counterparties={TEST_ROUTER},
    )
    assert verdict.status == ReconciliationStatus.FAKE_EVENT_REJECTED
    assert "External transfer from unauthorized address" in verdict.reason


def test_c19_fake_malformed_topic_padding_rejected() -> None:
    """Transfer log topic with non-zero high bytes in indexed address rejected."""
    bad_topic = "0x" + "ff" * 12 + TEST_WALLET[2:].lower()
    receipt = {
        "transactionHash": TEST_TX_HASH,
        "blockNumber": TEST_BLOCK_NUMBER,
        "blockHash": TEST_BLOCK_HASH,
        "status": 1,
        "gasUsed": 150000,
        "effectiveGasPrice": 10 * 10**9,
        "logs": [
            {
                "address": WETH_ADDRESS_4663,
                "topics": [TRANSFER_EVENT_TOPIC, bad_topic, _make_topic(TEST_ROUTER)],
                "data": "0x01",
                "logIndex": 0,
            }
        ],
    }

    verdict = reconcile_execution(
        receipt=receipt,
        expected_tx_hash=TEST_TX_HASH,
        wallet_address=TEST_WALLET,
        base_asset_address=WETH_ADDRESS_4663,
        base_decimals=18,
        base_asset_usd_price=Decimal("2500"),
        native_token_usd_price=Decimal("2500"),
    )
    assert verdict.status == ReconciliationStatus.FAKE_EVENT_REJECTED
    assert "Malformed indexed address padding" in verdict.reason


def test_c19_fake_duplicate_log_index_rejected() -> None:
    """Duplicate logIndex within the same receipt strictly rejected."""
    logs = [
        _make_transfer_log(WETH_ADDRESS_4663, TEST_WALLET, TEST_ROUTER, 100, 3),
        _make_transfer_log(WETH_ADDRESS_4663, TEST_ROUTER, TEST_WALLET, 110, 3),
    ]
    receipt = {
        "transactionHash": TEST_TX_HASH,
        "blockNumber": TEST_BLOCK_NUMBER,
        "blockHash": TEST_BLOCK_HASH,
        "status": 1,
        "gasUsed": 150000,
        "effectiveGasPrice": 10 * 10**9,
        "logs": logs,
    }

    verdict = reconcile_execution(
        receipt=receipt,
        expected_tx_hash=TEST_TX_HASH,
        wallet_address=TEST_WALLET,
        base_asset_address=WETH_ADDRESS_4663,
        base_decimals=18,
        base_asset_usd_price=Decimal("2500"),
        native_token_usd_price=Decimal("2500"),
    )
    assert verdict.status == ReconciliationStatus.FAKE_EVENT_REJECTED
    assert "Duplicate logIndex" in verdict.reason


def test_c19_fake_removed_log_rejected() -> None:
    """Transfer log marked removed due to chain reorg rejected."""
    log = _make_transfer_log(WETH_ADDRESS_4663, TEST_WALLET, TEST_ROUTER, 100, 0)
    log["removed"] = True
    receipt = {
        "transactionHash": TEST_TX_HASH,
        "blockNumber": TEST_BLOCK_NUMBER,
        "blockHash": TEST_BLOCK_HASH,
        "status": 1,
        "gasUsed": 150000,
        "effectiveGasPrice": 10 * 10**9,
        "logs": [log],
    }

    verdict = reconcile_execution(
        receipt=receipt,
        expected_tx_hash=TEST_TX_HASH,
        wallet_address=TEST_WALLET,
        base_asset_address=WETH_ADDRESS_4663,
        base_decimals=18,
        base_asset_usd_price=Decimal("2500"),
        native_token_usd_price=Decimal("2500"),
    )
    assert verdict.status == ReconciliationStatus.FAKE_EVENT_REJECTED
    assert "removed" in verdict.reason


def test_c19_fake_negative_or_zero_token_delta_rejected() -> None:
    """Debit exceeding or equal to credit on base token strictly rejected."""
    logs = [
        _make_transfer_log(WETH_ADDRESS_4663, TEST_WALLET, TEST_ROUTER, 10**16, 0),
        _make_transfer_log(WETH_ADDRESS_4663, TEST_ROUTER, TEST_WALLET, 10**16 - 100, 1),
    ]
    receipt = {
        "transactionHash": TEST_TX_HASH,
        "blockNumber": TEST_BLOCK_NUMBER,
        "blockHash": TEST_BLOCK_HASH,
        "status": 1,
        "gasUsed": 150000,
        "effectiveGasPrice": 10 * 10**9,
        "logs": logs,
    }

    verdict = reconcile_execution(
        receipt=receipt,
        expected_tx_hash=TEST_TX_HASH,
        wallet_address=TEST_WALLET,
        base_asset_address=WETH_ADDRESS_4663,
        base_decimals=18,
        base_asset_usd_price=Decimal("2500"),
        native_token_usd_price=Decimal("2500"),
    )
    assert verdict.status == ReconciliationStatus.FAKE_EVENT_REJECTED
    assert "token_delta=" in verdict.reason


def test_c19_fake_excessive_debit_beyond_plan_rejected() -> None:
    """Actual token debit exceeding planned amount_in rejected as fraudulent."""
    plan = _make_test_plan(base="WETH", amount_in_atoms=10**16)
    logs = [
        _make_transfer_log(WETH_ADDRESS_4663, TEST_WALLET, TEST_ROUTER, int(10**16 * 2), 0),
        _make_transfer_log(WETH_ADDRESS_4663, TEST_ROUTER, TEST_WALLET, int(10**16 * 2 + 1000), 1),
    ]
    receipt = {
        "transactionHash": TEST_TX_HASH,
        "blockNumber": TEST_BLOCK_NUMBER,
        "blockHash": TEST_BLOCK_HASH,
        "status": 1,
        "gasUsed": 150000,
        "effectiveGasPrice": 10 * 10**9,
        "logs": logs,
    }

    verdict = reconcile_execution(
        receipt=receipt,
        plan=plan,
        expected_tx_hash=TEST_TX_HASH,
        wallet_address=TEST_WALLET,
    )
    assert verdict.status == ReconciliationStatus.FAKE_EVENT_REJECTED
    assert "exceeds planned amount_in" in verdict.reason


def test_c19_whitelisted_permit2_and_router_accepted() -> None:
    """Permit2 counterparty for debit and Universal Router for credit verified successfully."""
    logs = [
        _make_transfer_log(WETH_ADDRESS_4663, TEST_WALLET, TEST_PERMIT2, 10**16, 0),
        _make_transfer_log(
            WETH_ADDRESS_4663, TEST_ROUTER, TEST_WALLET, int(10**16 + 5 * 10**14), 1
        ),
    ]
    receipt = {
        "transactionHash": TEST_TX_HASH,
        "nonce": 10,
        "blockNumber": TEST_BLOCK_NUMBER,
        "blockHash": TEST_BLOCK_HASH,
        "status": 1,
        "gasUsed": 150000,
        "effectiveGasPrice": 10 * 10**9,
        "logs": logs,
    }

    verdict = reconcile_execution(
        receipt=receipt,
        expected_tx_hash=TEST_TX_HASH,
        expected_nonce=10,
        wallet_address=TEST_WALLET,
        base_asset_address=WETH_ADDRESS_4663,
        base_decimals=18,
        base_asset_usd_price=Decimal("2500"),
        native_token_usd_price=Decimal("2500"),
    )
    assert verdict.status == ReconciliationStatus.VERIFIED
    assert verdict.token_delta_atoms == 5 * 10**14


def test_c19_non_transfer_event_logs_filtered() -> None:
    """Receipt containing non-transfer events alongside Transfer logs safely verified."""
    uniswap_swap_topic = "0xc42079f94a6350d7e6235f29174924f9d5fb2017966537c4742978a6750e2156"
    logs = [
        {
            "address": "0x3333333333333333333333333333333333333333",
            "topics": [uniswap_swap_topic],
            "data": "0x00",
            "logIndex": 0,
        },
        _make_transfer_log(WETH_ADDRESS_4663, TEST_WALLET, TEST_ROUTER, 10**16, 1),
        _make_transfer_log(
            WETH_ADDRESS_4663, TEST_ROUTER, TEST_WALLET, int(10**16 + 5 * 10**14), 2
        ),
    ]
    receipt = {
        "transactionHash": TEST_TX_HASH,
        "nonce": 11,
        "blockNumber": TEST_BLOCK_NUMBER,
        "blockHash": TEST_BLOCK_HASH,
        "status": 1,
        "gasUsed": 150000,
        "effectiveGasPrice": 10 * 10**9,
        "logs": logs,
    }

    verdict = reconcile_execution(
        receipt=receipt,
        expected_tx_hash=TEST_TX_HASH,
        expected_nonce=11,
        wallet_address=TEST_WALLET,
        base_asset_address=WETH_ADDRESS_4663,
        base_decimals=18,
        base_asset_usd_price=Decimal("2500"),
        native_token_usd_price=Decimal("2500"),
    )
    assert verdict.status == ReconciliationStatus.VERIFIED
    assert verdict.transfers_count == 2


# ==============================================================================
# 4. Integration, Interfaces, and Model Safety Tests
# ==============================================================================


def test_execution_plan_direct_integration() -> None:
    """Reconciliation directly consuming an ExecutionPlan cleanly extracts plan properties."""
    plan = _make_test_plan(base="WETH")
    amount_in = plan.amount_in.atoms
    amount_out = plan.expected_out.atoms

    logs = [
        _make_transfer_log(WETH_ADDRESS_4663, TEST_WALLET, TEST_ROUTER, amount_in, 0),
        _make_transfer_log(WETH_ADDRESS_4663, TEST_ROUTER, TEST_WALLET, amount_out, 1),
    ]
    receipt = {
        "transactionHash": TEST_TX_HASH,
        "nonce": 77,
        "blockNumber": TEST_BLOCK_NUMBER,
        "blockHash": TEST_BLOCK_HASH,
        "status": 1,
        "gasUsed": 120000,
        "effectiveGasPrice": 10 * 10**9,
        "logs": logs,
    }

    verdict = reconcile_execution(
        receipt=receipt,
        plan=plan,
        expected_tx_hash=TEST_TX_HASH,
        expected_nonce=77,
        wallet_address=TEST_WALLET,
    )
    assert verdict.status == ReconciliationStatus.VERIFIED
    assert verdict.plan_id == plan.plan_id
    assert verdict.base_asset_address == WETH_ADDRESS_4663
    assert verdict.token_delta_atoms == amount_out - amount_in


def test_encoded_calldata_integration() -> None:
    """Reconciliation directly consuming EncodedCalldata extracts router and plan metadata."""
    encoded = EncodedCalldata(
        plan_id="plan_test_encoded",
        route_id="route_test",
        chain_id=ROBINHOOD_CHAIN_ID,
        router_address=TEST_ROUTER,
        calldata_hex="0x3593564c" + "00" * 32,
        calldata_sha256="aa" * 32,
        commands_hex="0x00",
        commands_count=1,
        deadline=1800000000,
        amount_in=10**16,
        min_amount_out=10**16 + 1000,
    )

    logs = [
        _make_transfer_log(WETH_ADDRESS_4663, TEST_WALLET, TEST_ROUTER, 10**16, 0),
        _make_transfer_log(WETH_ADDRESS_4663, TEST_ROUTER, TEST_WALLET, 10**16 + 5000, 1),
    ]
    receipt = {
        "transactionHash": TEST_TX_HASH,
        "blockNumber": TEST_BLOCK_NUMBER,
        "blockHash": TEST_BLOCK_HASH,
        "status": 1,
        "gasUsed": 100000,
        "effectiveGasPrice": 10 * 10**9,
        "logs": logs,
    }

    verdict = reconcile_execution(
        receipt=receipt,
        encoded_calldata=encoded,
        expected_tx_hash=TEST_TX_HASH,
        wallet_address=TEST_WALLET,
        base_asset_address=WETH_ADDRESS_4663,
        base_decimals=18,
        base_asset_usd_price=Decimal("2500"),
        native_token_usd_price=Decimal("2500"),
    )
    assert verdict.status == ReconciliationStatus.VERIFIED
    assert verdict.plan_id == "plan_test_encoded"


def test_pure_reconciler_class_and_execution_reconciler_alias() -> None:
    """PureReconciler class and ExecutionReconciler alias operate identically."""
    reconciler = PureReconciler(
        default_counterparties=[TEST_ROUTER, TEST_PERMIT2],
        default_native_price_usd=Decimal("2500"),
    )

    receipt = {
        "transactionHash": TEST_TX_HASH,
        "nonce": 42,
        "blockNumber": TEST_BLOCK_NUMBER,
        "blockHash": TEST_BLOCK_HASH,
        "status": 0,
        "gasUsed": 100000,
        "effectiveGasPrice": 20 * 10**9,
        "logs": [],
    }

    res = reconciler.reconcile(
        receipt=receipt,
        expected_tx_hash=TEST_TX_HASH,
        wallet=TEST_WALLET,
    )
    assert res.status == ReconciliationStatus.REVERTED
    assert res.actual_gas_cost_usd == Decimal("5.00")

    # Verify alias
    assert ExecutionReconciler is PureReconciler
    assert ReconciliationResult is ReconciliationAssessment


def test_reconciliation_assessment_immutability() -> None:
    """ReconciliationAssessment is slotted and frozen; field reassignment raises FrozenInstanceError."""
    assessment = ReconciliationAssessment(
        status=ReconciliationStatus.VERIFIED,
        reason="Test",
        latch_decision=LatchDecision.NONE,
        tx_hash=TEST_TX_HASH,
        wallet_address=TEST_WALLET,
    )

    with pytest.raises((FrozenInstanceError, AttributeError)):
        assessment.status = ReconciliationStatus.REVERTED  # type: ignore[misc]


def test_reconciliation_assessment_serialization_roundtrip() -> None:
    """Assessment serializes to and deserializes from dictionary and canonical JSON."""
    assessment = ReconciliationAssessment(
        status=ReconciliationStatus.VERIFIED,
        reason="Verified successfully",
        latch_decision=LatchDecision.NONE,
        plan_id="plan_roundtrip",
        tx_hash=TEST_TX_HASH,
        nonce=42,
        block_number=TEST_BLOCK_NUMBER,
        block_hash=TEST_BLOCK_HASH,
        wallet_address=TEST_WALLET,
        base_asset_address=WETH_ADDRESS_4663,
        token_delta_atoms=500000000000000,
        token_delta_usd=Decimal("1.25"),
        gas_used=80000,
        effective_gas_price_wei=10000000000,
        actual_gas_cost_wei=800000000000000,
        actual_gas_cost_native=Decimal("0.0008"),
        actual_gas_cost_usd=Decimal("2.00"),
        net_profit_usd=Decimal("-0.75"),
        transfers_count=2,
        confirmations=5,
        details={"net_profit_usd": "-0.75"},
        raw_receipt={"status": 1},
    )

    data_dict = assessment.to_dict()
    reconstructed = ReconciliationAssessment.from_dict(data_dict)
    assert reconstructed.status == assessment.status
    assert reconstructed.reason == assessment.reason
    assert reconstructed.latch_decision == assessment.latch_decision
    assert reconstructed.plan_id == assessment.plan_id
    assert reconstructed.token_delta_atoms == assessment.token_delta_atoms
    assert reconstructed.token_delta_usd == assessment.token_delta_usd
    assert reconstructed.net_profit_usd == assessment.net_profit_usd
    assert reconstructed.gas_used == assessment.gas_used
    assert reconstructed.confirmations == assessment.confirmations

    json_str = assessment.to_json()
    reconstructed_json = ReconciliationAssessment.from_json(json_str)
    assert reconstructed_json.tx_hash == assessment.tx_hash
    assert reconstructed_json.details == assessment.details


def test_architecture_reconciliation_zero_forbidden_imports() -> None:
    """Security AST audit: reconciliation.py contains zero network IO, credentials, or DB imports."""
    source_path = (
        Path(__file__).resolve().parent.parent.parent / "atomic_execution" / "reconciliation.py"
    )
    assert source_path.exists(), f"Source file does not exist: {source_path}"

    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    forbidden_modules = {
        "sqlite3",
        "psycopg2",
        "asyncpg",
        "sqlalchemy",
        "requests",
        "httpx",
        "aiohttp",
        "urllib",
        "socket",
        "subprocess",
        "dotenv",
    }

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_pkg = alias.name.split(".")[0]
                assert root_pkg not in forbidden_modules, (
                    f"reconciliation.py contains forbidden import: {alias.name}"
                )
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root_pkg = node.module.split(".")[0]
                assert root_pkg not in forbidden_modules, (
                    f"reconciliation.py contains forbidden from-import: {node.module}"
                )
