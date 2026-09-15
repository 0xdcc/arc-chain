"""Candidate fee scanner integration; all ABI payloads are inert local fixtures."""

from unittest.mock import MagicMock

import pytest
from eth_abi import encode

from research.market_data.fee_scan import (
    batch_read_v3_pool_fees,
    read_v3_pool_fee,
    scan_pools,
)


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

    from research.market_data.fee_scan import scanned_to_pool_spec

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


def test_targeted_dex_passthrough_to_v3_reader():
    """Targeted契约: scan_pools 必须向 v3_reader 真实透传 dex 参数."""
    mock_reader = MagicMock(return_value=30.0)
    rpc = MagicMock()
    addr = "0x" + "11" * 20
    pools = scan_pools(
        min_tvl=0,
        rpc=rpc,
        v3_reader=mock_reader,
        raw_pools=[
            {
                "addr": addr,
                "dex": "ramses-v3",
                "name": "WETH / USDG 0.3%",
                "tvl": 100_000,
            }
        ],
    )
    assert len(pools) == 1
    mock_reader.assert_called_once_with(addr, rpc=rpc, dex="ramses-v3")
    assert pools[0].fee_bps == 30.0


def test_targeted_unknown_raw_not_recognized_as_one_bps():
    """Targeted契约: 未知 fork 的原始 uint 值绝不默认识别为 bps，保持 NaN 并保留证据."""
    import math

    rpc = MagicMock()
    rpc.call.return_value = {"result": "0x" + encode(["uint256"], [100]).hex()}
    pools = scan_pools(
        min_tvl=0,
        rpc=rpc,
        raw_pools=[
            {
                "addr": "0x" + "22" * 20,
                "dex": "giga-v3",
                "name": "FOO / BAR 0.01%",
                "tvl": 100_000,
            }
        ],
    )
    assert len(pools) == 1
    assert pools[0].fee_bps != 1.0
    assert math.isnan(pools[0].fee_bps)
    assert "raw=100" in pools[0].monitor_only_reason


def test_targeted_canonical_zero_fee_is_legitimate():
    """Targeted契约: canonical 0 费率 (0 ppm -> 0.0 bps) 为合法链上物理状态，不被降级."""
    rpc = MagicMock()
    rpc.call.return_value = {"result": "0x" + encode(["uint24"], [0]).hex()}
    pools = scan_pools(
        min_tvl=0,
        rpc=rpc,
        raw_pools=[
            {
                "addr": "0x" + "33" * 20,
                "dex": "uniswap-v3",
                "name": "ZERO / USDG 0%",
                "tvl": 100_000,
            }
        ],
    )
    assert len(pools) == 1
    assert pools[0].fee_bps == 0.0
    assert pools[0].monitor_only_reason == ""

