"""Uniswap V4 StateView sampling and strict 4-field ABI slot0 decoding."""

from __future__ import annotations

from dataclasses import dataclass

from arc_readiness.errors import ArcValidationError
from arc_readiness.rpc_readonly import ReadOnlyRpcTransport

# getSlot0(bytes32 poolId) selector on Uniswap V4 StateView
V4_STATEVIEW_GET_SLOT0_SELECTOR: str = "0xfa6793d5"


def _decode_signed_int24(val: int) -> int:
    u24 = val & 0xFFFFFF
    if u24 >= (1 << 23):
        return u24 - (1 << 24)
    return u24


@dataclass(frozen=True)
class V4Slot0:
    """Exact 4-field slot0 state returned by Uniswap V4 StateView."""

    sqrt_price_x96: int
    tick: int
    protocol_fee: int
    lp_fee: int

    def __post_init__(self) -> None:
        if self.sqrt_price_x96 <= 0:
            raise ArcValidationError(f"sqrt_price_x96 must be positive, got {self.sqrt_price_x96}")
        if self.protocol_fee < 0 or self.protocol_fee > 0xFFFFFF:
            raise ArcValidationError(f"Invalid protocol_fee: {self.protocol_fee}")
        if self.lp_fee < 0 or self.lp_fee > 0xFFFFFF:
            raise ArcValidationError(f"Invalid lp_fee: {self.lp_fee}")


def decode_v4_slot0(raw_hex_data: str) -> V4Slot0:
    """Decode exact 128-byte (4-word) ABI output from V4 StateView.getSlot0.

    Layout:
    word0 (0:32 bytes)   -> uint160 sqrtPriceX96
    word1 (32:64 bytes)  -> int24 tick
    word2 (64:96 bytes)  -> uint24 protocolFee
    word3 (96:128 bytes) -> uint24 lpFee

    Fails-closed if data is less than 128 bytes (256 hex chars).
    Never falls back to slicing 2 words to mimic V3!
    """
    clean = raw_hex_data.strip()
    if clean.startswith("0x"):
        clean = clean[2:]

    if len(clean) < 256:
        raise ArcValidationError(
            f"V4 StateView ABI truncation error: expected at least 128 bytes (256 hex chars), "
            f"got {len(clean)} hex chars. Slicing first two words is strictly prohibited."
        )

    sqrt_price_x96 = int(clean[0:64], 16)
    raw_tick = int(clean[64:128], 16)
    tick = _decode_signed_int24(raw_tick)
    protocol_fee = int(clean[128:192], 16)
    lp_fee = int(clean[192:256], 16)

    return V4Slot0(
        sqrt_price_x96=sqrt_price_x96,
        tick=tick,
        protocol_fee=protocol_fee,
        lp_fee=lp_fee,
    )


class V4StateViewSampler:
    """Reads fixed-block V4 pool slot0 state via verified StateView contract."""

    def __init__(
        self,
        transport: ReadOnlyRpcTransport,
        state_view_address: str,
        pool_manager_address: str,
        chain_id: int = 5042,
    ) -> None:
        self.transport = transport
        self.state_view_address = state_view_address.lower()
        self.pool_manager_address = pool_manager_address.lower()
        self.chain_id = chain_id

    def sample_slot0(
        self,
        pool_id: str,
        fixed_block_number: int | None = None,
    ) -> V4Slot0:
        """Query StateView.getSlot0(poolId) at an explicit block height."""
        clean_id = pool_id.strip().lower()
        if clean_id.startswith("0x"):
            clean_id = clean_id[2:]

        if len(clean_id) != 64:
            raise ArcValidationError(f"Invalid pool_id length: {pool_id}")

        calldata = V4_STATEVIEW_GET_SLOT0_SELECTOR + clean_id
        block_param = hex(fixed_block_number) if fixed_block_number is not None else "latest"
        if block_param == "latest":
            raise ArcValidationError(
                "Explicit fixed_block_number required for V4 state sampling: 'latest' is forbidden."
            )

        call_params = [
            {
                "to": self.state_view_address,
                "data": calldata,
            },
            block_param,
        ]

        raw_resp = self.transport.request("eth_call", call_params)
        if not raw_resp or not isinstance(raw_resp, str) or raw_resp == "0x":
            raise ArcValidationError(
                f"V4 StateView call returned empty or invalid response for pool {pool_id}: {raw_resp!r}"
            )

        return decode_v4_slot0(raw_resp)
