"""Normalized Uniswap V3 core event definitions and ABI-strict log decoders."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from arc_readiness.errors import ArcValidationError

_HEX_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_HEX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")

# Canonical V3 Topic0 hashes
TOPIC0_V3_POOL_CREATED = "0x783cca1c0411a9d02c2e0d0dae29f12d7b0d4317f31bc989e47f29ab4b7dd30f"
TOPIC0_V3_INITIALIZE = "0x98636036cb66a9c19a3743560c3d7f5b5730243ba788537a4a47f049d4e71887"
TOPIC0_V3_MINT = "0x7a530802941e15fb4c41b718010e7605530bdd0d0295660ae7056e6242664cd3"
TOPIC0_V3_SWAP = "0xc42079f9310a710e40449a470eedec9564ff68c7e4777a8459e4b499ee731350"


def _clean_address(raw: str) -> str:
    # Topic values are 32 bytes (66 chars). Extract 20 bytes (40 chars)
    if len(raw) == 66 and raw.startswith("0x"):
        addr = "0x" + raw[26:]
    elif len(raw) == 42 and raw.startswith("0x"):
        addr = raw
    else:
        raise ArcValidationError(f"Invalid address data: {raw}")
    if not _HEX_ADDR_RE.match(addr):
        raise ArcValidationError(f"Malformed hex address: {addr}")
    return addr.lower()


def _decode_signed_int24(val: int) -> int:
    """Decode 24-bit signed integer from uint256 word."""
    if val >= 2**23:
        return val - 2**24
    return val


def _decode_signed_int128(val: int) -> int:
    """Decode 128-bit signed integer from uint256 word."""
    if val >= 2**127:
        return val - 2**128
    return val


def _decode_signed_int256(val: int) -> int:
    """Decode 256-bit signed integer from uint256 word."""
    if val >= 2**255:
        return val - 2**256
    return val


@dataclass(frozen=True)
class V3PoolCreatedEvent:
    factory_address: str
    token0: str
    token1: str
    fee: int
    tick_spacing: int
    pool_address: str
    block_number: int
    block_hash: str
    tx_hash: str
    log_index: int

    def __post_init__(self) -> None:
        if self.token0 >= self.token1:
            raise ArcValidationError(f"token0 ({self.token0}) must be strictly less than token1 ({self.token1})")
        if self.fee <= 0:
            raise ArcValidationError(f"fee must be positive, got {self.fee}")
        if self.tick_spacing <= 0:
            raise ArcValidationError(f"tick_spacing must be positive, got {self.tick_spacing}")


@dataclass(frozen=True)
class V3InitializeEvent:
    pool_address: str
    sqrt_price_x96: int
    tick: int
    block_number: int
    tx_hash: str

    def __post_init__(self) -> None:
        if self.sqrt_price_x96 <= 0:
            raise ArcValidationError(f"sqrt_price_x96 must be positive, got {self.sqrt_price_x96}")


@dataclass(frozen=True)
class V3MintEvent:
    pool_address: str
    sender: str
    owner: str
    tick_lower: int
    tick_upper: int
    amount: int
    amount0: int
    amount1: int
    block_number: int


@dataclass(frozen=True)
class V3SwapEvent:
    pool_address: str
    sender: str
    recipient: str
    amount0: int
    amount1: int
    sqrt_price_x96: int
    liquidity: int
    tick: int
    block_number: int


def decode_v3_pool_created(log: dict[str, Any], verified_factory: str | None = None) -> V3PoolCreatedEvent:
    """Decode a PoolCreated(address token0, address token1, uint24 fee, int24 tickSpacing, address pool) log."""
    topics = log.get("topics", [])
    if not topics or topics[0].lower() != TOPIC0_V3_POOL_CREATED.lower():
        raise ArcValidationError(f"Invalid Topic0 for V3 PoolCreated: {topics[0] if topics else None}")

    log_emitter = _clean_address(log.get("address", ""))
    if verified_factory is not None and log_emitter != verified_factory.lower():
        raise ArcValidationError(
            f"PoolCreated emitted by unauthorized factory: expected {verified_factory}, got {log_emitter}"
        )

    if len(topics) < 4:
        raise ArcValidationError(f"V3 PoolCreated expects 4 topics, got {len(topics)}")

    t0 = _clean_address(topics[1])
    t1 = _clean_address(topics[2])
    fee = int(topics[3], 16) if isinstance(topics[3], str) else int(topics[3])

    raw_data = log.get("data", "")
    if raw_data.startswith("0x"):
        raw_data = raw_data[2:]

    # Data contains: int24 tickSpacing (32 bytes), address pool (32 bytes)
    if len(raw_data) < 128:
        raise ArcValidationError(f"V3 PoolCreated data too short: {len(raw_data)} chars")

    raw_tick_spacing = int(raw_data[0:64], 16)
    tick_spacing = _decode_signed_int24(raw_tick_spacing)

    pool_addr = _clean_address("0x" + raw_data[64:128])

    b_num = int(log.get("blockNumber", 0), 16) if isinstance(log.get("blockNumber"), str) else int(log.get("blockNumber", 0))
    b_hash = str(log.get("blockHash", "0x" + "00" * 32))
    tx_hash = str(log.get("transactionHash", "0x" + "00" * 32))
    log_idx = int(log.get("logIndex", 0), 16) if isinstance(log.get("logIndex"), str) else int(log.get("logIndex", 0))

    return V3PoolCreatedEvent(
        factory_address=log_emitter,
        token0=t0,
        token1=t1,
        fee=fee,
        tick_spacing=tick_spacing,
        pool_address=pool_addr,
        block_number=b_num,
        block_hash=b_hash,
        tx_hash=tx_hash,
        log_index=log_idx,
    )


def decode_v3_initialize(log: dict[str, Any]) -> V3InitializeEvent:
    """Decode an Initialize(uint160 sqrtPriceX96, int24 tick) log."""
    topics = log.get("topics", [])
    if not topics or topics[0].lower() != TOPIC0_V3_INITIALIZE.lower():
        raise ArcValidationError(f"Invalid Topic0 for V3 Initialize: {topics[0] if topics else None}")

    pool_addr = _clean_address(log.get("address", ""))
    raw_data = log.get("data", "")
    if raw_data.startswith("0x"):
        raw_data = raw_data[2:]

    if len(raw_data) < 128:
        raise ArcValidationError("V3 Initialize data too short")

    sqrt_price = int(raw_data[0:64], 16)
    raw_tick = int(raw_data[64:128], 16)
    tick = _decode_signed_int24(raw_tick)

    b_num = int(log.get("blockNumber", 0), 16) if isinstance(log.get("blockNumber"), str) else int(log.get("blockNumber", 0))
    tx_hash = str(log.get("transactionHash", "0x" + "00" * 32))

    return V3InitializeEvent(
        pool_address=pool_addr,
        sqrt_price_x96=sqrt_price,
        tick=tick,
        block_number=b_num,
        tx_hash=tx_hash,
    )


def decode_v3_mint(log: dict[str, Any]) -> V3MintEvent:
    """Decode a Mint(address sender, address indexed owner, int24 indexed tickLower, int24 indexed tickUpper, uint128 amount, uint256 amount0, uint256 amount1) log."""
    topics = log.get("topics", [])
    if not topics or topics[0].lower() != TOPIC0_V3_MINT.lower():
        raise ArcValidationError("Invalid Topic0 for V3 Mint")

    pool_addr = _clean_address(log.get("address", ""))
    owner = _clean_address(topics[1])
    tick_lower = _decode_signed_int24(int(topics[2], 16) if isinstance(topics[2], str) else int(topics[2]))
    tick_upper = _decode_signed_int24(int(topics[3], 16) if isinstance(topics[3], str) else int(topics[3]))

    raw_data = log.get("data", "")
    if raw_data.startswith("0x"):
        raw_data = raw_data[2:]

    # Data contains: address sender (32 bytes), uint128 amount (32 bytes), uint256 amount0 (32 bytes), uint256 amount1 (32 bytes)
    if len(raw_data) < 256:
        raise ArcValidationError("V3 Mint data too short")

    sender = _clean_address("0x" + raw_data[0:64])
    amount = int(raw_data[64:128], 16)
    amount0 = int(raw_data[128:192], 16)
    amount1 = int(raw_data[192:256], 16)
    b_num = int(log.get("blockNumber", 0), 16) if isinstance(log.get("blockNumber"), str) else int(log.get("blockNumber", 0))

    return V3MintEvent(
        pool_address=pool_addr,
        sender=sender,
        owner=owner,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        amount=amount,
        amount0=amount0,
        amount1=amount1,
        block_number=b_num,
    )


def decode_v3_swap(log: dict[str, Any]) -> V3SwapEvent:
    """Decode a Swap(...) event log."""
    topics = log.get("topics", [])
    if not topics or topics[0].lower() != TOPIC0_V3_SWAP.lower():
        raise ArcValidationError("Invalid Topic0 for V3 Swap")

    pool_addr = _clean_address(log.get("address", ""))
    sender = _clean_address(topics[1])
    recipient = _clean_address(topics[2])

    raw_data = log.get("data", "")
    if raw_data.startswith("0x"):
        raw_data = raw_data[2:]

    # int256 amount0, int256 amount1, uint160 sqrtPriceX96, uint128 liquidity, int24 tick
    if len(raw_data) < 320:
        raise ArcValidationError("V3 Swap data too short")

    amount0 = _decode_signed_int256(int(raw_data[0:64], 16))
    amount1 = _decode_signed_int256(int(raw_data[64:128], 16))
    sqrt_price = int(raw_data[64 * 2 : 64 * 3], 16)
    liquidity = int(raw_data[64 * 3 : 64 * 4], 16)
    tick = _decode_signed_int24(int(raw_data[64 * 4 : 64 * 5], 16))
    b_num = int(log.get("blockNumber", 0), 16) if isinstance(log.get("blockNumber"), str) else int(log.get("blockNumber", 0))

    return V3SwapEvent(
        pool_address=pool_addr,
        sender=sender,
        recipient=recipient,
        amount0=amount0,
        amount1=amount1,
        sqrt_price_x96=sqrt_price,
        liquidity=liquidity,
        tick=tick,
        block_number=b_num,
    )
