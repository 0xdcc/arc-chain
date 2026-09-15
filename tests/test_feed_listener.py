"""Unit tests for Robinhood Sequencer Feed and WS RPC real-time event listener."""

from __future__ import annotations

import json
import logging
import time
from unittest.mock import MagicMock, patch

import pytest

from research.market_data.feed_coordinator import FeedCoordinator as ArbitrageDaemon
from research.market_data.feed_listener import FeedEvent, FeedListener
from research.market_data.multicall import PoolSpec, PriceQuote
from research.market_data.token_policy import KNOWN_TAX_TOKENS

INDEX_ADDRESS = "0x56910d4409f3a0c78c64dd8d0545ff0705389870"
SAMPLE_POOL = "0x1111111111111111111111111111111111111111"


def test_feed_event_structure() -> None:
    """Verify FeedEvent dataclass structure, fields, and default values."""
    event = FeedEvent(source="sequencer_feed", event_type="sequence", sequence_number=42)
    assert event.source == "sequencer_feed"
    assert event.event_type == "sequence"
    assert event.sequence_number == 42
    assert event.block_number is None
    assert event.touched_pools == []
    assert isinstance(event.timestamp, float)
    assert event.raw_data == {}


def test_parse_sequencer_message_batch() -> None:
    """Verify parsing Nitro Sequencer Feed batch messages."""
    listener = FeedListener(feed_url=None, ws_rpc_url=None)

    nitro_payload = {
        "version": 1,
        "messages": [
            {
                "sequenceNumber": 5001,
                "message": {
                    "header": {"number": 123456},
                    "data": "0xmockdata1",
                },
            },
            {
                "sequenceNumber": 5002,
                "message": {
                    "header": {"number": 123457},
                    "data": "0xmockdata2",
                },
            },
        ],
    }

    events = listener.parse_sequencer_message(json.dumps(nitro_payload))
    assert len(events) == 2
    assert events[0].source == "sequencer_feed"
    assert events[0].event_type == "sequence"
    assert events[0].sequence_number == 5001
    assert events[0].block_number == 123456
    assert events[1].sequence_number == 5002
    assert events[1].block_number == 123457


def test_parse_sequencer_message_touched_pool() -> None:
    """Verify detecting monitored pool addresses touched by sequencer messages."""
    listener = FeedListener(
        feed_url=None,
        ws_rpc_url=None,
        monitored_pools=[SAMPLE_POOL],
    )

    nitro_msg = {
        "version": 1,
        "messages": [
            {
                "sequenceNumber": 9999,
                "message": {
                    "header": {"blockNumber": 200000},
                    "to": SAMPLE_POOL,
                },
            }
        ],
    }

    events = listener.parse_sequencer_message(json.dumps(nitro_msg))
    assert len(events) == 1
    assert SAMPLE_POOL.lower() in events[0].touched_pools


def test_parse_ws_rpc_message_new_heads() -> None:
    """Verify parsing standard EVM newHeads subscription notification."""
    listener = FeedListener(feed_url=None, ws_rpc_url=None)

    new_heads_payload = {
        "jsonrpc": "2.0",
        "method": "eth_subscription",
        "params": {
            "subscription": "0x12345",
            "result": {
                "number": "0x3039",  # 12345 in hex
                "hash": "0xdeadbeef",
                "parentHash": "0xfeedface",
            },
        },
    }

    events = listener.parse_ws_rpc_message(json.dumps(new_heads_payload))
    assert len(events) == 1
    assert events[0].source == "ws_rpc"
    assert events[0].event_type == "block"
    assert events[0].block_number == 12345


def test_parse_ws_rpc_message_swap_log() -> None:
    """Verify parsing standard EVM logs subscription notification."""
    listener = FeedListener(feed_url=None, ws_rpc_url=None)

    log_payload = {
        "jsonrpc": "2.0",
        "method": "eth_subscription",
        "params": {
            "subscription": "0x67890",
            "result": {
                "address": SAMPLE_POOL,
                "topics": ["0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"],
                "blockNumber": "0x303a",  # 12346
            },
        },
    }

    events = listener.parse_ws_rpc_message(json.dumps(log_payload))
    assert len(events) == 1
    assert events[0].source == "ws_rpc"
    assert events[0].event_type == "swap"
    assert events[0].block_number == 12346
    assert events[0].touched_pools == [SAMPLE_POOL.lower()]


def test_callback_dispatch_and_error_resilience() -> None:
    """Verify callback registration, dispatch, and isolation against individual callback errors."""
    listener = FeedListener(feed_url=None, ws_rpc_url=None)
    called_events: list[FeedEvent] = []

    def failing_callback(_event: FeedEvent) -> None:
        raise RuntimeError("Callback crash")

    def healthy_callback(event: FeedEvent) -> None:
        called_events.append(event)

    listener.register_callback(failing_callback)
    listener.register_callback(healthy_callback)

    test_event = FeedEvent(source="ws_rpc", event_type="block", block_number=100)
    listener.dispatch_event(test_event)

    assert len(called_events) == 1
    assert called_events[0].block_number == 100


def test_listener_start_and_stop_lifecycle() -> None:
    """Verify thread lifecycle start and clean stop."""
    listener = FeedListener(feed_url=None, ws_rpc_url=None)
    assert listener.is_running() is False

    listener.start()
    # Background thread is alive (running empty task or loop)
    assert listener._running is True

    listener.stop(timeout=1.0)
    assert listener.is_running() is False
    assert listener._running is False


def test_daemon_on_feed_event_wake_on_block() -> None:
    """Verify ArbitrageDaemon wakes immediately on new block feed events."""
    daemon = ArbitrageDaemon(mode="spread", min_tvl=100000.0)
    daemon.running = True

    # Initially wake_event is clear
    daemon._wake_event.clear()
    assert daemon._wake_event.is_set() is False

    event = FeedEvent(source="ws_rpc", event_type="block", block_number=54321)
    daemon.on_feed_event(event)

    # Event should be set to interrupt sleep
    assert daemon._wake_event.is_set() is True


def test_daemon_on_feed_event_triggers_fast_probe() -> None:
    """Verify ArbitrageDaemon triggers fast probe on touched pool event."""
    daemon = ArbitrageDaemon(mode="spread", min_tvl=100000.0)
    daemon.running = True

    pool1 = PoolSpec(
        address=SAMPLE_POOL,
        label="Pool 1",
        fee_bps=5.0,
        token0="0x" + "a" * 40,
        token1="0x" + "b" * 40,
    )
    daemon.pools = [pool1]

    with patch.object(daemon, "_probe_touched_pools") as mock_probe:
        event = FeedEvent(source="ws_rpc", event_type="swap", touched_pools=[SAMPLE_POOL])
        daemon.on_feed_event(event)
        assert mock_probe.called
        assert mock_probe.call_args[0][0] == [SAMPLE_POOL]


def test_daemon_on_feed_event_blocks_tax_tokens(caplog: pytest.LogCaptureFixture) -> None:
    """Verify ArbitrageDaemon blocks touched pool events involving tax tokens."""
    daemon = ArbitrageDaemon(mode="spread", min_tvl=100000.0)
    daemon.running = True

    with (
        patch.object(daemon, "_probe_touched_pools") as mock_probe,
        caplog.at_level(logging.WARNING),
    ):
        event = FeedEvent(source="ws_rpc", event_type="swap", touched_pools=[INDEX_ADDRESS])
        daemon.on_feed_event(event)
        mock_probe.assert_not_called()
        assert "[TAX_BLOCKED]" in caplog.text
