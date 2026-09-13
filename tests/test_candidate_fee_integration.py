"""Candidate fee scanner integration; all ABI payloads are inert local fixtures."""

from unittest.mock import MagicMock

import pytest
from arbitrage.pool_scanner import batch_read_v3_pool_fees, read_v3_pool_fee, scan_pools
from eth_abi import encode


@pytest.mark.parametrize("raw,bps", [(0, 0), (100, 1), (500, 5), (3000, 30), (10000, 100)])
def test_scanner_reads_canonical_fee_without_magnitude_guess(raw, bps):
    rpc = MagicMock()
    rpc.call.return_value = {"result": "0x" + encode(["uint24"], [raw]).hex()}
    pools = scan_pools(
        min_tvl=0,
        rpc=rpc,
        raw_pools=[
            {
                "addr": "0x" + "11" * 20,
                "dex": "uniswap-v3",
                "name": "WETH / USDG 0.01%",
                "tvl": 100_000,
            }
        ],
    )
    assert len(pools) == 1
    assert pools[0].fee_bps == bps


def test_fork_cannot_inherit_canonical_fee_unit():
    rpc = MagicMock()
    with pytest.raises(ValueError, match="adapter unverified"):
        read_v3_pool_fee("0x" + "11" * 20, rpc=rpc, dex="ramses-v3")
    rpc.call.assert_not_called()


def test_batch_filter_keeps_fee_results_on_the_right_addresses():
    first, last = "0x" + "11" * 20, "0x" + "22" * 20
    rpc = MagicMock()
    rpc.call.return_value = {
        "result": "0x"
        + encode(
            ["(bool,bytes)[]"],
            [[(True, encode(["uint24"], [100])), (True, encode(["uint24"], [0]))]],
        ).hex()
    }
    assert batch_read_v3_pool_fees([first, "invalid", last], rpc=rpc) == {first: 1.0, last: 0.0}


def test_unknown_fork_stays_visible_without_a_guessed_fee():
    import math

    from arbitrage.pool_config import scanned_to_pool_spec

    rpc = MagicMock()
    rpc.call.return_value = {"result": "0x" + encode(["uint256"], [100]).hex()}
    pools = scan_pools(
        min_tvl=0,
        rpc=rpc,
        raw_pools=[
            {
                "addr": "0x" + "11" * 20,
                "dex": "giga-v3",
                "name": "WETH / USDG 0.01%",
                "tvl": 100_000,
            }
        ],
    )
    assert len(pools) == 1
    assert math.isnan(pools[0].fee_bps)
    assert "raw=100" in pools[0].monitor_only_reason
    assert "MONITOR_ONLY" in scanned_to_pool_spec(pools[0]).label
