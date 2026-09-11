"""Unit tests for token transaction tax (Transfer Tax) physical guardrails."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from arbitrage.spread_monitor import PoolSpec, PriceQuote, find_spreads, scan_once
from arbitrage.triangular import (
    DirectedEdge,
    SwapLeg,
    TriangularArbAlert,
    calculate_triangular_path,
    find_triangular_opportunities,
)
from core.wallet_guard import (
    KNOWN_TAX_TOKENS,
    TaxTokenProhibitedError,
    WalletGuard,
    WalletGuardError,
    is_tax_token,
)

INDEX_ADDRESS = "0x56910d4409f3a0c78c64dd8d0545ff0705389870"
HOOD10_ADDRESS = "0x0d257ca40d40090be60c2d2ed5bb3535392838cc"
WETH_ADDRESS = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
USDG_ADDRESS = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"


def test_tax_token_prohibited_error_hierarchy() -> None:
    """Verify TaxTokenProhibitedError inherits from WalletGuardError."""
    assert issubclass(TaxTokenProhibitedError, WalletGuardError)
    err = TaxTokenProhibitedError("Tax token detected")
    assert str(err) == "Tax token detected"


def test_is_tax_token_detection() -> None:
    """Verify is_tax_token detects known tax tokens case-insensitively."""
    assert is_tax_token(INDEX_ADDRESS) is True
    assert is_tax_token(INDEX_ADDRESS.upper()) is True
    assert is_tax_token(HOOD10_ADDRESS) is True
    assert is_tax_token("INDEX") is True
    assert is_tax_token("HOOD10") is True

    assert is_tax_token(WETH_ADDRESS) is False
    assert is_tax_token(USDG_ADDRESS) is False
    assert is_tax_token("") is False


def test_wallet_guard_validate_token_tax_known_tokens() -> None:
    """Verify WalletGuard.validate_token_tax blocks known tax tokens."""
    guard = WalletGuard()

    with pytest.raises(TaxTokenProhibitedError) as exc_info:
        guard.validate_token_tax(INDEX_ADDRESS)
    assert "prohibited" in str(exc_info.value).lower()

    with pytest.raises(TaxTokenProhibitedError):
        guard.validate_token_tax(HOOD10_ADDRESS)


def test_wallet_guard_validate_token_tax_custom_fee_rate() -> None:
    """Verify WalletGuard.validate_token_tax blocks arbitrary tokens with fee_rate > 0."""
    guard = WalletGuard()
    custom_token = "0x1234567890123456789012345678901234567890"

    # 0% tax passes
    guard.validate_token_tax(custom_token, fee_rate=0.0)

    # >0% tax raises TaxTokenProhibitedError
    with pytest.raises(TaxTokenProhibitedError) as exc_info:
        guard.validate_token_tax(custom_token, fee_rate=2.5)
    assert "2.50%" in str(exc_info.value)


def test_wallet_guard_safe_tokens_pass() -> None:
    """Verify common non-tax tokens pass tax validation cleanly."""
    guard = WalletGuard()
    guard.validate_token_tax(WETH_ADDRESS)
    guard.validate_token_tax(USDG_ADDRESS)
    assert guard.is_tax_token(WETH_ADDRESS) is False


def test_wallet_guard_assert_tokens_tax_free() -> None:
    """Verify batch token tax inspection."""
    guard = WalletGuard()

    # All safe tokens: passes
    guard.assert_tokens_tax_free([WETH_ADDRESS, USDG_ADDRESS])

    # Poisoned list: raises immediately
    with pytest.raises(TaxTokenProhibitedError):
        guard.assert_tokens_tax_free([WETH_ADDRESS, INDEX_ADDRESS, USDG_ADDRESS])


def test_wallet_guard_verify_trade_with_tax_tokens() -> None:
    """Verify verify_trade intercepts when tax tokens are present."""
    guard = WalletGuard()

    # Clean trade passes
    report = guard.verify_trade(
        amount_usd=100.0,
        slippage_pct=1.0,
        expected_amount_out=1000,
        tokens=[WETH_ADDRESS, USDG_ADDRESS],
    )
    assert report["verified"] is True

    # Trade involving Index token raises
    with pytest.raises(TaxTokenProhibitedError):
        guard.verify_trade(
            amount_usd=100.0,
            slippage_pct=1.0,
            expected_amount_out=1000,
            tokens=[WETH_ADDRESS, INDEX_ADDRESS],
        )


def test_triangular_excludes_tax_tokens() -> None:
    """Verify triangular engine never generates cycles with tax tokens."""
    p_clean1 = PoolSpec(
        address="0x" + "1" * 40,
        label="WETH/USDG 0.05%",
        fee_bps=5.0,
        token0=WETH_ADDRESS,
        token1=USDG_ADDRESS,
    )
    p_clean2 = PoolSpec(
        address="0x" + "2" * 40,
        label="USDG/PONS 0.05%",
        fee_bps=5.0,
        token0=USDG_ADDRESS,
        token1="0x" + "c" * 40,
    )
    p_tax = PoolSpec(
        address="0x" + "3" * 40,
        label="INDEX/USDG 0.05%",
        fee_bps=5.0,
        token0=INDEX_ADDRESS,
        token1=USDG_ADDRESS,
    )

    q1 = PriceQuote(
        pool=p_clean1,
        base=WETH_ADDRESS,
        quote=USDG_ADDRESS,
        price=2500.0,
        raw_price_t1_per_t0=2500.0,
    )
    q2 = PriceQuote(
        pool=p_clean2, base=USDG_ADDRESS, quote="0x" + "c" * 40, price=1.0, raw_price_t1_per_t0=1.0
    )
    q_tax = PriceQuote(
        pool=p_tax, base=INDEX_ADDRESS, quote=USDG_ADDRESS, price=10.0, raw_price_t1_per_t0=10.0
    )

    opportunities = find_triangular_opportunities([q1, q2, q_tax])
    for opp in opportunities:
        assert INDEX_ADDRESS not in opp.cycle
        assert not any(is_tax_token(node) for node in opp.cycle)


def test_calculate_triangular_path_drops_tax_token() -> None:
    """Verify calculate_triangular_path drops cycles containing tax tokens."""
    p = PoolSpec(
        address="0x" + "1" * 40,
        label="Test/Pool",
        fee_bps=5.0,
        token0=WETH_ADDRESS,
        token1=INDEX_ADDRESS,
    )
    leg1 = DirectedEdge(
        from_token=WETH_ADDRESS,
        to_token=INDEX_ADDRESS,
        pool=p,
        rate=1.0,
        fee_bps=5.0,
        effective_rate=0.9995,
        weight=0.0,
    )
    leg2 = DirectedEdge(
        from_token=INDEX_ADDRESS,
        to_token=USDG_ADDRESS,
        pool=p,
        rate=1.0,
        fee_bps=5.0,
        effective_rate=0.9995,
        weight=0.0,
    )
    leg3 = DirectedEdge(
        from_token=USDG_ADDRESS,
        to_token=WETH_ADDRESS,
        pool=p,
        rate=1.0,
        fee_bps=5.0,
        effective_rate=0.9995,
        weight=0.0,
    )

    alert = calculate_triangular_path(leg1, leg2, leg3)
    assert alert is None


def test_spread_monitor_excludes_tax_tokens() -> None:
    """Verify find_spreads and scan_once drop tax token quotes."""
    p_tax1 = PoolSpec(
        address="0x" + "1" * 40,
        label="INDEX/USDG Pool 1",
        fee_bps=5.0,
        token0=INDEX_ADDRESS,
        token1=USDG_ADDRESS,
    )
    p_tax2 = PoolSpec(
        address="0x" + "2" * 40,
        label="INDEX/USDG Pool 2",
        fee_bps=30.0,
        token0=INDEX_ADDRESS,
        token1=USDG_ADDRESS,
    )

    q1 = PriceQuote(
        pool=p_tax1, base=INDEX_ADDRESS, quote=USDG_ADDRESS, price=10.0, raw_price_t1_per_t0=10.0
    )
    q2 = PriceQuote(
        pool=p_tax2, base=INDEX_ADDRESS, quote=USDG_ADDRESS, price=12.0, raw_price_t1_per_t0=12.0
    )

    spreads = find_spreads([q1, q2])
    assert len(spreads) == 0

    mock_reader = MagicMock()
    mock_reader.batch_quote.return_value = [q1, q2]
    quotes, alerts = scan_once([p_tax1, p_tax2], reader=mock_reader)
    assert len(quotes) == 0
    assert len(alerts) == 0


def test_weth_executor_blocks_tax_token() -> None:
    """Verify WethArbitrageExecutor blocks plans and executions with tax tokens."""
    from core.config import Config
    from execution.weth_arbitrage_executor import WethArbitrageExecutor

    cfg = Config(
        WETH_ADDRESS=WETH_ADDRESS,
        USDG_ADDRESS=USDG_ADDRESS,
        UNIVERSAL_ROUTER_ADDRESS="0x" + "9" * 40,
        PERMIT2_ADDRESS="0x" + "8" * 40,
        ROBINHOOD_RPC_URL="http://localhost:8545",
        PRIVATE_KEY="",
        PRIVATE_KEY_PATH="",
    )

    executor = WethArbitrageExecutor(config=cfg)

    # Two-hop planning with tax token
    with pytest.raises(TaxTokenProhibitedError):
        executor.plan_two_hop(
            token_b=INDEX_ADDRESS,
            pool1_fee=5.0,
            pool2_fee=5.0,
        )


def test_arbitrage_daemon_filters_tax_tokens_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    """Verify ArbitrageDaemon logs [TAX_BLOCKED] and drops alerts involving tax tokens."""
    from monitors.daemons.arbitrage_daemon import ArbitrageDaemon

    daemon = ArbitrageDaemon(mode="all", min_tvl=100000.0)

    p_tax = PoolSpec(
        address="0x" + "4" * 40,
        label="INDEX/USDG Pool",
        fee_bps=5.0,
        token0=INDEX_ADDRESS,
        token1=USDG_ADDRESS,
    )
    p_clean = PoolSpec(
        address="0x" + "5" * 40,
        label="WETH/USDG Pool",
        fee_bps=5.0,
        token0=WETH_ADDRESS,
        token1=USDG_ADDRESS,
    )

    from arbitrage.spread_monitor import SpreadAlert

    sa_tax = SpreadAlert(
        base=INDEX_ADDRESS,
        quote=USDG_ADDRESS,
        buy_pool=p_tax,
        sell_pool=p_tax,
        buy_price=10.0,
        sell_price=11.0,
        gross_spread_pct=10.0,
        total_fee_pct=0.1,
        net_spread_pct=9.9,
    )

    with caplog.at_level(logging.WARNING):
        daemon.handle_spread_alert(sa_tax)
        assert "[TAX_BLOCKED]" in caplog.text

    # Check triangular alert with tax token
    leg_tax = SwapLeg(
        from_token=WETH_ADDRESS,
        to_token=INDEX_ADDRESS,
        pool=p_tax,
        rate=1.0,
        fee_bps=5.0,
        effective_rate=0.9995,
    )
    ta_tax = TriangularArbAlert(
        start_token=WETH_ADDRESS,
        cycle=(WETH_ADDRESS, INDEX_ADDRESS, USDG_ADDRESS, WETH_ADDRESS),
        legs=[leg_tax, leg_tax, leg_tax],
        gross_multiplier=1.1,
        fee_multiplier=0.99,
        expected_multiplier=1.08,
        slippage_buffer_pct=0.6,
        net_multiplier=1.07,
        gross_profit_pct=10.0,
        net_profit_pct=7.0,
        total_fee_pct=1.0,
    )

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        daemon.handle_triangle_alert(ta_tax)
        assert "[TAX_BLOCKED]" in caplog.text
