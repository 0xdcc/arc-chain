"""Unit tests for USDG-denominated arbitrage execution, decimals scaling, and profit accounting."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from arbitrage.spread_monitor import PoolSpec, SpreadAlert
from arbitrage.triangular import SwapLeg, TriangularArbAlert
from core.config import Config, load_safe_config
from core.wallet_guard import WalletGuard
from execution.weth_arbitrage_executor import (
    CANONICAL_PERMIT2_ADDRESS,
    CANONICAL_UNIVERSAL_ROUTER,
    CANONICAL_USDG_ADDRESS,
    InsufficientAllowanceError,
    InsufficientFundsError,
    WethArbitrageExecutor,
    resolve_token_address,
)
from web3 import Web3

from tests.receipt_fixture import make_receipt_fixture

MOCK_WALLET = "0x23989C17bD91b8E4EA06254D9cA002DF5EF6a83f"
MOCK_PONS_ADDRESS = "0x39dbed3a2bd333467115de45665cc57f813c4571"
MOCK_TOKEN_C_ADDRESS = "0x4444444444444444444444444444444444444444"


@pytest.fixture
def mock_w3() -> MagicMock:
    """Mocked Web3 instance with independent contract mocks per address."""
    w3 = MagicMock()
    w3.eth.chain_id = 4663
    w3.eth.gas_price = 1_000_000_000
    w3.eth.get_balance.return_value = 10**18  # 1 ETH
    w3.from_wei = Web3.from_wei
    w3.to_wei = Web3.to_wei
    w3.to_checksum_address = Web3.to_checksum_address

    contracts: dict[str, MagicMock] = {}

    def _get_contract(address: str | None = None, **kwargs: Any) -> MagicMock:
        key = Web3.to_checksum_address(address) if address else "default"
        if key not in contracts:
            contracts[key] = MagicMock(name=f"MockContract_{key[:10]}")
        return contracts[key]

    w3.eth.contract.side_effect = _get_contract
    return w3


@pytest.fixture
def executor(mock_w3: MagicMock, monkeypatch: pytest.MonkeyPatch) -> WethArbitrageExecutor:
    """Executor initialized with mocked Web3, virtual test account, and default guard."""
    dummy_key = "0x" + "a" * 64
    config = load_safe_config(
        _env_file=None,
        UNIVERSAL_ROUTER_ADDRESS=CANONICAL_UNIVERSAL_ROUTER,
        PERMIT2_ADDRESS=CANONICAL_PERMIT2_ADDRESS,
        DRY_RUN=True,
        PRIVATE_KEY=dummy_key,
    )
    guard = WalletGuard(max_amount_usd=500.0, dry_run=True)
    inst = WethArbitrageExecutor(config=config, guard=guard, w3=mock_w3)

    # Virtual account resolution seam for non-production tests: avoid dependency on .env
    mock_account = MagicMock()
    mock_account.address = Web3.to_checksum_address(MOCK_WALLET)
    monkeypatch.setattr("eth_account.Account.from_key", lambda key: mock_account)

    # Mock WETH & USDG contract calls on distinct, address-separated contract mocks
    cast(Any, inst.weth_contract.functions).balanceOf.return_value.call.return_value = 10**18
    cast(Any, inst.weth_contract.functions).allowance.return_value.call.return_value = 10**24
    cast(Any, inst.usdg_contract.functions).balanceOf.return_value.call.return_value = 500 * 10**6
    cast(Any, inst.usdg_contract.functions).allowance.return_value.call.return_value = 10**12
    return inst


def make_mock_usdg_triangle_alert(rotate: bool = False) -> TriangularArbAlert:
    """Create a 3-hop triangular alert using USDG as the base currency."""
    usdg_addr = Web3.to_checksum_address(CANONICAL_USDG_ADDRESS)
    pons_addr = Web3.to_checksum_address(MOCK_PONS_ADDRESS)
    tokc_addr = Web3.to_checksum_address(MOCK_TOKEN_C_ADDRESS)

    p1 = PoolSpec(
        address="0x" + "1" * 40,
        label="USDG/PONS 0.05%",
        fee_bps=5.0,
        token0=usdg_addr,
        token1=pons_addr,
        dec0=6,
        dec1=18,
    )
    p2 = PoolSpec(
        address="0x" + "2" * 40,
        label="PONS/TOKEN_C 0.05%",
        fee_bps=5.0,
        token0=pons_addr,
        token1=tokc_addr,
        dec0=18,
        dec1=18,
    )
    p3 = PoolSpec(
        address="0x" + "3" * 40,
        label="TOKEN_C/USDG 0.05%",
        fee_bps=5.0,
        token0=tokc_addr,
        token1=usdg_addr,
        dec0=18,
        dec1=6,
    )

    leg1 = SwapLeg(
        from_token=usdg_addr,
        to_token=pons_addr,
        pool=p1,
        rate=2.0,
        fee_bps=5.0,
        effective_rate=1.999,
        from_symbol="USDG",
        to_symbol="PONS",
    )
    leg2 = SwapLeg(
        from_token=pons_addr,
        to_token=tokc_addr,
        pool=p2,
        rate=0.5,
        fee_bps=5.0,
        effective_rate=0.49975,
        from_symbol="PONS",
        to_symbol="TOKEN_C",
    )
    leg3 = SwapLeg(
        from_token=tokc_addr,
        to_token=usdg_addr,
        pool=p3,
        rate=1.05,
        fee_bps=5.0,
        effective_rate=1.049475,
        from_symbol="TOKEN_C",
        to_symbol="USDG",
    )

    if not rotate:
        return TriangularArbAlert(
            start_token=usdg_addr,
            cycle=("USDG", "PONS", tokc_addr, "USDG"),
            legs=[leg1, leg2, leg3],
            gross_multiplier=1.05,
            fee_multiplier=0.9985,
            expected_multiplier=1.05 * 0.9985,
            slippage_buffer_pct=0.5,
            net_multiplier=1.045,
            gross_profit_pct=5.0,
            net_profit_pct=4.85,
            total_fee_pct=0.15,
            optimal_size_usd=100.0,
            max_capacity_usd=1000.0,
            max_profit_usd=4.85,
            profit_at_500u=24.25,
            bottleneck_tvl=50000.0,
        )

    # Rotated: PONS -> TOKEN_C -> USDG -> PONS
    return TriangularArbAlert(
        start_token=pons_addr,
        cycle=("PONS", tokc_addr, "USDG", "PONS"),
        legs=[leg2, leg3, leg1],
        gross_multiplier=1.05,
        fee_multiplier=0.9985,
        expected_multiplier=1.05 * 0.9985,
        slippage_buffer_pct=0.5,
        net_multiplier=1.045,
        gross_profit_pct=5.0,
        net_profit_pct=4.85,
        total_fee_pct=0.15,
        optimal_size_usd=100.0,
        max_capacity_usd=1000.0,
        max_profit_usd=4.85,
        profit_at_500u=24.25,
        bottleneck_tvl=50000.0,
    )


class TestUsdgArbitrageExecutor:
    """Test suite for USDG base arbitrage planning, scaling, and execution."""

    def test_usdg_triangular_plan_decimals_six(self, executor: WethArbitrageExecutor) -> None:
        """验证 USDG 三角套利生成 Decimals=6、base_symbol=USDG、10 USDG 换算为 10,000,000 原子单位."""
        alert = make_mock_usdg_triangle_alert(rotate=False)
        plan = executor.plan_from_triangular_alert(alert, amount_usd=10.0, base_token="USDG")

        assert plan.base_symbol == "USDG"
        assert plan.decimals == 6
        assert plan.base_token.lower() == CANONICAL_USDG_ADDRESS.lower()
        # 核心断言：10 USDG 对应 10 * 10**6 = 10,000,000，绝非 10 * 10**18
        assert plan.amount_in_weth == 10_000_000
        assert plan.amount_usd == 10.0
        assert len(plan.legs) == 3
        assert plan.legs[0].from_token.lower() == CANONICAL_USDG_ADDRESS.lower()
        assert plan.legs[-1].to_token.lower() == CANONICAL_USDG_ADDRESS.lower()

    def test_usdg_triangular_plan_cycle_rotation(self, executor: WethArbitrageExecutor) -> None:
        """验证 USDG 三角路径不在首节点时，自动轮转 legs 起点至 USDG 闭环."""
        alert = make_mock_usdg_triangle_alert(rotate=True)
        plan = executor.plan_from_triangular_alert(alert, amount_usd=20.0, base_token="USDG")

        assert plan.base_symbol == "USDG"
        assert plan.decimals == 6
        assert plan.amount_in_weth == 20_000_000
        # 轮转后首腿必须从 USDG 出发，末腿必须回到 USDG
        assert plan.legs[0].from_token.lower() == CANONICAL_USDG_ADDRESS.lower()
        assert plan.legs[0].to_token.lower() == resolve_token_address(MOCK_PONS_ADDRESS).lower()
        assert plan.legs[-1].to_token.lower() == CANONICAL_USDG_ADDRESS.lower()

    def test_usdg_plan_two_hop_decimals_six(self, executor: WethArbitrageExecutor) -> None:
        """验证 plan_two_hop 指定 base_token='USDG' 时自适应 Decimals=6."""
        plan = executor.plan_two_hop(
            token_b=MOCK_PONS_ADDRESS,
            pool1_fee=500,
            pool2_fee=500,
            amount_usd=15.0,
            base_token="USDG",
        )

        assert plan.base_symbol == "USDG"
        assert plan.decimals == 6
        assert plan.base_token.lower() == CANONICAL_USDG_ADDRESS.lower()
        assert plan.amount_in_weth == 15_000_000
        assert plan.amount_usd == 15.0
        assert plan.legs[0].from_symbol == "USDG"
        assert plan.legs[1].to_symbol == "USDG"

    def test_usdg_plan_from_spread_alert_decimals_six(
        self, executor: WethArbitrageExecutor
    ) -> None:
        """验证 plan_from_spread_alert 处理 USDG 跨池套利时自适应 Decimals=6."""
        usdg_addr = Web3.to_checksum_address(CANONICAL_USDG_ADDRESS)
        pons_addr = Web3.to_checksum_address(MOCK_PONS_ADDRESS)

        p1 = PoolSpec(
            address="0x" + "1" * 40,
            label="USDG/PONS 0.05%",
            fee_bps=5.0,
            token0=usdg_addr,
            token1=pons_addr,
        )
        p2 = PoolSpec(
            address="0x" + "2" * 40,
            label="USDG/PONS 0.3%",
            fee_bps=30.0,
            token0=usdg_addr,
            token1=pons_addr,
        )

        alert = SpreadAlert(
            base=usdg_addr,
            quote=pons_addr,
            buy_pool=p1,
            sell_pool=p2,
            buy_price=2.0,
            sell_price=2.1,
            gross_spread_pct=5.0,
            total_fee_pct=0.35,
            net_spread_pct=4.65,
        )

        plan = executor.plan_from_spread_alert(alert, amount_usd=25.0, base_token="USDG")
        assert plan.base_symbol == "USDG"
        assert plan.decimals == 6
        assert plan.base_token.lower() == CANONICAL_USDG_ADDRESS.lower()
        assert plan.amount_in_weth == 25_000_000
        assert plan.amount_usd == 25.0

    def test_usdg_balance_and_allowance_helpers(self, executor: WethArbitrageExecutor) -> None:
        """测试 USDG balance、allowance 读取与 approve tx 组装辅助方法."""
        wallet = Web3.to_checksum_address(MOCK_WALLET)
        spender = Web3.to_checksum_address(CANONICAL_UNIVERSAL_ROUTER)

        usdg_bal = executor.get_usdg_balance(wallet)
        assert usdg_bal == 500 * 10**6

        tok_bal = executor.get_token_balance(wallet, CANONICAL_USDG_ADDRESS)
        assert tok_bal == 500 * 10**6

        usdg_allow = executor.get_usdg_allowance(wallet, spender)
        assert usdg_allow == 10**12

        tok_allow = executor.get_token_allowance(wallet, CANONICAL_USDG_ADDRESS, spender)
        assert tok_allow == 10**12

        appr_tx = executor.build_approve_tx(
            spender, amount=100_000_000, token_address=CANONICAL_USDG_ADDRESS
        )
        assert appr_tx["to"].lower() == CANONICAL_USDG_ADDRESS.lower()

    def test_usdg_preflight_insufficient_balance_raises(
        self, executor: WethArbitrageExecutor
    ) -> None:
        """测试 USDG 余额不足时 preflight_check 抛出 InsufficientFundsError."""
        alert = make_mock_usdg_triangle_alert(rotate=False)
        plan = executor.plan_from_triangular_alert(alert, amount_usd=10.0, base_token="USDG")

        # 模拟 USDG 余额只有 5 USDG (5_000_000 原子单位)，而计划需要 10 USDG
        cast(
            Any, executor.usdg_contract.functions
        ).balanceOf.return_value.call.return_value = 5_000_000

        with pytest.raises(InsufficientFundsError) as excinfo:
            executor.preflight_check(
                wallet_address=MOCK_WALLET,
                amount_in_weth=plan.amount_in_weth,
                amount_usd=plan.amount_usd,
                slippage_pct=plan.slippage_pct,
                expected_amount_out=plan.expected_amount_out,
                base_symbol=plan.base_symbol,
                base_token=plan.base_token,
                decimals=plan.decimals,
            )
        assert "USDG balance" in str(excinfo.value)

    def test_usdg_preflight_insufficient_allowance_fails_execution(
        self, executor: WethArbitrageExecutor
    ) -> None:
        """测试 USDG 授权不足时 preflight 标记 has_allowance=False，真实执行抛 InsufficientAllowanceError."""
        alert = make_mock_usdg_triangle_alert(rotate=False)
        plan = executor.plan_from_triangular_alert(alert, amount_usd=10.0, base_token="USDG")

        # 模拟充足余额但授权为 0
        cast(Any, executor.usdg_contract.functions).balanceOf.return_value.call.return_value = (
            50 * 10**6
        )
        cast(Any, executor.usdg_contract.functions).allowance.return_value.call.return_value = 0

        preflight = executor.preflight_check(
            wallet_address=MOCK_WALLET,
            amount_in_weth=plan.amount_in_weth,
            amount_usd=plan.amount_usd,
            slippage_pct=plan.slippage_pct,
            expected_amount_out=plan.expected_amount_out,
            base_symbol=plan.base_symbol,
            base_token=plan.base_token,
            decimals=plan.decimals,
        )
        assert preflight["has_allowance"] is False
        assert preflight["base_symbol"] == "USDG"

        with pytest.raises(InsufficientAllowanceError) as excinfo:
            executor.execute(plan, wallet_address=MOCK_WALLET, dry_run=False)
        assert "USDG allowance insufficient" in str(excinfo.value)

    def test_usdg_broadcast_swap_profit_accounting(self, tmp_path, monkeypatch) -> None:
        """Keep 2.5 USDG token gain, independently subtract canonical fixture gas."""
        ctx = make_receipt_fixture(tmp_path, monkeypatch, base="USDG")
        res = ctx.executor.broadcast_swap(
            tx={},
            wallet_address=ctx.wallet,
            base_symbol="USDG",
            base_token=ctx.token,
            plan=ctx.plan,
        )
        gas_usd = Decimal(ctx.gas_used * ctx.gas_price) / 10**18 * ctx.native_price
        token_usd = Decimal(ctx.gain) / 10**ctx.plan.decimals * ctx.base_price
        assert res["status"] == "EXECUTED_SUCCESS"
        assert res["real_profit_usdg"] == 2.5
        assert res["real_profit_usd"] == pytest.approx(float(token_usd - gas_usd))
        assert res["actual_gas_usd"] == gas_usd
        assert res["token_delta_usd"] == token_usd
        assert res["net_profit_usd"] == token_usd - gas_usd
        ctx.w3.eth.account.sign_transaction.assert_called_once()
        assert ctx.ledger.status(4663, ctx.wallet, "USDG")["mode"] == "NORMAL"

    def test_usdg_build_swap_calldata(self, executor: WethArbitrageExecutor) -> None:
        """测试 USDG base 的交易 calldata 组装."""
        alert = make_mock_usdg_triangle_alert(rotate=False)
        plan = executor.plan_from_triangular_alert(alert, amount_usd=10.0, base_token="USDG")
        calldata_info = executor.build_swap_calldata(plan, recipient=MOCK_WALLET)
        assert calldata_info["calldata"].startswith("0x")
        assert len(calldata_info["calldata"]) > 10
        assert calldata_info["selector"].startswith("0x")

    def test_usdg_execute_dry_run_simulation(self, executor: WethArbitrageExecutor) -> None:
        """测试 USDG base 在 dry_run=True 且 simulate=True 下成功模拟."""
        alert = make_mock_usdg_triangle_alert(rotate=False)
        plan = executor.plan_from_triangular_alert(alert, amount_usd=10.0, base_token="USDG")

        with patch.object(executor.w3.eth, "call", return_value=b""):
            res = executor.execute(plan, wallet_address=MOCK_WALLET, dry_run=True, simulate=True)
            assert res["status"] == "DRY_RUN_SUCCESS"
            assert res["simulation"] is not None
            assert res["simulation"]["success"] is True
