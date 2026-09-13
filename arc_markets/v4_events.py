"""Uniswap V4 event models, ABI decoding, and keccak-256 PoolId derivation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from arc_readiness.errors import ArcValidationError

_HEX_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_HEX_BYTES32_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")

# Topic0 for Uniswap V4 PoolManager events
# Initialize(PoolId indexed id, Currency indexed currency0, Currency indexed currency1, uint24 fee, int24 tickSpacing, IHooks hooks)
TOPIC0_V4_INITIALIZE = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"  # placeholder or canonical
# Canonical hash of Initialize(bytes32,address,address,uint24,int24,address)
TOPIC0_V4_INITIALIZE_CANONICAL = "0xa1e43e498fe63c8a996f0119e8c46ea4fa5e27fb95393a54b38d38bf030fb8a9"

DYNAMIC_FEE_FLAG: int = 0x800000  # Bit 23 flag in fee represents dynamic fee hook


def _clean_address(raw: str) -> str:
    if len(raw) == 66 and raw.startswith("0x"):
        addr = "0x" + raw[26:]
    elif len(raw) == 42 and raw.startswith("0x"):
        addr = raw
    else:
        raise ArcValidationError(f"Invalid address format: {raw}")
    if not _HEX_ADDR_RE.match(addr):
        raise ArcValidationError(f"Malformed hex address: {addr}")
    return addr.lower()


def _decode_signed_int24(val: int) -> int:
    u24 = val & 0xFFFFFF
    if u24 >= (1 << 23):
        return u24 - (1 << 24)
    return u24


@dataclass(frozen=True)
class V4PoolKey:
    """Canonical 5-tuple PoolKey identifying a Uniswap V4 pool."""

    currency0: str
    currency1: str
    fee: int
    tick_spacing: int
    hooks: str

    def __post_init__(self) -> None:
        c0 = _clean_address(self.currency0)
        c1 = _clean_address(self.currency1)
        # Currency0 must be strictly less than currency1
        if c0 >= c1:
            raise ArcValidationError(
                f"V4 PoolKey currency order invariant violated: currency0 ({c0}) must be strictly less than currency1 ({c1})"
            )
        if self.fee < 0 or self.fee > 0xFFFFFF:
            raise ArcValidationError(f"Invalid V4 fee: {self.fee}")
        if self.tick_spacing <= 0:
            raise ArcValidationError(f"tick_spacing must be positive, got {self.tick_spacing}")
        _clean_address(self.hooks)

    @property
    def is_dynamic_fee(self) -> bool:
        return (self.fee & DYNAMIC_FEE_FLAG) != 0

    @property
    def has_hooks(self) -> bool:
        return self.hooks.lower() != "0x0000000000000000000000000000000000000000"

    def compute_pool_id(self) -> str:
        """Derive standard keccak256(abi.encode(currency0, currency1, fee, tickSpacing, hooks))."""
        try:
            from eth_abi.abi import encode
            from eth_utils.crypto import keccak
        except ImportError as exc:
            raise RuntimeError(
                "EVM Keccak-256 and ABI encoding dependencies (eth_abi, eth_utils) required"
            ) from exc

        # Standard EVM Keccak-256 encoding for Uniswap V4 PoolKey 5-tuple (no sha3 fallback)
        c0 = _clean_address(self.currency0)
        c1 = _clean_address(self.currency1)
        hooks = _clean_address(self.hooks)

        encoded = encode(
            ["address", "address", "uint24", "int24", "address"],
            [c0, c1, self.fee, self.tick_spacing, hooks],
        )
        return "0x" + keccak(encoded).hex()


@dataclass(frozen=True)
class V4InitializeEvent:
    pool_id: str
    currency0: str
    currency1: str
    fee: int
    tick_spacing: int
    hooks: str
    manager_address: str
    block_number: int
    tx_hash: str

    def __post_init__(self) -> None:
        if not _HEX_BYTES32_RE.match(self.pool_id):
            raise ArcValidationError(f"pool_id must be a 66-char hex bytes32 string: {self.pool_id}")


def decode_v4_initialize(log: dict[str, Any], verified_manager: str) -> V4InitializeEvent:
    """Decode a Uniswap V4 Initialize event log."""
    emitter = _clean_address(log.get("address", ""))
    if emitter.lower() != verified_manager.lower():
        raise ArcValidationError(
            f"V4 Initialize emitted by unauthorized manager: expected {verified_manager}, got {emitter}"
        )

    topics = log.get("topics", [])
    if not topics:
        raise ArcValidationError("V4 Initialize log has no topics")

    raw_pool_id = str(topics[1]).lower() if len(topics) > 1 else ""
    if not _HEX_BYTES32_RE.match(raw_pool_id):
        raise ArcValidationError(f"Invalid pool_id in topic 1: {raw_pool_id}")

    raw_data = log.get("data", "")
    if raw_data.startswith("0x"):
        raw_data = raw_data[2:]

    # Data layout:
    # currency0 (32 bytes), currency1 (32 bytes), fee (32 bytes), tickSpacing (32 bytes), hooks (32 bytes)
    if len(raw_data) < 320:
        raise ArcValidationError(f"V4 Initialize data too short: {len(raw_data)} chars")

    c0 = _clean_address("0x" + raw_data[0:64])
    c1 = _clean_address("0x" + raw_data[64:128])
    fee = int(raw_data[128:192], 16)
    raw_ts = int(raw_data[192:256], 16)
    tick_spacing = _decode_signed_int24(raw_ts)
    hooks = _clean_address("0x" + raw_data[256:320])

    b_num = int(log.get("blockNumber", 0), 16) if isinstance(log.get("blockNumber"), str) else int(log.get("blockNumber", 0))
    tx_hash = str(log.get("transactionHash", "0x" + "00" * 32))

    return V4InitializeEvent(
        pool_id=raw_pool_id,
        currency0=c0,
        currency1=c1,
        fee=fee,
        tick_spacing=tick_spacing,
        hooks=hooks,
        manager_address=emitter,
        block_number=b_num,
        tx_hash=tx_hash,
    )
