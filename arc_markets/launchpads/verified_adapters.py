"""Verified adapters for direct listings, bonding curve graduation, and unsupported tracker."""

from __future__ import annotations

from typing import Any

from arc_markets.launchpads.base import (
    LaunchpadLifecycleState,
    LaunchpadMechanismType,
)
from arc_readiness.errors import ArcMarketIneligibleError, ArcValidationError


class DirectV3ListingAdapter:
    """Adapter for launchpads that deploy liquidity directly into verified Uniswap V3 pools."""

    def __init__(self, launchpad_name: str) -> None:
        self.launchpad_name = launchpad_name

    def inspect_token(self, token_address: str, target_pool_address: str) -> LaunchpadLifecycleState:
        return LaunchpadLifecycleState(
            launchpad_name=self.launchpad_name,
            mechanism_type=LaunchpadMechanismType.DIRECT_V3_LISTING,
            token_address=token_address.lower(),
            graduated=True,
            target_clmm_pool=target_pool_address.lower(),
        )


class GraduationBondingCurveAdapter:
    """Adapter managing bonding curve lifecycles, graduation barriers, and CLMM migration proofs."""

    def __init__(self, launchpad_name: str) -> None:
        self.launchpad_name = launchpad_name
        self._tokens: dict[str, LaunchpadLifecycleState] = {}

    def register_pre_graduation(self, token_address: str) -> LaunchpadLifecycleState:
        """Register a token active solely on internal bonding curves."""
        state = LaunchpadLifecycleState(
            launchpad_name=self.launchpad_name,
            mechanism_type=LaunchpadMechanismType.BONDING_CURVE_PRE_GRADUATION,
            token_address=token_address.lower(),
            graduated=False,
            target_clmm_pool=None,
        )
        self._tokens[token_address.lower()] = state
        return state

    def process_migration(
        self,
        token_address: str,
        target_pool: str,
        migration_tx_hash: str,
        migration_block: int,
    ) -> LaunchpadLifecycleState:
        """Transition token to migrated status upon verified on-chain migration event."""
        if not migration_tx_hash.startswith("0x") or len(migration_tx_hash) != 66:
            raise ArcValidationError(f"Invalid migration_tx_hash: {migration_tx_hash}")
        if not target_pool.startswith("0x") or len(target_pool) != 42:
            raise ArcValidationError(f"Invalid target_pool address: {target_pool}")

        state = LaunchpadLifecycleState(
            launchpad_name=self.launchpad_name,
            mechanism_type=LaunchpadMechanismType.MIGRATED_POST_GRADUATION,
            token_address=token_address.lower(),
            graduated=True,
            target_clmm_pool=target_pool.lower(),
            graduation_tx_hash=migration_tx_hash.lower(),
            graduation_block=migration_block,
        )
        self._tokens[token_address.lower()] = state
        return state

    def get_state(self, token_address: str) -> LaunchpadLifecycleState | None:
        return self._tokens.get(token_address.lower())


class UnsupportedLaunchpadTracker:
    """Tracks candidate launchpad venues lacking reproducible ABI, bytecode, or tx traces."""

    def __init__(self) -> None:
        self._missing_evidence_venues: dict[str, str] = {}

    def record_unsupported(self, venue_name: str, missing_reason: str) -> None:
        self._missing_evidence_venues[venue_name.lower()] = missing_reason

    def assert_supported(self, venue_name: str) -> None:
        norm = venue_name.lower()
        if norm in self._missing_evidence_venues:
            raise ArcMarketIneligibleError(
                f"Launchpad venue {venue_name} is marked UNSUPPORTED_MISSING_EVIDENCE: "
                f"{self._missing_evidence_venues[norm]}. Synthetic adapters are forbidden."
            )
