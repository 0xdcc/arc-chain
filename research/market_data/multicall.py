"""Multicall2 batch reading and ABI encoding/decoding core (Arc v3 research).

Features:
1. Targets standard EVM / L2 Multicall2 contract tryAggregate(requireSuccess=False, calls);
2. Atomic block height binding via call[0] getBlockNumber();
3. Read-only transport decoupling: accepts any ReadOnlyRpcTransport protocol or callable;
4. Decoupled from RobinhoodRpc and external funds modules (inlines pure hex conversion);
5. Graceful per-pool error isolation: reverted pools are safely skipped;
6. Supports Uniswap V3 slot0(), Uniswap V4 StateView.getSlot0(bytes32 poolId), and V2 getReserves();
7. Full 128-byte retdata decoding for Uniswap V4 in both default and provenance modes;
8. Malformed hex / corrupt ABI fails closed with explicit error;
9. Type-safe AnyPool = PoolSpec | V4PoolSpec union (M1 PoolIdentity seam reserved for M3);
10. Exposes standard API signatures required by subsequent pool_reader (reserved for M3).
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from eth_abi.abi import decode as abi_decode
from eth_abi.abi import encode as abi_encode
from web3 import Web3

if TYPE_CHECKING:
    from research.market_data.types import TokenIdentity

try:
    from research.market_data.catalog import ROBINHOOD_CHAIN_ID
    from research.market_data.catalog import get_verified_token as _get_verified_token

    get_verified_token: Callable[[str], TokenIdentity] | None = _get_verified_token
except ImportError:
    ROBINHOOD_CHAIN_ID = 4663
    get_verified_token = None

logger = logging.getLogger(__name__)

# Default contract addresses
MULTICALL2_ADDRESS = "0x2cAC2D899eCC914d704FeaAE33ac1bF36277DaD1"
STATE_VIEW_ADDRESS = "0xf3334192d15450cdd385c8b70e03f9a6bd9e673b"

# Function selectors
# Multicall2: tryAggregate(bool requireSuccess, (address,bytes)[] calls) -> 0xbce38bd7
TRY_AGGREGATE_SELECTOR = "0xbce38bd7"
# Multicall2: getBlockNumber() -> 0x42cbb15c
GET_BLOCK_NUMBER_SELECTOR = "0x42cbb15c"
# Uniswap V3: slot0() -> 0x3850c7bd
V3_SLOT0_SELECTOR = "0x3850c7bd"
# Uniswap V2: getReserves() -> 0x0902f1ac
GET_RESERVES_SELECTOR = "0x0902f1ac"
# Uniswap V4 StateView: getSlot0(bytes32 poolId) -> 0xc815641c
STATE_VIEW_GET_SLOT0_SELECTOR = "0xc815641c"

_Q96 = 2**96


@runtime_checkable
class ReadOnlyRpcTransport(Protocol):
    """Minimal read-only JSON-RPC transport contract.

    Generic .call() is restricted by MulticallPoolReader to read-only methods
    ('eth_call', 'eth_getBlockByNumber'). It never issues state-modifying requests.
    """

    def call(
        self,
        method: str,
        params: list[Any] | tuple[Any, ...] | None = None,
        block_identifier: str | None = None,
    ) -> dict[str, Any]:
        """Execute one read-only JSON-RPC call."""
        ...


def _hex_value(value: Any, size: int) -> str:
    """Normalize and validate fixed-width hex bytes or string.

    Decoupled from external execution.funds modules.
    """
    if isinstance(value, bytes):
        value = "0x" + value.hex()
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]+", value):
        raise ValueError(f"Invalid hex string: {value!r}")
    expected_len = 2 + size * 2
    if len(value) != expected_len:
        raise ValueError(
            f"Invalid hex length: expected {expected_len} characters ({size} bytes), got {len(value)}"
        )
    return value.lower()


@dataclass
class PoolSpec:
    """Specification for standard EVM 20-byte pool."""

    address: str
    label: str
    fee_bps: float = 30.0
    token0: str = ""
    token1: str = ""
    dec0: int = 18
    dec1: int = 18
    tvl_usd: float = 0.0
    dex: str = "uniswap-v3"

    @property
    def is_v2(self) -> bool:
        """Return True if pool is Uniswap V2 or compatible."""
        return "v2" in self.dex.lower() or "uniswap-v2" in self.label.lower()

    def __post_init__(self) -> None:
        if len(self.address) != 42:
            raise ValueError(
                f"Pool address must be 20-byte (42 chars), got {len(self.address)}: {self.address}"
            )


@dataclass
class V4PoolSpec:
    """Specification for Uniswap V4 32-byte poolId."""

    address: str
    label: str
    fee_bps: float = 30.0
    token0: str = ""
    token1: str = ""
    dec0: int = 18
    dec1: int = 18
    tvl_usd: float = 0.0
    tick_spacing: int = 60

    def __post_init__(self) -> None:
        if len(self.address) != 66:
            raise ValueError(
                f"V4 poolId must be 32-byte (66 chars), got {len(self.address)}: {self.address}"
            )


# Strict, verifiable pool union (removed | Any for sound type safety)
AnyPool = PoolSpec | V4PoolSpec


@dataclass
class PriceQuote:
    """Read-only normalized spot price quote for a pool."""

    pool: AnyPool
    base: str
    quote: str
    price: float
    raw_price_t1_per_t0: float
    base_symbol: str = ""
    quote_symbol: str = ""
    block_number: int = 0
    ts: float = field(default_factory=time.time)
    block_hash: str = ""
    block_timestamp: int | float | None = None
    active_liquidity: int | None = None
    raw_fee: int | None = None
    fee_denominator: int | None = None


def _lookup_token_decimals_in_catalog(
    token_catalog: Any,
    chain_id: int | None,
    addr: str,
) -> int | None:
    """Look up token decimals from injected catalog, callable, or mapping."""
    if token_catalog is None:
        return None

    if callable(token_catalog):
        try:
            res = token_catalog(chain_id, addr)
        except TypeError:
            res = token_catalog(addr)
        if res is not None:
            if isinstance(res, int) and not isinstance(res, bool):
                return res
            if hasattr(res, "decimals"):
                return getattr(res, "decimals", None)
            if isinstance(res, dict) and "decimals" in res:
                return int(res["decimals"])

    if isinstance(token_catalog, Mapping):
        val = None
        if chain_id is not None and (chain_id, addr) in token_catalog:
            val = token_catalog[(chain_id, addr)]
        elif chain_id is not None and (chain_id, addr.lower()) in token_catalog:
            val = token_catalog[(chain_id, addr.lower())]
        elif addr in token_catalog:
            val = token_catalog[addr]
        elif addr.lower() in token_catalog:
            val = token_catalog[addr.lower()]

        if val is not None:
            if isinstance(val, int) and not isinstance(val, bool):
                return val
            if hasattr(val, "decimals"):
                tok_chain = getattr(val, "chain_id", None)
                if tok_chain is not None and chain_id is not None and tok_chain != chain_id:
                    raise ValueError(
                        f"Injected catalog token chain_id {tok_chain} disagrees with pool chain_id {chain_id}"
                    )
                return getattr(val, "decimals", None)
            if isinstance(val, dict):
                tok_chain = val.get("chain_id")
                if tok_chain is not None and chain_id is not None and tok_chain != chain_id:
                    raise ValueError(
                        f"Injected catalog token chain_id {tok_chain} disagrees with pool chain_id {chain_id}"
                    )
                if "decimals" in val:
                    return int(val["decimals"])

    if hasattr(token_catalog, "get_token"):
        try:
            tok = token_catalog.get_token(addr, chain_id=chain_id)
            if tok is not None:
                tok_chain = getattr(tok, "chain_id", None)
                if tok_chain is not None and chain_id is not None and tok_chain != chain_id:
                    raise ValueError(
                        f"Injected catalog token chain_id {tok_chain} disagrees with pool chain_id {chain_id}"
                    )
                return getattr(tok, "decimals", None)
        except (KeyError, AttributeError):
            pass

    if hasattr(token_catalog, "get_verified_token"):
        try:
            tok = token_catalog.get_verified_token(addr)
            if tok is not None:
                tok_chain = getattr(tok, "chain_id", None)
                if tok_chain is not None and chain_id is not None and tok_chain != chain_id:
                    raise ValueError(
                        f"Injected catalog token chain_id {tok_chain} disagrees with pool chain_id {chain_id}"
                    )
                return getattr(tok, "decimals", None)
        except (KeyError, AttributeError):
            pass

    return None


def _resolve_token_decimals(
    token_obj: Any,
    token_addr: str,
    pool_chain_id: int | None,
    pool_explicit_dec: int | None,
    token_catalog: Any,
    token_name: str,
    pool_addr: str,
) -> int:
    """Resolve token decimals strictly without implicit 18 default.

    Sources checked:
    1. Direct decimals on token_obj (e.g. TokenIdentity or duck-type object)
    2. Injected token_catalog (mapping, callable, or catalog instance)
    3. Built-in verified catalog (strictly for ROBINHOOD_CHAIN_ID=4663 only)

    Raises:
        ValueError: If decimals missing, conflicting, invalid range, or wrong chain.
        TypeError: If decimals is not an integer.
    """
    token_attr_dec: int | None = None
    if hasattr(token_obj, "decimals"):
        token_attr_dec = getattr(token_obj, "decimals", None)
        token_chain = getattr(token_obj, "chain_id", None)
        if token_chain is not None and pool_chain_id is not None and token_chain != pool_chain_id:
            raise ValueError(
                f"{token_name} chain_id {token_chain} does not match pool chain_id {pool_chain_id}"
            )

    injected_dec = _lookup_token_decimals_in_catalog(token_catalog, pool_chain_id, token_addr)

    catalog_dec = injected_dec
    if catalog_dec is None:
        if pool_chain_id == ROBINHOOD_CHAIN_ID and get_verified_token is not None:
            try:
                verified = get_verified_token(token_addr)
                if verified.chain_id != pool_chain_id:
                    raise ValueError(
                        f"Verified token chain_id {verified.chain_id} != pool chain_id {pool_chain_id}"
                    )
                catalog_dec = verified.decimals
            except KeyError:
                pass
        elif pool_chain_id is not None and pool_chain_id != ROBINHOOD_CHAIN_ID:
            # Strictly forbid applying historical 4663 whitelist to other chains
            pass

    sources: list[tuple[str, int]] = []
    if pool_explicit_dec is not None:
        sources.append(("pool", pool_explicit_dec))
    if token_attr_dec is not None:
        sources.append(("token_attr", token_attr_dec))
    if catalog_dec is not None:
        sources.append(("catalog", catalog_dec))

    if not sources:
        raise ValueError(
            f"Missing explicit decimals for {token_name} ({token_addr}) in pool {pool_addr}; "
            f"defaulting to 18 is strictly forbidden (chain_id={pool_chain_id})"
        )

    # Validate against conflicting decimals across sources
    first_source, first_dec = sources[0]
    for s_name, s_dec in sources[1:]:
        if s_dec != first_dec:
            raise ValueError(
                f"Conflicting decimals for {token_name}: {first_source} specifies {first_dec}, "
                f"while {s_name} specifies {s_dec}"
            )

    resolved = first_dec
    if not isinstance(resolved, int) or isinstance(resolved, bool):
        raise TypeError(f"Decimals for {token_name} must be integer, got {type(resolved).__name__}")
    if not (0 <= resolved <= 18):
        raise ValueError(f"Decimals for {token_name} must be in [0, 18], got {resolved}")

    return resolved


def adapt_pool_identity(
    pool: Any,
    token_catalog: Any = None,
) -> AnyPool:
    """Explicit adapter seam for M1 PoolIdentity -> PoolSpec/V4PoolSpec.

    Reserved for M3 integration without deleting or mutating M2 pool models.
    Strictly resolves token decimals from TokenIdentity, explicit pool attributes,
    or injected catalog. Defaulting to 18 is forbidden.
    Historical 4663 catalog is never automatically applied to other chains.
    """
    if isinstance(pool, (PoolSpec, V4PoolSpec)):
        return pool
    addr = getattr(pool, "address", None) or getattr(pool, "pool_id", None)
    if not isinstance(addr, str) or not addr.strip():
        raise TypeError(f"Cannot adapt pool object without valid address string: {pool!r}")
    addr = addr.strip()
    label = getattr(pool, "label", getattr(pool, "symbol", str(pool)))
    fee_bps = float(getattr(pool, "fee_bps", 30.0))

    t0_obj = getattr(pool, "token0", None)
    t1_obj = getattr(pool, "token1", None)
    if t0_obj is None or t1_obj is None:
        raise ValueError(f"Pool {addr} missing token0 or token1")

    t0 = str(getattr(t0_obj, "address", t0_obj)).strip().lower()
    t1 = str(getattr(t1_obj, "address", t1_obj)).strip().lower()

    pool_chain_id = getattr(pool, "chain_id", None)

    p_dec0 = getattr(pool, "dec0", getattr(pool, "decimals0", None))
    p_dec1 = getattr(pool, "dec1", getattr(pool, "decimals1", None))

    if (p_dec0 is None) != (p_dec1 is None) and token_catalog is None and pool_chain_id != ROBINHOOD_CHAIN_ID:
        raise ValueError(f"Partial decimals on pool {addr}: dec0={p_dec0}, dec1={p_dec1}")

    dec0 = _resolve_token_decimals(t0_obj, t0, pool_chain_id, p_dec0, token_catalog, "token0", addr)
    dec1 = _resolve_token_decimals(t1_obj, t1, pool_chain_id, p_dec1, token_catalog, "token1", addr)

    if len(addr) == 66:
        return V4PoolSpec(
            address=addr,
            label=label,
            fee_bps=fee_bps,
            token0=t0,
            token1=t1,
            dec0=dec0,
            dec1=dec1,
            tick_spacing=int(getattr(pool, "tick_spacing", 60)),
        )
    dex = getattr(pool, "dex", getattr(pool, "protocol", "uniswap-v3"))
    return PoolSpec(
        address=addr,
        label=label,
        fee_bps=fee_bps,
        token0=t0,
        token1=t1,
        dec0=dec0,
        dec1=dec1,
        dex=dex,
    )


def encode_multicall_calls(
    calls: Sequence[tuple[str, bytes]],
    require_success: bool = False,
) -> str:
    """Encode tryAggregate(bool requireSuccess, (address,bytes)[] calls) calldata."""
    normalized_calls = [(Web3.to_checksum_address(addr), data) for addr, data in calls]
    encoded_args = abi_encode(["bool", "(address,bytes)[]"], [require_success, normalized_calls])
    sel = (
        TRY_AGGREGATE_SELECTOR
        if TRY_AGGREGATE_SELECTOR.startswith("0x")
        else "0x" + TRY_AGGREGATE_SELECTOR
    )
    return sel + encoded_args.hex()


def decode_multicall_response(raw_hex: str) -> list[tuple[bool, bytes]]:
    """Decode tryAggregate return data ((bool,bytes)[]).

    Raises:
        ValueError: If hex string or ABI encoding is malformed.
    """
    if not isinstance(raw_hex, str):
        raise ValueError(f"Expected hex string, got {type(raw_hex).__name__}")
    clean_hex = raw_hex[2:] if raw_hex.startswith("0x") else raw_hex
    if not clean_hex:
        return []
    try:
        raw_bytes = bytes.fromhex(clean_hex)
    except ValueError as exc:
        raise ValueError(f"Malformed hex in multicall response: {exc}") from exc
    try:
        decoded = abi_decode(["(bool,bytes)[]"], raw_bytes)[0]
    except Exception as exc:
        raise ValueError(f"Malformed ABI in multicall response: {exc}") from exc
    return list(decoded)


def encode_pool_slot0_call(
    pool: AnyPool,
    state_view_address: str = STATE_VIEW_ADDRESS,
) -> tuple[str, bytes]:
    """Generate (target, callData) tuple for slot0 / StateView.getSlot0 query."""
    addr = pool.address.strip()
    if len(addr) == 42:
        is_v2 = (
            getattr(pool, "is_v2", False)
            or "v2" in getattr(pool, "dex", "").lower()
            or "uniswap-v2" in getattr(pool, "label", "").lower()
        )
        if is_v2:
            v2_sel = (
                GET_RESERVES_SELECTOR[2:]
                if GET_RESERVES_SELECTOR.startswith("0x")
                else GET_RESERVES_SELECTOR
            )
            return (Web3.to_checksum_address(addr), bytes.fromhex(v2_sel))
        # V3 pool: target = pool.address, selector = 0x3850c7bd
        v3_sel = V3_SLOT0_SELECTOR[2:] if V3_SLOT0_SELECTOR.startswith("0x") else V3_SLOT0_SELECTOR
        return (Web3.to_checksum_address(addr), bytes.fromhex(v3_sel))
    elif len(addr) == 66:
        # V4 pool: target = StateView, selector = 0xc815641c + bytes32 poolId
        pool_id_hex = addr[2:] if addr.startswith("0x") else addr
        v4_sel = (
            STATE_VIEW_GET_SLOT0_SELECTOR[2:]
            if STATE_VIEW_GET_SLOT0_SELECTOR.startswith("0x")
            else STATE_VIEW_GET_SLOT0_SELECTOR
        )
        calldata = bytes.fromhex(v4_sel) + bytes.fromhex(pool_id_hex)
        return (Web3.to_checksum_address(state_view_address), calldata)
    else:
        raise ValueError(f"Unsupported pool address format: {addr}")


def decode_quote_from_result(
    pool: AnyPool,
    success: bool,
    retdata: bytes,
    block_number: int,
    ts: float | None = None,
) -> PriceQuote | None:
    """Decode raw multicall return data for a single pool into PriceQuote.

    Isolates per-pool reverts and malformed return data: returns None instead of raising.
    Uniswap V4 StateView.getSlot0 requires complete 128-byte retdata.
    """
    if not success:
        logger.debug("Multicall return failure: pool %s reverted", getattr(pool, "label", str(pool)))
        return None

    dec0 = getattr(pool, "dec0", None)
    dec1 = getattr(pool, "dec1", None)
    if dec0 is None or dec1 is None:
        raise ValueError(
            f"Pool {getattr(pool, 'label', str(pool))} missing explicit dec0/dec1: "
            f"dec0={dec0}, dec1={dec1}; defaulting to 18 is strictly forbidden"
        )
    if not isinstance(dec0, int) or isinstance(dec0, bool) or not (0 <= dec0 <= 18):
        raise ValueError(f"Invalid dec0 {dec0} for pool {getattr(pool, 'label', str(pool))}")
    if not isinstance(dec1, int) or isinstance(dec1, bool) or not (0 <= dec1 <= 18):
        raise ValueError(f"Invalid dec1 {dec1} for pool {getattr(pool, 'label', str(pool))}")
    is_v2 = (
        getattr(pool, "is_v2", False)
        or "v2" in getattr(pool, "dex", "").lower()
        or "uniswap-v2" in getattr(pool, "label", "").lower()
    )
    is_v4 = len(getattr(pool, "address", "")) == 66 or isinstance(pool, V4PoolSpec)

    t0 = getattr(pool, "token0", "").lower()
    t1 = getattr(pool, "token1", "").lower()

    raw_fee: int | None = None
    fee_denominator: int | None = None

    if is_v2:
        if len(retdata) < 64:
            logger.debug(
                "Multicall return data too short (%d bytes): V2 pool %s",
                len(retdata),
                getattr(pool, "label", str(pool)),
            )
            return None
        reserve0 = int.from_bytes(retdata[:32], "big")
        reserve1 = int.from_bytes(retdata[32:64], "big")
        if reserve0 <= 0 or reserve1 <= 0:
            logger.debug(
                "Multicall V2 decode error: pool %s reserves (%d, %d)",
                getattr(pool, "label", str(pool)),
                reserve0,
                reserve1,
            )
            return None
        p_t1_per_t0 = (reserve1 / reserve0) * (10 ** (dec0 - dec1))
    elif is_v4:
        # Uniswap V4 StateView.getSlot0 returns exactly 128 bytes:
        # (uint160 sqrtPriceX96, int24 tick, uint24 protocolFee, uint24 lpFee)
        # Truncated retdata (e.g. 32 bytes) must be rejected in all modes.
        if len(retdata) != 128:
            logger.debug(
                "Multicall return data invalid length (%d bytes, expected 128): V4 pool %s",
                len(retdata),
                getattr(pool, "label", str(pool)),
            )
            return None
        sqrt_price_x96 = int.from_bytes(retdata[:32], "big")
        if sqrt_price_x96 <= 0:
            logger.debug(
                "Multicall decode error: V4 pool %s sqrtPriceX96 <= 0 (%d)",
                getattr(pool, "label", str(pool)),
                sqrt_price_x96,
            )
            return None
        raw_fee = int.from_bytes(retdata[96:128], "big")
        fee_denominator = 1_000_000

        if t0 and t1 and t0 > t1:
            c1_per_c0 = (sqrt_price_x96 / _Q96) ** 2 * (10 ** (dec1 - dec0))
            p_t1_per_t0 = 1.0 / c1_per_c0 if c1_per_c0 > 0 else 0.0
        else:
            p_t1_per_t0 = (sqrt_price_x96 / _Q96) ** 2 * (10 ** (dec0 - dec1))
    else:
        # Uniswap V3 slot0()
        if len(retdata) < 32:
            logger.debug(
                "Multicall return data too short (%d bytes): pool %s",
                len(retdata),
                getattr(pool, "label", str(pool)),
            )
            return None

        # First 32 bytes is sqrtPriceX96 (uint160)
        sqrt_price_x96 = int.from_bytes(retdata[:32], "big")
        if sqrt_price_x96 <= 0:
            logger.debug(
                "Multicall decode error: pool %s sqrtPriceX96 <= 0 (%d)",
                getattr(pool, "label", str(pool)),
                sqrt_price_x96,
            )
            return None

        if t0 and t1 and t0 > t1:
            c1_per_c0 = (sqrt_price_x96 / _Q96) ** 2 * (10 ** (dec1 - dec0))
            p_t1_per_t0 = 1.0 / c1_per_c0 if c1_per_c0 > 0 else 0.0
        else:
            p_t1_per_t0 = (sqrt_price_x96 / _Q96) ** 2 * (10 ** (dec0 - dec1))

    if t0 and t1:
        if t0 < t1:
            base, quote = t0, t1
            price = p_t1_per_t0
        else:
            base, quote = t1, t0
            price = 1.0 / p_t1_per_t0 if p_t1_per_t0 else 0.0
    else:
        base = getattr(pool, "token0", "token0")
        quote = getattr(pool, "token1", "token1")
        price = p_t1_per_t0

    label = getattr(pool, "label", "")
    base_sym = ""
    quote_sym = ""
    if "/" in label:
        parts = label.split("/")
        sym0 = parts[0].strip()
        sym1 = parts[1].split()[0].strip()
        if base == t0 and quote == t1:
            base_sym, quote_sym = sym0, sym1
        elif base == t1 and quote == t0:
            base_sym, quote_sym = sym1, sym0
        else:
            base_sym, quote_sym = sym0, sym1

    return PriceQuote(
        pool=pool,
        base=base,
        quote=quote,
        price=price,
        raw_price_t1_per_t0=p_t1_per_t0,
        base_symbol=base_sym,
        quote_symbol=quote_sym,
        block_number=block_number,
        ts=ts if ts is not None else time.time(),
        raw_fee=raw_fee,
        fee_denominator=fee_denominator,
    )


class MulticallPoolReader:
    """Read-only batch pool reader via L2 Multicall2 tryAggregate.

    Decoupled from RobinhoodRpc and external funds modules.
    Accepts any ReadOnlyRpcTransport protocol or object with .call() method.
    """

    def __init__(
        self,
        rpc: ReadOnlyRpcTransport | Any | None = None,
        multicall_address: str = MULTICALL2_ADDRESS,
        state_view_address: str = STATE_VIEW_ADDRESS,
        token_catalog: Any = None,
    ) -> None:
        self._rpc = rpc
        self.multicall_address = Web3.to_checksum_address(multicall_address)
        self.state_view_address = Web3.to_checksum_address(state_view_address)
        self.token_catalog = token_catalog

    def batch_quote_multicall(
        self,
        pools: Sequence[AnyPool],
        *,
        with_provenance: bool = False,
        token_catalog: Any = None,
    ) -> list[PriceQuote]:
        """Execute single-call tryAggregate(False, calls) to read pool quotes.

        call[0]: getBlockNumber()
        call[1..N]: slot0() (V3) or StateView.getSlot0(poolId) (V4)
        """
        if not pools:
            return []

        if self._rpc is None:
            raise RuntimeError(
                "Read-only RPC transport must be injected; direct network defaults are disabled."
            )

        # 1. Filter valid pools and build calls
        valid_pools: list[AnyPool] = []
        calls: list[tuple[str, bytes]] = []

        # call[0]: Multicall2 getBlockNumber()
        get_blk_sel = (
            GET_BLOCK_NUMBER_SELECTOR[2:]
            if GET_BLOCK_NUMBER_SELECTOR.startswith("0x")
            else GET_BLOCK_NUMBER_SELECTOR
        )
        calls.append(
            (
                self.multicall_address,
                bytes.fromhex(get_blk_sel),
            )
        )

        cat = token_catalog if token_catalog is not None else getattr(self, "token_catalog", None)
        for p in pools:
            try:
                adapted_p = p if isinstance(p, (PoolSpec, V4PoolSpec)) else adapt_pool_identity(p, token_catalog=cat)
                call_tuple = encode_pool_slot0_call(adapted_p, self.state_view_address)
                calls.append(call_tuple)
                valid_pools.append(adapted_p)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Failed to encode pool call %s: %s", getattr(p, "label", str(p)), exc)

        if not valid_pools:
            return []

        header: dict[str, Any] | None = None
        liquidity_indices: dict[int, int] = {}
        if with_provenance:
            response = self._rpc.call("eth_getBlockByNumber", ["latest", False])
            header = response.get("result")
            if not isinstance(header, dict):
                raise RuntimeError("Missing block header for quote provenance")
            _hex_value(header.get("hash"), 32)
            if int(header["number"], 16) <= 0 or int(header["timestamp"], 16) <= 0:
                raise RuntimeError("Invalid block header")
            for index, pool in enumerate(valid_pools):
                if getattr(pool, "is_v2", False):
                    continue
                liquidity_indices[index] = len(calls)
                if len(pool.address) == 66:
                    data = Web3.keccak(text="getLiquidity(bytes32)")[:4] + bytes.fromhex(
                        pool.address[2:]
                    )
                    calls.append((self.state_view_address, bytes(data)))
                else:
                    calls.append((pool.address, bytes(Web3.keccak(text="liquidity()")[:4])))

        # 2. Encode tryAggregate(False, calls)
        calldata = encode_multicall_calls(calls, require_success=False)

        # 3. Trigger eth_call via injected transport
        res = self._rpc.call(
            "eth_call",
            [
                {"to": self.multicall_address, "data": calldata},
                {"blockHash": header["hash"], "requireCanonical": True} if header else "latest",
            ],
        )
        raw_result = res.get("result")
        if not raw_result or not isinstance(raw_result, str) or raw_result == "0x":
            raise RuntimeError(f"Multicall returned invalid result: {raw_result!r}")

        # 4. Decode ((bool,bytes)[]) - fails closed on malformed hex or ABI
        try:
            decoded = decode_multicall_response(raw_result)
        except Exception as exc:
            raise RuntimeError(f"Multicall response decoding failed: {exc}") from exc

        if len(decoded) != len(calls):
            raise RuntimeError(
                f"Multicall return count mismatch: expected {len(calls)}, got {len(decoded)}"
            )

        # 5. Atomically parse call[0] block number
        b_success, b_data = decoded[0]
        if b_success and len(b_data) >= 32:
            atomic_block_number = int.from_bytes(b_data[:32], "big")
        else:
            raise RuntimeError("Multicall block identity unavailable; cannot relabel old prices")
        if header is not None:
            if atomic_block_number != int(header["number"], 16):
                raise RuntimeError("Multicall block number disagrees with requested block hash")
            rechecked = self._rpc.call("eth_getBlockByNumber", [header["number"], False]).get(
                "result"
            )
            if not isinstance(rechecked, dict) or _hex_value(rechecked.get("hash"), 32) != _hex_value(
                header["hash"], 32
            ):
                raise RuntimeError("Quote block reorganized")

        # 6. Parse quotes for each pool with single-pool revert isolation
        now_ts = time.time()
        quotes: list[PriceQuote] = []
        for i, pool in enumerate(valid_pools):
            success, retdata = decoded[i + 1]
            quote = decode_quote_from_result(
                pool=pool,
                success=success,
                retdata=retdata,
                block_number=atomic_block_number,
                ts=now_ts,
            )
            if quote is not None:
                if header is not None:
                    quote.block_hash = _hex_value(header["hash"], 32)
                    quote.block_timestamp = int(header["timestamp"], 16)
                    if i in liquidity_indices:
                        ok, liquidity_data = decoded[liquidity_indices[i]]
                        if not ok or len(liquidity_data) != 32:
                            continue
                        quote.active_liquidity = int.from_bytes(liquidity_data, "big")
                        if quote.active_liquidity <= 0 or quote.active_liquidity >= 2**128:
                            continue
                    if len(pool.address) == 66:
                        if len(retdata) != 128:
                            continue
                        quote.raw_fee = int.from_bytes(retdata[96:128], "big")
                        quote.fee_denominator = 1_000_000
                quotes.append(quote)

        return quotes
