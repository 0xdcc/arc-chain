"""Tests for T16: V3 Tick Bitmap Coverage Proofs, Word State Distinctions, and Missing Tick Barriers."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from arc_markets.tick_coverage import (
    TickCoverageSnapshot,
    TickData,
    TickWord,
    TickWordStatus,
)
from arc_markets.tick_loader import TickBitmapLoader
from arc_readiness.errors import ArcMarketIneligibleError, ArcValidationError

_POOL_ADDR = "0x" + "11" * 20
_EPOCH_HASH = "0x" + "aa" * 32


class TestTickCoverage:
    """Test suite for word state verification and fail-closed missing coverage barriers."""

    def test_word_states_and_coverage_assertion(self) -> None:
        snap = TickCoverageSnapshot(
            pool_address=_POOL_ADDR,
            tick_spacing=60,
            current_tick=0,
            epoch_block=1000,
            epoch_hash=_EPOCH_HASH,
        )

        # Word 0: READ_POPULATED with tick 0 initialized
        snap.words[0] = TickWord(word_pos=0, status=TickWordStatus.READ_POPULATED, bitmap_value=1)
        snap.ticks[0] = TickData(tick=0, liquidity_gross=1000, liquidity_net=500)

        # Word 1: READ_EMPTY
        snap.words[1] = TickWord(word_pos=1, status=TickWordStatus.READ_EMPTY, bitmap_value=0)

        assert snap.is_word_covered(0) is True
        assert snap.is_word_covered(1) is True
        assert snap.is_word_covered(2) is False  # UNREAD_UNKNOWN

        # Tick 0 is in word 0 (covered)
        snap.assert_tick_covered(0)

        # Tick 15360 is in word 1 (compressed = 256, word = 1, covered empty)
        snap.assert_tick_covered(15360)

        # Tick 30720 is in word 2 (compressed = 512, word = 2, unread!)
        with pytest.raises(ArcMarketIneligibleError, match="falls into unread bitmap word 2"):
            snap.assert_tick_covered(30720)

    def test_range_covered_fails_closed_on_uncovered_word(self) -> None:
        snap = TickCoverageSnapshot(
            pool_address=_POOL_ADDR,
            tick_spacing=60,
            current_tick=0,
            epoch_block=1000,
            epoch_hash=_EPOCH_HASH,
        )
        snap.words[0] = TickWord(word_pos=0, status=TickWordStatus.READ_EMPTY)
        snap.words[2] = TickWord(word_pos=2, status=TickWordStatus.READ_EMPTY)
        # Word 1 is missing!

        with pytest.raises(ArcMarketIneligibleError, match="Bitmap word 1.*is unread"):
            snap.assert_range_covered(min_tick=0, max_tick=30720)

    def test_export_to_canonical_tick_coverage(self) -> None:
        snap = TickCoverageSnapshot(
            pool_address=_POOL_ADDR,
            tick_spacing=60,
            current_tick=120,
            epoch_block=1000,
            epoch_hash=_EPOCH_HASH,
        )
        snap.words[0] = TickWord(word_pos=0, status=TickWordStatus.READ_POPULATED, bitmap_value=1)
        snap.ticks[0] = TickData(tick=0, liquidity_gross=5000, liquidity_net=1000)

        cov = snap.to_tick_coverage(is_complete=False)
        assert cov.pool_id == _POOL_ADDR.lower()
        assert cov.current_tick == 120
        assert cov.initialized_ticks_count == 1
        assert cov.as_of_block == 1000
        assert cov.is_complete is False

    def test_tick_bitmap_loader_fixed_block_query(self) -> None:
        mock_transport = MagicMock()

        def fake_request(method, params):
            if method == "eth_call":
                to_addr = params[0]["to"]
                calldata = params[0]["data"]
                # tickBitmap call
                if calldata.startswith("0x53334685"):
                    # Word 0 has bit 0 set (1)
                    return "0x" + "00" * 31 + "01"
                # ticks call
                if calldata.startswith("0xf30dba93"):
                    # return gross = 10000, net = 2000
                    gross_hex = (10000).to_bytes(16, "big").hex().zfill(64)
                    net_hex = (2000).to_bytes(16, "big").hex().zfill(64)
                    return "0x" + gross_hex + net_hex
            return "0x"

        mock_transport.request.side_effect = fake_request

        loader = TickBitmapLoader(
            transport=mock_transport,
            pool_address=_POOL_ADDR,
            tick_spacing=60,
        )

        snap = loader.load_words_around_tick(
            current_tick=0,
            epoch_block=1000,
            epoch_hash=_EPOCH_HASH,
            words_left=0,
            words_right=0,
        )

        assert len(snap.words) == 1
        assert snap.words[0].status == TickWordStatus.READ_POPULATED
        assert len(snap.ticks) == 1
        assert snap.ticks[0].liquidity_gross == 10000
        assert snap.ticks[0].liquidity_net == 2000

    def test_invalid_epoch_rejected(self) -> None:
        loader = TickBitmapLoader(
            transport=MagicMock(),
            pool_address=_POOL_ADDR,
            tick_spacing=60,
        )
        with pytest.raises(ArcValidationError, match="epoch_block cannot be negative"):
            loader.load_words_around_tick(0, epoch_block=-1, epoch_hash=_EPOCH_HASH)

        with pytest.raises(ArcValidationError, match="Invalid epoch_hash"):
            loader.load_words_around_tick(0, epoch_block=1000, epoch_hash="0xinvalid")
