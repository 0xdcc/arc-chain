"""Tests for T15: Uniswap V4 StateView 4-Field ABI Slot0 Decoding and State Sampling."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from arc_markets.v4_state import (
    V4StateViewSampler,
    decode_v4_slot0,
)
from arc_readiness.errors import ArcValidationError
from arc_readiness.rpc_readonly import ReadOnlyRpcTransport

_SAMPLE_POOL_ID = "0x" + "11" * 32
_STATE_VIEW_ADDR = "0x" + "aa" * 20
_MANAGER_ADDR = "0x" + "bb" * 20


class TestV4StateViewDecoding:
    """Test suite for strict 4-field ABI slot0 decoding and StateView sampler invariants."""

    def test_decode_v4_slot0_success(self) -> None:
        # Word 0: sqrtPriceX96 = 79228162514264337593543950336 (hex: 0x01000000000000000000000000)
        word0 = (1 << 96).to_bytes(32, "big").hex()
        # Word 1: tick = -120 (signed int24: 2^24 - 120 = 16777136, hex: 0xffff88)
        word1 = ((1 << 256) - 120).to_bytes(32, "big").hex()
        # Word 2: protocolFee = 1000 (0x3e8)
        word2 = (1000).to_bytes(32, "big").hex()
        # Word 3: lpFee = 2500 (0x9c4)
        word3 = (2500).to_bytes(32, "big").hex()

        raw_payload = "0x" + word0 + word1 + word2 + word3
        assert len(raw_payload) == 2 + 256

        slot0 = decode_v4_slot0(raw_payload)
        assert slot0.sqrt_price_x96 == 1 << 96
        assert slot0.tick == -120
        assert slot0.protocol_fee == 1000
        assert slot0.lp_fee == 2500

    def test_decode_v4_slot0_rejects_truncated_data(self) -> None:
        # Only 64 bytes (2 words, like old V3): must be rejected!
        word0 = (1 << 96).to_bytes(32, "big").hex()
        word1 = (0).to_bytes(32, "big").hex()
        truncated = "0x" + word0 + word1

        with pytest.raises(ArcValidationError, match="V4 StateView ABI truncation error"):
            decode_v4_slot0(truncated)

    def test_stateview_sampler_requires_fixed_block(self) -> None:
        transport = ReadOnlyRpcTransport("https://rpc.arc.io", handler=MagicMock())
        sampler = V4StateViewSampler(
            transport=transport,
            state_view_address=_STATE_VIEW_ADDR,
            pool_manager_address=_MANAGER_ADDR,
        )

        with pytest.raises(ArcValidationError, match="Explicit fixed_block_number required"):
            sampler.sample_slot0(_SAMPLE_POOL_ID, fixed_block_number=None)

    def test_stateview_sampler_full_call(self) -> None:
        word0 = (1 << 96).to_bytes(32, "big").hex()
        word1 = (50).to_bytes(32, "big").hex()
        word2 = (0).to_bytes(32, "big").hex()
        word3 = (500).to_bytes(32, "big").hex()
        raw_payload = "0x" + word0 + word1 + word2 + word3

        mock_transport = MagicMock()
        mock_transport.request.return_value = raw_payload

        sampler = V4StateViewSampler(
            transport=mock_transport,
            state_view_address=_STATE_VIEW_ADDR,
            pool_manager_address=_MANAGER_ADDR,
        )

        slot0 = sampler.sample_slot0(_SAMPLE_POOL_ID, fixed_block_number=1000)
        assert slot0.sqrt_price_x96 == 1 << 96
        assert slot0.tick == 50
        assert slot0.protocol_fee == 0
        assert slot0.lp_fee == 500

        # Assert correct method and block parameter:
        mock_transport.request.assert_called_once()
        args = mock_transport.request.call_args[0]
        assert args[0] == "eth_call"
        assert args[1][1] == hex(1000)  # Hex block parameter, never latest!
