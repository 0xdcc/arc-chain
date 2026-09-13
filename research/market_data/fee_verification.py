"""Uniswap V3 / V4 链上真实费率读取与强校验纯研究模块.

本模块严格遵循 AGENTS.md 金融安全与离线研究规范:
1. 纯只读解耦: 强制要求外部注入 ReadOnlyRpcTransport / handler, 绝不隐式构造 HTTP 客户端或建立 live 网络连接;
2. 零资金与零真实网络操作: 纯链上 eth_call 只读读取, 无资金、无 approve、无交易组装;
3. 严格协议解析:
   - V3: 严格校验 dex=="uniswap-v3", 调 fee() selector (0xddca3f43), uint24 ppm 转化为 bps (ppm / 100.0);
   - V4: 严格按 32 字节 poolId 通过 StateView.getSlot0(poolId) (0xc815641c) 读取 128 字节 ABI 编码数据;
4. 异常与截断防御 (Fail-Closed):
   - 空结果 ("0x", "")、RPC 异常抛出 RuntimeError;
   - 截断数据 (V3 非 32 字节 word, V4 非 128 字节) 抛出 ValueError;
   - 动态费 (DYNAMIC_FEE_FLAG 0x800000) 绝不默认合法, 抛出 ValueError 阻断;
   - 费率越界 (< 0 或 > 1_000_000 ppm) 抛出 ValueError 阻断;
   - 绝对禁止使用 30.0 bps 或池名称猜测值兜底;
5. 明确支持 uint24 值为 0 (0.0 bps) 的合法情况, 与 RPC 返回空数据明确区分.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from eth_abi.abi import decode as abi_decode
from web3 import Web3

logger = logging.getLogger(__name__)

# Uniswap V3 函数选择器: fee() -> 0xddca3f43
V3_FEE_SELECTOR: str = "0xddca3f43"

# Uniswap V4 StateView 官方合约地址与选择器
STATE_VIEW_ADDRESS: str = "0xf3334192d15450cdd385c8b70e03f9a6bd9e673b"
STATE_VIEW_GET_SLOT0_SELECTOR: str = "0xc815641c"

# Uniswap V4 动态费率标志位 (lpFee 最高位 bit 23: 0x800000)
DYNAMIC_FEE_FLAG: int = 0x800000

# Uniswap V2 标准费率 (常量 30.0 bps, 仅供规范参考)
V2_STANDARD_FEE_BPS: float = 30.0


@runtime_checkable
class ReadOnlyRpcTransport(Protocol):
    """只读 RPC 传输抽象接口 (解耦 HTTP 构造与真实网络依赖)."""

    def call(self, method: str, params: list[Any]) -> Any:
        ...


def _invoke_rpc_call(rpc: Any, to_addr: str, calldata: str) -> Any:
    """调用注入的只读 RPC 处理器, 绝不隐式构造 HTTP 连接."""
    if rpc is None:
        raise ValueError(
            "ReadOnlyRpcTransport must be explicitly injected; default HTTP construction is prohibited"
        )

    params: list[Any] = [{"to": to_addr, "data": calldata}, "latest"]

    if hasattr(rpc, "call") and callable(rpc.call):
        return rpc.call("eth_call", params)
    elif callable(rpc):
        return rpc("eth_call", params)
    else:
        raise TypeError(
            f"Injected rpc must be callable or provide a .call() method, got: {type(rpc)!r}"
        )


def decode_v3_fee_data(raw: Any) -> float:
    """解析 V3 fee() 返回数据 (uint24 ppm) 并转换为 bps.

    EVM 标准: fee() 返回 uint24 填充为 32 字节 word (ppm, 1e-6).
    换算关系: 1 bps = 100 ppm, 即 fee_bps = fee_ppm / 100.0.
    """
    if raw is None:
        raise RuntimeError("eth_call returned None for fee()")

    if isinstance(raw, int):
        val = raw
    elif isinstance(raw, str):
        clean = raw.strip()
        if not clean or clean.lower() == "0x":
            raise RuntimeError("empty eth_call result for fee()")
        if clean.lower().startswith("0x"):
            clean_hex = clean[2:]
        else:
            clean_hex = clean

        if len(clean_hex) == 0:
            raise RuntimeError("empty eth_call result for fee()")

        try:
            val = int(clean_hex, 16)
        except ValueError as exc:
            raise ValueError(f"Invalid hex string returned for fee(): {raw!r}") from exc
    elif isinstance(raw, (bytes, bytearray)):
        if len(raw) == 0:
            raise RuntimeError("empty bytes returned for fee()")
        if len(raw) != 32:
            raise ValueError(f"V3 fee data must be 32 bytes word, got len={len(raw)}")
        val = int.from_bytes(raw, byteorder="big")
    else:
        raise ValueError(f"Unexpected return type for fee(): {type(raw)!r}")

    if not isinstance(val, int) or not (0 <= val <= 1_000_000):
        raise ValueError(f"Invalid fee value from on-chain: {val} (must be in [0, 1000000] ppm)")

    return float(val) / 100.0


def read_v3_pool_fee(
    addr: str,
    rpc: Any,
    *,
    dex: str = "uniswap-v3",
) -> float:
    """读取 Uniswap V3 (或分支) 池真实链上费率 (单位: bps).

    遵循严苛门禁规范:
    1. 必须提供已注入的只读 rpc, 杜绝 HTTP 连接构造;
    2. dex 必须为已验证适配器 ('uniswap-v3'), 未验证分支抛出 ValueError;
    3. 调用失败 (revert / 超时 / 空数据) 抛出异常 (fail-closed), 绝不使用 30.0 兜底;
    4. 返回值单位为 bps (100 ppm -> 1.0 bps, 3000 ppm -> 30.0 bps, 10000 ppm -> 100.0 bps);
    5. 合法 0 费率 (0 ppm -> 0.0 bps) 正确支持, 与空返回值截然区分.
    """
    if dex != "uniswap-v3":
        raise ValueError(f"Fee unit adapter unverified for {dex}: monitor-only")

    clean_addr = addr.strip()
    if not clean_addr.startswith(("0x", "0X")) or len(clean_addr) != 42:
        raise ValueError(
            f"V3 pool address must be 20-byte hex (42 characters with 0x), got {len(clean_addr)}: {addr!r}"
        )

    try:
        to_addr = Web3.to_checksum_address(clean_addr)
    except Exception as exc:
        raise ValueError(f"Invalid EVM address for V3 pool: {addr!r}") from exc

    res = _invoke_rpc_call(rpc, to_addr, V3_FEE_SELECTOR)
    raw = res.get("result") if isinstance(res, dict) else res

    if raw is None or raw == "" or raw == "0x" or raw == "0X":
        raise RuntimeError(f"empty eth_call result for fee() on {addr}")

    return decode_v3_fee_data(raw)


def decode_stateview_slot0_fee(data: bytes | str) -> float:
    """从 StateView.getSlot0 返回的 128 字节数据中解析 lpFee 并换算为 bps.

    StateView.getSlot0(bytes32 poolId) 返回 4 个 32 字节独立 word (共 128 字节):
        word 0 (0..32):   uint160 sqrtPriceX96
        word 1 (32..64):  int24 tick
        word 2 (64..96):  uint24 protocolFee
        word 3 (96..128): uint24 lpFee (ppm)

    安全校验:
    - 长度截断 (< 128 字节) 抛出 ValueError;
    - 动态费 (DYNAMIC_FEE_FLAG 0x800000 置位) 抛出 ValueError 阻断;
    - 越界费率 (> 1_000_000 或 < 0) 抛出 ValueError 阻断;
    - 换算单位: 1 bps = 100 ppm, 即 fee_bps = lpFee / 100.0.
    """
    if isinstance(data, str):
        clean = data.strip()
        if clean.startswith(("0x", "0X")):
            clean = clean[2:]
        if len(clean) == 0:
            raise RuntimeError("empty StateView return data")
        try:
            raw_bytes = bytes.fromhex(clean)
        except ValueError as exc:
            raise ValueError(f"Invalid hex string in StateView return data: {data!r}") from exc
    elif isinstance(data, (bytes, bytearray)):
        raw_bytes = bytes(data)
    else:
        raise ValueError(f"StateView data must be str or bytes, got: {type(data)!r}")

    if len(raw_bytes) != 128:
        raise ValueError(
            f"StateView slot0 data must be exactly 128 bytes (4 words), got len={len(raw_bytes)}"
        )

    decoded = abi_decode(["uint160", "int24", "uint24", "uint24"], raw_bytes)
    lp_fee = int(decoded[3])

    # 动态费检查 (Bit 23 动态费标志位)
    if (lp_fee & DYNAMIC_FEE_FLAG) != 0:
        raise ValueError(
            f"Dynamic fee pool (flag 0x800000 set in lpFee: 0x{lp_fee:x}) cannot be verified statically; hook execution required"
        )

    # 范围校验: [0, 1000000]
    if not (0 <= lp_fee <= 1_000_000):
        raise ValueError(f"Invalid V4 lpFee value out of range [0, 1000000]: {lp_fee}")

    return float(lp_fee) / 100.0


def read_v4_pool_fee(
    addr: str,
    rpc: Any,
    *,
    state_view_address: str = STATE_VIEW_ADDRESS,
) -> float:
    """通过官方 StateView.getSlot0(bytes32 poolId) 读取 Uniswap V4 池真实费率 (单位: bps).

    遵循严苛门禁规范:
    1. 必须提供已注入的只读 rpc, 杜绝 HTTP 连接构造;
    2. addr 必须为 32 字节 poolId (66 字符含 0x 或 64 字符十六进制);
    3. 通过 eth_call StateView.getSlot0(poolId) 获取完整 128 字节数据;
    4. 截断数据、空数据或 RPC 异常绝不兜底, 立即 fail-closed 抛错;
    5. 动态费率 (DYNAMIC_FEE_FLAG) 严禁默认合法, 抛出 ValueError;
    6. 返回值单位为 bps (例如 lpFee=7000 -> 70.0 bps, 3000 -> 30.0 bps, 0 -> 0.0 bps).
    """
    clean_id = addr.strip()
    if clean_id.startswith(("0x", "0X")):
        pool_id_hex = clean_id[2:]
    else:
        pool_id_hex = clean_id

    if len(pool_id_hex) != 64:
        raise ValueError(
            f"V4 poolId must be 32-byte hex (64 hex chars, or 66 with 0x), got {len(pool_id_hex)}: {addr!r}"
        )

    try:
        int(pool_id_hex, 16)
    except ValueError as exc:
        raise ValueError(f"Invalid hex characters in V4 poolId: {addr!r}") from exc

    v4_sel = STATE_VIEW_GET_SLOT0_SELECTOR
    if v4_sel.startswith(("0x", "0X")):
        v4_sel = v4_sel[2:]

    calldata = "0x" + v4_sel + pool_id_hex.lower()

    res = _invoke_rpc_call(rpc, state_view_address, calldata)
    raw = res.get("result") if isinstance(res, dict) else res

    if raw is None or raw == "" or raw == "0x" or raw == "0X":
        raise RuntimeError(f"empty eth_call result for StateView.getSlot0 on {addr}")

    return decode_stateview_slot0_fee(raw)
