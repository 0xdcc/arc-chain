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

import logging
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from research.market_data.fee_verification import (
    DYNAMIC_FEE_FLAG,
    STATE_VIEW_ADDRESS,
    STATE_VIEW_GET_SLOT0_SELECTOR,
    V2_STANDARD_FEE_BPS,
    V3_FEE_SELECTOR,
    ReadOnlyRpcTransport,
    decode_stateview_slot0_fee,
    decode_v3_fee_data,
    read_v3_pool_fee,
    read_v4_pool_fee,
)

logger = logging.getLogger(__name__)

ZERO_ADDRESS: str = "0x0000000000000000000000000000000000000000"

# DEX 架构分类与白名单集合
V3_FORK_DEXES: frozenset[str] = frozenset({"uniswap-v3", "up-v3", "giga-v3", "ramses-v3"})
V2_DEXES: frozenset[str] = frozenset({"uniswap-v2"})
V4_DEXES: frozenset[str] = frozenset({"uniswap-v4"})
SUPPORTED_DEXES: frozenset[str] = V3_FORK_DEXES | V2_DEXES | V4_DEXES
EXCLUDED_DEXES: frozenset[str] = frozenset({
    "alandale-cl",
    "bankr",
    "orvex-v4",
    "ramses-dlmm",
    "pons-v2",
})

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
    meta_tok0 = raw_meta.get("currency0") or raw_meta.get("token0") or raw_meta.get("token0_address")
    meta_tok1 = raw_meta.get("currency1") or raw_meta.get("token1") or raw_meta.get("token1_address")
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

    exp_token_set = {str(t).strip().lower() for t in expected_tokens if isinstance(t, str) and t.strip()}
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
            logger.warning("[FEE_UNVERIFIED] V4 池 %s 元数据 fee_bps 越界 (%.2f bps)", addr, bps_val)
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
                logger.warning("[INVALID_TOKEN_ADDR] V4 池 %s 地址/PoolId 非法(须66字符)，跳过", addr)
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
                logger.warning("[INVALID_TOKEN_ADDR] 池 %s token0=%s token1=%s，跳过", addr, tok0_str, tok1_str)
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
            meta_dict: dict[str, Any] | None = None
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
                    logger.warning("[FEE_MISMATCH] 名称=%.2f 链上=%.2f 池=%s", name_fee_bps, fee_bps, addr)
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
                    logger.warning("[FEE_MISMATCH] 名称=%.2f 链上=%.2f 池=%s", name_fee_bps, fee_bps, addr)
        elif is_v3_pool:
            # V3 链上读取: fee() (0xddca3f43)
            try:
                chain_fee_bps = v3_reader(addr, rpc=rpc)
            except Exception as exc:
                logger.warning(
                    "[FEE_UNVERIFIED] V3 池 %s (%s) 链上 fee() 调用失败: %s，跳过该池",
                    addr,
                    raw_name,
                    exc,
                )
                continue

            fee_bps = chain_fee_bps
            if abs(fee_bps - name_fee_bps) > 1.0:
                logger.warning("[FEE_MISMATCH] 名称=%.2f 链上=%.2f 池=%s", name_fee_bps, fee_bps, addr)
        else:
            logger.warning("[FEE_UNVERIFIED] 未知架构池 %s (%s, dex=%s) 无法验证真实费率，跳过该池", addr, raw_name, dex)
            continue

        # 关联代币地址与元数据
        tok0_addr = str(item.get("token0_address", ""))
        tok1_addr = str(item.get("token1_address", ""))
        vol24h = float(item.get("vol24h") or 0.0)
        created = str(item.get("created", ""))

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
            )
        )

    return scanned


__all__ = [
    "DEFAULT_V4_METADATA",
    "DYNAMIC_FEE_FLAG",
    "EXCLUDED_DEXES",
    "STATE_VIEW_ADDRESS",
    "STATE_VIEW_GET_SLOT0_SELECTOR",
    "SUPPORTED_DEXES",
    "V2_DEXES",
    "V2_STANDARD_FEE_BPS",
    "V3_FEE_SELECTOR",
    "V3_FORK_DEXES",
    "V4_DEXES",
    "ZERO_ADDRESS",
    "ReadOnlyRpcTransport",
    "ScannedPool",
    "_validate_v4_metadata",
    "decode_stateview_slot0_fee",
    "decode_v3_fee_data",
    "is_valid_pool_address",
    "is_valid_token_address",
    "parse_pool_name",
    "read_v3_pool_fee",
    "read_v4_pool_fee",
    "scan_pools",
]
