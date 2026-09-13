"""On-demand V3 tick bitmap word and initialized tick loader with strict fixed-block enforcement."""

from __future__ import annotations

from arc_markets.tick_coverage import (
    TickCoverageSnapshot,
    TickData,
    TickWord,
    TickWordStatus,
)
from arc_readiness.errors import ArcValidationError
from arc_readiness.rpc_readonly import ReadOnlyRpcTransport

# Standard Uniswap V3 Pool selectors
V3_TICK_BITMAP_SELECTOR: str = "0x53334685"  # tickBitmap(int16)
V3_TICKS_SELECTOR: str = "0xf30dba93"  # ticks(int24)


def _encode_int16(val: int) -> str:
    # 32-byte hex representation of signed int16
    u256 = val if val >= 0 else (1 << 256) + val
    return f"{u256:064x}"


def _encode_int24(val: int) -> str:
    # 32-byte hex representation of signed int24
    u256 = val if val >= 0 else (1 << 256) + val
    return f"{u256:064x}"


def _decode_signed_int128(val: int) -> int:
    u128 = val & ((1 << 128) - 1)
    if u128 >= (1 << 127):
        return u128 - (1 << 128)
    return u128


class TickBitmapLoader:
    """Loads bounded tick bitmap words and initialized ticks via ReadOnlyRpcTransport at a fixed block."""

    def __init__(
        self,
        transport: ReadOnlyRpcTransport,
        pool_address: str,
        tick_spacing: int,
        chain_id: int = 5042,
    ) -> None:
        self.transport = transport
        self.pool_address = pool_address.lower()
        self.tick_spacing = tick_spacing
        self.chain_id = chain_id

    def load_word(self, word_pos: int, fixed_block: int) -> int:
        """Call tickBitmap(word_pos) at fixed_block."""
        calldata = V3_TICK_BITMAP_SELECTOR + _encode_int16(word_pos)
        call_params = [
            {"to": self.pool_address, "data": calldata},
            hex(fixed_block),
        ]
        raw = self.transport.request("eth_call", call_params)
        if not raw or not isinstance(raw, str) or raw == "0x":
            return 0
        clean = raw[2:] if raw.startswith("0x") else raw
        return int(clean, 16)

    def load_tick(self, tick: int, fixed_block: int) -> TickData | None:
        """Call ticks(tick) at fixed_block to fetch gross and net liquidity."""
        calldata = V3_TICKS_SELECTOR + _encode_int24(tick)
        call_params = [
            {"to": self.pool_address, "data": calldata},
            hex(fixed_block),
        ]
        raw = self.transport.request("eth_call", call_params)
        if not raw or not isinstance(raw, str) or raw == "0x":
            return None
        clean = raw[2:] if raw.startswith("0x") else raw
        if len(clean) < 64:
            return None

        # word 0: uint128 liquidityGross
        gross = int(clean[0:64], 16) & ((1 << 128) - 1)
        if gross == 0:
            return None

        # word 1: int128 liquidityNet
        raw_net = int(clean[64:128], 16) if len(clean) >= 128 else 0
        net = _decode_signed_int128(raw_net)

        return TickData(tick=tick, liquidity_gross=gross, liquidity_net=net, initialized=True)

    def load_words_around_tick(
        self,
        current_tick: int,
        epoch_block: int,
        epoch_hash: str,
        words_left: int = 1,
        words_right: int = 1,
    ) -> TickCoverageSnapshot:
        """Scan a bounded window of bitmap words around current_tick."""
        if epoch_block < 0:
            raise ArcValidationError(f"epoch_block cannot be negative: {epoch_block}")
        if not epoch_hash.startswith("0x") or len(epoch_hash) != 66:
            raise ArcValidationError(f"Invalid epoch_hash: {epoch_hash}")

        snapshot = TickCoverageSnapshot(
            pool_address=self.pool_address,
            tick_spacing=self.tick_spacing,
            current_tick=current_tick,
            epoch_block=epoch_block,
            epoch_hash=epoch_hash,
            chain_id=self.chain_id,
        )

        center_w = snapshot.word_position(current_tick)
        for w in range(center_w - words_left, center_w + words_right + 1):
            val = self.load_word(w, fixed_block=epoch_block)
            if val == 0:
                snapshot.words[w] = TickWord(word_pos=w, status=TickWordStatus.READ_EMPTY, bitmap_value=0)
            else:
                snapshot.words[w] = TickWord(word_pos=w, status=TickWordStatus.READ_POPULATED, bitmap_value=val)
                # Inspect each set bit
                for bit in range(256):
                    if (val & (1 << bit)) != 0:
                        t = ((w << 8) + bit) * self.tick_spacing
                        t_data = self.load_tick(t, fixed_block=epoch_block)
                        if t_data is not None:
                            snapshot.ticks[t] = t_data

        return snapshot
