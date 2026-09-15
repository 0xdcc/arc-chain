"""Pool snapshot reading coordinator and spot price reader (Arc v3 research).

Coordinates atomic or consistent reads of multiple Uniswap V3 and V4 liquidity pools
and encapsulates results into M1's MarketSnapshot.

Features:
1. Multi-protocol support: Uniswap V3 (slot0 direct call) & Uniswap V4 (StateView.getSlot0).
2. Strong error isolation: Failure in one pool (revert, bad address, unknown fork)
   never crashes the batch; produces a degraded snapshot (sqrt_price_x96=None,
   liquidity=None, tick=None) or gracefully skips, with logging.
3. Block number consistency: All pool snapshots bind to the same block_number.
4. Flexible RPC integration: Supports Multicall2 batching with direct eth_call fallback.
5. UnifiedPoolReader alias for SnapshotCoordinator.
6. PoolReader spot price reader with Multicall priority and fallback.
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import time
from collections.abc import Callable, Sequence
from typing import Any

from eth_abi.abi import decode as abi_decode
from eth_abi.abi import encode as abi_encode
from web3 import Web3

from research.market_data.catalog import ROBINHOOD_CHAIN_ID, get_verified_token
from research.market_data.multicall import (
    GET_BLOCK_NUMBER_SELECTOR,
    GET_RESERVES_SELECTOR,
    MULTICALL2_ADDRESS,
    STATE_VIEW_ADDRESS,
    STATE_VIEW_GET_SLOT0_SELECTOR,
    TRY_AGGREGATE_SELECTOR,
    V3_SLOT0_SELECTOR,
    AnyPool,
    MulticallPoolReader,
    PoolSpec,
    PriceQuote,
    ReadOnlyRpcTransport,
    V4PoolSpec,
    adapt_pool_identity,
    decode_multicall_response,
    decode_quote_from_result,
    encode_multicall_calls,
    encode_pool_slot0_call,
)
from research.market_data.types import (
    MarketSnapshot,
    PoolIdentity,
    PoolStateSnapshot,
    TokenAmount,
    TokenIdentity,
)

logger = logging.getLogger(__name__)

__all__ = [
    "GET_BLOCK_NUMBER_SELECTOR",
    "GET_RESERVES_SELECTOR",
    "MULTICALL2_ADDRESS",
    "STATE_VIEW_ADDRESS",
    "STATE_VIEW_GET_SLOT0_SELECTOR",
    "TRY_AGGREGATE_SELECTOR",
    "V3_SLOT0_SELECTOR",
    "AnyPool",
    "MarketSnapshot",
    "MulticallPoolReader",
    "PoolIdentity",
    "PoolReader",
    "PoolSpec",
    "PoolStateSnapshot",
    "PriceQuote",
    "ROBINHOOD_CHAIN_ID",
    "ReadOnlyRpcTransport",
    "SnapshotCoordinator",
    "TokenAmount",
    "TokenIdentity",
    "UnifiedPoolReader",
    "V4PoolSpec",
    "_is_proxy_reachable",
    "adapt_pool_identity",
    "decode_multicall_response",
    "decode_quote_from_result",
    "decode_slot0",
    "encode_multicall_calls",
    "encode_pool_call",
    "encode_pool_slot0_call",
    "get_rpc_block_number",
    "get_verified_token",
    "is_v3_pool",
    "is_v4_pool",
    "probe_proxy",
    "read_market_snapshot",
]


def is_v4_pool(pool: Any) -> bool:
    """Determine whether pool is a Uniswap V4 pool."""
    proto = getattr(pool, "protocol", "").lower()
    if "v4" in proto:
        return True
    clean_id = (
        getattr(pool, "pool_id", None) or getattr(pool, "address", "") or ""
    ).strip().lower()
    if clean_id.startswith("0x") and len(clean_id) == 66:
        return True
    return False


def is_v3_pool(pool: Any) -> bool:
    """Determine whether pool is a Uniswap V3 or compatible pool."""
    if is_v4_pool(pool):
        return False
    proto = getattr(pool, "protocol", "").lower()
    if "v3" in proto:
        return True
    clean_id = (
        getattr(pool, "pool_id", None) or getattr(pool, "address", "") or ""
    ).strip().lower()
    if clean_id.startswith("0x") and len(clean_id) == 42:
        return True
    return False


def encode_pool_call(
    pool: Any,
    state_view_address: str = STATE_VIEW_ADDRESS,
) -> tuple[str, bytes]:
    """Encode the slot0 / getSlot0 call tuple (target_address, calldata) for a pool.

    Raises:
        ValueError: If pool ID or protocol format is unsupported.
    """
    clean_id = (
        getattr(pool, "pool_id", None) or getattr(pool, "address", "") or ""
    ).strip()
    if is_v4_pool(pool):
        if not clean_id.startswith("0x") or len(clean_id) != 66:
            raise ValueError(
                f"Invalid Uniswap V4 pool_id format (expected 32-byte hex): '{clean_id}'"
            )
        raw_id = clean_id[2:]
        sel_hex = (
            STATE_VIEW_GET_SLOT0_SELECTOR[2:]
            if STATE_VIEW_GET_SLOT0_SELECTOR.startswith("0x")
            else STATE_VIEW_GET_SLOT0_SELECTOR
        )
        calldata = bytes.fromhex(sel_hex) + bytes.fromhex(raw_id)
        return (Web3.to_checksum_address(state_view_address), calldata)

    if is_v3_pool(pool):
        if not clean_id.startswith("0x") or len(clean_id) != 42:
            raise ValueError(
                f"Invalid Uniswap V3 pool address format (expected 20-byte hex): '{clean_id}'"
            )
        sel_hex = (
            V3_SLOT0_SELECTOR[2:]
            if V3_SLOT0_SELECTOR.startswith("0x")
            else V3_SLOT0_SELECTOR
        )
        return (Web3.to_checksum_address(clean_id), bytes.fromhex(sel_hex))

    proto = getattr(pool, "protocol", "unknown")
    raise ValueError(f"Unsupported pool protocol or address format: {proto} ({clean_id})")


def decode_slot0(retdata: bytes, is_v4: bool = False) -> tuple[int, int]:
    """Decode slot0 return data into (sqrt_price_x96, tick).

    Supports ABI-encoded returns (uint160, int24, ...) and packed 32-byte slot0.

    Raises:
        ValueError: If data is invalid, empty, or sqrt_price_x96 <= 0.
    """
    if len(retdata) < 32:
        raise ValueError(f"Slot0 return data too short ({len(retdata)} bytes, expected >= 32)")

    if len(retdata) >= 64:
        sqrt_price_x96 = int.from_bytes(retdata[0:32], "big")
        tick = int.from_bytes(retdata[32:64], "big", signed=True)
    elif len(retdata) == 32:
        # Packed 32-byte slot0
        val = int.from_bytes(retdata, "big")
        sqrt_price_x96 = val & ((1 << 160) - 1)
        tick_raw = (val >> 160) & 0xFFFFFF
        tick = tick_raw if tick_raw < 0x800000 else tick_raw - 0x1000000
    else:
        raise ValueError(f"Unexpected slot0 return data length: {len(retdata)}")

    if sqrt_price_x96 <= 0:
        raise ValueError(f"Invalid non-positive sqrt_price_x96: {sqrt_price_x96}")

    return sqrt_price_x96, tick


def _extract_rpc_result(res: Any) -> str | None:
    """Safely extract hex string result from RPC response."""
    if isinstance(res, dict):
        if res.get("error"):
            raise RuntimeError(f"RPC returned error: {res['error']}")
        res = res.get("result")
    if isinstance(res, str):
        return res
    if isinstance(res, bytes):
        return "0x" + res.hex()
    return None


def get_rpc_block_number(rpc: Any) -> int:
    """Retrieve the current block number from an RPC client or return 0."""
    if rpc is None:
        return 0
    # 1. Try rpc.block_number
    if hasattr(rpc, "block_number"):
        try:
            b = rpc.block_number
            if callable(b):
                b = b()
            if isinstance(b, int) and not isinstance(b, bool):
                return b
            if isinstance(b, str):
                return int(b, 16) if b.startswith("0x") else int(b)
        except Exception:
            pass
    # 2. Try rpc.call("eth_blockNumber", [])
    if hasattr(rpc, "call") and callable(rpc.call):
        try:
            res = rpc.call("eth_blockNumber", [])
            out = _extract_rpc_result(res)
            if out is not None:
                return int(out, 16) if out.startswith("0x") else int(out)
        except Exception:
            pass
    # 3. Try web3-style rpc.eth.block_number
    if hasattr(rpc, "eth") and hasattr(rpc.eth, "block_number"):
        try:
            b = rpc.eth.block_number
            if callable(b):
                b = b()
            if isinstance(b, int) and not isinstance(b, bool):
                return b
            if isinstance(b, str):
                return int(b, 16) if b.startswith("0x") else int(b)
        except Exception:
            pass
    return 0


class SnapshotCoordinator:
    """Multi-pool snapshot reading coordinator ensuring block consistency and error isolation."""

    def __init__(
        self,
        rpc: Any = None,
        multicall_address: str = MULTICALL2_ADDRESS,
        state_view_address: str = STATE_VIEW_ADDRESS,
        chain_id: int = ROBINHOOD_CHAIN_ID,
        use_multicall: bool = True,
        token_catalog: Any = None,
    ) -> None:
        self.rpc = rpc
        self.multicall_address = multicall_address
        self.state_view_address = state_view_address
        self.chain_id = chain_id
        self.use_multicall = use_multicall
        self.token_catalog = token_catalog

    def read_market_snapshot(
        self_or_pools: Any,
        pools: Any = None,
        rpc: Any = None,
        block_number: int | None = None,
        *,
        use_multicall: bool | None = None,
        skip_failed: bool = False,
    ) -> MarketSnapshot:
        """Read a consistent MarketSnapshot across multiple pools.

        Can be called as an instance method:
            coordinator.read_market_snapshot(pools, rpc=None, block_number=None)
        or as a class/static method:
            SnapshotCoordinator.read_market_snapshot(pools, rpc=rpc, block_number=block_number)
        """
        if isinstance(self_or_pools, SnapshotCoordinator):
            self = self_or_pools
            actual_pools = pools if pools is not None else []
            active_rpc = rpc if rpc is not None else self.rpc
            actual_block = block_number
            active_multicall = (
                use_multicall if use_multicall is not None else self.use_multicall
            )
            active_chain_id = self.chain_id
        else:
            # Called as class method: SnapshotCoordinator.read_market_snapshot(...)
            actual_pools = self_or_pools if self_or_pools is not None else []
            if rpc is not None and not isinstance(rpc, int):
                active_rpc = rpc
                actual_block = block_number
            elif isinstance(pools, int):
                active_rpc = None
                actual_block = pools
            else:
                active_rpc = pools
                actual_block = (
                    block_number
                    if block_number is not None
                    else (rpc if isinstance(rpc, int) else None)
                )
            active_multicall = use_multicall if use_multicall is not None else True
            active_chain_id = ROBINHOOD_CHAIN_ID
            self = SnapshotCoordinator(
                rpc=active_rpc,
                chain_id=active_chain_id,
                use_multicall=active_multicall,
            )

        # Boundary condition: empty pools list
        if not actual_pools:
            resolved_block = (
                actual_block
                if actual_block is not None
                else get_rpc_block_number(active_rpc)
            )
            return MarketSnapshot(
                chain_id=active_chain_id,
                block_number=resolved_block,
                captured_at=time.time(),
                pools={},
            )

        if active_multicall:
            try:
                return self._read_multicall(
                    pools=actual_pools,
                    rpc=active_rpc,
                    block_number=actual_block,
                    skip_failed=skip_failed,
                )
            except Exception as exc:
                logger.warning(
                    "Multicall execution failed (%s), falling back to direct eth_call",
                    exc,
                )
                return self._read_direct(
                    pools=actual_pools,
                    rpc=active_rpc,
                    block_number=actual_block,
                    skip_failed=skip_failed,
                )
        else:
            return self._read_direct(
                pools=actual_pools,
                rpc=active_rpc,
                block_number=actual_block,
                skip_failed=skip_failed,
            )

    def _read_multicall(
        self,
        pools: Sequence[Any],
        rpc: Any,
        block_number: int | None,
        skip_failed: bool,
    ) -> MarketSnapshot:
        valid_pools: list[Any] = []
        calls: list[tuple[str, bytes]] = []
        snapshots: dict[str, PoolStateSnapshot] = {}

        # Call[0]: Multicall2 getBlockNumber()
        get_blk_sel = bytes.fromhex(
            GET_BLOCK_NUMBER_SELECTOR[2:]
            if GET_BLOCK_NUMBER_SELECTOR.startswith("0x")
            else GET_BLOCK_NUMBER_SELECTOR
        )
        calls.append((Web3.to_checksum_address(self.multicall_address), get_blk_sel))

        now_ts = int(time.time())

        # Encode calls with per-pool error isolation
        for p in pools:
            try:
                target, calldata = encode_pool_call(p, self.state_view_address)
                calls.append((target, calldata))
                valid_pools.append(p)
            except Exception as exc:
                logger.warning(
                    "Failed to encode call for pool %s: %s",
                    getattr(p, "pool_id", str(p)),
                    exc,
                )
                if not skip_failed and hasattr(p, "pool_id"):
                    snapshots[p.pool_id] = PoolStateSnapshot(
                        pool=p,
                        block_number=0,  # Updated once block is resolved
                        block_timestamp=now_ts,
                        sqrt_price_x96=None,
                        liquidity=None,
                        tick=None,
                        raw_response={"error": str(exc)},
                    )

        # Encode tryAggregate(requireSuccess=False, calls)
        normalized_calls = [(Web3.to_checksum_address(addr), data) for addr, data in calls]
        encoded_args = abi_encode(["bool", "(address,bytes)[]"], [False, normalized_calls])
        sel_hex = (
            TRY_AGGREGATE_SELECTOR
            if TRY_AGGREGATE_SELECTOR.startswith("0x")
            else "0x" + TRY_AGGREGATE_SELECTOR
        )
        calldata_hex = sel_hex + encoded_args.hex()

        tag = hex(block_number) if block_number is not None and block_number > 0 else "latest"
        res = rpc.call("eth_call", [{"to": self.multicall_address, "data": calldata_hex}, tag])
        hex_data = _extract_rpc_result(res)
        if not hex_data or hex_data == "0x":
            raise RuntimeError(f"Multicall returned invalid result: {hex_data!r}")

        clean_hex = hex_data[2:] if hex_data.startswith("0x") else hex_data
        raw_bytes = bytes.fromhex(clean_hex)
        decoded = list(abi_decode(["(bool,bytes)[]"], raw_bytes)[0])

        if len(decoded) != len(calls):
            raise RuntimeError(
                f"Multicall return count mismatch: expected {len(calls)}, got {len(decoded)}"
            )

        # Resolve block number from call[0] if block_number not explicitly passed
        b_success, b_data = decoded[0]
        if block_number is not None:
            resolved_block = block_number
        elif b_success and len(b_data) >= 32:
            resolved_block = int.from_bytes(b_data[:32], "big")
        else:
            resolved_block = get_rpc_block_number(rpc)

        # Align block_number on any pre-encoded failure snapshots
        for pid, snap in list(snapshots.items()):
            if snap.block_number != resolved_block:
                snapshots[pid] = PoolStateSnapshot(
                    pool=snap.pool,
                    block_number=resolved_block,
                    block_timestamp=snap.block_timestamp,
                    sqrt_price_x96=snap.sqrt_price_x96,
                    liquidity=snap.liquidity,
                    tick=snap.tick,
                    raw_response=snap.raw_response,
                )

        # Decode each pool's result
        for i, p in enumerate(valid_pools):
            success, retdata = decoded[i + 1]
            if not success:
                logger.warning("Pool %s reverted during multicall execution", p.pool_id)
                if not skip_failed:
                    snapshots[p.pool_id] = PoolStateSnapshot(
                        pool=p,
                        block_number=resolved_block,
                        block_timestamp=now_ts,
                        sqrt_price_x96=None,
                        liquidity=None,
                        tick=None,
                        raw_response={"reverted": True},
                    )
                continue

            try:
                sqrt_price_x96, tick = decode_slot0(retdata, is_v4=is_v4_pool(p))
                snapshots[p.pool_id] = PoolStateSnapshot(
                    pool=p,
                    block_number=resolved_block,
                    block_timestamp=now_ts,
                    sqrt_price_x96=sqrt_price_x96,
                    liquidity=None,
                    tick=tick,
                    raw_response={"raw_hex": "0x" + retdata.hex()},
                )
            except Exception as exc:
                logger.warning(
                    "Failed to decode slot0 response for pool %s: %s",
                    p.pool_id,
                    exc,
                )
                if not skip_failed:
                    snapshots[p.pool_id] = PoolStateSnapshot(
                        pool=p,
                        block_number=resolved_block,
                        block_timestamp=now_ts,
                        sqrt_price_x96=None,
                        liquidity=None,
                        tick=None,
                        raw_response={"error": str(exc)},
                    )

        chain_id = getattr(pools[0], "chain_id", self.chain_id) if pools else self.chain_id
        return MarketSnapshot(
            chain_id=chain_id,
            block_number=resolved_block,
            captured_at=time.time(),
            pools=snapshots,
        )

    def _read_direct(
        self,
        pools: Sequence[Any],
        rpc: Any,
        block_number: int | None,
        skip_failed: bool,
    ) -> MarketSnapshot:
        if block_number is not None:
            resolved_block = block_number
        else:
            resolved_block = get_rpc_block_number(rpc)

        tag = hex(resolved_block) if resolved_block > 0 else "latest"
        now_ts = int(time.time())
        snapshots: dict[str, PoolStateSnapshot] = {}

        for p in pools:
            try:
                target, calldata = encode_pool_call(p, self.state_view_address)
                res = rpc.call(
                    "eth_call",
                    [{"to": target, "data": "0x" + calldata.hex()}, tag],
                )
                hex_data = _extract_rpc_result(res)
                if not hex_data or hex_data == "0x":
                    raise RuntimeError(
                        f"Empty eth_call response for pool {getattr(p, 'pool_id', str(p))}"
                    )

                clean_hex = hex_data[2:] if hex_data.startswith("0x") else hex_data
                retdata = bytes.fromhex(clean_hex)
                sqrt_price_x96, tick = decode_slot0(retdata, is_v4=is_v4_pool(p))

                snapshots[p.pool_id] = PoolStateSnapshot(
                    pool=p,
                    block_number=resolved_block,
                    block_timestamp=now_ts,
                    sqrt_price_x96=sqrt_price_x96,
                    liquidity=None,
                    tick=tick,
                    raw_response={"raw_hex": hex_data},
                )
            except Exception as exc:
                logger.warning(
                    "Failed to read pool %s directly: %s",
                    getattr(p, "pool_id", str(p)),
                    exc,
                )
                if not skip_failed and hasattr(p, "pool_id"):
                    snapshots[p.pool_id] = PoolStateSnapshot(
                        pool=p,
                        block_number=resolved_block,
                        block_timestamp=now_ts,
                        sqrt_price_x96=None,
                        liquidity=None,
                        tick=None,
                        raw_response={"error": str(exc)},
                    )

        chain_id = getattr(pools[0], "chain_id", self.chain_id) if pools else self.chain_id
        return MarketSnapshot(
            chain_id=chain_id,
            block_number=resolved_block,
            captured_at=time.time(),
            pools=snapshots,
        )


UnifiedPoolReader = SnapshotCoordinator


def read_market_snapshot(
    pools: Sequence[PoolIdentity],
    rpc: Any = None,
    block_number: int | None = None,
    *,
    use_multicall: bool = True,
    skip_failed: bool = False,
) -> MarketSnapshot:
    """Read same-block MarketSnapshot across multiple pools."""
    coordinator = SnapshotCoordinator(rpc=rpc, use_multicall=use_multicall)
    return coordinator.read_market_snapshot(
        pools=pools,
        rpc=rpc,
        block_number=block_number,
        use_multicall=use_multicall,
        skip_failed=skip_failed,
    )


def _is_proxy_reachable(proxy_url: str, timeout: float = 0.2) -> bool:
    """Check whether a proxy endpoint is reachable without performing live network IO.

    Safe offline default returning False. Real network sockets are never opened
    unless explicitly configured via transport/reachable callbacks.
    """
    return False


def probe_proxy(
    proxy_url: str | None = None,
    candidate_urls: Sequence[str] | None = None,
    reachable_check: Callable[[str], bool] | None = None,
) -> str | None:
    """Select active proxy URL by explicit configuration or injected reachability check.

    - If explicit `proxy_url` is passed, it is directly returned (passthrough).
    - If `proxy_url` is None, checks environment variables or candidate URLs using
      the injected reachability check (defaulting to _is_proxy_reachable).
    - If none reachable, returns None.
    """
    if proxy_url:
        return proxy_url

    is_reachable = reachable_check if reachable_check is not None else _is_proxy_reachable

    env_proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
    if env_proxy and is_reachable(env_proxy):
        return env_proxy

    candidates = candidate_urls if candidate_urls is not None else ("http://127.0.0.1:7890",)
    for cand in candidates:
        if cand and is_reachable(cand):
            return cand

    return None


class PoolReader:
    """Spot price reader with priority Multicall2 batching and per-pool fallback."""

    @staticmethod
    def _decode_string(hexdata: str) -> str:
        """Decode ERC20 symbol/name return data from ABI hex payload.

        Handles:
        1. Standard ABI dynamic string: [offset 32B][length 32B][data bytes].
        2. Non-standard / unpadded ABI dynamic string (data truncated to length bytes).
        3. bytes32 string layout with trailing null bytes (left-aligned ASCII).
        4. Historical fixture compatibility: right-aligned in 32-byte word with leading nulls and 1-byte length prefix.
        5. bytes32 right-aligned ASCII string with leading null bytes.

        Safety & Failclosed Policy:
        - Fails closed on invalid hex formatting (raises ValueError).
        - Fails closed on truncated dynamic ABI string data or invalid utf-8 (raises ValueError).
        - Returns empty string ("") on empty input, 0x, or all-null bytes (preserves original contract).
        - Strictly never returns synthetic dummy symbols (e.g. "UNKNOWN") to mask decoding errors.
        """
        if not isinstance(hexdata, str):
            raise TypeError(f"Expected str, got {type(hexdata).__name__}")

        clean = hexdata.strip()
        if clean.startswith(("0x", "0X")):
            clean = clean[2:]

        if not clean:
            return ""

        try:
            raw = bytes.fromhex(clean)
        except ValueError as e:
            raise ValueError(f"Invalid hex string for ABI decoding: {e}") from e

        if not raw:
            return ""

        # 1. Standard ABI dynamic string: [32B offset][32B length][data...]
        if len(raw) >= 64:
            offset = int.from_bytes(raw[:32], "big")
            if offset == 32 or (32 <= offset <= len(raw) - 32 and offset % 32 == 0):
                length = int.from_bytes(raw[offset : offset + 32], "big")
                if offset + 32 + length > len(raw):
                    raise ValueError(
                        f"ABI dynamic string truncated or out of bounds: offset={offset}, "
                        f"length={length}, total_bytes={len(raw)}"
                    )
                data = raw[offset + 32 : offset + 32 + length]
                try:
                    return data.decode("utf-8").strip("\x00").strip()
                except UnicodeDecodeError as e:
                    raise ValueError(f"ABI dynamic string has invalid utf-8: {e}") from e

        # 2. bytes32 layout or unformatted raw bytes
        body = raw.rstrip(b"\x00")
        if not body:
            return ""

        candidate = body.lstrip(b"\x00")
        if not candidate:
            return ""

        # Historical fixture compatibility: strictly 32-byte raw with real leading nulls,
        # no trailing nulls, and a 1-byte length prefix in [1, 31] matching remaining payload length.
        if (
            len(raw) == 32
            and raw.startswith(b"\x00")
            and 1 <= candidate[0] <= 31
            and candidate[0] == len(candidate) - 1
            and raw == b"\x00" * (32 - len(candidate)) + candidate
        ):
            stripped = candidate[1:]
            try:
                return stripped.decode("utf-8").strip("\x00").strip()
            except UnicodeDecodeError as e:
                raise ValueError(f"Historical fixture string has invalid utf-8: {e}") from e

        # General bytes32 (left- or right-padded with nulls)
        try:
            return candidate.decode("utf-8").strip("\x00").strip()
        except UnicodeDecodeError as e:
            raise ValueError(f"String has invalid utf-8: {e}") from e

    def __init__(
        self,
        rpc: Any = None,
        proxy_url: str | None = None,
        multicall_reader: MulticallPoolReader | None = None,
        token_catalog: Any = None,
        batch_strategy: str = "multicall",
    ) -> None:
        self._rpc = rpc
        self.proxy_url = proxy_url
        self.token_catalog = token_catalog
        self.batch_strategy = batch_strategy
        self._multicall_reader: MulticallPoolReader | None = None
        if multicall_reader is not None:
            self._multicall_reader = multicall_reader
        elif rpc is not None:
            self._multicall_reader = MulticallPoolReader(rpc=rpc, token_catalog=token_catalog)

        # Transparent proxy configuration onto transport if supported
        if proxy_url is not None and rpc is not None:
            session = getattr(rpc, "_session", None)
            if session is not None:
                proxies = getattr(session, "proxies", None)
                if isinstance(proxies, dict):
                    proxies["http"] = proxy_url
                    proxies["https"] = proxy_url
            if hasattr(rpc, "proxy_url"):
                rpc.proxy_url = proxy_url

    def get_latest_block_number(self) -> int:
        """Fetch latest block number from injected RPC."""
        return get_rpc_block_number(getattr(self, "_rpc", None))

    def quote(self, pool: AnyPool, block_number: int | None = None) -> PriceQuote:
        """Read a single pool spot price quote directly via eth_call."""
        if block_number is None or block_number == 0:
            block_number = self.get_latest_block_number()

        target, calldata = encode_pool_slot0_call(pool)
        tag = hex(block_number) if block_number is not None and block_number > 0 else "latest"
        rpc = getattr(self, "_rpc", None)
        if rpc is None:
            raise RuntimeError("RPC transport must be configured to fetch quote")

        res = rpc.call("eth_call", [{"to": target, "data": "0x" + calldata.hex()}, tag])
        hex_data = _extract_rpc_result(res)
        if not hex_data or hex_data == "0x":
            raise RuntimeError(
                f"Empty eth_call result for pool {getattr(pool, 'label', str(pool))}"
            )

        clean_hex = hex_data[2:] if hex_data.startswith("0x") else hex_data
        retdata = bytes.fromhex(clean_hex)
        q = decode_quote_from_result(
            pool=pool,
            success=True,
            retdata=retdata,
            block_number=block_number,
        )
        if q is None:
            raise ValueError(
                f"Failed to decode slot0 quote for pool {getattr(pool, 'label', str(pool))}"
            )
        return q

    def batch_quote_multicall(self, pools: Sequence[AnyPool]) -> list[PriceQuote]:
        """Read pool quotes via Multicall2 tryAggregate."""
        mc_reader = getattr(self, "_multicall_reader", None)
        if mc_reader is None:
            mc_reader = MulticallPoolReader(
                rpc=getattr(self, "_rpc", None),
                token_catalog=getattr(self, "token_catalog", None),
            )
            self._multicall_reader = mc_reader
        return mc_reader.batch_quote_multicall(pools)

    def batch_quote(
        self,
        pools: Sequence[AnyPool],
        max_workers: int = 15,
        proxy_url: str | None = None,
        strategy: str | None = None,
    ) -> list[PriceQuote]:
        """Batch read pool quotes with Multicall priority and fallback.

        1. Prioritizes Multicall2 single RPC call to eliminate block skew.
        2. On Multicall error, gracefully falls back to querying per-pool at identical block height.
        3. Isolates per-pool reverts so individual failures do not block the batch.
        """
        if not pools:
            return []

        effective_strategy = (
            strategy
            or getattr(self, "batch_strategy", None)
            or getattr(self, "strategy", None)
            or "multicall"
        )

        # 1. Multicall2 priority (unless explicit thread mode configured)
        if effective_strategy != "thread":
            try:
                mc_reader = getattr(self, "_multicall_reader", None)
                if mc_reader is None:
                    mc_reader = MulticallPoolReader(
                        rpc=getattr(self, "_rpc", None),
                        token_catalog=getattr(self, "token_catalog", None),
                    )
                    self._multicall_reader = mc_reader
                quotes = mc_reader.batch_quote_multicall(pools)
                return quotes
            except Exception as mc_exc:
                logger.warning(
                    "Multicall batch quote failed (%s), falling back to per-pool read",
                    mc_exc,
                )

        # 2. Graceful fallback (or explicit thread mode) at identical block height to prevent block skew
        current_block = self.get_latest_block_number()
        rpc = getattr(self, "_rpc", None)
        prev_throttle = getattr(rpc, "throttle", None) if rpc is not None else None
        try:
            if rpc is not None and hasattr(rpc, "throttle"):
                rpc.throttle = 0.0

            quotes_fallback: list[PriceQuote] = []
            actual_workers = max(1, min(max_workers, len(pools)))

            def _read_pool(p: AnyPool) -> PriceQuote | None:
                try:
                    return self.quote(p, block_number=current_block)
                except Exception as exc:
                    logger.warning(
                        "Per-pool quote fallback failed for pool %s: %s",
                        getattr(p, "label", getattr(p, "address", str(p))),
                        exc,
                    )
                    return None

            with concurrent.futures.ThreadPoolExecutor(max_workers=actual_workers) as executor:
                for item in executor.map(_read_pool, pools):
                    if item is not None:
                        quotes_fallback.append(item)
            return quotes_fallback
        finally:
            if prev_throttle is not None and rpc is not None and hasattr(rpc, "throttle"):
                rpc.throttle = prev_throttle
