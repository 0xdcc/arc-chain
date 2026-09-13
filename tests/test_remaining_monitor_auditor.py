"""Monitor/auditor regressions; deterministic fixtures, no real sends or RPC."""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from arbitrage.feed_listener import FeedEvent, FeedListener
from arbitrage.spread_monitor import PoolReader, PoolSpec, PriceQuote, find_spreads
from arbitrage.v4_reader import POOL_MANAGER_ADDRESS, SWAP_EVENT_TOPIC0
from execution.audit_verifier import ReceiptAuditor
from execution.funds import BASES, PriceEvidence
from monitors.daemons.arbitrage_daemon import ArbitrageDaemon
from monitors.daemons.chain_auditor_watcher import format_audit_card

from tests.test_remaining_funds import BLOCK, ROUTER, TX, WALLET, receipt


def quote(index, price, fee=5):
    pool = PoolSpec(
        "0x" + f"{index:040x}",
        f"pool {index}",
        fee,
        token0=BASES["WETH"][0],
        token1=BASES["USDG"][0],
        tvl_usd=100000,
    )
    return PriceQuote(
        pool,
        pool.token0,
        pool.token1,
        price,
        price,
        block_number=10,
        block_hash=BLOCK,
        block_timestamp=100,
        ts=100,
    )


@pytest.mark.parametrize("bad_price,bad_fee", [(99, 800), (1, 1)])
def test_bad_extreme_cannot_suppress_profitable_pair(bad_price, bad_fee):
    good_a, good_b = quote(1, 100), quote(2, 102)
    candidate = find_spreads([good_a, good_b, quote(3, bad_price, bad_fee)])
    assert len(candidate) == 1
    assert candidate[0].buy_pool == good_a.pool
    assert candidate[0].sell_pool == good_b.pool


def test_partial_multicall_fills_only_missing_pool():
    first, second = quote(1, 100), quote(2, 102)
    reader = PoolReader(rpc=MagicMock())
    reader.batch_quote_multicall = MagicMock(return_value=[first])
    reader.get_latest_block_number = MagicMock(return_value=10)
    reader.quote = MagicMock(return_value=second)
    result = reader.batch_quote([first.pool, second.pool])
    assert result == [first, second]
    reader.quote.assert_called_once_with(second.pool, block_number=10)
    assert reader.last_coverage == {"total": 2, "successful": 2, "failed": 0}


def test_v4_subscription_and_topic_id_not_manager_address():
    pool_id = "0x" + "66" * 32
    listener = FeedListener(feed_url=None, ws_rpc_url=None, monitored_pools=[pool_id])
    assert listener.log_subscriptions() == [
        {"address": POOL_MANAGER_ADDRESS, "topics": [SWAP_EVENT_TOPIC0, [pool_id]]}
    ]
    data = {
        "method": "eth_subscription",
        "params": {
            "result": {
                "address": POOL_MANAGER_ADDRESS,
                "topics": [SWAP_EVENT_TOPIC0, pool_id],
                "blockNumber": "0xa",
                "removed": False,
            }
        },
    }
    assert listener.parse_ws_rpc_message(data)[0].touched_pools == [pool_id]
    data["params"]["result"]["removed"] = True
    assert listener.parse_ws_rpc_message(data) == []


def test_network_queue_never_calls_slow_callback_and_is_bounded():
    callback = MagicMock()
    listener = FeedListener(feed_url=None, ws_rpc_url=None, on_event=callback)
    for _ in range(1030):
        listener.enqueue_event(FeedEvent("ws_rpc", "block"))
    callback.assert_not_called()
    assert listener._event_queue.qsize() == 1024
    assert listener.dropped_events == 6
    assert listener._rescan_required.is_set()


def test_single_changed_pool_reads_competitor_and_flushes_last_dirty_event():
    first, second = quote(1, 100), quote(2, 102)
    daemon = ArbitrageDaemon(mode="spread", auto_execute=False)
    daemon.running = True
    daemon.pools = [first.pool, second.pool]
    daemon.reader.quote = MagicMock(side_effect=lambda p: first if p == first.pool else second)
    daemon.handle_spread_alert = MagicMock()
    with patch("monitors.daemons.arbitrage_daemon.time.time", return_value=100):
        daemon._probe_touched_pools([first.pool.address])
    assert daemon.reader.quote.call_count == 2
    daemon.handle_spread_alert.assert_called_once()
    with patch("monitors.daemons.arbitrage_daemon.time.time", return_value=100.01):
        daemon._probe_touched_pools([first.pool.address])
    assert daemon.reader.quote.call_count == 2
    assert first.pool.address in daemon._dirty_probe_pools
    with patch("monitors.daemons.arbitrage_daemon.time.time", return_value=102):
        daemon.flush_dirty_probes()
    assert daemon.reader.quote.call_count == 4
    assert not daemon._dirty_probe_pools


def test_real_feed_callback_enqueues_and_trade_worker_queue_is_bounded():
    daemon = ArbitrageDaemon(auto_execute=False)
    daemon.on_feed_event = MagicMock()
    daemon._enqueue_feed_event(FeedEvent("ws_rpc", "swap", touched_pools=[ROUTER]))
    daemon.on_feed_event.assert_not_called()
    assert daemon._feed_events.qsize() == 1
    daemon._trade_thread = MagicMock()
    daemon._try_auto_snipe_spread = MagicMock()
    daemon._dispatch_trade("spread", quote(1, 100))
    daemon._dispatch_trade("spread", quote(2, 102))
    daemon._try_auto_snipe_spread.assert_not_called()
    assert daemon._trade_requests.qsize() == 1
    assert daemon.trade_requests_dropped == 1


def auditor_rpc(*, wrong_wallet=False, reorg=False):
    data = receipt()
    tx = {
        "hash": TX,
        "chainId": "0x1237",
        "from": ROUTER if wrong_wallet else WALLET,
        "to": ROUTER,
        "value": "0x0",
        "blockNumber": "0xa",
        "blockHash": BLOCK,
    }

    def rpc(method, params):
        return {
            "eth_chainId": "0x1237",
            "eth_blockNumber": "0xb",
            "eth_getTransactionByHash": tx,
            "eth_getTransactionReceipt": data,
            "eth_getBlockByNumber": {
                "number": "0xa",
                "timestamp": "0x64",
                "hash": ("0x" + "77" * 32) if reorg else BLOCK,
            },
        }[method]

    return rpc


def make_auditor(rpc):
    def price(token, block):
        return PriceEvidence(
            token,
            Decimal(1 if token == BASES["USDG"][0] else 2500),
            10,
            BLOCK,
            100,
            "independent-fixture",
        )

    return ReceiptAuditor(
        rpc, wallet=WALLET, router=ROUTER, counterparties={ROUTER}, price_reader=price
    )


def test_event_cannot_self_report_confirmation_or_net_profit():
    event = {
        "event_type": "LIVE_EXECUTION_SUCCESS",
        "tx_hash": TX,
        "base_symbol": "USDG",
        "verification": "VERIFIED",
        "real_profit_usd": 9999,
    }
    assert "PENDING" in format_audit_card(event)
    verdict = make_auditor(auditor_rpc()).verify(event)
    assert verdict.state == "VERIFIED"
    assert verdict.accounting["net_profit_usd"] == Decimal("-.15")
    card = format_audit_card(event, verdict)
    assert "-0.150000" in card
    assert "9999" not in card
    assert "已暂停" not in card


@pytest.mark.parametrize(
    "kwargs,state", [({"wrong_wallet": True}, "REJECTED"), ({"reorg": True}, "PENDING")]
)
def test_wrong_wallet_and_noncanonical_receipt_not_verified(kwargs, state):
    event = {"tx_hash": TX, "base_symbol": "USDG"}
    assert make_auditor(auditor_rpc(**kwargs)).verify(event).state == state


def test_fake_hash_or_test_event_never_queries_chain():
    rpc = MagicMock()
    auditor = make_auditor(rpc)
    assert auditor.verify({"tx_hash": "0xprofit_probe", "base_symbol": "USDG"}).state == "REJECTED"
    assert auditor.verify({"tx_hash": TX, "base_symbol": "USDG", "test": True}).state == "REJECTED"
    rpc.assert_not_called()


def test_real_monitor_multicall_entry_binds_hash_time_and_active_liquidity():
    from arbitrage.multicall_reader import MulticallPoolReader
    from arbitrage.v4_reader import V4PoolSpec
    from eth_abi import encode

    pool = V4PoolSpec(
        address="0x" + "77" * 32,
        label="WETH/USDG",
        fee_bps=1,
        token0=BASES["WETH"][0],
        token1=BASES["USDG"][0],
        dec0=18,
        dec1=6,
    )
    header = {"number": "0xa", "hash": BLOCK, "timestamp": "0x64"}
    replies = [
        (True, encode(["uint256"], [10])),
        (True, encode(["uint160", "int24", "uint24", "uint24"], [2**96, 0, 0, 100])),
        (True, encode(["uint128"], [123456])),
    ]
    calls = []

    def rpc_call(method, params):
        calls.append((method, params))
        if method == "eth_getBlockByNumber":
            return {"result": header}
        assert method == "eth_call"
        assert params[1] == {"blockHash": BLOCK, "requireCanonical": True}
        return {"result": "0x" + encode(["(bool,bytes)[]"], [replies]).hex()}

    rpc = MagicMock()
    rpc.call.side_effect = rpc_call
    reader = PoolReader.__new__(PoolReader)
    reader.proxy_url = None
    reader._rpc = rpc
    reader._multicall_reader = MulticallPoolReader(rpc=rpc)
    results = reader.batch_quote([pool])
    assert len(results) == 1
    assert results[0].block_hash == BLOCK
    assert results[0].block_timestamp == 100
    assert results[0].active_liquidity == 123456
    assert results[0].raw_fee == 100
    assert results[0].fee_denominator == 1_000_000
    assert [method for method, _ in calls] == [
        "eth_getBlockByNumber",
        "eth_call",
        "eth_getBlockByNumber",
    ]


def test_multicall_reorg_does_not_claim_verified_quote():
    from arbitrage.multicall_reader import MulticallPoolReader
    from eth_abi import encode

    rpc = MagicMock()
    pool = quote(1, 100).pool
    rpc.call.side_effect = [
        {"result": {"number": "0xa", "hash": BLOCK, "timestamp": "0x64"}},
        {
            "result": "0x"
            + encode(
                ["(bool,bytes)[]"],
                [
                    [
                        (True, encode(["uint256"], [10])),
                        (True, encode(["uint160"], [2**96])),
                        (True, encode(["uint128"], [1000])),
                    ]
                ],
            ).hex()
        },
        {"result": {"number": "0xa", "hash": "0x" + "88" * 32, "timestamp": "0x64"}},
    ]
    with pytest.raises(RuntimeError, match="reorganized"):
        MulticallPoolReader(rpc=rpc).batch_quote_multicall([pool], with_provenance=True)


def test_feed_three_failures_latch_without_callback_or_restart():
    callback = MagicMock()
    listener = FeedListener(feed_url=None, ws_rpc_url=None, on_event=callback)
    assert listener._record_failure("ws_rpc", RuntimeError("fixture")) is False
    assert listener._record_failure("ws_rpc", RuntimeError("fixture")) is False
    assert listener._record_failure("ws_rpc", RuntimeError("fixture")) is True
    callback.assert_not_called()
    assert listener.terminal_error is not None
    assert "ws_rpc" not in listener.last_seen
    with pytest.raises(RuntimeError, match="latched"):
        listener.start()


def test_cancelling_empty_feed_stops_callback_worker():
    import asyncio

    listener = FeedListener(feed_url=None, ws_rpc_url=None)

    async def exercise():
        task = asyncio.create_task(listener.run_async())
        await asyncio.sleep(0)
        task.cancel()
        await task

    asyncio.run(exercise())
    assert listener._running is False
    assert listener._callback_thread is not None
    assert not listener._callback_thread.is_alive()


def test_unknown_tvl_never_becomes_half_a_million_capacity():
    from dataclasses import replace

    from arbitrage.spread_monitor import estimate_realized_profit_usd

    first, second = quote(1, 100), quote(2, 102)
    first.pool = replace(first.pool, tvl_usd=0)
    second.pool = replace(second.pool, tvl_usd=0)
    alerts = find_spreads([first, second])
    assert len(alerts) == 1  # Observable spread remains visible for real amount quotation.
    assert alerts[0].bottleneck_tvl == 0
    assert alerts[0].capacity_source == "UNKNOWN"
    assert alerts[0].max_capacity_usd == alerts[0].max_profit_usd == 0
    assert estimate_realized_profit_usd(2, 0.1, 0, 0, 500) == (0, 0, 0)


def test_auditor_rejects_header_height_mismatch():
    original = auditor_rpc()

    def rpc(method, params):
        result = original(method, params)
        if method == "eth_getBlockByNumber":
            return {**result, "number": "0xc"}
        return result

    verdict = make_auditor(rpc).verify({"tx_hash": TX, "base_symbol": "USDG"})
    assert verdict.state == "REJECTED"
    assert "height disagrees" in verdict.reason
