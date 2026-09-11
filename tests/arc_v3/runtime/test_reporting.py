"""Tests for Hermes Reporting & Fake Notification Dispatcher (T39)

Covers:
- Reporting generator: JSON metrics, Markdown rendering, 4-tier outcome separation
- Fake notifier: positive net profit delivery, unprofitable suppression, idempotency deduplication
- Safety enforcement: prohibition of real external broadcast without explicit channel auth
"""

import json
import pytest

from arc_runtime.notifications import (
    DeliveryStatus,
    FakeNotificationDispatcher,
    OpportunityNotification,
)
from arc_runtime.reporting import (
    generate_opportunity_json_summary,
    generate_opportunity_markdown_report,
)


class TestHermesReporting:
    """Test suite for reporting and notification dispatch."""

    def test_reporting_json_summary_structure(self) -> None:
        sample_records = [
            {
                "observation_id": "obs-001-abcdef123456",
                "cycle_summary": "USDC -> WETH -> USDC",
                "net_profit_atoms": 500000,
                "status": "quoted",
                "simulation_status": None,
                "output_verified": False,
                "as_of_block": 1001,
            },
            {
                "observation_id": "obs-002-123456abcdef",
                "cycle_summary": "USDC -> PONS -> USDC",
                "net_profit_atoms": -1000,
                "status": "unprofitable",
                "simulation_status": None,
                "output_verified": False,
                "as_of_block": 1001,
            },
            {
                "observation_id": "obs-003-7890abcdef12",
                "cycle_summary": "USDC -> USDG -> USDC",
                "net_profit_atoms": 250000,
                "status": "simulated",
                "simulation_status": "CALL_SUCCEEDED",
                "output_verified": True,
                "as_of_block": 1002,
            },
        ]
        res = generate_opportunity_json_summary(sample_records, candidate_sha="candidate-test-sha")
        summary = res["summary"]

        assert summary["total_observed"] == 3
        assert summary["positive_estimated"] == 2
        assert summary["unprofitable"] == 1
        assert summary["simulated"] == 1
        assert summary["output_verified"] == 1
        assert summary["realized_profit"] == 0  # strictly 0 in research mode

        assert len(res["items"]) == 3
        assert res["items"][0]["tier"] == "ESTIMATED"
        assert res["items"][2]["tier"] == "OUTPUT_VERIFIED"

    def test_reporting_markdown_renders_table(self) -> None:
        sample_records = [
            {
                "observation_id": "obs-001-abcdef123456",
                "cycle_summary": "USDC -> WETH -> USDC",
                "net_profit_atoms": 500000,
                "status": "quoted",
                "as_of_block": 1001,
            }
        ]
        md = generate_opportunity_markdown_report(sample_records, candidate_sha="test-sha-123")
        assert "# Arc Chain Opportunity & Research Report" in md
        assert "USDC -> WETH -> USDC" in md
        assert "REALIZED" in md
        assert "0 ATOMS" in md
        assert "Disclaimers & Safety Enforcements" in md

    def test_fake_notification_dispatcher_delivers_positive_profit(self) -> None:
        dispatcher = FakeNotificationDispatcher(allow_real_send=False)
        notif = OpportunityNotification(
            notification_id="notif-001",
            opportunity_id="opp-100",
            chain_id=5042,
            cycle_summary="USDC -> WETH -> USDC",
            expected_profit_atoms=1000000,
            net_profit_usd=1.00,
            is_estimated=True,
            as_of_block=50000,
            created_at=1726050000.0,
        )
        receipt = dispatcher.dispatch(notif)
        assert receipt.status == DeliveryStatus.DELIVERED_FAKE
        assert receipt.channel == "mock_qq"
        assert "[FAKE_QQ]" in receipt.raw_payload_preview
        assert dispatcher.total_delivered == 1

    def test_fake_notification_suppresses_unprofitable(self) -> None:
        dispatcher = FakeNotificationDispatcher(allow_real_send=False)
        unprofitable_notif = OpportunityNotification(
            notification_id="notif-002",
            opportunity_id="opp-101",
            chain_id=5042,
            cycle_summary="USDC -> PONS -> USDC",
            expected_profit_atoms=0,
            net_profit_usd=-0.05,
            is_estimated=True,
            as_of_block=50000,
            created_at=1726050000.0,
        )
        receipt = dispatcher.dispatch(unprofitable_notif)
        assert receipt.status == DeliveryStatus.SUPPRESSED_UNPROFITABLE
        assert dispatcher.total_delivered == 0

    def test_fake_notification_idempotent_deduplication(self) -> None:
        dispatcher = FakeNotificationDispatcher(allow_real_send=False)
        notif = OpportunityNotification(
            notification_id="notif-003",
            opportunity_id="opp-102",
            chain_id=5042,
            cycle_summary="USDC -> WETH -> USDC",
            expected_profit_atoms=2000000,
            net_profit_usd=2.00,
            is_estimated=True,
            as_of_block=50001,
            created_at=1726050000.0,
        )
        # First send: delivers fake
        r1 = dispatcher.dispatch(notif)
        assert r1.status == DeliveryStatus.DELIVERED_FAKE
        assert dispatcher.total_delivered == 1

        # Second send: duplicate suppressed
        r2 = dispatcher.dispatch(notif)
        assert r2.status == DeliveryStatus.SUPPRESSED_DUPLICATE
        assert dispatcher.total_delivered == 1

    def test_fake_notification_rejects_real_send_without_auth(self) -> None:
        with pytest.raises(PermissionError, match="Real notification broadcast requires"):
            FakeNotificationDispatcher(allow_real_send=True, auth_token=None)

    def test_notification_requires_estimated_flag(self) -> None:
        with pytest.raises(ValueError, match="Pre-execution notifications must explicitly set is_estimated=True"):
            OpportunityNotification(
                notification_id="notif-bad",
                opportunity_id="opp-bad",
                chain_id=5042,
                cycle_summary="A -> B -> A",
                expected_profit_atoms=100,
                net_profit_usd=0.1,
                is_estimated=False,  # illegal pre-execution
                as_of_block=1,
                created_at=100.0,
            )
