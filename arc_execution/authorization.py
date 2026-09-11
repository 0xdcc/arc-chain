"""Arc Execution Authorization and Security Cards (T28)

Enforces:
- Structured authorization card: wallet, target router, max budget, expiry, and config hash
- Immutable hard cap: single transaction NEVER exceeds 500 USD equivalent (500_000_000 atoms for 6 decimals)
- min_output > 0 is strictly mandatory
- Simple CLI boolean flags cannot bypass authorization
- Budget reservation is atomic and fail-closed
- Real signers are NEVER loaded in offline research environments
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import time
from typing import Any


class AuthorizationError(ValueError):
    """Raised when execution request violates authorization card bounds."""


class AuthorizationExpiredError(AuthorizationError):
    """Raised when authorization card has expired."""


class AuthorizationBudgetExceededError(AuthorizationError):
    """Raised when requested amount exceeds remaining authorization budget."""


@dataclass(frozen=True, slots=True)
class ExecutionAuthorizationCard:
    """Immutable authorization card governing restricted transaction execution."""

    auth_id: str
    wallet_address: str
    target_router: str
    chain_id: int
    config_hash: str
    expires_at_utc: float
    total_budget_atoms: int
    spent_budget_atoms: int = 0
    single_tx_cap_atoms: int = 500_000_000  # 500 USD hard cap (6 decimals)
    is_active: bool = True
    offline_mock_only: bool = True  # Real signers are permanently locked out

    def __post_init__(self) -> None:
        if self.chain_id not in (5042, 5042002):
            raise AuthorizationError(f"Invalid chain_id {self.chain_id}; must be Arc mainnet or testnet")
        if self.single_tx_cap_atoms > 500_000_000:
            raise AuthorizationError(
                f"single_tx_cap_atoms ({self.single_tx_cap_atoms}) exceeds global hard cap of 500 USD (500,000,000 atoms)"
            )
        if self.total_budget_atoms < 0:
            raise AuthorizationError("total_budget_atoms cannot be negative")
        if not self.offline_mock_only:
            raise AuthorizationError("offline_mock_only must be True in research environment")

    @property
    def remaining_budget_atoms(self) -> int:
        return max(0, self.total_budget_atoms - self.spent_budget_atoms)

    def is_expired(self, now_utc: float | None = None) -> bool:
        current_time = now_utc if now_utc is not None else time.time()
        return current_time >= self.expires_at_utc

    def validate_request(
        self,
        router_address: str,
        amount_atoms: int,
        min_output_atoms: int,
        config_hash: str,
        now_utc: float | None = None,
    ) -> None:
        """Validate transaction request against card invariants."""
        if not self.is_active:
            raise AuthorizationError(f"Authorization card {self.auth_id} is inactive")

        if self.is_expired(now_utc):
            raise AuthorizationExpiredError(
                f"Authorization card {self.auth_id} expired at {self.expires_at_utc} UTC"
            )

        if self.config_hash != config_hash:
            raise AuthorizationError(
                f"Config hash mismatch: expected {self.config_hash}, got {config_hash}"
            )

        if router_address.lower() != self.target_router.lower():
            raise AuthorizationError(
                f"Router address mismatch: authorized for {self.target_router}, requested {router_address}"
            )

        if amount_atoms <= 0:
            raise AuthorizationError(f"amount_atoms must be strictly positive, got {amount_atoms}")

        if min_output_atoms <= 0:
            raise AuthorizationError(
                f"min_output_atoms must be strictly positive, got {min_output_atoms}"
            )

        if amount_atoms > self.single_tx_cap_atoms:
            raise AuthorizationError(
                f"Requested amount {amount_atoms} exceeds single tx hard cap {self.single_tx_cap_atoms}"
            )

        if amount_atoms > self.remaining_budget_atoms:
            raise AuthorizationBudgetExceededError(
                f"Requested amount {amount_atoms} exceeds remaining budget {self.remaining_budget_atoms}"
            )

    def reserve_budget(self, amount_atoms: int, now_utc: float | None = None) -> ExecutionAuthorizationCard:
        """Atomically deduct budget and return a new updated card instance."""
        if amount_atoms > self.remaining_budget_atoms:
            raise AuthorizationBudgetExceededError(
                f"Cannot reserve {amount_atoms}; remaining budget is {self.remaining_budget_atoms}"
            )
        new_spent = self.spent_budget_atoms + amount_atoms
        return replace(self, spent_budget_atoms=new_spent)
