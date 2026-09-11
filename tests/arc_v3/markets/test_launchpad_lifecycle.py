"""Tests for T17: Launchpad Mechanism Taxonomy, Bonding Curve Lifecycle, and Graduation Verification."""

from __future__ import annotations

import pytest

from arc_markets.launchpads.base import (
    LaunchpadLifecycleState,
    LaunchpadMechanismType,
)
from arc_markets.launchpads.verified_adapters import (
    DirectV3ListingAdapter,
    GraduationBondingCurveAdapter,
    UnsupportedLaunchpadTracker,
)
from arc_readiness.errors import ArcMarketIneligibleError, ArcValidationError

_TOKEN_ADDR = "0x" + "11" * 20
_POOL_ADDR = "0x" + "22" * 20
_TX_HASH = "0x" + "aa" * 32


class TestLaunchpadLifecycle:
    """Test suite for launchpad classification, graduation milestones, and missing evidence gates."""

    def test_direct_v3_listing_quoteable(self) -> None:
        adapter = DirectV3ListingAdapter(launchpad_name="arc_fair_launch")
        state = adapter.inspect_token(token_address=_TOKEN_ADDR, target_pool_address=_POOL_ADDR)

        assert state.mechanism_type == LaunchpadMechanismType.DIRECT_V3_LISTING
        assert state.graduated is True
        assert state.target_clmm_pool == _POOL_ADDR.lower()
        assert state.is_quoteable_on_clmm is True
        state.assert_quoteable()

    def test_bonding_curve_pre_graduation_rejected(self) -> None:
        adapter = GraduationBondingCurveAdapter(launchpad_name="arc_bonding_curve")
        state = adapter.register_pre_graduation(token_address=_TOKEN_ADDR)

        assert state.mechanism_type == LaunchpadMechanismType.BONDING_CURVE_PRE_GRADUATION
        assert state.graduated is False
        assert state.is_quoteable_on_clmm is False

        with pytest.raises(ArcMarketIneligibleError, match="Routing swaps through ungraduated launchpad"):
            state.assert_quoteable()

    def test_graduation_transition_unlocks_quoteability(self) -> None:
        adapter = GraduationBondingCurveAdapter(launchpad_name="arc_bonding_curve")
        adapter.register_pre_graduation(token_address=_TOKEN_ADDR)

        grad_state = adapter.process_migration(
            token_address=_TOKEN_ADDR,
            target_pool=_POOL_ADDR,
            migration_tx_hash=_TX_HASH,
            migration_block=5000,
        )

        assert grad_state.mechanism_type == LaunchpadMechanismType.MIGRATED_POST_GRADUATION
        assert grad_state.graduated is True
        assert grad_state.graduation_tx_hash == _TX_HASH.lower()
        assert grad_state.graduation_block == 5000
        assert grad_state.is_quoteable_on_clmm is True
        grad_state.assert_quoteable()

    def test_invalid_migration_payload_fails_validation(self) -> None:
        adapter = GraduationBondingCurveAdapter(launchpad_name="arc_bonding_curve")

        with pytest.raises(ArcValidationError, match="Invalid migration_tx_hash"):
            adapter.process_migration(
                token_address=_TOKEN_ADDR,
                target_pool=_POOL_ADDR,
                migration_tx_hash="0xinvalid",
                migration_block=5000,
            )

    def test_unsupported_tracker_fails_closed(self) -> None:
        tracker = UnsupportedLaunchpadTracker()
        tracker.record_unsupported(
            venue_name="unverified_launchpad",
            missing_reason="Missing verified deployment and public ABI",
        )

        with pytest.raises(ArcMarketIneligibleError, match="UNSUPPORTED_MISSING_EVIDENCE"):
            tracker.assert_supported("unverified_launchpad")
