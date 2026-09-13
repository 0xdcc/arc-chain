"""Arc USDC Wall and OTC Sources Integration Tests (T31)

Verifies:
- Bid and ask orderbook separation
- Pruning of expired quotes (never reuse stale screenshots)
- Payload SHA-256 hash preservation
- Fail-closed defense for private / auth-required gateways
- Channel health tracking and rejection of non-cash loyalty rewards
"""

from __future__ import annotations

import json

import pytest

from arbitrage_contracts.arc_extensions import OtcQuote
from arc_research.wall.channel_status import (
    ChannelHealthStatus,
    OtcChannelMonitor,
)
from arc_research.wall.sources import (
    OtcOrderBook,
    OtcSourceCollector,
    OtcSourceConfig,
    OtcSourceError,
)


class TestArcOtcWallSources:
    """Test suite for T31 OTC Wall Sources and Channels."""

    def test_bid_ask_separation_and_hash_preservation(self) -> None:
        cfg = OtcSourceConfig(venue="circle_public", endpoint_url="https://api.circle.com/public/otc")
        collector = OtcSourceCollector(cfg)

        payload_dict = {
            "bids": [
                {
                    "quote_id": "bid-01",
                    "base_asset": "USDC",
                    "quote_asset": "USD",
                    "amount_in_atoms": 100_000_000,
                    "amount_out_atoms": 100_000_000,
                    "expiry_timestamp": 2000.0,
                }
            ],
            "asks": [
                {
                    "quote_id": "ask-01",
                    "base_asset": "USDC",
                    "quote_asset": "USD",
                    "amount_in_atoms": 50_000_000,
                    "amount_out_atoms": 50_050_000,
                    "expiry_timestamp": 2000.0,
                }
            ],
        }
        raw_json = json.dumps(payload_dict)

        ob = collector.parse_orderbook_payload(raw_json, now_utc=1000.0)
        assert len(ob.bids) == 1
        assert len(ob.asks) == 1
        assert ob.bids[0].side == "buy"
        assert ob.asks[0].side == "sell"
        assert len(ob.raw_payload_hash) == 64
        assert ob.is_executable(now_utc=1000.0) is True

    def test_expired_quotes_pruned(self) -> None:
        cfg = OtcSourceConfig(venue="otc_desk_a", endpoint_url="https://desk-a.com/quotes")
        collector = OtcSourceCollector(cfg)

        # Quote expired at timestamp 900, current time is 1000
        payload_dict = {
            "bids": [
                {
                    "quote_id": "bid-expired",
                    "base_asset": "USDC",
                    "quote_asset": "USD",
                    "amount_in_atoms": 100_000_000,
                    "amount_out_atoms": 99_900_000,
                    "expiry_timestamp": 900.0,
                }
            ],
            "asks": [],
        }
        ob = collector.parse_orderbook_payload(json.dumps(payload_dict), now_utc=1000.0)
        assert len(ob.bids) == 0
        assert ob.is_stale is True
        assert ob.is_executable(now_utc=1000.0) is False

    def test_private_gateway_requires_auth_rejected(self) -> None:
        """Rule: Unauthorized access to private gateways must fail closed."""
        cfg = OtcSourceConfig(
            venue="private_vip_otc",
            endpoint_url="https://vip.otc.com/rfq",
            requires_auth=True,
        )
        collector = OtcSourceCollector(cfg)

        with pytest.raises(OtcSourceError, match="requires authentication; unauthorized private gateway"):
            collector.parse_orderbook_payload("{}", now_utc=1000.0)

    def test_channel_monitor_status_tracking(self) -> None:
        monitor = OtcChannelMonitor(venue="circle_channel")
        # 1. Active channel with cash settlement
        rep1 = monitor.record_probe(
            status=ChannelHealthStatus.ACTIVE,
            latency_ms=45,
            cash_flow_confirmed=True,
            timestamp_utc=1000.0,
        )
        assert rep1.is_reliable() is True

        # 2. Rate limited channel
        rep2 = monitor.record_probe(
            status=ChannelHealthStatus.RATE_LIMITED,
            latency_ms=120,
            rate_limited_seconds=60,
            timestamp_utc=1005.0,
        )
        assert rep2.is_reliable() is False
        assert rep2.rate_limited_until_utc == 1065.0

        # 3. Non-cash reward points cannot be claimed as cash flow
        rep3 = monitor.record_probe(
            status=ChannelHealthStatus.ACTIVE,
            latency_ms=50,
            cash_flow_confirmed=False,
            timestamp_utc=1010.0,
        )
        assert rep3.is_reliable() is False
