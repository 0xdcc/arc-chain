"""Uniswap V4 现货读取与状态解码纯研究子集.

本模块严格遵守 AGENTS.md 金融规范与离线研究约束:
1. 纯只读解耦: 接受任何符合 ReadOnlyRpcTransport 规范的 RPC 客户端;
2. 零真实资金与网络操作: 无 live 网络请求, 无 endpoint 构造, 无 network loop;
3. 严格精度换算: sqrtPriceX96 到代币价格计算显式使用 dec0/dec1 缩放, 杜绝隐式默认 18;
4. 反向报价支持: 支持 token0/token1 与 base/quote 顺序不一致时的严格倒数与规范归一化;
5. 明确区分 packed32 slot (extsload/eth_getStorageAt) 与 StateView128 ABI 解码入口;
6. ZeroSlippageError 局部异常防护, 彻底解耦 legacy walletguard;
7. 费用扫描与过滤逻辑由 fee_verification 分支独立负责, 本模块仅提供纯解码契约.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from eth_abi.abi import decode as abi_decode
from eth_abi.abi import encode as abi_encode
from web3 import Web3

from research.market_data.multicall import (
    PriceQuote,
    ReadOnlyRpcTransport,
)
from research.market_data.multicall import (
    V4PoolSpec as MulticallV4PoolSpec,
)

logger = logging.getLogger(__name__)

# Uniswap V4 Robinhood 链官方常量 (仅作为离线测试夹具与默认参数)
POOL_MANAGER_ADDRESS = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
POOLS_SLOT = 6  # PoolManager 中 pools mapping 的基槽位
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# 函数选择器: extsload(bytes32) -> 0x1e2eaeaf
EXTSLOAD_SELECTOR = "0x1e2eaeaf"

# V4 Swap 事件签名: Swap(bytes32 indexed id, address indexed sender, int128 amount0, int128 amount1, uint160 sqrtPriceX96, uint128 liquidity, int24 tick, uint24 fee)
SWAP_EVENT_TOPIC0 = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"

_Q96 = 2**96


class ZeroSlippageError(ValueError):
    """当 V4 池状态为异常零价格或滑点不合法时抛出.

    作为只读保护异常, 避免依赖 legacy core.wallet_guard.
    """


def compute_v4_pool_id(
    currency0: str,
    currency1: str,
    fee: int,
    tick_spacing: int,
    hooks: str = ZERO_ADDRESS,
) -> str:
    """计算 Uniswap V4 PoolId: keccak256(abi.encode(currency0, currency1, fee, tickSpacing, hooks)).

    Uniswap 规范: currency0 < currency1.
    """
    c0 = Web3.to_checksum_address(currency0)
    c1 = Web3.to_checksum_address(currency1)
    if int(c0, 16) > int(c1, 16):
        c0, c1 = c1, c0
    h_addr = Web3.to_checksum_address(hooks) if hooks else ZERO_ADDRESS
    encoded = abi_encode(
        ["address", "address", "uint24", "int24", "address"],
        [c0, c1, int(fee), int(tick_spacing), h_addr],
    )
    return "0x" + Web3.keccak(encoded).hex().lower()


@dataclass
class V4PoolSpec(MulticallV4PoolSpec):
    """Uniswap V4 池规格 (32 字节 PoolId), 强化 32 字节 hex 严格格式校验."""

    hooks: str = ZERO_ADDRESS
    dex: str = "uniswap-v4"

    @property
    def is_v2(self) -> bool:
        return False

    def __post_init__(self) -> None:
        """校验 PoolId 必须为 32 字节 hex (含 0x 共 66 字符)."""
        addr = self.address.lower()
        if not addr.startswith("0x") or len(addr) != 66:
            raise ValueError(
                f"V4 PoolId must be 32-byte hex (66 chars with 0x), got {len(self.address)}: {self.address}"
            )
        try:
            int(addr[2:], 16)
        except ValueError as exc:
            raise ValueError(f"Invalid hex characters in PoolId: {self.address}") from exc

        if self.hooks and self.hooks != ZERO_ADDRESS:
            try:
                Web3.to_checksum_address(self.hooks)
            except Exception as exc:
                raise ValueError(f"Invalid hooks address: {self.hooks}") from exc

    def compute_pool_id(self) -> str:
        """根据当前 token0, token1, fee_bps, tick_spacing, hooks 计算对应的 poolId."""
        fee_uint24 = int(round(self.fee_bps * 100))
        return compute_v4_pool_id(
            currency0=self.token0,
            currency1=self.token1,
            fee=fee_uint24,
            tick_spacing=self.tick_spacing,
            hooks=self.hooks,
        )


@dataclass
class V4PoolState:
    """Uniswap V4 池核心状态."""

    sqrt_price_x96: int
    tick: int
    protocol_fee: int
    lp_fee: int
    observed_block: int = 0
    observed_hash: str = ""
    from_log: bool = False

    def sqrt_price_x96_to_price(
        self,
        dec0: int = 18,
        dec1: int = 18,
        base_token: str | None = None,
        quote_token: str | None = None,
        token0: str | None = None,
        token1: str | None = None,
    ) -> float:
        """根据 sqrtPriceX96 与代币精度计算价格.

        默认计算 1 token0 对应多少 token1 (price = (sqrt/2^96)^2 * 10^(dec0-dec1)).
        若传入 base_token 与 quote_token (且配合 token0 与 token1):
        - 当 base==token0 且 quote==token1 时, 返回 1 token0 = ? token1;
        - 当 base==token1 且 quote==token0 时, 顺序相反, 正确取倒数 (1.0 / price).
        """
        if self.sqrt_price_x96 <= 0:
            return 0.0
        p_t1_per_t0 = (self.sqrt_price_x96 / _Q96) ** 2 * (10 ** (dec0 - dec1))
        if base_token and quote_token and token0 and token1:
            b = base_token.lower()
            q = quote_token.lower()
            t0 = token0.lower()
            t1 = token1.lower()
            if b == t1 and q == t0:
                return 1.0 / p_t1_per_t0 if p_t1_per_t0 > 0 else 0.0
            if b == t0 and q == t1:
                return p_t1_per_t0
        return p_t1_per_t0


def calculate_pool_storage_slot(pool_id: str, slot: int = POOLS_SLOT) -> str:
    """计算 V4 PoolId 在 PoolManager 中的 pools mapping 存储槽位置.

    规则: keccak256(poolId || uint256(slot)).
    """
    clean_id = pool_id.strip().lower()
    if clean_id.startswith("0x"):
        clean_id = clean_id[2:]
    if len(clean_id) != 64:
        raise ValueError(f"pool_id 必须为 32 字节 hex (64 字符), got len={len(clean_id)}: {pool_id}")

    try:
        id_bytes = bytes.fromhex(clean_id)
    except ValueError as exc:
        raise ValueError(f"pool_id 包含非法十六进制字符: {pool_id}") from exc

    slot_bytes = int(slot).to_bytes(32, byteorder="big")
    raw_slot = Web3.keccak(id_bytes + slot_bytes)
    return "0x" + raw_slot.hex().lower()


def decode_slot0_data(slot_hex: str | bytes) -> V4PoolState:
    """解析 PoolManager 存储槽位的 32 字节 Packed 数据 (extsload / eth_getStorageAt).

    布局 (从高位到低位, 256 bit):
        [231:208] 24 bit: lpFee (uint24)
        [207:184] 24 bit: protocolFee (uint24)
        [183:160] 24 bit: tick (int24, 2的补码)
        [159:0]   160 bit: sqrtPriceX96 (uint160)
    """
    if isinstance(slot_hex, bytes):
        clean = slot_hex.hex().lower()
    else:
        clean = slot_hex.strip().lower()
    if clean.startswith("0x"):
        clean = clean[2:]
    if not clean:
        raise ValueError("空 slot 数据，无法解码")

    try:
        val = int(clean, 16)
    except ValueError as exc:
        raise ValueError(f"非法十六进制 slot 数据: {slot_hex!r}") from exc

    sqrt_price_x96 = val & ((1 << 160) - 1)
    tick_raw = (val >> 160) & 0xFFFFFF
    # 24-bit 补码转换
    if tick_raw >= (1 << 23):
        tick = tick_raw - (1 << 24)
    else:
        tick = tick_raw

    protocol_fee = (val >> 184) & 0xFFFFFF
    lp_fee = (val >> 208) & 0xFFFFFF

    return V4PoolState(
        sqrt_price_x96=sqrt_price_x96,
        tick=tick,
        protocol_fee=protocol_fee,
        lp_fee=lp_fee,
    )


def decode_stateview_slot0_data(data: bytes | str) -> tuple[int, int, int, int]:
    """解析 StateView.getSlot0(bytes32 poolId) 返回的 128 字节 ABI 编码数据.

    返回值:
        (sqrt_price_x96, tick, protocol_fee, lp_fee)
    说明:
        为费用核验分支 (fee_verification) 提供显式 StateView ABI 解码入口,
        绝不与 packed32 slot 混淆.
    """
    if isinstance(data, str):
        clean = data.strip().lower()
        if clean.startswith("0x"):
            clean = clean[2:]
        raw_bytes = bytes.fromhex(clean)
    else:
        raw_bytes = data

    if len(raw_bytes) != 128:
        raise ValueError(f"StateView slot0 数据必须为 128 字节, got len={len(raw_bytes)}")

    decoded = abi_decode(["uint160", "int24", "uint24", "uint24"], raw_bytes)
    return int(decoded[0]), int(decoded[1]), int(decoded[2]), int(decoded[3])


def decode_stateview_slot0_state(data: bytes | str) -> V4PoolState:
    """解析 StateView.getSlot0(bytes32 poolId) 返回数据为 V4PoolState 对象."""
    sqrt_price_x96, tick, protocol_fee, lp_fee = decode_stateview_slot0_data(data)
    return V4PoolState(
        sqrt_price_x96=sqrt_price_x96,
        tick=tick,
        protocol_fee=protocol_fee,
        lp_fee=lp_fee,
    )


def decode_swap_log_data(data_hex: str | bytes) -> dict[str, Any]:
    """解码 V4 Swap 事件非 indexed 的 6 个 32 字节 word (共 192 字节).

    字段布局:
        word 0: int128 amount0 (符号数补码)
        word 1: int128 amount1 (符号数补码)
        word 2: uint160 sqrtPriceX96
        word 3: uint128 liquidity
        word 4: int24 tick (符号数补码)
        word 5: uint24 fee
    """
    if isinstance(data_hex, bytes):
        clean = data_hex.hex().lower()
    else:
        clean = data_hex.strip().lower()
    if clean.startswith("0x"):
        clean = clean[2:]

    if len(clean) < 64 * 6:
        raise ValueError(f"Swap 日志数据不足 6 个 word, got len={len(clean)}")

    words = [clean[i : i + 64] for i in range(0, 64 * 6, 64)]

    def _to_signed_256(hex_str: str) -> int:
        v = int(hex_str, 16)
        if v >= (1 << 255):
            v -= 1 << 256
        return v

    amount0 = _to_signed_256(words[0])
    amount1 = _to_signed_256(words[1])
    sqrt_price_x96 = int(words[2], 16)
    liquidity = int(words[3], 16)

    tick_val = int(words[4], 16)
    # ABI 中 int24 在 32 字节 word 中符号扩展
    tick = tick_val if tick_val < (1 << 255) else tick_val - (1 << 256)
    fee = int(words[5], 16)

    return {
        "amount0": amount0,
        "amount1": amount1,
        "sqrt_price_x96": sqrt_price_x96,
        "liquidity": liquidity,
        "tick": tick,
        "fee": fee,
    }


class V4Reader:
    """Uniswap V4 现货状态读取器 (纯只读 RPC 解耦)."""

    def __init__(
        self,
        rpc: ReadOnlyRpcTransport | Any | None = None,
        pool_manager: str = POOL_MANAGER_ADDRESS,
    ) -> None:
        self._rpc = rpc
        self._pm = Web3.to_checksum_address(pool_manager)
        self._w3 = Web3()

    def read_pool_state_via_extsload(
        self, pool_id: str, block_number: int | None = None
    ) -> V4PoolState:
        """首选方案: 调用 PoolManager 的 extsload(bytes32) 读取 slot0."""
        if self._rpc is None:
            raise ValueError("RPC client is required for read_pool_state_via_extsload")
        storage_slot = calculate_pool_storage_slot(pool_id)
        call_data = EXTSLOAD_SELECTOR + storage_slot[2:]
        tag = hex(block_number) if block_number is not None else "latest"
        resp = self._rpc.call("eth_call", [{"to": self._pm, "data": call_data}, tag])
        result = resp.get("result") if isinstance(resp, dict) else resp
        if not result or result == "0x":
            raise RuntimeError(f"extsload returned empty data for pool {pool_id}")
        return decode_slot0_data(result)

    def read_pool_state_via_storage_at(
        self, pool_id: str, block_number: int | None = None
    ) -> V4PoolState:
        """次选方案: 直接发起 eth_getStorageAt 探查."""
        if self._rpc is None:
            raise ValueError("RPC client is required for read_pool_state_via_storage_at")
        storage_slot = calculate_pool_storage_slot(pool_id)
        tag = hex(block_number) if block_number is not None else "latest"
        resp = self._rpc.call("eth_getStorageAt", [self._pm, storage_slot, tag])
        result = resp.get("result") if isinstance(resp, dict) else resp
        if not result or result == "0x":
            raise RuntimeError(f"eth_getStorageAt returned empty data for pool {pool_id}")
        return decode_slot0_data(result)

    def read_pool_state_via_logs(
        self,
        pool_id: str,
        block_window: int = 50,
        to_block: int | None = None,
    ) -> V4PoolState:
        """兜底方案: 查询近窗 Swap 事件日志反推最新成交价格."""
        if self._rpc is None:
            raise ValueError("RPC client is required for read_pool_state_via_logs")
        if to_block is None:
            try:
                latest_resp = self._rpc.call("eth_blockNumber", [])
                raw_val = (
                    latest_resp.get("result", "0x0")
                    if isinstance(latest_resp, dict)
                    else latest_resp
                )
                latest = int(raw_val, 16) if isinstance(raw_val, str) else int(raw_val)
            except Exception:
                latest = 0
        else:
            latest = to_block
        from_block = max(0, latest - block_window)
        pid = "0x" + pool_id.lower().replace("0x", "")

        resp = self._rpc.call(
            "eth_getLogs",
            [
                {
                    "address": self._pm,
                    "fromBlock": hex(from_block),
                    "toBlock": hex(latest),
                    "topics": [SWAP_EVENT_TOPIC0, pid],
                }
            ],
        )
        logs = resp.get("result", []) if isinstance(resp, dict) else resp
        if not logs:
            raise RuntimeError(
                f"No swap logs found for pool {pool_id} in blocks [{from_block}, {latest}]"
            )

        valid_logs = [
            log for log in logs if isinstance(log, dict) and not log.get("removed", False)
        ]
        if not valid_logs:
            raise RuntimeError("No canonical swap log available")
        last_log = max(
            valid_logs,
            key=lambda log: (
                int(log.get("blockNumber", "0x0"), 16)
                if isinstance(log.get("blockNumber"), str)
                else int(log.get("blockNumber", 0)),
                int(log.get("logIndex", "0x0"), 16)
                if isinstance(log.get("logIndex"), str)
                else int(log.get("logIndex", 0)),
            ),
        )
        decoded = decode_swap_log_data(last_log.get("data", "0x"))
        blk = (
            int(last_log.get("blockNumber", "0x0"), 16)
            if isinstance(last_log.get("blockNumber"), str)
            else int(last_log.get("blockNumber", 0))
        )
        return V4PoolState(
            sqrt_price_x96=decoded["sqrt_price_x96"],
            tick=decoded["tick"],
            protocol_fee=0,
            lp_fee=decoded["fee"],
            observed_block=blk,
            observed_hash=last_log.get("blockHash", ""),
            from_log=True,
        )

    def read_pool_state(
        self, pool_id: str, block_number: int | None = None
    ) -> V4PoolState:
        """三级降级读取 V4 池状态: extsload -> eth_getStorageAt -> getLogs."""
        try:
            return self.read_pool_state_via_extsload(pool_id, block_number=block_number)
        except Exception as exc1:
            logger.debug(
                "extsload failed for pool %s: %s, trying eth_getStorageAt",
                pool_id,
                exc1,
            )
            try:
                return self.read_pool_state_via_storage_at(pool_id, block_number=block_number)
            except Exception as exc2:
                logger.debug(
                    "eth_getStorageAt failed for pool %s: %s, trying getLogs",
                    pool_id,
                    exc2,
                )
                return self.read_pool_state_via_logs(pool_id, to_block=block_number)

    def quote(
        self,
        pool: V4PoolSpec,
        block_number: int | None = None,
        base: str | None = None,
        quote: str | None = None,
    ) -> PriceQuote:
        """读取 V4 池状态并组装为统一的 PriceQuote."""
        if block_number is None and self._rpc is not None:
            try:
                bn_resp = self._rpc.call("eth_blockNumber", [])
                raw_val = bn_resp.get("result", "0x0") if isinstance(bn_resp, dict) else bn_resp
                block_number = int(raw_val, 16) if isinstance(raw_val, str) else int(raw_val)
            except Exception:
                block_number = 0
        elif block_number is None:
            block_number = 0

        state = self.read_pool_state(pool.address, block_number=block_number)
        if state.sqrt_price_x96 <= 0:
            raise ZeroSlippageError(f"Pool {pool.label} sqrtPriceX96 <= 0, invalid state")

        t0 = pool.token0.lower() if pool.token0 else ""
        t1 = pool.token1.lower() if pool.token1 else ""

        if not t0 or not t1:
            raise ValueError(f"Pool {pool.label} missing token addresses, cannot quote safely")

        # Uniswap V4 底层 slot0 永远按底层 currency0 < currency1 排序 (较小地址为 c0)
        # 若 pool.token0 <= pool.token1, 则 pool.token0 即为底层 c0
        if t0 <= t1:
            p_t1_per_t0 = state.sqrt_price_x96_to_price(dec0=pool.dec0, dec1=pool.dec1)
        else:
            # pool.token0 > pool.token1: pool.token1 才是底层 c0
            c1_per_c0 = state.sqrt_price_x96_to_price(dec0=pool.dec1, dec1=pool.dec0)
            p_t1_per_t0 = 1.0 / c1_per_c0 if c1_per_c0 > 0 else 0.0

        if base and quote:
            req_base = base.lower()
            req_quote = quote.lower()
            if req_base == t0 and req_quote == t1:
                final_price = p_t1_per_t0
            elif req_base == t1 and req_quote == t0:
                final_price = 1.0 / p_t1_per_t0 if p_t1_per_t0 > 0 else 0.0
            else:
                final_price = p_t1_per_t0
            norm_base, norm_quote = req_base, req_quote
        else:
            if t0 < t1:
                norm_base, norm_quote = t0, t1
                final_price = p_t1_per_t0
            else:
                norm_base, norm_quote = t1, t0
                final_price = 1.0 / p_t1_per_t0 if p_t1_per_t0 > 0 else 0.0

        label = getattr(pool, "label", "")
        base_sym = ""
        quote_sym = ""
        if "/" in label:
            parts = label.split("/")
            sym0 = parts[0].strip()
            sym1 = parts[1].split()[0].strip()
            if norm_base == t0:
                base_sym, quote_sym = sym0, sym1
            elif norm_base == t1:
                base_sym, quote_sym = sym1, sym0
            else:
                base_sym, quote_sym = sym0, sym1

        effective_block = state.observed_block if state.from_log else block_number
        return PriceQuote(
            pool=pool,
            base=norm_base,
            quote=norm_quote,
            price=final_price,
            raw_price_t1_per_t0=p_t1_per_t0,
            base_symbol=base_sym,
            quote_symbol=quote_sym,
            block_number=effective_block if effective_block is not None else 0,
            block_hash=state.observed_hash if state.from_log else "",
            raw_fee=state.lp_fee,
            fee_denominator=1_000_000,
            ts=time.time(),
        )
