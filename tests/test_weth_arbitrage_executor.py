"""Comprehensive unit tests for WethArbitrageExecutor."""

from __future__ import annotations

import os
import stat
import tempfile
from decimal import Decimal
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from uniswap_universal_router_decoder import RouterCodec
from web3 import Web3

from arbitrage.spread_monitor import PoolSpec, SpreadAlert
from arbitrage.triangular import SwapLeg, TriangularArbAlert
from arbitrage.v4_reader import V4PoolSpec
from core.config import Config
from core.wallet_guard import (
    DryRunInterception,
    ExcessiveAmountError,
    InsecureKeyFileError,
    InvalidSlippageError,
    TaxTokenProhibitedError,
    WalletGuard,
    ZeroSlippageError,
)
from execution.weth_arbitrage_executor import (
    CANONICAL_PERMIT2_ADDRESS,
    CANONICAL_UNIVERSAL_ROUTER,
    CANONICAL_USDG_ADDRESS,
    CANONICAL_WETH_ADDRESS,
    ArbitrageDataError,
    ArbitrageExecutionError,
    ArbitrageLeg,
    ArbitragePlan,
    InsufficientAllowanceError,
    InsufficientFundsError,
    InsufficientGasError,
    SimulationRevertError,
    WethArbitrageExecutor,
    resolve_token_address,
)
from tests.receipt_fixture import make_receipt_fixture

# Test Constants
MOCK_WALLET = "0x23989C17bD91b8E4EA06254D9cA002DF5EF6a83f"
MOCK_PONS_ADDRESS = "0x39dbed3a2bd333467115de45665cc57f813c4571"
MOCK_PRIVATE_KEY = "0x4f3edf983ac636a65a842ce7c78d9aa706d3b113bce9c46f30d7dc1a08f167ee"
MOCK_DERIVED_ADDR = "0x11Ad3C8E7a8B948F5E2FE8af5f48D58E2373f052"


@pytest.fixture
def mock_w3() -> MagicMock:
    """Mocked Web3 instance avoiding network dependencies."""
    w3 = MagicMock()
    w3.eth.chain_id = 4663
    w3.eth.gas_price = 1_000_000_000
    w3.eth.get_balance.return_value = 10**18  # 1 ETH
    w3.from_wei = Web3.from_wei
    w3.to_wei = Web3.to_wei
    w3.to_checksum_address = Web3.to_checksum_address
    return w3


@pytest.fixture
def executor(mock_w3: MagicMock) -> WethArbitrageExecutor:
    """Executor initialized with mocked Web3 and default guard."""
    config = Config(
        UNIVERSAL_ROUTER_ADDRESS=CANONICAL_UNIVERSAL_ROUTER,
        PERMIT2_ADDRESS=CANONICAL_PERMIT2_ADDRESS,
        DRY_RUN=True,
    )
    guard = WalletGuard(max_amount_usd=500.0, dry_run=True)
    inst = WethArbitrageExecutor(config=config, guard=guard, w3=mock_w3)

    # Mock WETH contract calls
    cast(Any, inst.weth_contract.functions).balanceOf.return_value.call.return_value = (
        10**18
    )  # 1 WETH
    cast(Any, inst.weth_contract.functions).allowance.return_value.call.return_value = (
        10**24
    )  # ample
    return inst


class TestTokenResolution:
    """Token normalization and resolution tests."""

    def test_resolve_known_symbols(self):
        """Verify WETH and USDG resolve to canonical addresses."""
        assert resolve_token_address("WETH") == Web3.to_checksum_address(CANONICAL_WETH_ADDRESS)
        assert resolve_token_address("weth") == Web3.to_checksum_address(CANONICAL_WETH_ADDRESS)
        assert resolve_token_address("USDG") == Web3.to_checksum_address(CANONICAL_USDG_ADDRESS)
        assert resolve_token_address("PONS") == Web3.to_checksum_address(MOCK_PONS_ADDRESS)

    def test_resolve_raw_address(self):
        """Verify 42-char raw address checksumming."""
        raw = "0x2e8c31162b855a2ffa90f6f8634643ad6f111e18"
        assert resolve_token_address(raw) == Web3.to_checksum_address(raw)

    def test_resolve_unknown_fails(self):
        """Verify unknown symbol raises ValueError."""
        with pytest.raises(ValueError):
            resolve_token_address("NON_EXISTENT_TOKEN_XYZ")

    def test_resolve_zero_address_fails_with_invalid_token(self):
        """零地址直接抛出 ValueError 且消息含 INVALID_TOKEN."""
        with pytest.raises(ValueError) as excinfo:
            resolve_token_address("0x0000000000000000000000000000000000000000")
        assert "INVALID_TOKEN" in str(excinfo.value)


class TestPreflightInterceptions:
    """Verification of WalletGuard funds safety and limit checks."""

    def test_amount_exceeding_500u_intercepted(self, executor: WethArbitrageExecutor):
        """Trades over 500U are hard-blocked by ExcessiveAmountError."""
        with pytest.raises(ExcessiveAmountError):
            executor.preflight_check(
                wallet_address=MOCK_WALLET,
                amount_in_weth=10**17,
                amount_usd=500.01,
                slippage_pct=1.0,
                expected_amount_out=10**17,
            )

    def test_zero_slippage_intercepted(self, executor: WethArbitrageExecutor):
        """Zero slippage is hard-blocked to eradicate MEV sandwich vectors."""
        with pytest.raises(ZeroSlippageError):
            executor.preflight_check(
                wallet_address=MOCK_WALLET,
                amount_in_weth=10**17,
                amount_usd=100.0,
                slippage_pct=0.0,
                expected_amount_out=10**17,
            )

    def test_slippage_outside_range_intercepted(self, executor: WethArbitrageExecutor):
        """Slippage outside 0.1% ~ 5.0% is blocked by InvalidSlippageError."""
        with pytest.raises(InvalidSlippageError):
            executor.preflight_check(
                wallet_address=MOCK_WALLET,
                amount_in_weth=10**17,
                amount_usd=100.0,
                slippage_pct=0.05,  # Below 0.1%
                expected_amount_out=10**17,
            )
        with pytest.raises(InvalidSlippageError):
            executor.preflight_check(
                wallet_address=MOCK_WALLET,
                amount_in_weth=10**17,
                amount_usd=100.0,
                slippage_pct=6.0,  # Above 5.0%
                expected_amount_out=10**17,
            )

    def test_insufficient_gas_intercepted(
        self, executor: WethArbitrageExecutor, mock_w3: MagicMock
    ):
        """Insufficient native ETH for gas raises InsufficientGasError."""
        mock_w3.eth.get_balance.return_value = 1000  # negligible wei
        with pytest.raises(InsufficientGasError):
            executor.preflight_check(
                wallet_address=MOCK_WALLET,
                amount_in_weth=10**17,
                amount_usd=100.0,
                slippage_pct=1.0,
                expected_amount_out=10**17,
                min_gas_wei=500_000_000_000,
            )

    def test_insufficient_weth_balance_intercepted(self, executor: WethArbitrageExecutor):
        """Wallet having less WETH than amount_in raises InsufficientFundsError."""
        cast(Any, executor.weth_contract.functions).balanceOf.return_value.call.return_value = (
            10**15
        )  # 0.001 WETH
        with pytest.raises(InsufficientFundsError):
            executor.preflight_check(
                wallet_address=MOCK_WALLET,
                amount_in_weth=10**17,  # 0.1 WETH required
                amount_usd=250.0,
                slippage_pct=1.0,
                expected_amount_out=10**17,
            )

    def test_insufficient_allowance_generates_approval_tx_in_dry_run(
        self, executor: WethArbitrageExecutor
    ):
        """When allowance is 0 in dry-run mode, preflight flags it and returns approve_tx."""
        cast(Any, executor.weth_contract.functions).allowance.return_value.call.return_value = 0
        report = executor.preflight_check(
            wallet_address=MOCK_WALLET,
            amount_in_weth=10**17,
            amount_usd=100.0,
            slippage_pct=1.0,
            expected_amount_out=10**17,
        )
        assert report["has_allowance"] is False
        assert report["approve_tx"] is not None
        assert report["approve_tx"]["to"] == Web3.to_checksum_address(CANONICAL_WETH_ADDRESS)
        assert "data" in report["approve_tx"]


class TestPathAndCalldataConstruction:
    """Calldata construction tests for 2-hop, 3-hop, and mixed paths."""

    def test_two_hop_v3_path(self, executor: WethArbitrageExecutor):
        """Test 2-hop cross-pool WETH -> USDG -> WETH calldata assembly."""
        plan = executor.plan_two_hop(
            token_b="USDG",
            pool1_fee=1.0,  # 0.01% -> 100
            pool2_fee=5.0,  # 0.05% -> 500
            pool1_dex="uniswap-v3",
            pool2_dex="uniswap-v3",
            amount_usd=100.0,
            slippage_pct=1.0,
        )
        assert len(plan.legs) == 2
        assert plan.legs[0].from_token == executor.weth_address
        assert plan.legs[0].to_token == Web3.to_checksum_address(CANONICAL_USDG_ADDRESS)
        assert plan.legs[1].to_token == executor.weth_address
        assert plan.amount_out_min > 0

        res = executor.build_swap_calldata(plan)
        assert res["calldata"].startswith("0x")
        assert res["all_v3"] is True
        assert res["decoded_function"] == "execute"

    def test_three_hop_v3_path(self, executor: WethArbitrageExecutor):
        """Test 3-hop V3 loop: WETH -> USDG -> PONS -> WETH."""
        legs = [
            ArbitrageLeg(
                from_token=executor.weth_address,
                to_token=CANONICAL_USDG_ADDRESS,
                pool_fee=100,
                dex="uniswap-v3",
            ),
            ArbitrageLeg(
                from_token=CANONICAL_USDG_ADDRESS,
                to_token=MOCK_PONS_ADDRESS,
                pool_fee=3000,
                dex="uniswap-v3",
            ),
            ArbitrageLeg(
                from_token=MOCK_PONS_ADDRESS,
                to_token=executor.weth_address,
                pool_fee=3000,
                dex="uniswap-v3",
            ),
        ]
        plan = ArbitragePlan(
            legs=legs,
            amount_in_weth=10**17,
            expected_amount_out=int(10**17 * 1.01),
            amount_out_min=int(10**17 * 1.00),
            amount_usd=250.0,
            slippage_pct=1.0,
        )
        res = executor.build_swap_calldata(plan)
        assert res["all_v3"] is True
        assert res["selector"] == res["calldata"][:10]

    def test_three_hop_v4_path(self, executor: WethArbitrageExecutor):
        """Test 3-hop V4 loop: WETH -> USDG -> PONS -> WETH."""
        legs = [
            ArbitrageLeg(
                from_token=executor.weth_address,
                to_token=CANONICAL_USDG_ADDRESS,
                pool_fee=100,
                dex="uniswap-v4",
            ),
            ArbitrageLeg(
                from_token=CANONICAL_USDG_ADDRESS,
                to_token=MOCK_PONS_ADDRESS,
                pool_fee=2300,
                dex="uniswap-v4",
            ),
            ArbitrageLeg(
                from_token=MOCK_PONS_ADDRESS,
                to_token=executor.weth_address,
                pool_fee=3000,
                dex="uniswap-v4",
            ),
        ]
        plan = ArbitragePlan(
            legs=legs,
            amount_in_weth=10**17,
            expected_amount_out=int(10**17 * 1.01),
            amount_out_min=int(10**17 * 1.00),
            amount_usd=250.0,
            slippage_pct=1.0,
        )
        res = executor.build_swap_calldata(plan)
        assert res["all_v4"] is True
        assert res["decoded_function"] == "execute"

    def test_mixed_v3_v4_hops(self, executor: WethArbitrageExecutor):
        """Test mixed V3 and V4 hops chained together."""
        legs = [
            ArbitrageLeg(
                from_token=executor.weth_address,
                to_token=CANONICAL_USDG_ADDRESS,
                pool_fee=100,
                dex="uniswap-v4",
            ),
            ArbitrageLeg(
                from_token=CANONICAL_USDG_ADDRESS,
                to_token=MOCK_PONS_ADDRESS,
                pool_fee=3000,
                dex="uniswap-v3",
            ),
            ArbitrageLeg(
                from_token=MOCK_PONS_ADDRESS,
                to_token=executor.weth_address,
                pool_fee=3000,
                dex="uniswap-v3",
            ),
        ]
        plan = ArbitragePlan(
            legs=legs,
            amount_in_weth=10**17,
            expected_amount_out=int(10**17 * 1.01),
            amount_out_min=int(10**17 * 1.00),
            amount_usd=250.0,
            slippage_pct=1.0,
        )
        res = executor.build_swap_calldata(plan)
        assert res["all_v3"] is False
        assert res["all_v4"] is False
        assert res["decoded_function"] == "execute"


class TestOpportunityIngestion:
    """Tests converting TriangularArbAlert and JSONL records into plans."""

    def test_plan_from_triangular_alert_direct(self, executor: WethArbitrageExecutor):
        """Test converting TriangularArbAlert starting from WETH."""
        p1 = PoolSpec(address="0x" + "1" * 40, label="USDG/WETH 0.01%", fee_bps=1.0)
        p2 = PoolSpec(address="0x" + "2" * 40, label="PONS/USDG 0.3%", fee_bps=30.0)
        p3 = PoolSpec(address="0x" + "3" * 40, label="PONS/WETH 0.3%", fee_bps=30.0)

        legs = [
            SwapLeg(
                from_token="WETH",
                to_token="USDG",
                pool=p1,
                rate=2500.0,
                fee_bps=1.0,
                effective_rate=2497.5,
            ),
            SwapLeg(
                from_token="USDG",
                to_token=MOCK_PONS_ADDRESS,
                pool=p2,
                rate=5.0,
                fee_bps=30.0,
                effective_rate=4.985,
            ),
            SwapLeg(
                from_token=MOCK_PONS_ADDRESS,
                to_token="WETH",
                pool=p3,
                rate=0.00008,
                fee_bps=30.0,
                effective_rate=0.000079,
            ),
        ]
        alert = TriangularArbAlert(
            start_token="WETH",
            cycle=("WETH", "USDG", MOCK_PONS_ADDRESS, "WETH"),
            legs=legs,
            gross_multiplier=1.015,
            fee_multiplier=0.994,
            expected_multiplier=1.0089,
            slippage_buffer_pct=0.6,
            net_multiplier=1.0029,
            gross_profit_pct=1.5,
            net_profit_pct=0.29,
            total_fee_pct=0.6,
        )

        plan = executor.plan_from_triangular_alert(alert, amount_usd=200.0)
        assert len(plan.legs) == 3
        assert plan.legs[0].from_token == executor.weth_address
        assert plan.legs[-1].to_token == executor.weth_address
        assert plan.amount_usd == 200.0
        assert plan.amount_out_min > 0

    def test_plan_from_triangular_alert_rotated(self, executor: WethArbitrageExecutor):
        """When alert cycle starts at USDG but contains WETH, legs are rotated to WETH."""
        p1 = PoolSpec(address="0x" + "1" * 40, label="PONS/USDG 0.3%", fee_bps=30.0)
        p2 = PoolSpec(address="0x" + "2" * 40, label="PONS/WETH 0.3%", fee_bps=30.0)
        p3 = PoolSpec(address="0x" + "3" * 40, label="USDG/WETH 0.01%", fee_bps=1.0)

        legs = [
            SwapLeg(
                from_token="USDG",
                to_token=MOCK_PONS_ADDRESS,
                pool=p1,
                rate=5.0,
                fee_bps=30.0,
                effective_rate=4.985,
            ),
            SwapLeg(
                from_token=MOCK_PONS_ADDRESS,
                to_token="WETH",
                pool=p2,
                rate=0.00008,
                fee_bps=30.0,
                effective_rate=0.000079,
            ),
            SwapLeg(
                from_token="WETH",
                to_token="USDG",
                pool=p3,
                rate=2500.0,
                fee_bps=1.0,
                effective_rate=2497.5,
            ),
        ]
        alert = TriangularArbAlert(
            start_token="USDG",
            cycle=("USDG", MOCK_PONS_ADDRESS, "WETH", "USDG"),
            legs=legs,
            gross_multiplier=1.015,
            fee_multiplier=0.994,
            expected_multiplier=1.0089,
            slippage_buffer_pct=0.6,
            net_multiplier=1.0029,
            gross_profit_pct=1.5,
            net_profit_pct=0.29,
            total_fee_pct=0.6,
        )

        plan = executor.plan_from_triangular_alert(alert, amount_usd=150.0)
        assert plan.legs[0].from_token == executor.weth_address
        assert plan.legs[0].to_token == Web3.to_checksum_address(CANONICAL_USDG_ADDRESS)
        assert plan.legs[-1].to_token == executor.weth_address

    def test_load_and_plan_from_jsonl(self, executor: WethArbitrageExecutor):
        """Verify reading actual opportunities from data/arbitrage_opportunities.jsonl."""
        record = executor.load_latest_opportunity("data/arbitrage_opportunities.jsonl")
        assert "details" in record
        plan = executor.plan_from_opportunity_dict(record, amount_usd=100.0)
        assert len(plan.legs) == 3
        assert plan.legs[0].from_token == executor.weth_address
        assert plan.legs[-1].to_token == executor.weth_address
        assert plan.amount_out_min > 0

    def test_plan_from_triangular_alert_rejects_zero_address(self, executor: WethArbitrageExecutor):
        """执行器对含零地址的三角套利告警立即抛出 ValueError 且消息含 INVALID_TOKEN."""
        p1 = PoolSpec("0x" + "1" * 40, "USDG/WETH 0.01%", 1.0)
        p2 = PoolSpec("0x" + "2" * 40, "ZERO/USDG 0.3%", 30.0)
        p3 = PoolSpec("0x" + "3" * 40, "WETH/ZERO 0.3%", 30.0)
        zero_addr = "0x0000000000000000000000000000000000000000"

        legs = [
            SwapLeg(
                from_token="WETH",
                to_token="USDG",
                pool=p1,
                rate=2500.0,
                fee_bps=1.0,
                effective_rate=2497.5,
            ),
            SwapLeg(
                from_token="USDG",
                to_token=zero_addr,
                pool=p2,
                rate=1.0,
                fee_bps=30.0,
                effective_rate=0.997,
            ),
            SwapLeg(
                from_token=zero_addr,
                to_token="WETH",
                pool=p3,
                rate=0.0004,
                fee_bps=30.0,
                effective_rate=0.000398,
            ),
        ]
        alert = TriangularArbAlert(
            start_token="WETH",
            cycle=("WETH", "USDG", zero_addr, "WETH"),
            legs=legs,
            gross_multiplier=1.05,
            fee_multiplier=0.99,
            expected_multiplier=1.039,
            slippage_buffer_pct=0.6,
            net_multiplier=1.033,
            gross_profit_pct=5.0,
            net_profit_pct=3.3,
            total_fee_pct=0.61,
        )

        with pytest.raises(ValueError) as excinfo:
            executor.plan_from_triangular_alert(alert, amount_usd=100.0)
        assert "INVALID_TOKEN" in str(excinfo.value)
        assert isinstance(excinfo.value, ArbitrageDataError)

    def test_plan_from_spread_alert_rejects_zero_address(self, executor: WethArbitrageExecutor):
        """执行器对含零地址的跨池价差告警立即抛出 ValueError 且消息含 INVALID_TOKEN."""
        zero_addr = "0x0000000000000000000000000000000000000000"
        p_buy = PoolSpec("0x" + "1" * 40, "ZERO/WETH 0.05%", 5.0)
        p_sell = PoolSpec("0x" + "2" * 40, "ZERO/WETH 0.3%", 30.0)

        sa = SpreadAlert(
            base=zero_addr,
            quote="WETH",
            buy_pool=p_buy,
            sell_pool=p_sell,
            buy_price=0.0004,
            sell_price=0.00042,
            fee_pct=0.35,
            total_fee_pct=0.35,
            gross_spread_pct=5.0,
            net_spread_pct=4.65,
            max_capacity_usd=1000.0,
        )

        with pytest.raises(ValueError) as excinfo:
            executor.plan_from_spread_alert(sa, amount_usd=100.0)
        assert "INVALID_TOKEN" in str(excinfo.value)
        assert isinstance(excinfo.value, ArbitrageDataError)


class TestSimulationAndBroadcast:
    """Atomic simulation and guarded broadcast dispatch tests."""

    def test_simulation_success(self, executor: WethArbitrageExecutor, mock_w3: MagicMock):
        """Verify eth_call simulation succeeds when call returns bytes."""
        mock_w3.eth.call.return_value = b"\x00" * 32
        res = executor.simulate_swap({"to": CANONICAL_UNIVERSAL_ROUTER, "data": "0x1234"})
        assert res["success"] is True

    def test_simulation_revert_raises_error(
        self, executor: WethArbitrageExecutor, mock_w3: MagicMock
    ):
        """When eth_call simulation reverts, SimulationRevertError is raised to block execution."""
        mock_w3.eth.call.side_effect = ValueError("execution reverted: STF")
        with pytest.raises(SimulationRevertError) as exc_info:
            executor.simulate_swap({"to": CANONICAL_UNIVERSAL_ROUTER, "data": "0x1234"})
        assert "reverted" in str(exc_info.value)

    def test_broadcast_blocked_by_default_dry_run(self, executor: WethArbitrageExecutor):
        """Live broadcast is blocked when DRY_RUN=True."""
        assert executor.guard.is_dry_run() is True
        with pytest.raises(DryRunInterception):
            executor.broadcast_swap(
                tx={"to": CANONICAL_UNIVERSAL_ROUTER, "data": "0x"},
                wallet_address=MOCK_WALLET,
            )

    def test_broadcast_blocked_by_insecure_key_permissions(self, executor: WethArbitrageExecutor):
        """Key file with permissions looser than 0600 raises InsecureKeyFileError."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as tf:
            tf.write("0x" + "a" * 64)
            key_path = tf.name

        try:
            # Set loose permissions (0644)
            os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
            executor.guard.dry_run = False
            executor.config.PRIVATE_KEY_PATH = key_path

            with pytest.raises(InsecureKeyFileError):
                executor.broadcast_swap(
                    tx={"to": CANONICAL_UNIVERSAL_ROUTER, "data": "0x"},
                    wallet_address=MOCK_WALLET,
                )
        finally:
            if os.path.exists(key_path):
                os.unlink(key_path)

    def test_live_broadcast_success_and_profit_audit(self, tmp_path, monkeypatch) -> None:
        """Preserve WETH/wei gain and use the same canonical fixture for real gas/net."""
        ctx = make_receipt_fixture(tmp_path, monkeypatch, base="WETH")
        expected_profit = 5 * 10**15
        report = ctx.executor.broadcast_swap(
            tx={},
            wallet_address=ctx.wallet,
            base_symbol="WETH",
            base_token=ctx.token,
            plan=ctx.plan,
        )
        gas_usd = Decimal(ctx.gas_used * ctx.gas_price) / 10**18 * ctx.native_price
        token_usd = Decimal(expected_profit) / 10**18 * ctx.base_price
        assert report["status"] == "EXECUTED_SUCCESS"
        assert report["real_profit_wei"] == expected_profit
        assert report["real_profit_eth"] == pytest.approx(0.005)
        assert report["actual_gas_usd"] == gas_usd
        assert report["net_profit_usd"] == token_usd - gas_usd
        assert report["real_profit_usd"] == pytest.approx(float(token_usd - gas_usd))
        ctx.w3.eth.account.sign_transaction.assert_called_once()


class TestPlanFromSpreadAlert:
    """Unit tests for plan_from_spread_alert in WethArbitrageExecutor."""

    def test_plan_from_spread_alert_quote_weth(self, executor: WethArbitrageExecutor):
        """Test converting SpreadAlert where quote token is WETH."""
        pool_buy = PoolSpec(
            address="0x" + "1" * 40,
            label="USDG/WETH 0.05%",
            fee_bps=5.0,
            token0=CANONICAL_USDG_ADDRESS,
            token1=CANONICAL_WETH_ADDRESS,
            dec0=18,
            dec1=18,
        )
        pool_sell = PoolSpec(
            address="0x" + "2" * 40,
            label="USDG/WETH 0.3%",
            fee_bps=30.0,
            token0=CANONICAL_USDG_ADDRESS,
            token1=CANONICAL_WETH_ADDRESS,
            dec0=18,
            dec1=18,
        )
        alert = SpreadAlert(
            base=CANONICAL_USDG_ADDRESS,
            quote=CANONICAL_WETH_ADDRESS,
            buy_pool=pool_buy,
            sell_pool=pool_sell,
            buy_price=0.00040,
            sell_price=0.00042,
            gross_spread_pct=5.0,
            total_fee_pct=0.35,
            net_spread_pct=4.45,
            max_capacity_usd=1000.0,
            optimal_size_usd=500.0,
        )

        plan = executor.plan_from_spread_alert(alert, amount_usd=100.0, slippage_pct=0.5)
        assert len(plan.legs) == 2
        # Leg 1: WETH -> USDG via buy_pool
        assert plan.legs[0].from_token == executor.weth_address
        assert plan.legs[0].to_token == Web3.to_checksum_address(CANONICAL_USDG_ADDRESS)
        assert plan.legs[0].pool_fee == 500
        assert plan.legs[0].dex == "uniswap-v3"
        # Leg 2: USDG -> WETH via sell_pool
        assert plan.legs[1].from_token == Web3.to_checksum_address(CANONICAL_USDG_ADDRESS)
        assert plan.legs[1].to_token == executor.weth_address
        assert plan.legs[1].pool_fee == 3000
        assert plan.legs[1].dex == "uniswap-v3"

        assert plan.amount_out_min > 0
        assert plan.amount_in_weth > 0
        assert plan.amount_usd == 100.0
        assert plan.expected_multiplier > 1.0
        assert plan.metadata["source"] == "SpreadAlert"

    def test_plan_from_spread_alert_base_weth(self, executor: WethArbitrageExecutor):
        """Test converting SpreadAlert where base token is WETH."""
        pool_buy = PoolSpec(
            address="0x" + "1" * 40,
            label="WETH/USDG 0.05%",
            fee_bps=5.0,
            token0=CANONICAL_WETH_ADDRESS,
            token1=CANONICAL_USDG_ADDRESS,
            dec0=18,
            dec1=18,
        )
        pool_sell = PoolSpec(
            address="0x" + "2" * 40,
            label="WETH/USDG 0.3%",
            fee_bps=30.0,
            token0=CANONICAL_WETH_ADDRESS,
            token1=CANONICAL_USDG_ADDRESS,
            dec0=18,
            dec1=18,
        )
        alert = SpreadAlert(
            base=CANONICAL_WETH_ADDRESS,
            quote=CANONICAL_USDG_ADDRESS,
            buy_pool=pool_buy,
            sell_pool=pool_sell,
            buy_price=2400.0,
            sell_price=2500.0,
            gross_spread_pct=4.167,
            total_fee_pct=0.35,
            net_spread_pct=3.617,
        )

        plan = executor.plan_from_spread_alert(alert, amount_usd=200.0, slippage_pct=0.5)
        assert len(plan.legs) == 2
        # Leg 1: WETH -> USDG via sell_pool
        assert plan.legs[0].from_token == executor.weth_address
        assert plan.legs[0].to_token == Web3.to_checksum_address(CANONICAL_USDG_ADDRESS)
        assert plan.legs[0].pool_fee == 3000
        # Leg 2: USDG -> WETH via buy_pool
        assert plan.legs[1].from_token == Web3.to_checksum_address(CANONICAL_USDG_ADDRESS)
        assert plan.legs[1].to_token == executor.weth_address
        assert plan.legs[1].pool_fee == 500

        assert plan.amount_out_min > 0
        assert plan.amount_in_weth > 0
        assert plan.amount_usd == 200.0

    def test_plan_from_spread_alert_no_weth_raises(self, executor: WethArbitrageExecutor):
        """Spread not involving WETH must raise ValueError."""
        pool = PoolSpec(
            address="0x" + "1" * 40,
            label="USDG/PONS 0.05%",
            fee_bps=5.0,
            token0=CANONICAL_USDG_ADDRESS,
            token1=MOCK_PONS_ADDRESS,
        )
        alert = SpreadAlert(
            base=CANONICAL_USDG_ADDRESS,
            quote=MOCK_PONS_ADDRESS,
            buy_pool=pool,
            sell_pool=pool,
            buy_price=1.0,
            sell_price=1.05,
            gross_spread_pct=5.0,
            total_fee_pct=0.1,
            net_spread_pct=4.9,
        )
        with pytest.raises(ValueError, match="Spread does not involve WETH"):
            executor.plan_from_spread_alert(alert, amount_usd=100.0)

    def test_plan_from_spread_alert_tax_token_blocked(self, executor: WethArbitrageExecutor):
        """Tax token in spread route must be immediately rejected."""
        tax_token = "0x5555555555555555555555555555555555555555"
        executor.guard.known_tax_tokens[tax_token.lower()] = 5.0

        pool = PoolSpec(
            address="0x" + "1" * 40,
            label="TAX/WETH 0.05%",
            fee_bps=5.0,
            token0=tax_token,
            token1=CANONICAL_WETH_ADDRESS,
        )
        alert = SpreadAlert(
            base=tax_token,
            quote=CANONICAL_WETH_ADDRESS,
            buy_pool=pool,
            sell_pool=pool,
            buy_price=1.0,
            sell_price=1.05,
            gross_spread_pct=5.0,
            total_fee_pct=0.1,
            net_spread_pct=4.9,
        )
        with pytest.raises(TaxTokenProhibitedError):
            executor.plan_from_spread_alert(alert, amount_usd=100.0)

    def test_plan_from_spread_alert_with_v4_pool(self, executor: WethArbitrageExecutor):
        """Test converting SpreadAlert involving a V4 pool."""
        v4_pool_id = "0x" + "9" * 64
        pool_v4 = V4PoolSpec(
            address=v4_pool_id,
            label="USDG / WETH 0.01% (v4)",
            fee_bps=1.0,
            token0=CANONICAL_WETH_ADDRESS,
            token1=CANONICAL_USDG_ADDRESS,
            tick_spacing=1,
        )
        pool_v3 = PoolSpec(
            address="0x" + "2" * 40,
            label="USDG/WETH 0.05%",
            fee_bps=5.0,
            token0=CANONICAL_USDG_ADDRESS,
            token1=CANONICAL_WETH_ADDRESS,
        )
        alert = SpreadAlert(
            base=CANONICAL_USDG_ADDRESS,
            quote=CANONICAL_WETH_ADDRESS,
            buy_pool=pool_v4,
            sell_pool=pool_v3,
            buy_price=0.00040,
            sell_price=0.00042,
            gross_spread_pct=5.0,
            total_fee_pct=0.06,
            net_spread_pct=4.74,
        )
        plan = executor.plan_from_spread_alert(alert, amount_usd=100.0)
        assert len(plan.legs) == 2
        assert plan.legs[0].dex == "uniswap-v4"
        assert plan.legs[0].pool_address == v4_pool_id
        assert plan.legs[1].dex == "uniswap-v3"
