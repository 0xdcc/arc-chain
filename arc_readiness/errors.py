"""Dedicated exception hierarchy for Arc readiness contracts and validation."""

from __future__ import annotations


class ArcReadinessError(Exception):
    """Base exception for all Arc readiness errors."""


class ArcValidationError(ArcReadinessError, ValueError):
    """Raised when data structure validation fails or constraints are violated."""


class ArcNetworkMismatchError(ArcReadinessError):
    """Raised when observed network parameters mismatch expected Arc identity."""


class ArcDualInterfaceMismatchError(ArcReadinessError):
    """Raised when dual-interface (native 18d vs ERC-20 6d) balances diverge unexpectedly."""


class ArcEventDeduplicationError(ArcReadinessError):
    """Raised when duplicate or conflicting event records are detected."""


class ArcMarketIneligibleError(ArcReadinessError):
    """Raised when an asset, market, or pool is ineligible for Arc operations."""


class ArcContractBridgeError(ArcReadinessError):
    """Raised when bridging, precompile interaction, or contract mapping fails."""
