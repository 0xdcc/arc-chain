"""纯离线输入候选池筛选与真实费率确权模块 (research.market_data.fee_scan).

本模块严格遵循 AGENTS.md 金融安全与离线纯研究规范:
1. 纯输入接缝: 仅处理显式传入的 raw_pools 候选列表, 绝无网络 fetch 或磁盘 pickle/json 隐式加载;
2. 只读依赖注入: 依赖只读 rpc 与 reader 函数, 绝无内部构造 HTTP 客户端或 live 节点连接;
3. 零费率与空响应严格剥离:
   - 链上读取成功的数值 0 (0 ppm -> 0.0 bps) 为合法物理费率正常入库;
   - 空数据 ("0x", "")、Revert 异常、截断数据一律 fail-closed 跳过并记录 [FEE_UNVERIFIED];
   - 绝不新增 allow_zero 开关, 绝不宣称所有 V3 工厂均支持 0 费档, 仅处理 RPC 有效成功返回;
4. 单点 'latest' 物理边界声明 (拒绝伪快照包装):
   - 本函数对各池执行单个 eth_call(..., "latest"), 单点 latest 仅反映调用瞬间的最新块高;
   - 顺序循环调用不具备跨池原子性与区块一致性, 严禁误称为 snapshot (快照);
   - 跨池一致性须显式指定 block_identifier 或采用 Multicall 聚合调用;
5. V4 元数据分级确权与强身份绑定:
   - 必须建立与当前候选池绑定的显式 expected chain/token/pool 身份比对;
   - 绝不全量硬编码 5042 误拒历史研究链 4663;
   - 若缺失链或币种比较依据, 不得冒充采纳元数据, 走明确只读 StateView reader fallback;
   - 冲突元数据一律拒绝且不以其费率为真, fallback 链上读取结果无绑定证据不得称 metadata verified;
   - 费率合法性: fee_bps 与 ppm 统一协议合法范围 [0, 10000.0] bps / [0, 1000000] ppm;
   - fee_bps 是费率浮点/数值, 严禁误作 bitfield 位运算; ppm 动态费标志位 (0x800000) 严格拦截;
6. 链上费率真实覆盖:
   - 差异 > 1.0 bps 时以链上真实值为准覆盖标称值, 并记录 [FEE_MISMATCH] 日志.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from research.market_data.fee_verification import (
    DYNAMIC_FEE_FLAG,
    STATE_VIEW_ADDRESS,
    STATE_VIEW_GET_SLOT0_SELECTOR,
    V2_STANDARD_FEE_BPS,
    V3_FEE_SELECTOR,
    ReadOnlyRpcTransport,
    batch_read_v3_pool_fees,
    decode_stateview_slot0_fee,
    decode_v3_fee_data,
    read_raw_v3_pool_fee,
    read_v3_pool_fee,
    read_v4_pool_fee,
)
from research.market_data.multicall import PoolSpec
from research.market_data.v4_reader import V4PoolSpec

logger = logging.getLogger(__name__)

ZERO_ADDRESS: str = "0x0000000000000000000000000000000000000000"

# DEX 架构分类与白名单集合
V3_FORK_DEXES: frozenset[str] = frozenset({"uniswap-v3", "up-v3", "giga-v3", "ramses-v3"})
V2_DEXES: frozenset[str] = frozenset({"uniswap-v2"})
V4_DEXES: frozenset[str] = frozenset({"uniswap-v4"})
SUPPORTED_DEXES: frozenset[str] = V3_FORK_DEXES | V2_DEXES | V4_DEXES
EXCLUDED_DEXES: frozenset[str] = frozenset(
    {
        "alandale-cl",
        "bankr",
        "orvex-v4",
        "ramses-dlmm",
        "pons-v2",
    }
)

# 离线已知 V4 池静态核验元数据 (纯内存常量, 零网络, 零磁盘 I/O, 用于确定性离线回放)
DEFAULT_V4_METADATA: dict[str, dict[str, Any]] = {
    "0x4be9657ec9002e528f4f17a5c43edc525a07f888f7b180c2afbf75e096c4f38a": {
        "pool_id": "0x4be9657ec9002e528f4f17a5c43edc525a07f888f7b180c2afbf75e096c4f38a",
        "chain_id": 5042,
        "label": "PONS / USDG 0.3%",
        "tvl_usd": 7059641.3651,
        "currency0": "0x39dBED3a2bd333467115dE45665cC57F813C4571",
        "currency1": "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168",
        "fee": 3000,
        "tick_spacing": 60,
        "hooks": ZERO_ADDRESS,
    },
}


def is_valid_token_address(addr: str | None) -> bool:
    """校验是否为合规且非全零的 42 字符 EVM 代币地址."""
    if not addr or not isinstance(addr, str):
        return False
    clean = addr.strip()
    if len(clean) != 42 or not clean.startswith(("0x", "0X")):
        return False
    if clean.lower() == ZERO_ADDRESS:
        return False
    try:
        val = int(clean, 16)
        return val != 0
    except ValueError:
        return False


def is_valid_pool_address(addr: str | None, is_v4: bool = False) -> bool:
    """校验池地址或 PoolId 架构长度 (V4 66 字符 / V2/V3 42 字符) 及非零格式."""
    if not addr or not isinstance(addr, str):
        return False
    clean = addr.strip()
    expected_len = 66 if is_v4 else 42
    if len(clean) != expected_len or not clean.startswith(("0x", "0X")):
        return False
    zero_hex = "0x" + "0" * (expected_len - 2)
    if clean.lower() == zero_hex:
        return False
    try:
        val = int(clean, 16)
        return val != 0
    except ValueError:
        return False


@dataclass(frozen=True)
class ScannedPool:
    """已确权候选池规格 (标准化扫描输出)."""

    dex: str
    name: str
    addr: str
    tvl_usd: float
    fee_bps: float
    token0_symbol: str = ""
    token1_symbol: str = ""
    token0_address: str = ""
    token1_address: str = ""
    vol24h: float = 0.0
    created: str = ""
    tick_spacing: int | None = None
    hooks: str | None = None
    dec0: int | None = None
    dec1: int | None = None
    monitor_only_reason: str = ""

    @property
    def is_v4(self) -> bool:
        """是否为 Uniswap V4 池 (bytes32 PoolId, 66 字符)."""
        return len(self.addr) == 66 or self.dex == "uniswap-v4"

    @property
    def canonical_pair(self) -> tuple[str, str]:
        """按字典序排列的标准化币对."""
        return (
            (self.token0_symbol, self.token1_symbol)
            if self.token0_symbol <= self.token1_symbol
            else (self.token1_symbol, self.token0_symbol)
        )


def parse_pool_name(name: str, dex: str = "") -> tuple[str, str, float]:
    """从池子名称中解析代币符号及手续费率 (基点 bps).

    例如:
        'TOKEN / WETH 0.01%' -> ('TOKEN', 'WETH', 1.0)
        'WETH / USDG 0.01%'  -> ('WETH', 'USDG', 1.0)
        'Nasduck / WETH'     -> ('NASDUCK', 'WETH', 30.0)
        'PONS / USDG 0.3%'   -> ('PONS', 'USDG', 30.0)
    """
    parts = name.split("/")
    if len(parts) != 2:
        return "", "", 30.0

    tok0 = parts[0].strip().upper()
    rest = parts[1].strip()

    pct_match = re.search(r"([0-9.]+)%", rest)
    if pct_match:
        fee_pct = float(pct_match.group(1))
        fee_bps = fee_pct * 100.0
        tok1 = re.sub(r"[0-9.]+", "", rest).replace("%", "").strip().upper()
    else:
        fee_bps = 30.0
        tok1 = rest.strip().upper()

    return tok0, tok1, fee_bps


def _validate_v4_metadata(
    addr: str,
    raw_meta: Any,
    *,
    expected_tokens: Sequence[str] | set[str] | None = None,
    expected_chain_id: int | None = None,
) -> float | None:
    """核验 V4 池元数据强身份绑定 (Pool/Chain/Tokens) 及合法费率表示.

    若元数据非法、身份冲突、缺失关键身份或无比较依据、置位动态费标志位或字段越界,
    一律返回 None (由调用方降级 StateView 或 fail-closed 剔除).
    """
    if not isinstance(raw_meta, dict):
        return None

    # 1. pool_id / address 强身份校验 (必填, 必须与候选池 addr 严格匹配)
    meta_pid = raw_meta.get("pool_id")
    if meta_pid is None:
        meta_pid = raw_meta.get("address")
    if meta_pid is None:
        logger.warning("[FEE_UNVERIFIED] V4 池 %s 元数据缺失 pool_id/address 身份字段", addr)
        return None
    if str(meta_pid).strip().lower() != addr.lower():
        logger.warning(
            "[FEE_UNVERIFIED] V4 池 %s 元数据 pool_id 不匹配 (%s)，拒绝该元数据",
            addr,
            meta_pid,
        )
        return None

    # 2. chain_id 强身份校验与比较依据检查
    chain_id = raw_meta.get("chain_id")
    if chain_id is None:
        logger.warning("[FEE_UNVERIFIED] V4 池 %s 元数据缺失 chain_id 身份字段，拒绝采纳", addr)
        return None
    try:
        cid = int(chain_id)
        if cid <= 0:
            logger.warning("[FEE_UNVERIFIED] V4 池 %s 元数据 chain_id 非法 (%s)", addr, chain_id)
            return None
    except (ValueError, TypeError):
        logger.warning("[FEE_UNVERIFIED] V4 池 %s 元数据 chain_id 解析失败 (%s)", addr, chain_id)
        return None

    if expected_chain_id is None:
        logger.warning(
            "[FEE_UNVERIFIED] V4 池 %s 候选池缺少 expected_chain_id 比较依据，无法采纳元数据",
            addr,
        )
        return None
    if cid != expected_chain_id:
        logger.warning(
            "[FEE_UNVERIFIED] V4 池 %s 元数据 chain_id (%d) 与期望链 (%d) 不匹配，拒绝该元数据",
            addr,
            cid,
            expected_chain_id,
        )
        return None

    # 3. token/currency 强身份校验与币种一致性比对
    meta_tok0 = (
        raw_meta.get("currency0") or raw_meta.get("token0") or raw_meta.get("token0_address")
    )
    meta_tok1 = (
        raw_meta.get("currency1") or raw_meta.get("token1") or raw_meta.get("token1_address")
    )
    if not meta_tok0 or not meta_tok1:
        logger.warning(
            "[FEE_UNVERIFIED] V4 池 %s 元数据缺失 currency0/currency1 身份字段，拒绝采纳",
            addr,
        )
        return None

    m0_str = str(meta_tok0).strip()
    m1_str = str(meta_tok1).strip()
    if not is_valid_token_address(m0_str) or not is_valid_token_address(m1_str):
        logger.warning(
            "[FEE_UNVERIFIED] V4 池 %s 元数据 token 地址非法 (%s, %s)，拒绝采纳",
            addr,
            m0_str,
            m1_str,
        )
        return None

    meta_token_set = {m0_str.lower(), m1_str.lower()}
    if len(meta_token_set) != 2:
        logger.warning("[FEE_UNVERIFIED] V4 池 %s 元数据 token0 与 token1 相同，拒绝采纳", addr)
        return None

    if expected_tokens is None:
        logger.warning(
            "[FEE_UNVERIFIED] V4 池 %s 候选池缺少 expected_tokens 比较依据，无法采纳元数据",
            addr,
        )
        return None

    exp_token_set = {
        str(t).strip().lower() for t in expected_tokens if isinstance(t, str) and t.strip()
    }
    if len(exp_token_set) != 2:
        logger.warning(
            "[FEE_UNVERIFIED] V4 池 %s 候选池 token 比较依据集合非法 (元素数!=2)，拒绝采纳元数据",
            addr,
        )
        return None

    if meta_token_set != exp_token_set:
        logger.warning(
            "[FEE_UNVERIFIED] V4 池 %s 元数据 token %r 与候选池 %r 不匹配，拒绝采纳",
            addr,
            meta_token_set,
            exp_token_set,
        )
        return None

    # 4. 费率合法性检查 (统一 fee 与 fee_bps 范围校验)
    has_fee = "fee" in raw_meta
    has_bps = "fee_bps" in raw_meta

    if not has_fee and not has_bps:
        logger.warning("[FEE_UNVERIFIED] V4 池 %s 元数据缺失 fee/fee_bps 字段", addr)
        return None

    fee_val_bps: float | None = None
    direct_bps: float | None = None

    if has_fee:
        raw_val = raw_meta["fee"]
        if isinstance(raw_val, bool) or not isinstance(raw_val, (int, float)):
            logger.warning("[FEE_UNVERIFIED] V4 池 %s 元数据 fee 非数值: %r", addr, raw_val)
            return None
        if not math.isfinite(raw_val):
            logger.warning("[FEE_UNVERIFIED] V4 池 %s 元数据 fee 非有限数值: %r", addr, raw_val)
            return None
        int_fee = int(raw_val)
        if int_fee & DYNAMIC_FEE_FLAG:
            logger.warning("[FEE_UNVERIFIED] V4 池 %s 元数据置位动态费标志位 (0x800000)", addr)
            return None
        if int_fee < 0 or int_fee > 1_000_000:
            logger.warning("[FEE_UNVERIFIED] V4 池 %s 元数据 fee 越界 (%d ppm)", addr, int_fee)
            return None
        fee_val_bps = float(int_fee) / 100.0

    if has_bps:
        raw_val = raw_meta["fee_bps"]
        if isinstance(raw_val, bool) or not isinstance(raw_val, (int, float)):
            logger.warning("[FEE_UNVERIFIED] V4 池 %s 元数据 fee_bps 非数值: %r", addr, raw_val)
            return None
        if not math.isfinite(raw_val):
            logger.warning("[FEE_UNVERIFIED] V4 池 %s 元数据 fee_bps 非有限数值: %r", addr, raw_val)
            return None
        bps_val = float(raw_val)
        # fee_bps 是费率不是 bitfield, 不对 bps 做位运算, 严格按 [0.0, 10000.0] 协议范围拦截
        if bps_val < 0.0 or bps_val > 10_000.0:
            logger.warning(
                "[FEE_UNVERIFIED] V4 池 %s 元数据 fee_bps 越界 (%.2f bps)", addr, bps_val
            )
            return None
        direct_bps = bps_val

    # 若两种表示同时存在, 校验数值一致性
    if fee_val_bps is not None and direct_bps is not None:
        if abs(fee_val_bps - direct_bps) > 1e-4:
            logger.warning(
                "[FEE_UNVERIFIED] V4 池 %s 元数据 fee (%.2f bps) 与 fee_bps (%.2f bps) 冲突",
                addr,
                fee_val_bps,
                direct_bps,
            )
            return None
        return direct_bps

    if direct_bps is not None:
        return direct_bps

    return fee_val_bps


def scan_pools(
    min_tvl: float = 50_000.0,
    raw_pools: Sequence[dict[str, Any]] | None = None,
    rpc: ReadOnlyRpcTransport | Any | None = None,
    *,
    chain_id: int | None = None,
    v4_metadata: dict[str, dict[str, Any]] | Callable[[str], dict[str, Any] | None] | None = None,
    v3_reader: Callable[..., float] = read_v3_pool_fee,
    v4_reader: Callable[..., float] = read_v4_pool_fee,
) -> list[ScannedPool]:
    """纯离线输入候选池筛选与真实费率确权器.

    架构与安全规范:
    1. 纯输入接缝: 强制显式传入 raw_pools 候选列表, 绝无网络 fetch 或磁盘 pickle/json 隐式加载;
    2. 只读依赖注入: 依赖只读 rpc 与 reader 函数, 绝无内部构造 HTTP client 或 live socket 连接;
    3. 数值 0 与空响应严格剥离:
       - 链上读取成功的数值 0 (0 ppm -> 0.0 bps) 为合法物理费率正常入库;
       - 空数据 ("0x", "")、Revert 异常、截断数据一律 fail-closed 跳过并记录 [FEE_UNVERIFIED];
       - 绝不新增 allow_zero 开关, 绝不宣称所有 V3 工厂均支持 0 费档, 仅处理 RPC 有效成功返回;
    4. 单点 'latest' 物理边界说明:
       - 本函数对各池执行单个 eth_call(..., 'latest'), 单点 latest 仅反映调用瞬间的最新块高;
       - 顺序循环调用不具备跨池原子性与区块一致性, 严禁误称为 snapshot (快照);
    5. V4 元数据分级确权与强身份绑定:
       - 必须具有显式 expected chain/token/pool 身份比对;
       - 缺失比较依据、冲突或非法时明确拒绝采纳, 降级 StateView.getSlot0 (v4_reader);
       - 降级读取结果绝不误称为 metadata verified; 两级均读不到一律剔除, 严禁 30.0 bps 兜底;
    6. 链上费率真实覆盖:
       - 差异 > 1.0 bps 时以链上真实值为准覆盖标称值, 并记录 [FEE_MISMATCH] 日志.
    """
    if raw_pools is None:
        return []

    scanned: list[ScannedPool] = []

    for item in raw_pools:
        # 池级元数据显式重置，防止跨池污染 (防 V4->V3 继承上池元数据)
        meta_dict: dict[str, Any] | None = None
        monitor_only_reason: str = ""

        addr = str(item.get("addr", "")).strip().lower()
        if not addr:
            continue

        try:
            tvl = float(item.get("tvl") or item.get("tvl_usd") or 0.0)
        except (ValueError, TypeError):
            tvl = 0.0

        if tvl < min_tvl:
            continue

        raw_name = str(item.get("name", "")).strip()
        dex_raw = str(item.get("dex", "")).strip().lower()
        dex = dex_raw.replace("-robinhood", "")

        # 排除已知非标 DEX
        if any(ex in dex for ex in EXCLUDED_DEXES):
            continue

        # 仅保留受支持的 DEX 架构
        if dex not in SUPPORTED_DEXES and not any(sd in dex for sd in SUPPORTED_DEXES):
            continue

        tok0, tok1, name_fee_bps = parse_pool_name(raw_name, dex=dex)
        if not tok0 or not tok1:
            continue

        is_v4_pool = len(addr) == 66 or dex == "uniswap-v4" or "v4" in dex
        is_v2_pool = dex in V2_DEXES or "v2" in dex
        is_v3_pool = dex in V3_FORK_DEXES or "v3" in dex

        # 地址合法性校验 (V4 须 66 字符, V2/V3 须 42 字符)
        if is_v4_pool:
            if not is_valid_pool_address(addr, is_v4=True):
                logger.warning(
                    "[INVALID_TOKEN_ADDR] V4 池 %s 地址/PoolId 非法(须66字符)，跳过", addr
                )
                continue
        else:
            if not is_valid_pool_address(addr, is_v4=False):
                logger.warning("[INVALID_TOKEN_ADDR] V2/V3 池 %s 地址非法(须42字符)，跳过", addr)
                continue

        # 代币地址合法性校验 (若 item 中显式提供)
        raw_tok0 = item.get("token0_address")
        raw_tok1 = item.get("token1_address")
        if "token0_address" in item or "token1_address" in item:
            tok0_str = str(raw_tok0) if raw_tok0 is not None else None
            tok1_str = str(raw_tok1) if raw_tok1 is not None else None
            if not is_valid_token_address(tok0_str) or not is_valid_token_address(tok1_str):
                logger.warning(
                    "[INVALID_TOKEN_ADDR] 池 %s token0=%s token1=%s，跳过", addr, tok0_str, tok1_str
                )
                continue

        # 解析当前候选池 expected chain_id (item 优先, 其次 scan_pools 入参)
        expected_cid: int | None = None
        pool_cid = item.get("chain_id")
        if pool_cid is None:
            pool_cid = item.get("chain")
        if pool_cid is not None:
            try:
                parsed_cid = int(pool_cid)
                if parsed_cid > 0:
                    expected_cid = parsed_cid
            except (ValueError, TypeError):
                expected_cid = None
        if expected_cid is None:
            expected_cid = chain_id

        # 候选池代币地址比较依据提取
        candidate_t0 = item.get("token0_address") or item.get("currency0") or item.get("token0")
        candidate_t1 = item.get("token1_address") or item.get("currency1") or item.get("token1")
        expected_tokens: tuple[str, str] | None = None
        if candidate_t0 is not None and candidate_t1 is not None:
            cand_t0_str = str(candidate_t0).strip()
            cand_t1_str = str(candidate_t1).strip()
            if is_valid_token_address(cand_t0_str) and is_valid_token_address(cand_t1_str):
                expected_tokens = (cand_t0_str, cand_t1_str)

        # 费率确权流程
        fee_bps: float
        if is_v2_pool:
            # V2 协议固定 30.0 bps, 零 RPC 调用
            fee_bps = V2_STANDARD_FEE_BPS
        elif is_v4_pool:
            # V4 两级确权: 强身份绑定 Metadata -> StateView.getSlot0 Fallback
            meta_fee_bps: float | None = None

            # 1. 查找元数据
            if v4_metadata is not None:
                if callable(v4_metadata):
                    meta_dict = v4_metadata(addr)
                elif isinstance(v4_metadata, dict):
                    meta_dict = v4_metadata.get(addr.lower())
            elif addr.lower() in DEFAULT_V4_METADATA:
                meta_dict = DEFAULT_V4_METADATA[addr.lower()]

            if meta_dict is not None:
                meta_fee_bps = _validate_v4_metadata(
                    addr,
                    meta_dict,
                    expected_tokens=expected_tokens,
                    expected_chain_id=expected_cid,
                )

            if meta_fee_bps is not None:
                fee_bps = meta_fee_bps
                if abs(fee_bps - name_fee_bps) > 1.0:
                    logger.warning(
                        "[FEE_MISMATCH] 名称=%.2f 链上=%.2f 池=%s", name_fee_bps, fee_bps, addr
                    )
            else:
                # 2. 降级 StateView 读取 (元数据缺失、非法、冲突或无绑定依据时)
                logger.info(
                    "[FEE_FALLBACK] V4 池 %s 元数据未通过校验或缺少绑定依据，降级调用 StateView.getSlot0",
                    addr,
                )
                try:
                    chain_fee_bps = v4_reader(addr, rpc=rpc)
                except Exception as exc:
                    logger.warning(
                        "[FEE_UNVERIFIED] V4 池 %s (%s) 无法从 PoolKey 或 StateView 读取真实费率: %s，跳过该池",
                        addr,
                        raw_name,
                        exc,
                    )
                    continue

                fee_bps = chain_fee_bps
                if abs(fee_bps - name_fee_bps) > 1.0:
                    logger.warning(
                        "[FEE_MISMATCH] 名称=%.2f 链上=%.2f 池=%s", name_fee_bps, fee_bps, addr
                    )
        elif is_v3_pool:
            # V3 链上读取: fee() (0xddca3f43)
            try:
                chain_fee_bps = v3_reader(addr, rpc=rpc, dex=dex)
                fee_bps = chain_fee_bps
                if abs(fee_bps - name_fee_bps) > 1.0:
                    logger.warning(
                        "[FEE_MISMATCH] 名称=%.2f 链上=%.2f 池=%s", name_fee_bps, fee_bps, addr
                    )
            except ValueError as exc:
                # 结构化条件区分：未验证 DEX 分叉保留监控证据 vs Canonical 失败严格阻断
                if dex != "uniswap-v3" and "unverified" in str(exc).lower():
                    raw_uint = read_raw_v3_pool_fee(addr, rpc=rpc)
                    if raw_uint is None:
                        logger.warning(
                            "[FEE_UNVERIFIED] V3 分叉池 %s (%s, dex=%s) 链上 fee() 空响应或不可读，跳过该池",
                            addr,
                            raw_name,
                            dex,
                        )
                        continue
                    fee_bps = math.nan
                    monitor_only_reason = (
                        f"Fee unit adapter unverified for {dex}: monitor-only (raw={raw_uint})"
                    )
                    logger.info(
                        "[FEE_MONITOR_ONLY] V3 分叉池 %s (%s, dex=%s) 未验证费率单位: %s",
                        addr,
                        raw_name,
                        dex,
                        monitor_only_reason,
                    )
                else:
                    logger.warning(
                        "[FEE_UNVERIFIED] V3 池 %s (%s) 链上 fee() 校验失败: %s，跳过该池",
                        addr,
                        raw_name,
                        exc,
                    )
                    continue
            except Exception as exc:
                logger.warning(
                    "[FEE_UNVERIFIED] V3 池 %s (%s) 链上 fee() 调用失败: %s，跳过该池",
                    addr,
                    raw_name,
                    exc,
                )
                continue
        else:
            logger.warning(
                "[FEE_UNVERIFIED] 未知架构池 %s (%s, dex=%s) 无法验证真实费率，跳过该池",
                addr,
                raw_name,
                dex,
            )
            continue

        # 关联代币地址与元数据
        tok0_addr = str(item.get("token0_address", ""))
        tok1_addr = str(item.get("token1_address", ""))
        vol24h = float(item.get("vol24h") or 0.0)
        created = str(item.get("created", ""))

        tick_spacing_val: int | None = None
        hooks_val: str | None = None
        if "tick_spacing" in item and item["tick_spacing"] is not None:
            try:
                tick_spacing_val = int(item["tick_spacing"])
            except (ValueError, TypeError):
                pass
        elif (
            is_v4_pool
            and meta_dict
            and "tick_spacing" in meta_dict
            and meta_dict["tick_spacing"] is not None
        ):
            try:
                tick_spacing_val = int(meta_dict["tick_spacing"])
            except (ValueError, TypeError):
                pass

        if "hooks" in item and item["hooks"] is not None:
            hooks_val = str(item["hooks"]).strip()
        elif is_v4_pool and meta_dict and "hooks" in meta_dict and meta_dict["hooks"] is not None:
            hooks_val = str(meta_dict["hooks"]).strip()

        scanned.append(
            ScannedPool(
                dex=dex,
                name=raw_name,
                addr=addr,
                tvl_usd=tvl,
                fee_bps=fee_bps,
                token0_symbol=tok0,
                token1_symbol=tok1,
                token0_address=tok0_addr,
                token1_address=tok1_addr,
                vol24h=vol24h,
                created=created,
                tick_spacing=tick_spacing_val,
                hooks=hooks_val,
                monitor_only_reason=monitor_only_reason,
            )
        )

    return scanned


# ==============================================================================
# 纯离线工具: 池归并、规格转换与元数据缓存加载 (M4C 离线修复迁移)
# ==============================================================================


def group_by_pair(
    pools: Sequence[ScannedPool],
) -> dict[tuple[str, str], list[ScannedPool]]:
    """按标准代币对 (canonical_pair) 对候选池进行归并 (纯离线计算).

    无论代币在池名中的先后顺序如何，均按 ScannedPool.canonical_pair 字典序聚合。
    """
    if pools is None:
        raise ValueError("pools cannot be None")
    grouped: dict[tuple[str, str], list[ScannedPool]] = {}
    for pool in pools:
        pair = pool.canonical_pair
        if pair not in grouped:
            grouped[pair] = []
        grouped[pair].append(pool)
    return grouped


def get_overlapping_pools(
    pools: Sequence[ScannedPool],
    min_pools: int = 2,
) -> dict[tuple[str, str], list[ScannedPool]]:
    """提取具有 >= min_pools 个候选池的重叠代币对字典 (纯离线分析).

    用于多 DEX 间重叠流动性池与跨池套利路径的纯内存筛选。
    """
    if pools is None:
        raise ValueError("pools cannot be None")
    grouped = group_by_pair(pools)
    return {pair: plist for pair, plist in grouped.items() if len(plist) >= min_pools}


def scanned_to_pool_spec(pool: ScannedPool) -> PoolSpec | V4PoolSpec:
    """将标准化 ScannedPool 转换为只读研究 PoolSpec 或 V4PoolSpec 实例.

    安全与门禁防线:
    1. 严格地址校验: 拒绝零地址、非法长度与畸形 hex;
    2. 显式代币防护: 零地址代币一律抛出 ValueError, 记录安全告警;
    3. 精确类型映射: 66 字符 / V4 映射为 V4PoolSpec, 42 字符映射为 PoolSpec;
    4. 费率与标签刚性: 未知费率/NaN 保持 NaN 并标记 [MONITOR_ONLY], 杜绝默认 fee=0;
    5. 杜绝隐式假设: 不默认 decimals=18 或篡改物理状态;
    6. V4 PoolKey 契约刚性: 必须具备显式 hooks、tick_spacing 与确定费率，未知 hook 绝不默认零地址 (杜绝白化).
    """
    if pool is None:
        raise ValueError("pool cannot be None")

    # 1. 代币地址零地址与非法格式强校验 (若提供)
    for tok_addr, label in [(pool.token0_address, "token0"), (pool.token1_address, "token1")]:
        if tok_addr:
            if not is_valid_token_address(tok_addr):
                logger.warning(
                    "[INVALID_TOKEN_ADDR] scanned_to_pool_spec 拒绝非法 %s 代币地址: %r (池: %s)",
                    label,
                    tok_addr,
                    pool.addr,
                )
                raise ValueError(
                    f"invalid token addresses: {label} address {tok_addr!r} is zero or invalid"
                )

    # 2. 标签与费率状态确权 (杜绝未知 fee 默认为 0)
    pool_label = pool.name
    is_monitor_only = math.isnan(pool.fee_bps) or bool(getattr(pool, "monitor_only_reason", ""))
    if is_monitor_only and "MONITOR_ONLY" not in pool_label:
        pool_label = f"{pool.name} [MONITOR_ONLY]".strip()

    # 3. 区分 V4 与 V2/V3 池规格
    if pool.is_v4:
        if not is_valid_pool_address(pool.addr, is_v4=True):
            logger.warning("[INVALID_TOKEN_ADDR] V4 PoolId 非法: %s", pool.addr)
            raise ValueError(
                f"invalid pool address: {pool.addr} is not a valid 32-byte hex V4 PoolId"
            )

        # 静态元数据对齐 (纯离线字典查询)
        v4_meta = DEFAULT_V4_METADATA.get(pool.addr.lower(), {})

        # V4 PoolKey 显式字段刚性校验: 未知 hooks 严禁默认 ZERO_ADDRESS 白化
        hooks: str | None = None
        if hasattr(pool, "hooks") and pool.hooks is not None:
            hooks = pool.hooks
        elif "hooks" in v4_meta and v4_meta["hooks"] is not None:
            hooks = v4_meta["hooks"]

        if hooks is None or hooks == "":
            logger.warning(
                "[V4_HOOK_UNKNOWN] V4 池 %s 缺失显式 hooks 配置，无法确定 PoolKey，拒绝转换为 V4PoolSpec",
                pool.addr,
            )
            raise ValueError(
                f"cannot determine V4 pool key: missing explicit hooks for pool {pool.addr}"
            )

        if hooks != ZERO_ADDRESS:
            if not is_valid_token_address(hooks):
                raise ValueError(
                    f"cannot determine V4 pool key: invalid hooks address {hooks!r} for pool {pool.addr}"
                )

        # tick_spacing 刚性校验: 必须显式已知，严禁默认 60
        tick_spacing: int | None = None
        if hasattr(pool, "tick_spacing") and pool.tick_spacing is not None:
            tick_spacing = pool.tick_spacing
        elif "tick_spacing" in v4_meta and v4_meta["tick_spacing"] is not None:
            tick_spacing = v4_meta["tick_spacing"]

        if tick_spacing is None:
            logger.warning(
                "[V4_TICK_SPACING_UNKNOWN] V4 池 %s 缺失显式 tick_spacing 配置，拒绝转换为 V4PoolSpec",
                pool.addr,
            )
            raise ValueError(
                f"cannot determine V4 pool key: missing explicit tick_spacing for pool {pool.addr}"
            )
        if not isinstance(tick_spacing, int) or tick_spacing <= 0:
            raise ValueError(
                f"cannot determine V4 pool key: invalid tick_spacing {tick_spacing!r} for pool {pool.addr}"
            )

        # fee 刚性校验: 必须显式已知且有效数值，无法确定 pool key 时拒绝，不伪造默认费率 0
        fee_bps: float | None = None
        if not math.isnan(pool.fee_bps) and pool.fee_bps >= 0:
            fee_bps = pool.fee_bps
        elif "fee" in v4_meta and v4_meta["fee"] is not None:
            fee_bps = float(v4_meta["fee"]) / 100.0
        elif "fee_bps" in v4_meta and v4_meta["fee_bps"] is not None:
            fee_bps = float(v4_meta["fee_bps"])

        if fee_bps is None or math.isnan(fee_bps):
            logger.warning(
                "[V4_FEE_UNKNOWN] V4 池 %s 缺失显式有效费率，无法确定 PoolKey，拒绝转换为 V4PoolSpec",
                pool.addr,
            )
            raise ValueError(
                f"cannot determine V4 pool key: missing explicit fee for pool {pool.addr}"
            )

        # decimals 映射: 若显式提供则透传，不擅自伪造默认 18
        v4_kwargs: dict[str, Any] = {
            "address": pool.addr,
            "label": pool_label,
            "fee_bps": fee_bps,
            "token0": pool.token0_address or pool.token0_symbol,
            "token1": pool.token1_address or pool.token1_symbol,
            "tvl_usd": pool.tvl_usd,
            "tick_spacing": tick_spacing,
            "hooks": hooks,
            "dex": pool.dex,
        }
        dec0 = pool.dec0 if hasattr(pool, "dec0") and pool.dec0 is not None else v4_meta.get("dec0")
        dec1 = pool.dec1 if hasattr(pool, "dec1") and pool.dec1 is not None else v4_meta.get("dec1")
        if dec0 is not None:
            v4_kwargs["dec0"] = dec0
        if dec1 is not None:
            v4_kwargs["dec1"] = dec1

        return V4PoolSpec(**v4_kwargs)

    # V2 / V3 规格
    if not is_valid_pool_address(pool.addr, is_v4=False):
        logger.warning("[INVALID_TOKEN_ADDR] V2/V3 池地址非法: %s", pool.addr)
        raise ValueError(
            f"invalid pool address: {pool.addr} is not a valid 20-byte hex EVM pool address"
        )

    v3_kwargs: dict[str, Any] = {
        "address": pool.addr,
        "label": pool_label,
        "fee_bps": pool.fee_bps,
        "token0": pool.token0_address or pool.token0_symbol,
        "token1": pool.token1_address or pool.token1_symbol,
        "tvl_usd": pool.tvl_usd,
        "dex": pool.dex,
    }
    dec0 = pool.dec0 if hasattr(pool, "dec0") and pool.dec0 is not None else None
    dec1 = pool.dec1 if hasattr(pool, "dec1") and pool.dec1 is not None else None
    if dec0 is not None:
        v3_kwargs["dec0"] = dec0
    if dec1 is not None:
        v3_kwargs["dec1"] = dec1

    return PoolSpec(**v3_kwargs)


def load_pools_metadata_cache(
    cache_path: str | Path,
) -> dict[str, dict[str, Any]]:
    """从指定的离线元数据缓存文件中读取并过滤池配置 (纯离线工具).

    安全与隔离规范:
    1. 强制显式输入: 必须由调用方显式传入 cache_path，严禁默认 host 路径或自动网络拉取;
    2. Fail-closed: 文件不存在或读取异常时返回空字典并记日志，绝不静默网络回退;
    3. 零地址与非法地址刚性过滤: 剔除 token0/token1/pool 地址为零地址或非法 hex 的条目;
    4. 杜绝隐式假设: 不擅自填充默认 decimals=18，未知费率绝不补零.
    """
    if cache_path is None:
        raise ValueError("cache_path must be explicitly provided; automatic host path is forbidden")

    p = Path(cache_path)
    if not p.is_file():
        logger.warning("[CACHE_NOT_FOUND] metadata cache file does not exist: %s", cache_path)
        return {}

    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        logger.warning("[CACHE_READ_ERROR] failed to read metadata cache %s: %s", cache_path, exc)
        return {}

    if not isinstance(data, dict):
        return {}

    raw_pools = data.get("pools")
    if not isinstance(raw_pools, dict):
        raw_pools = data

    filtered_pools: dict[str, dict[str, Any]] = {}
    for raw_addr, pool_info in raw_pools.items():
        if not isinstance(pool_info, dict):
            continue

        addr_str = str(pool_info.get("address") or raw_addr).strip()
        is_v4 = pool_info.get("dex") == "uniswap-v4" or len(addr_str) == 66
        if not is_valid_pool_address(addr_str, is_v4=is_v4):
            logger.warning(
                "[INVALID_TOKEN_ADDR] cache entry rejected: invalid pool address %s", addr_str
            )
            continue

        tok0 = str(pool_info.get("token0") or pool_info.get("currency0") or "").strip()
        tok1 = str(pool_info.get("token1") or pool_info.get("currency1") or "").strip()

        # 检查代币地址: 若存在则必须是有效非零 42 字符地址
        if tok0 and not is_valid_token_address(tok0):
            logger.warning(
                "[INVALID_TOKEN_ADDR] cache entry %s rejected: invalid token0 %s", addr_str, tok0
            )
            continue
        if tok1 and not is_valid_token_address(tok1):
            logger.warning(
                "[INVALID_TOKEN_ADDR] cache entry %s rejected: invalid token1 %s", addr_str, tok1
            )
            continue

        filtered_pools[addr_str.lower()] = pool_info

    return filtered_pools


def save_pools_metadata_cache(
    cache_path: str | Path,
    pools_data: dict[str, Any],
) -> None:
    """将池元数据字典保存至显式指定的离线缓存文件 (纯离线工具)."""
    if cache_path is None:
        raise ValueError("cache_path must be explicitly provided")
    p = Path(cache_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": time.time(),
        "pools": pools_data,
    }
    with p.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


# 历史别名纯离线兼容声明
get_grouped_pools = group_by_pair
get_overlapping_pools_config = get_overlapping_pools
load_dynamic_pools = scan_pools

__all__ = [
    "DEFAULT_V4_METADATA",
    "DYNAMIC_FEE_FLAG",
    "EXCLUDED_DEXES",
    "PoolSpec",
    "ReadOnlyRpcTransport",
    "STATE_VIEW_ADDRESS",
    "STATE_VIEW_GET_SLOT0_SELECTOR",
    "SUPPORTED_DEXES",
    "ScannedPool",
    "V2_DEXES",
    "V2_STANDARD_FEE_BPS",
    "V3_FEE_SELECTOR",
    "V3_FORK_DEXES",
    "V4_DEXES",
    "V4PoolSpec",
    "ZERO_ADDRESS",
    "_validate_v4_metadata",
    "batch_read_v3_pool_fees",
    "decode_stateview_slot0_fee",
    "decode_v3_fee_data",
    "get_grouped_pools",
    "get_overlapping_pools",
    "get_overlapping_pools_config",
    "group_by_pair",
    "is_valid_pool_address",
    "is_valid_token_address",
    "load_dynamic_pools",
    "load_pools_metadata_cache",
    "parse_pool_name",
    "read_v3_pool_fee",
    "read_v4_pool_fee",
    "save_pools_metadata_cache",
    "scan_pools",
    "scanned_to_pool_spec",
]
