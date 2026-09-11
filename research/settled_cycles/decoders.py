"""Strict deterministic event decoders for settled-cycle research."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from arbitrage_contracts.identity import validate_bytes32, validate_evm_address

SWAP_V3_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
SWAP_V4_TOPIC = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
DEPOSIT_WETH_TOPIC = "0xe1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d3c4660300e274e944097073e"
WITHDRAWAL_WETH_TOPIC = "0x7fcf532c1207038a823a0c05291be65d6449741add6e94066936c4f63f5097a8"

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _require_hex_string(value: Any) -> str:
    if type(value) is not str:
        raise TypeError("Hex value must be a string")
    if not value.startswith("0x") or len(value) == 2:
        raise ValueError("Hex value must have a non-empty 0x prefix")
    if any(character not in _HEX_DIGITS for character in value[2:]):
        raise ValueError("Hex value contains invalid characters")
    return value


def _parse_hex(value: str) -> int:
    return int(value, 16) if value.startswith("0x") else int(f"0x{value}", 16)


def _parse_unsigned(value: Any, bits: int) -> int:
    if type(value) is int and not isinstance(value, bool):
        raw = value
    else:
        raw = _parse_hex(value)
    if raw < 0 or raw > (1 << bits) - 1:
        raise ValueError(f"Value is outside unsigned {bits}-bit bounds")
    return raw


def _parse_signed(value: Any, bits: int) -> int:
    if type(value) is int and not isinstance(value, bool):
        raw = value
        if raw >= 1 << (bits - 1) and raw < 1 << bits:
            val = raw - (1 << bits)
        else:
            val = raw
    else:
        s = _require_hex_string(value)
        raw = int(s, 16)
        if len(s[2:]) == 64:
            # Full 32-byte EVM slot: interpret as 256-bit two's complement,
            # then require the result to fit the narrower signed range.
            val = raw - (1 << 256) if raw >= (1 << 255) else raw
        elif raw >= 1 << (bits - 1) and raw < 1 << bits:
            val = raw - (1 << bits)
        else:
            val = raw

    min_val = -(1 << (bits - 1))
    max_val = (1 << (bits - 1)) - 1
    if val < min_val or val > max_val:
        raise ValueError(f"Value {val} is outside signed {bits}-bit bounds")
    return val


def parse_uint256(hex_or_int: str | int) -> int:
    """Parse and validate an unsigned 256-bit integer."""
    return _parse_unsigned(hex_or_int, 256)


def to_signed_int256(hex_or_int: str | int) -> int:
    """Convert a 256-bit two's-complement value to a signed integer."""
    return _parse_signed(hex_or_int, 256)


def to_signed_int128(hex_or_int: str | int) -> int:
    """Convert a 128-bit two's-complement value to a signed integer."""
    return _parse_signed(hex_or_int, 128)


def to_signed_int24(hex_or_int: str | int) -> int:
    """Convert a 24-bit two's-complement value to a signed integer."""
    return _parse_signed(hex_or_int, 24)


def parse_topic_address(topic: str) -> str:
    """Parse a strictly zero-padded 32-byte topic into an EVM address."""
    if type(topic) is not str:
        raise TypeError("Topic address must be a string")
    if len(topic) != 66 or not topic.startswith("0x"):
        raise ValueError("Topic address must be 32 bytes with an 0x prefix")
    if topic[2:26] != "0" * 24:
        raise ValueError("Topic address is not strictly zero padded")
    return validate_evm_address(f"0x{topic[26:]}")


def _log_parts(log: Mapping[str, Any]) -> tuple[list[Any], str, int | None]:
    topics = log.get("topics")
    if not isinstance(topics, list) or not topics:
        raise ValueError("missing_topics")
    data = log.get("data", "0x")
    if type(data) is not str:
        raise ValueError("invalid_data")
    log_index = log.get("logIndex")
    if log_index is not None and (type(log_index) is not int or isinstance(log_index, bool) or log_index < 0):
        raise ValueError("invalid_log_index")
    return topics, data, log_index


def _word(data: str, offset: int) -> str:
    start = 2 + offset * 64
    return "0x" + data[start : start + 64]


@dataclass(frozen=True, slots=True)
class DecodedTransfer:
    """A decoded ERC-20 Transfer event."""

    token_address: str
    from_address: str
    to_address: str
    value_atoms: int
    log_index: int | None


@dataclass(frozen=True, slots=True)
class DecodedSwapV3:
    """A decoded Uniswap V3 Swap event."""

    pool_address: str
    sender: str
    recipient: str
    amount0_atoms: int
    amount1_atoms: int
    sqrt_price_x96: int
    liquidity: int
    tick: int
    log_index: int | None


@dataclass(frozen=True, slots=True)
class DecodedSwapV4:
    """A decoded Uniswap V4 Swap event."""

    manager_address: str
    pool_id: str
    sender: str
    amount0_atoms: int
    amount1_atoms: int
    sqrt_price_x96: int
    liquidity: int
    tick: int
    fee: int
    log_index: int | None


@dataclass(frozen=True, slots=True)
class DecodedWrap:
    """A decoded WETH Deposit event."""

    token_address: str
    dst: str
    wad_atoms: int
    log_index: int | None


@dataclass(frozen=True, slots=True)
class DecodedUnwrap:
    """A decoded WETH Withdrawal event."""

    token_address: str
    src: str
    wad_atoms: int
    log_index: int | None


@dataclass(frozen=True, slots=True)
class UnsupportedLog:
    """An original log retained because it cannot be decoded safely."""

    address: str
    topics: tuple[str, ...]
    data: str
    log_index: int | None
    reason: str


DecodedLog = DecodedTransfer | DecodedSwapV3 | DecodedSwapV4 | DecodedWrap | DecodedUnwrap


def _unsupported(log: Mapping[str, Any], reason: str) -> UnsupportedLog:
    topics_value = log.get("topics")
    topics = tuple(topics_value) if isinstance(topics_value, list) else ()
    address = log.get("address")
    data = log.get("data", "0x")
    log_index = log.get("logIndex")
    return UnsupportedLog(
        address=address if type(address) is str else str(address),
        topics=topics,
        data=data if type(data) is str else str(data),
        log_index=log_index if type(log_index) is int and not isinstance(log_index, bool) else None,
        reason=reason,
    )


def decode_log(log: Mapping[str, Any]) -> DecodedLog | UnsupportedLog:
    """Decode one supported event log or preserve it as unsupported."""
    try:
        raw_address = log.get("address")
        if not isinstance(raw_address, str):
            raise ValueError("missing_address")
        address = validate_evm_address(raw_address)
        topics, data, log_index = _log_parts(log)
        topic0 = topics[0]
        if type(topic0) is not str:
            raise ValueError("unsupported_topic")

        if topic0 == SWAP_V3_TOPIC:
            if len(topics) != 3 or not data.startswith("0x") or len(data) != 322:
                return _unsupported(log, "v3_bad_data_length")
            return DecodedSwapV3(
                pool_address=address,
                sender=parse_topic_address(topics[1]),
                recipient=parse_topic_address(topics[2]),
                amount0_atoms=to_signed_int256(_word(data, 0)),
                amount1_atoms=to_signed_int256(_word(data, 1)),
                sqrt_price_x96=parse_uint256(_word(data, 2)),
                liquidity=parse_uint256(_word(data, 3)),
                tick=to_signed_int24(_word(data, 4)),
                log_index=log_index,
            )

        if topic0 == SWAP_V4_TOPIC:
            if len(topics) != 3:
                return _unsupported(log, "v4_bad_topics_length")
            if not data.startswith("0x") or len(data) != 386:
                return _unsupported(log, "v4_bad_data_length")
            pool_id = validate_bytes32(topics[1])
            try:
                amount0 = to_signed_int128(_word(data, 0))
                amount1 = to_signed_int128(_word(data, 1))
                tick = to_signed_int24(_word(data, 4))
                fee = _parse_unsigned(_word(data, 5), 24)
            except (TypeError, ValueError):
                return _unsupported(log, "v4_out_of_bounds")
            return DecodedSwapV4(
                manager_address=address,
                pool_id=pool_id,
                sender=parse_topic_address(topics[2]),
                amount0_atoms=amount0,
                amount1_atoms=amount1,
                sqrt_price_x96=parse_uint256(_word(data, 2)),
                liquidity=parse_uint256(_word(data, 3)),
                fee=fee,
                tick=tick,
                log_index=log_index,
            )

        if topic0 == TRANSFER_TOPIC:
            if len(topics) != 3 or not data.startswith("0x") or len(data) != 66:
                return _unsupported(log, "transfer_bad_data_length")
            return DecodedTransfer(
                token_address=address,
                from_address=parse_topic_address(topics[1]),
                to_address=parse_topic_address(topics[2]),
                value_atoms=parse_uint256(data),
                log_index=log_index,
            )

        if topic0 == DEPOSIT_WETH_TOPIC:
            if len(topics) != 2 or not data.startswith("0x") or len(data) != 66:
                return _unsupported(log, "wrap_bad_data_length")
            return DecodedWrap(
                token_address=address,
                dst=parse_topic_address(topics[1]),
                wad_atoms=parse_uint256(data),
                log_index=log_index,
            )

        if topic0 == WITHDRAWAL_WETH_TOPIC:
            if len(topics) != 2 or not data.startswith("0x") or len(data) != 66:
                return _unsupported(log, "unwrap_bad_data_length")
            return DecodedUnwrap(
                token_address=address,
                src=parse_topic_address(topics[1]),
                wad_atoms=parse_uint256(data),
                log_index=log_index,
            )
        return _unsupported(log, "unsupported_topic")
    except (TypeError, ValueError) as error:
        reason = str(error)
        return _unsupported(log, reason if reason == "missing_topics" else "malformed_log")
