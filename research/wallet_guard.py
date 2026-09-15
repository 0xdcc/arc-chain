"""Offline legacy policy calculations only; never authorizes execution.

Slippage-discount output is not the execution principal+gas+1 floor.
"""
import math
from fractions import Fraction
from pathlib import Path
from typing import Any

from arc_readiness.file_hygiene import InsecureFileError, check_private_file_metadata


class WalletGuardError(Exception):
    """Base exception for all wallet guard safety violations."""

    pass


class TaxTokenProhibitedError(WalletGuardError):
    """Raised when a trade or path involves a token with transfer tax/fee-on-transfer."""

    pass


class ExcessiveAmountError(WalletGuardError):
    """Raised when trade amount exceeds the hard per-transaction limit (500U)."""

    pass


class InvalidSlippageError(WalletGuardError):
    """Raised when slippage is outside the safe range (0.1% ~ 5.0%)."""

    pass


class ZeroSlippageError(InvalidSlippageError):
    """Raised when slippage is zero or leads to min_amount_out = 0 (MEV sandwich vector)."""

    pass


class InsecureKeyFileError(WalletGuardError):
    """Raised when private key file permissions do not strictly match 0600."""

    pass


class DryRunInterception(WalletGuardError):
    """Raised when live broadcast is attempted while dry-run mode is engaged."""

    pass


KNOWN_TAX_TOKENS: dict[str, float] = {
    "0x56910d4409f3a0c78c64dd8d0545ff0705389870".lower(): 3.0,  # Index (3% transfer tax)
    "0x0d257ca40d40090be60c2d2ed5bb3535392838cc".lower(): 5.0,  # Hood10 (5% transfer tax)
}

KNOWN_TAX_SYMBOLS: set[str] = {"INDEX", "HOOD10"}


def is_tax_token(token_address_or_symbol: str) -> bool:
    """Check whether a token address or symbol has known transfer tax.

    Args:
        token_address_or_symbol: Token EVM contract address or symbol.

    Returns:
        True if token is known to impose transfer fee / tax.
    """
    if not token_address_or_symbol:
        return False
    clean = token_address_or_symbol.strip()
    if clean.upper() in KNOWN_TAX_SYMBOLS:
        return True
    return clean.lower() in KNOWN_TAX_TOKENS


class WalletGuard:
    """Security guardrail enforcing risk controls before any transaction dispatch."""

    # Absolute hard limit across the system (500 USD)
    HARD_CAP_MAX_USD: float = 500.0
    MIN_SAFE_SLIPPAGE_PCT: float = 0.1
    MAX_SAFE_SLIPPAGE_PCT: float = 5.0

    def __init__(
        self,
        max_amount_usd: float = 500.0,
        min_slippage_pct: float = 0.1,
        max_slippage_pct: float = 5.0,
        dry_run: bool = True,
        known_tax_tokens: dict[str, float] | None = None,
    ) -> None:
        if any(
            isinstance(value, bool) or not math.isfinite(value) or value <= 0
            for value in (max_amount_usd, min_slippage_pct, max_slippage_pct)
        ):
            raise ValueError("Wallet limits must be finite positive numbers")
        # Never allow configuring a limit higher than the hard cap
        self.max_amount_usd = min(float(max_amount_usd), self.HARD_CAP_MAX_USD)
        self.min_slippage_pct = max(float(min_slippage_pct), self.MIN_SAFE_SLIPPAGE_PCT)
        self.max_slippage_pct = min(float(max_slippage_pct), self.MAX_SAFE_SLIPPAGE_PCT)
        if type(dry_run) is not bool:
            raise ValueError("dry_run must be boolean")
        if self.min_slippage_pct > self.max_slippage_pct:
            raise ValueError("Inverted slippage bounds")
        self.dry_run = dry_run
        self.known_tax_tokens: dict[str, float] = dict(KNOWN_TAX_TOKENS)
        if known_tax_tokens:
            for k, v in known_tax_tokens.items():
                if isinstance(v, bool) or not math.isfinite(v) or v < 0:
                    raise ValueError("Invalid tax rate")
                self.known_tax_tokens[k.lower()] = max(self.known_tax_tokens.get(k.lower(), 0.0), float(v))

    def is_tax_token(self, token_address: str) -> bool:
        """Check whether a token address or symbol has a known transfer tax."""
        if not token_address:
            return False
        clean = token_address.strip()
        if clean.upper() in KNOWN_TAX_SYMBOLS:
            return True
        return clean.lower() in self.known_tax_tokens

    def validate_token_tax(self, token_address: str, fee_rate: float = 0.0) -> None:
        """Enforce physical block against transfer-tax / fee-on-transfer tokens.

        Args:
            token_address: Token contract address or symbol.
            fee_rate: Known or declared fee rate percentage (e.g. 3.0 for 3%).

        Raises:
            TaxTokenProhibitedError: If token has transfer tax or is in known tax registry.
        """
        if isinstance(fee_rate, bool) or not math.isfinite(fee_rate) or fee_rate < 0:
            raise ValueError("Invalid tax rate")
        clean = token_address.strip() if token_address else ""
        known_fee = self.known_tax_tokens.get(clean.lower(), 0.0)
        effective_tax = max(float(fee_rate), known_fee)
        if effective_tax > 0.0 or self.is_tax_token(token_address):
            tax_display = effective_tax if effective_tax > 0.0 else 3.0
            raise TaxTokenProhibitedError(
                f"Token {token_address} is prohibited: transfer tax rate is {tax_display:.2f}%. "
                "Fee-on-transfer tokens are strictly banned from arbitrage closed loops."
            )

    def assert_tokens_tax_free(self, tokens: list[str]) -> None:
        """Validate a list of token addresses, blocking immediately if any has transfer tax.

        Args:
            tokens: List of token addresses or symbols.

        Raises:
            TaxTokenProhibitedError: If any token has transfer tax.
        """
        for token in tokens:
            self.validate_token_tax(token)

    def validate_amount(self, amount_usd: float) -> float:
        """Validate transaction value against hard limits.

        Args:
            amount_usd: Value of the trade in USD.

        Returns:
            Validated amount_usd.

        Raises:
            ValueError: If amount is non-positive.
            ExcessiveAmountError: If amount exceeds maximum limit.
        """
        if isinstance(amount_usd, bool) or not math.isfinite(amount_usd) or amount_usd <= 0:
            raise ValueError(f"Trade amount must be strictly greater than 0, got {amount_usd}")

        if amount_usd > self.max_amount_usd:
            raise ExcessiveAmountError(
                f"Trade amount ${amount_usd:.2f} USD exceeds safety limit "
                f"of ${self.max_amount_usd:.2f} USD (Hard cap: ${self.HARD_CAP_MAX_USD:.2f} USD)."
            )

        return float(amount_usd)

    def validate_slippage(self, slippage_pct: float) -> float:
        """Enforce strict slippage bounds.

        Eliminates zero slippage (0 min_amount_out) and absurdly loose slippage.

        Args:
            slippage_pct: Slippage percentage (e.g., 1.5 for 1.5%).

        Returns:
            Validated slippage percentage.

        Raises:
            ZeroSlippageError: If slippage is 0 or negative.
            InvalidSlippageError: If slippage is outside [min_slippage_pct, max_slippage_pct].
        """
        if isinstance(slippage_pct, bool) or not math.isfinite(slippage_pct):
            raise InvalidSlippageError("Slippage must be a finite number")
        if slippage_pct <= 0.0:
            raise ZeroSlippageError(
                f"Slippage cannot be 0% or negative ({slippage_pct}%). "
                "Hardcoding 0 slippage exposes trades to MEV sandwich attacks!"
            )

        if slippage_pct < self.min_slippage_pct:
            raise InvalidSlippageError(
                f"Slippage {slippage_pct}% is below minimum safe threshold ({self.min_slippage_pct}%)."
            )

        if slippage_pct > self.max_slippage_pct:
            raise InvalidSlippageError(
                f"Slippage {slippage_pct}% exceeds maximum safe threshold ({self.max_slippage_pct}%)."
            )

        return float(slippage_pct)

    def calculate_min_amount_out(self, expected_amount_out: int, slippage_pct: float) -> int:
        """Calculate guaranteed minimum output with strict non-zero protection.

        Formula: min_amount_out = int(expected_amount_out * (1 - slippage_pct / 100))

        Args:
            expected_amount_out: Expected output token units (in wei/smallest unit).
            slippage_pct: Slippage percentage.

        Returns:
            Calculated non-zero min_amount_out.

        Raises:
            ValueError: If expected_amount_out is not positive.
            ZeroSlippageError: If calculated amount_out_min <= 0.
        """
        if type(expected_amount_out) is not int or expected_amount_out <= 0:
            raise ValueError(
                f"Expected amount out must be a positive integer, got {expected_amount_out}"
            )

        self.validate_slippage(slippage_pct)

        factor = 1 - Fraction(str(slippage_pct)) / 100
        amount_out_min = expected_amount_out * factor.numerator // factor.denominator

        # IRON RULE: Strictly forbid amount_out_min = 0
        if amount_out_min <= 0:
            raise ZeroSlippageError(
                f"Calculated amount_out_min is {amount_out_min}. "
                "A minimum output of 0 is strictly forbidden to prevent MEV sandwich attacks."
            )

        return amount_out_min

    def check_private_key_file(self, file_path: str | Path) -> Path:
        """Inspect metadata only; this is not a key loader or execution authorization."""
        path = Path(file_path)
        path.lstat()
        try:
            check_private_file_metadata(path)
        except InsecureFileError as exc:
            raise InsecureKeyFileError(str(exc)) from exc
        return path.resolve()

    def is_dry_run(self) -> bool:
        """Check if dry-run simulation mode is active."""
        return self.dry_run

    def assert_can_broadcast(self) -> None:
        """Ensure live transaction dispatch is allowed.

        Raises:
            DryRunInterception: If engine is operating in dry-run mode.
        """
        if self.dry_run:
            raise DryRunInterception(
                "Live transaction execution blocked by WalletGuard: Dry-run simulation mode is active."
            )

    def verify_trade(
        self,
        amount_usd: float,
        slippage_pct: float,
        expected_amount_out: int | None = None,
        tokens: list[str] | None = None,
    ) -> dict[str, Any]:
        """Perform comprehensive pre-flight verification on a proposed trade.

        Returns:
            Dict containing verification outcomes.
        """
        valid_amount = self.validate_amount(amount_usd)
        valid_slippage = self.validate_slippage(slippage_pct)

        if tokens is not None:
            self.assert_tokens_tax_free(tokens)

        min_out: int | None = None
        if expected_amount_out is not None:
            min_out = self.calculate_min_amount_out(expected_amount_out, valid_slippage)

        return {
            "verified": True,
            "amount_usd": valid_amount,
            "slippage_pct": valid_slippage,
            "min_amount_out": min_out,
            "tokens": tokens,
            "dry_run": self.dry_run,
        }
