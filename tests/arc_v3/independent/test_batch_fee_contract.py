"""Uniswap V3 batch fee reading contract tests (Arc v3 independent).

Strictly tests batch_read_v3_pool_fees API contract:
1. Filters invalid addresses and binds returned fees strictly to filtered valid addresses;
2. Preserves canonical uint24 ppm -> bps precision (0 raw is legitimate 0.0 bps, not error);
3. Graceful per-pool failure isolation (failed child is omitted from results);
4. Fail-closed defense on length mismatch (multicall return count mismatch);
5. Rejects unverified DEX forks before any RPC invocation;
6. Rejects invocation without explicitly injected RPC transport;
7. Empty or all-invalid address lists return empty mapping without RPC calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from eth_abi.abi import encode as abi_encode

from research.market_data.fee_scan import batch_read_v3_pool_fees


class TestBatchV3FeeContract:
    """Rigorous contract test suite for batch_read_v3_pool_fees."""

    def test_batch_filter_keeps_fee_results_on_the_right_addresses(self) -> None:
        """Original first-invalid-last contract: filters invalid address and binds results."""
        first, last = "0x" + "11" * 20, "0x" + "22" * 20
        rpc = MagicMock()
        rpc.call.return_value = {
            "result": "0x"
            + abi_encode(
                ["(bool,bytes)[]"],
                [[(True, abi_encode(["uint24"], [100])), (True, abi_encode(["uint24"], [0]))]],
            ).hex()
        }
        res = batch_read_v3_pool_fees([first, "invalid", last], rpc=rpc)
        assert res == {first: 1.0, last: 0.0}

    def test_batch_read_v3_pool_fees_accepts_numeric_zero_fee(self) -> None:
        """Zero fee (0 ppm -> 0.0 bps) is legitimate on-chain state, not a failure."""
        addr = "0x" + "33" * 20
        rpc = MagicMock()
        rpc.call.return_value = {
            "result": "0x"
            + abi_encode(
                ["(bool,bytes)[]"],
                [[(True, abi_encode(["uint24"], [0]))]],
            ).hex()
        }
        res = batch_read_v3_pool_fees([addr], rpc=rpc)
        assert res == {addr: 0.0}

    def test_batch_read_v3_pool_fees_omits_failed_child(self) -> None:
        """Single child revert in multicall is safely omitted; successful sibling kept."""
        p1, p2, p3 = "0x" + "44" * 20, "0x" + "55" * 20, "0x" + "66" * 20
        rpc = MagicMock()
        rpc.call.return_value = {
            "result": "0x"
            + abi_encode(
                ["(bool,bytes)[]"],
                [
                    [
                        (True, abi_encode(["uint24"], [3000])),
                        (False, b""),  # child reverted
                        (True, abi_encode(["uint24"], [500])),
                    ]
                ],
            ).hex()
        }
        res = batch_read_v3_pool_fees([p1, p2, p3], rpc=rpc)
        assert res == {p1: 30.0, p3: 5.0}
        assert p2 not in res

    def test_batch_read_v3_pool_fees_length_mismatch_fails_closed(self) -> None:
        """Multicall return count mismatch fails closed with RuntimeError."""
        p1, p2 = "0x" + "77" * 20, "0x" + "88" * 20
        rpc = MagicMock()
        # Return only 1 result for 2 queried pools
        rpc.call.return_value = {
            "result": "0x"
            + abi_encode(
                ["(bool,bytes)[]"],
                [[(True, abi_encode(["uint24"], [3000]))]],
            ).hex()
        }
        with pytest.raises(RuntimeError, match="Multicall return count mismatch"):
            batch_read_v3_pool_fees([p1, p2], rpc=rpc)

    def test_batch_read_v3_pool_fees_unknown_dex_rejected_without_call(self) -> None:
        """Unverified DEX fork rejected immediately before making RPC call."""
        addr = "0x" + "99" * 20
        rpc = MagicMock()
        with pytest.raises(ValueError, match="adapter unverified"):
            batch_read_v3_pool_fees([addr], rpc=rpc, dex="ramses-v3")
        rpc.call.assert_not_called()

    def test_batch_read_v3_pool_fees_no_rpc_rejected(self) -> None:
        """Missing RPC transport is rejected immediately (no implicit HTTP construction)."""
        addr = "0x" + "aa" * 20
        with pytest.raises(ValueError, match="ReadOnlyRpcTransport must be explicitly injected"):
            batch_read_v3_pool_fees([addr], rpc=None)

    def test_batch_read_v3_pool_fees_empty_or_invalid_addresses(self) -> None:
        """Empty or all-invalid address list returns empty dict without RPC invocation."""
        rpc = MagicMock()
        assert batch_read_v3_pool_fees([], rpc=rpc) == {}
        assert batch_read_v3_pool_fees(["invalid", "0x123", ""], rpc=rpc) == {}
        rpc.call.assert_not_called()

    @pytest.mark.parametrize(
        "raw_ppm,expected_bps",
        [(100, 1.0), (500, 5.0), (3000, 30.0), (10000, 100.0)],
    )
    def test_batch_read_v3_pool_fees_canonical_ppm_to_bps(
        self, raw_ppm: int, expected_bps: float
    ) -> None:
        """Canonical uint24 ppm is converted exactly to bps without magnitude guess."""
        addr = "0x" + "bb" * 20
        rpc = MagicMock()
        rpc.call.return_value = {
            "result": "0x"
            + abi_encode(
                ["(bool,bytes)[]"],
                [[(True, abi_encode(["uint24"], [raw_ppm]))]],
            ).hex()
        }
        res = batch_read_v3_pool_fees([addr], rpc=rpc)
        assert res == {addr: expected_bps}
