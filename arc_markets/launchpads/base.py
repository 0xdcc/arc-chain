"""Launchpad mechanism taxonomy, lifecycle states, and adapter interface contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from arc_readiness.errors import ArcMarketIneligibleError, ArcValidationError


class LaunchpadMechanismType(StrEnum):
    """Classification of token issuance and market introduction mechanisms."""

    DIRECT_V3_LISTING = "direct_v3_listing"
    BONDING_CURVE_PRE_GRADUATION = "bonding_curve_pre_graduation"
    MIGRATED_POST_GRADUATION = "migrated_post_graduation"
    UNSUPPORTED_MISSING_EVIDENCE = "unsupported_missing_evidence"


@dataclass(frozen=True)
class LaunchpadLifecycleState:
    """Verified lifecycle and migration state of a launchpad asset."""

    launchpad_name: str
    mechanism_type: LaunchpadMechanismType
    token_address: str
    graduated: bool
    target_clmm_pool: str | None = None
    graduation_tx_hash: str | None = None
    graduation_block: int | None = None

    def __post_init__(self) -> None:
        clean_token = self.token_address.strip().lower()
        if not clean_token.startswith("0x") or len(clean_token) != 42:
            raise ArcValidationError(f"Invalid token_address: {self.token_address}")

        if self.mechanism_type == LaunchpadMechanismType.MIGRATED_POST_GRADUATION:
            if not self.graduated:
                raise ArcValidationError("MIGRATED_POST_GRADUATION requires graduated=True")
            if not self.target_clmm_pool:
                raise ArcValidationError("MIGRATED_POST_GRADUATION requires verified target_clmm_pool")
            if not self.graduation_tx_hash:
                raise ArcValidationError("MIGRATED_POST_GRADUATION requires graduation_tx_hash")

    @property
    def is_quoteable_on_clmm(self) -> bool:
        """Determines if the token can be routed through standard CLMM graph quoting."""
        if self.mechanism_type == LaunchpadMechanismType.DIRECT_V3_LISTING:
            return True
        if self.mechanism_type == LaunchpadMechanismType.MIGRATED_POST_GRADUATION:
            return self.graduated and self.target_clmm_pool is not None
        # Pre-graduation bonding curves or unsupported mechanisms are strictly unquoteable
        return False

    def assert_quoteable(self) -> None:
        """Fails closed if the token is in bonding curve or unverified status."""
        if not self.is_quoteable_on_clmm:
            raise ArcMarketIneligibleError(
                f"Token {self.token_address} on {self.launchpad_name} is in "
                f"{self.mechanism_type} status (graduated={self.graduated}). "
                "Routing swaps through ungraduated launchpad bonding curves is strictly prohibited."
            )
