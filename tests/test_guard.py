"""Unit tests for WalletGuard funds safety and risk control guardrails."""

import os
import stat
import tempfile

import pytest
from core.wallet_guard import (
    DryRunInterception,
    ExcessiveAmountError,
    InsecureKeyFileError,
    InvalidSlippageError,
    WalletGuard,
    ZeroSlippageError,
)


def test_amount_within_limit():
    """Verify that trade amounts within the 500U limit pass validation."""
    guard = WalletGuard(max_amount_usd=500.0)
    assert guard.validate_amount(50.0) == 50.0
    assert guard.validate_amount(500.0) == 500.0


def test_amount_exceeds_hard_limit():
    """Verify that trades exceeding 500U are intercepted with ExcessiveAmountError."""
    guard = WalletGuard(max_amount_usd=500.0)
    with pytest.raises(ExcessiveAmountError) as exc_info:
        guard.validate_amount(500.01)
    assert "exceeds safety limit" in str(exc_info.value)

    with pytest.raises(ExcessiveAmountError):
        guard.validate_amount(1000.0)


def test_hard_cap_cannot_be_exceeded():
    """Verify that configuring a higher limit still clamps to the 500U hard cap."""
    guard = WalletGuard(max_amount_usd=2000.0)
    assert guard.max_amount_usd == 500.0
    with pytest.raises(ExcessiveAmountError):
        guard.validate_amount(501.0)


def test_non_positive_amount():
    """Verify that zero or negative trade amounts raise ValueError."""
    guard = WalletGuard()
    with pytest.raises(ValueError):
        guard.validate_amount(0.0)
    with pytest.raises(ValueError):
        guard.validate_amount(-50.0)


def test_slippage_within_range():
    """Verify that slippage in [0.1%, 5.0%] is accepted."""
    guard = WalletGuard()
    assert guard.validate_slippage(0.1) == 0.1
    assert guard.validate_slippage(1.0) == 1.0
    assert guard.validate_slippage(1.5) == 1.5
    assert guard.validate_slippage(5.0) == 5.0


def test_slippage_below_minimum():
    """Verify that slippage below 0.1% raises InvalidSlippageError."""
    guard = WalletGuard()
    with pytest.raises(InvalidSlippageError) as exc_info:
        guard.validate_slippage(0.05)
    assert "below minimum safe threshold" in str(exc_info.value)


def test_slippage_above_maximum():
    """Verify that slippage above 5.0% raises InvalidSlippageError."""
    guard = WalletGuard()
    with pytest.raises(InvalidSlippageError) as exc_info:
        guard.validate_slippage(5.1)
    assert "exceeds maximum safe threshold" in str(exc_info.value)


def test_zero_slippage_strictly_rejected():
    """Verify the iron rule: 0% slippage is strictly forbidden to eradicate MEV sandwich attacks."""
    guard = WalletGuard()
    with pytest.raises(ZeroSlippageError) as exc_info:
        guard.validate_slippage(0.0)
    assert "Slippage cannot be 0%" in str(exc_info.value)

    with pytest.raises(ZeroSlippageError):
        guard.validate_slippage(-0.5)


def test_calculate_min_amount_out_standard():
    """Verify correct calculation of guaranteed minimum output."""
    guard = WalletGuard()
    # 1,000,000 with 1.0% slippage -> 990,000
    assert guard.calculate_min_amount_out(1_000_000, 1.0) == 990_000

    # 10,000,000 with 1.5% slippage -> 9,850,000
    assert guard.calculate_min_amount_out(10_000_000, 1.5) == 9_850_000

    # 200,000 with 0.1% slippage -> 199,800
    assert guard.calculate_min_amount_out(200_000, 0.1) == 199_800


def test_zero_min_amount_out_forbidden():
    """Verify that calculation resulting in 0 min_amount_out is strictly intercepted."""
    guard = WalletGuard()
    with pytest.raises(ValueError):
        guard.calculate_min_amount_out(0, 1.0)

    with pytest.raises(ZeroSlippageError):
        # 0 expected output or 0 slippage
        guard.calculate_min_amount_out(1_000_000, 0.0)


def test_private_key_file_permissions_600():
    """Verify that a private key file with 0600 permissions passes inspection."""
    guard = WalletGuard()
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp_path = tmp.name
        tmp.write(b"0x0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")

    try:
        os.chmod(tmp_path, 0o600)
        assert guard.check_private_key_file(tmp_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def test_private_key_file_insecure_permissions_644():
    """Verify that private key files with 0644 permissions are rejected."""
    guard = WalletGuard()
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp_path = tmp.name
        tmp.write(b"0xsecretkey")

    try:
        os.chmod(tmp_path, 0o644)
        with pytest.raises(InsecureKeyFileError) as exc_info:
            guard.check_private_key_file(tmp_path)
        assert "insecure permissions" in str(exc_info.value)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def test_private_key_file_insecure_permissions_777():
    """Verify that private key files with 0777 permissions are rejected."""
    guard = WalletGuard()
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp_path = tmp.name

    try:
        os.chmod(tmp_path, 0o777)
        with pytest.raises(InsecureKeyFileError):
            guard.check_private_key_file(tmp_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def test_dry_run_control():
    """Verify that dry-run mode intercepts live dispatch attempts."""
    guard = WalletGuard(dry_run=True)
    assert guard.is_dry_run() is True
    with pytest.raises(DryRunInterception) as exc_info:
        guard.assert_can_broadcast()
    assert "Dry-run simulation mode is active" in str(exc_info.value)

    guard_unlocked = WalletGuard(dry_run=False)
    assert guard_unlocked.is_dry_run() is False
    # Should not raise
    guard_unlocked.assert_can_broadcast()


def test_verify_trade_composite():
    """Verify full composite pre-flight validation."""
    guard = WalletGuard()
    report = guard.verify_trade(amount_usd=250.0, slippage_pct=2.0, expected_amount_out=1_000_000)
    assert report["verified"] is True
    assert report["amount_usd"] == 250.0
    assert report["slippage_pct"] == 2.0
    assert report["min_amount_out"] == 980_000
    assert report["dry_run"] is True
