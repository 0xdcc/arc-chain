"""Uniswap V3 / V4 离线费用筛选与契约独立单元测试 (tests.arc_v3.independent.test_fee_scan_contract).

本测试套件独立校验 research.market_data.fee_scan 模块的核心安全红线与物理契约:
1. 纯输入接缝: raw_pools 显式入参校验, 绝无网络 fetch 或磁盘 pickle/json 隐式加载;
2. 零费率与空响应语义解耦:
   - 链上读取成功的数值 0 (0 ppm -> 0.0 bps) 确认为合法费率正常入库;
   - RPC 空响应 ("0x")、Revert 异常与截断一律 fail-closed 跳过并记录 [FEE_UNVERIFIED];
   - 强校验函数签名: 严禁引入 allow_zero / allow_zero_fee 开关参数;
3. 链上真实费率覆盖:
   - 链上值与池名称标称值差异 > 1.0 bps 时以链上真实值为准覆盖标称值, 记录 [FEE_MISMATCH];
4. V4 两级确权与强身份绑定:
   - 显式 metadata 字段核验 (pool_id 一致性、chain_id 强绑定、代币双向比对);
   - 绝不硬编码 5042 误拒历史研究链 4663;
   - 缺失比较依据、冲突或非法时明确拒绝采纳, 降级 StateView.getSlot0 (128 字节解析);
   - 两级均读不到时 fail-closed 剔除, 严禁使用 30.0 bps 兜底;
5. 费率合法性与范围控制:
   - fee_bps 与 ppm 统一协议合法范围 [0.0, 10000.0] bps / [0, 1000000] ppm;
   - fee_bps 浮点数严禁误作 bitfield 位运算; ppm 动态费标志位 (0x800000) 严格拦截;
   - 非有限数值 (NaN, Inf)、负数、超界一律拦截;
6. 地址与代币防线:
   - 42 字符 (V2/V3) 与 66 字符 (V4) 架构长度校验, 全零地址拦截, [INVALID_TOKEN_ADDR] 告警;
7. 单点 'latest' 边界认知:
   - 独立测试确证循环调用发起单点 eth_call(..., "latest"), 不误称为原子 snapshot.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any
from unittest.mock import MagicMock

import pytest
from eth_abi import encode as abi_encode

from research.market_data.fee_scan import (
    _validate_v4_metadata,
    scan_pools,
)


def _make_mock_rpc(
    v3_fee_return: str | int | None = None, raise_exc: Exception | None = None
) -> MagicMock:
    """构造用于模拟 JSON-RPC eth_call 的 Mock 客户端."""
    mock_rpc = MagicMock()
    if raise_exc is not None:
        mock_rpc.call.side_effect = raise_exc
    elif v3_fee_return is not None:
        if isinstance(v3_fee_return, int):
            hex_val = "0x" + hex(v3_fee_return)[2:].zfill(64)
            mock_rpc.call.return_value = {"result": hex_val}
        else:
            mock_rpc.call.return_value = {"result": v3_fee_return}
    else:
        mock_rpc.call.return_value = {"result": "0x"}
    return mock_rpc


class TestFeeScanPureSeamAndSignature:
    """纯输入接缝与签名契约校验."""

    def test_signature_has_no_allow_zero_switch(self) -> None:
        """核心规范: 严禁在 scan_pools 中新增 allow_zero / allow_zero_fee 开关."""
        sig = inspect.signature(scan_pools)
        assert "allow_zero" not in sig.parameters
        assert "allow_zero_fee" not in sig.parameters

    def test_raw_pools_none_or_empty_returns_empty_list(self) -> None:
        """纯输入接缝: raw_pools 为 None 或 [] 时直接返回空列表, 零副作用."""
        assert scan_pools(raw_pools=None) == []
        assert scan_pools(raw_pools=[]) == []

    def test_min_tvl_filtering(self) -> None:
        """TVL 门槛过滤: 小于 min_tvl 的池子直接剔除, 零 RPC 调用."""
        mock_rpc = MagicMock()
        pool = {
            "dex": "uniswap-v3",
            "name": "LOW / WETH 0.3%",
            "addr": "0x1111111111111111111111111111111111111111",
            "tvl": 1000.0,
        }
        res = scan_pools(min_tvl=50_000.0, raw_pools=[pool], rpc=mock_rpc)
        assert len(res) == 0
        assert mock_rpc.call.call_count == 0


class TestFeeScanNumericZeroVsEmpty:
    """数值 0 费率合法性与空响应 Fail-Closed 对照校验."""

    def test_numeric_zero_fee_is_accepted_as_valid_pool(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """合法数值 0 (32 字节 0 ppm -> 0.0 bps) 正常入库, 覆盖名称标称值并记录超阈值警告."""
        caplog.set_level(logging.WARNING)
        raw_pool: dict[str, Any] = {
            "dex": "uniswap-v3",
            "name": "ZERO / WETH 0.05%",
            "addr": "0x1234567890123456789012345678901234567890",
            "tvl": 100_000.0,
        }
        mock_rpc = _make_mock_rpc(v3_fee_return=0)
        scanned = scan_pools(min_tvl=50_000.0, raw_pools=[raw_pool], rpc=mock_rpc)

        assert len(scanned) == 1
        assert scanned[0].fee_bps == 0.0
        assert "[FEE_MISMATCH]" in caplog.text
        assert "名称=5.00" in caplog.text
        assert "链上=0.00" in caplog.text

    def test_numeric_zero_fee_at_boundary_does_not_warn(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """边界对照: 链上 0.00 bps 与名称 0.01% (1.00 bps) 差值恰为 1.0 bps (<=1.0 未超阈值), 正常入库且不发警告."""
        caplog.set_level(logging.WARNING)
        raw_pool: dict[str, Any] = {
            "dex": "uniswap-v3",
            "name": "ZERO / WETH 0.01%",
            "addr": "0x1234567890123456789012345678901234567890",
            "tvl": 100_000.0,
        }
        mock_rpc = _make_mock_rpc(v3_fee_return=0)
        scanned = scan_pools(min_tvl=50_000.0, raw_pools=[raw_pool], rpc=mock_rpc)

        assert len(scanned) == 1
        assert scanned[0].fee_bps == 0.0
        assert "[FEE_MISMATCH]" not in caplog.text

    def test_empty_0x_response_is_rejected_fail_closed(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """RPC 返回 '0x' 空响应时必须 fail-closed 跳过该池, 记录 [FEE_UNVERIFIED]."""
        caplog.set_level(logging.WARNING)
        raw_pool: dict[str, Any] = {
            "dex": "uniswap-v3",
            "name": "EMPTY / WETH 0.05%",
            "addr": "0x1234567890123456789012345678901234567890",
            "tvl": 100_000.0,
        }
        mock_rpc = _make_mock_rpc(v3_fee_return="0x")
        scanned = scan_pools(min_tvl=50_000.0, raw_pools=[raw_pool], rpc=mock_rpc)

        assert len(scanned) == 0
        assert "[FEE_UNVERIFIED]" in caplog.text

    def test_rpc_revert_is_rejected_fail_closed(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """RPC 调用 Revert 时必须 fail-closed 跳过该池, 严禁使用 30.0 兜底."""
        caplog.set_level(logging.WARNING)
        raw_pool: dict[str, Any] = {
            "dex": "uniswap-v3",
            "name": "REVERT / WETH 0.3%",
            "addr": "0x1234567890123456789012345678901234567890",
            "tvl": 100_000.0,
        }
        mock_rpc = _make_mock_rpc(raise_exc=RuntimeError("execution reverted"))
        scanned = scan_pools(min_tvl=50_000.0, raw_pools=[raw_pool], rpc=mock_rpc)

        assert len(scanned) == 0
        assert "[FEE_UNVERIFIED]" in caplog.text


class TestFeeScanChainOverridesAndLogging:
    """链上费率覆盖与日志告警契约."""

    def test_v3_chain_fee_overrides_name_and_logs_mismatch(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """差异 > 1.0 bps 时链上真实值覆盖池名称标称值, 记录 [FEE_MISMATCH]."""
        caplog.set_level(logging.WARNING)
        raw_pool: dict[str, Any] = {
            "dex": "uniswap-v3",
            "name": "TEST / WETH 0.01%",  # 1.0 bps
            "addr": "0x1111111111111111111111111111111111111111",
            "tvl": 100_000.0,
        }
        mock_rpc = _make_mock_rpc(v3_fee_return=3000)  # 30.0 bps
        scanned = scan_pools(min_tvl=50_000.0, raw_pools=[raw_pool], rpc=mock_rpc)

        assert len(scanned) == 1
        assert scanned[0].fee_bps == pytest.approx(30.0)
        assert "[FEE_MISMATCH]" in caplog.text
        assert "名称=1.00" in caplog.text
        assert "链上=30.00" in caplog.text

    def test_v3_matching_fee_does_not_log_mismatch(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """差异 <= 1.0 bps 时正常确权, 不输出 [FEE_MISMATCH] 告警."""
        caplog.set_level(logging.WARNING)
        raw_pool: dict[str, Any] = {
            "dex": "uniswap-v3",
            "name": "TEST / WETH 0.3%",  # 30.0 bps
            "addr": "0x1111111111111111111111111111111111111111",
            "tvl": 100_000.0,
        }
        mock_rpc = _make_mock_rpc(v3_fee_return=3000)  # 30.0 bps
        scanned = scan_pools(min_tvl=50_000.0, raw_pools=[raw_pool], rpc=mock_rpc)

        assert len(scanned) == 1
        assert scanned[0].fee_bps == pytest.approx(30.0)
        assert "[FEE_MISMATCH]" not in caplog.text


class TestFeeScanV4MetadataValidation:
    """V4 元数据核验与 StateView 降级契约."""

    def test_injected_v4_metadata_success(self) -> None:
        """显式注入合规元数据 (补全身份), 成功确权且无需发起 StateView eth_call."""
        mock_rpc = MagicMock()
        v4_id = "0x" + "11" * 32
        tok0 = "0x" + "22" * 20
        tok1 = "0x" + "33" * 20
        v4_pool = {
            "dex": "uniswap-v4",
            "name": "V4 / USDG 0.05%",
            "addr": v4_id,
            "tvl": 100_000.0,
            "chain_id": 5042,
            "token0_address": tok0,
            "token1_address": tok1,
        }
        custom_meta = {
            v4_id: {
                "pool_id": v4_id,
                "chain_id": 5042,
                "fee": 500,  # 5.0 bps
                "currency0": tok0,
                "currency1": tok1,
            }
        }
        scanned = scan_pools(
            min_tvl=50_000.0,
            raw_pools=[v4_pool],
            rpc=mock_rpc,
            v4_metadata=custom_meta,
        )
        assert len(scanned) == 1
        assert scanned[0].fee_bps == pytest.approx(5.0)
        assert mock_rpc.call.call_count == 0

    def test_v4_metadata_with_dynamic_fee_flag_is_rejected(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """元数据置位动态费标志位 (0x800000) 时判定不可信, 降级调用 StateView."""
        caplog.set_level(logging.WARNING)
        mock_rpc = MagicMock()
        encoded = abi_encode(["uint160", "int24", "uint24", "uint24"], [2**96, 0, 0, 3000])
        mock_rpc.call.return_value = {"result": "0x" + encoded.hex()}

        v4_id = "0x" + "44" * 32
        tok0 = "0x" + "22" * 20
        tok1 = "0x" + "33" * 20
        v4_pool = {
            "dex": "uniswap-v4",
            "name": "DYNAMIC / USDG 0.3%",
            "addr": v4_id,
            "tvl": 100_000.0,
            "chain_id": 5042,
            "token0_address": tok0,
            "token1_address": tok1,
        }
        tainted_meta = {
            v4_id: {
                "pool_id": v4_id,
                "chain_id": 5042,
                "currency0": tok0,
                "currency1": tok1,
                "fee": 0x800500,  # 动态费标志位置位
            }
        }
        scanned = scan_pools(
            min_tvl=50_000.0,
            raw_pools=[v4_pool],
            rpc=mock_rpc,
            v4_metadata=tainted_meta,
        )
        # 降级通过 StateView 成功确权
        assert len(scanned) == 1
        assert scanned[0].fee_bps == pytest.approx(30.0)
        assert "置位动态费标志位" in caplog.text

    def test_v4_metadata_mismatched_pool_id_rejected(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """元数据中 pool_id 与候选池地址不一致时拒绝使用."""
        caplog.set_level(logging.WARNING)
        mock_rpc = MagicMock()
        mock_rpc.call.side_effect = RuntimeError("StateView revert")

        v4_id = "0x" + "55" * 32
        tok0 = "0x" + "22" * 20
        tok1 = "0x" + "33" * 20
        v4_pool = {
            "dex": "uniswap-v4",
            "name": "MISMATCH / USDG 0.3%",
            "addr": v4_id,
            "tvl": 100_000.0,
            "chain_id": 5042,
            "token0_address": tok0,
            "token1_address": tok1,
        }
        corrupted_meta = {
            v4_id: {
                "pool_id": "0x" + "66" * 32,  # 不匹配
                "chain_id": 5042,
                "currency0": tok0,
                "currency1": tok1,
                "fee": 3000,
            }
        }
        scanned = scan_pools(
            min_tvl=50_000.0,
            raw_pools=[v4_pool],
            rpc=mock_rpc,
            v4_metadata=corrupted_meta,
        )
        # 元数据被拒, StateView Revert -> fail-closed 剔除
        assert len(scanned) == 0
        assert "pool_id 不匹配" in caplog.text
        assert "[FEE_UNVERIFIED]" in caplog.text


class TestFeeScanAddressAndTokens:
    """地址合法性与架构防线校验."""

    def test_invalid_pool_address_skipped(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """池地址长度异常或全零时剔除, 记录 [INVALID_TOKEN_ADDR]."""
        caplog.set_level(logging.WARNING)
        short_addr_pool = {
            "dex": "uniswap-v3",
            "name": "SHORT / WETH 0.3%",
            "addr": "0x1234",
            "tvl": 100_000.0,
        }
        zero_addr_pool = {
            "dex": "uniswap-v3",
            "name": "ZERO / WETH 0.3%",
            "addr": "0x0000000000000000000000000000000000000000",
            "tvl": 100_000.0,
        }
        scanned = scan_pools(min_tvl=50_000.0, raw_pools=[short_addr_pool, zero_addr_pool])
        assert len(scanned) == 0
        assert "[INVALID_TOKEN_ADDR]" in caplog.text

    def test_invalid_explicit_token_address_skipped(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """显式传入非法代币地址时剔除, 记录 [INVALID_TOKEN_ADDR]."""
        caplog.set_level(logging.WARNING)
        bad_token_pool = {
            "dex": "uniswap-v3",
            "name": "BADTOK / WETH 0.3%",
            "addr": "0x1111111111111111111111111111111111111111",
            "tvl": 100_000.0,
            "token0_address": "0xbad",
            "token1_address": "0x2222222222222222222222222222222222222222",
        }
        scanned = scan_pools(min_tvl=50_000.0, raw_pools=[bad_token_pool])
        assert len(scanned) == 0
        assert "[INVALID_TOKEN_ADDR]" in caplog.text


class TestFeeScanMetadataIdentityAndRateControl:
    """M4C 独立返修: 错链/错代币防采纳、身份绑定与两种费率表示合法性检验."""

    # ---------------- 1. 错链反例与降级隔离 ----------------
    def test_wrong_chain_id_rejected_and_falls_back(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """错链反例: 元数据 chain_id 为以太坊 1 或 Base 8453, 与候选池 5042 冲突时拒绝采纳."""
        caplog.set_level(logging.WARNING)
        v4_id = "0x" + "77" * 32
        tok0 = "0x" + "22" * 20
        tok1 = "0x" + "33" * 20
        v4_pool = {
            "dex": "uniswap-v4",
            "name": "CROSSCHAIN / USDG 0.05%",
            "addr": v4_id,
            "tvl": 100_000.0,
            "chain_id": 5042,
            "token0_address": tok0,
            "token1_address": tok1,
        }
        # 错链元数据 (冒充 5 bps)
        wrong_chain_meta = {
            v4_id: {
                "pool_id": v4_id,
                "chain_id": 1,  # 错误链 ID
                "currency0": tok0,
                "currency1": tok1,
                "fee": 500,
            }
        }
        # StateView 读取真实费率 30.0 bps
        mock_rpc = MagicMock()
        encoded = abi_encode(["uint160", "int24", "uint24", "uint24"], [2**96, 0, 0, 3000])
        mock_rpc.call.return_value = {"result": "0x" + encoded.hex()}

        scanned = scan_pools(
            min_tvl=50_000.0,
            raw_pools=[v4_pool],
            rpc=mock_rpc,
            v4_metadata=wrong_chain_meta,
        )
        # 错链元数据被拒, 不以其 5.0 bps 为真, 通过 StateView 得到真实 30.0 bps
        assert len(scanned) == 1
        assert scanned[0].fee_bps == pytest.approx(30.0)
        assert "与期望链" in caplog.text

    def test_wrong_chain_id_with_failed_rpc_skips_fail_closed(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """错链元数据拒绝且 StateView 失败时, fail-closed 剔除该池, 绝不误采纳错链费率."""
        caplog.set_level(logging.WARNING)
        v4_id = "0x" + "77" * 32
        tok0 = "0x" + "22" * 20
        tok1 = "0x" + "33" * 20
        v4_pool = {
            "dex": "uniswap-v4",
            "name": "CROSSCHAIN / USDG 0.05%",
            "addr": v4_id,
            "tvl": 100_000.0,
            "chain_id": 5042,
            "token0_address": tok0,
            "token1_address": tok1,
        }
        wrong_chain_meta = {
            v4_id: {
                "pool_id": v4_id,
                "chain_id": 8453,  # Base
                "currency0": tok0,
                "currency1": tok1,
                "fee": 500,
            }
        }
        mock_rpc = MagicMock()
        mock_rpc.call.side_effect = RuntimeError("RPC unreachable")

        scanned = scan_pools(
            min_tvl=50_000.0,
            raw_pools=[v4_pool],
            rpc=mock_rpc,
            v4_metadata=wrong_chain_meta,
        )
        assert len(scanned) == 0
        assert "[FEE_UNVERIFIED]" in caplog.text

    # ---------------- 2. 错代币反例与降级隔离 ----------------
    def test_token_mismatch_rejected_and_falls_back(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """错代币反例: 元数据 token 与候选池 token 不一致时拒绝采纳."""
        caplog.set_level(logging.WARNING)
        v4_id = "0x" + "88" * 32
        tok0 = "0x" + "22" * 20
        tok1 = "0x" + "33" * 20
        bogus_tok = "0x" + "99" * 20

        v4_pool = {
            "dex": "uniswap-v4",
            "name": "TOK_MISMATCH / USDG 0.05%",
            "addr": v4_id,
            "tvl": 100_000.0,
            "chain_id": 5042,
            "token0_address": tok0,
            "token1_address": tok1,
        }
        mismatched_meta = {
            v4_id: {
                "pool_id": v4_id,
                "chain_id": 5042,
                "currency0": tok0,
                "currency1": bogus_tok,  # 错代币
                "fee": 500,
            }
        }
        mock_rpc = MagicMock()
        encoded = abi_encode(["uint160", "int24", "uint24", "uint24"], [2**96, 0, 0, 3000])
        mock_rpc.call.return_value = {"result": "0x" + encoded.hex()}

        scanned = scan_pools(
            min_tvl=50_000.0,
            raw_pools=[v4_pool],
            rpc=mock_rpc,
            v4_metadata=mismatched_meta,
        )
        assert len(scanned) == 1
        assert scanned[0].fee_bps == pytest.approx(30.0)
        assert "不匹配" in caplog.text

    # ---------------- 3. 缺失关键身份字段反例 ----------------
    def test_missing_pool_id_in_metadata_rejected(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """缺身份反例: 元数据缺失 pool_id/address 时必须拒绝."""
        caplog.set_level(logging.WARNING)
        v4_id = "0x" + "88" * 32
        tok0 = "0x" + "22" * 20
        tok1 = "0x" + "33" * 20
        meta_missing_pid = {
            "chain_id": 5042,
            "currency0": tok0,
            "currency1": tok1,
            "fee": 3000,
        }
        res = _validate_v4_metadata(
            v4_id,
            meta_missing_pid,
            expected_tokens=(tok0, tok1),
            expected_chain_id=5042,
        )
        assert res is None
        assert "缺失 pool_id/address" in caplog.text

    def test_missing_chain_id_in_metadata_rejected(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """缺身份反例: 元数据缺失 chain_id 时必须拒绝."""
        caplog.set_level(logging.WARNING)
        v4_id = "0x" + "88" * 32
        tok0 = "0x" + "22" * 20
        tok1 = "0x" + "33" * 20
        meta_missing_cid = {
            "pool_id": v4_id,
            "currency0": tok0,
            "currency1": tok1,
            "fee": 3000,
        }
        res = _validate_v4_metadata(
            v4_id,
            meta_missing_cid,
            expected_tokens=(tok0, tok1),
            expected_chain_id=5042,
        )
        assert res is None
        assert "缺失 chain_id" in caplog.text

    def test_missing_tokens_in_metadata_rejected(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """缺身份反例: 元数据缺失 currency0/currency1 时必须拒绝."""
        caplog.set_level(logging.WARNING)
        v4_id = "0x" + "88" * 32
        tok0 = "0x" + "22" * 20
        tok1 = "0x" + "33" * 20
        meta_missing_toks = {
            "pool_id": v4_id,
            "chain_id": 5042,
            "fee": 3000,
        }
        res = _validate_v4_metadata(
            v4_id,
            meta_missing_toks,
            expected_tokens=(tok0, tok1),
            expected_chain_id=5042,
        )
        assert res is None
        assert "缺失 currency0/currency1" in caplog.text

    def test_missing_chain_comparison_basis_falls_back_to_reader(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """比较依据缺失: 候选池未提供 chain_id, 元数据有 chain_id 但无比较依据, 拒绝元数据并走 StateView."""
        caplog.set_level(logging.WARNING)
        v4_id = "0x" + "88" * 32
        tok0 = "0x" + "22" * 20
        tok1 = "0x" + "33" * 20
        # 候选池无 chain_id
        v4_pool = {
            "dex": "uniswap-v4",
            "name": "NO_CID / USDG 0.3%",
            "addr": v4_id,
            "tvl": 100_000.0,
            "token0_address": tok0,
            "token1_address": tok1,
        }
        meta = {
            v4_id: {
                "pool_id": v4_id,
                "chain_id": 5042,
                "currency0": tok0,
                "currency1": tok1,
                "fee": 500,  # 5.0 bps
            }
        }
        mock_rpc = MagicMock()
        encoded = abi_encode(["uint160", "int24", "uint24", "uint24"], [2**96, 0, 0, 3000])
        mock_rpc.call.return_value = {"result": "0x" + encoded.hex()}

        scanned = scan_pools(
            min_tvl=50_000.0,
            raw_pools=[v4_pool],
            rpc=mock_rpc,
            v4_metadata=meta,
        )
        # 元数据无比较依据不采纳, 降级读取 StateView 得 30.0 bps
        assert len(scanned) == 1
        assert scanned[0].fee_bps == pytest.approx(30.0)
        assert "缺少 expected_chain_id 比较依据" in caplog.text

    # ---------------- 4. 费率合法性与范围控制反例 ----------------
    def test_fee_bps_negative_rejected(self) -> None:
        """fee_bps 负数反例: 严格拒绝."""
        v4_id = "0x" + "88" * 32
        tok0 = "0x" + "22" * 20
        tok1 = "0x" + "33" * 20
        meta = {
            "pool_id": v4_id,
            "chain_id": 5042,
            "currency0": tok0,
            "currency1": tok1,
            "fee_bps": -1.0,
        }
        assert _validate_v4_metadata(v4_id, meta, expected_tokens=(tok0, tok1), expected_chain_id=5042) is None

    def test_fee_bps_out_of_range_rejected(self) -> None:
        """fee_bps 超出协议范围 (>10000.0 bps) 反例: 严格拒绝."""
        v4_id = "0x" + "88" * 32
        tok0 = "0x" + "22" * 20
        tok1 = "0x" + "33" * 20
        meta = {
            "pool_id": v4_id,
            "chain_id": 5042,
            "currency0": tok0,
            "currency1": tok1,
            "fee_bps": 10000.1,
        }
        assert _validate_v4_metadata(v4_id, meta, expected_tokens=(tok0, tok1), expected_chain_id=5042) is None

    def test_fee_bps_non_finite_rejected(self) -> None:
        """fee_bps 非有限值 (NaN, Inf) 反例: 严格拒绝."""
        v4_id = "0x" + "88" * 32
        tok0 = "0x" + "22" * 20
        tok1 = "0x" + "33" * 20
        meta_nan = {
            "pool_id": v4_id,
            "chain_id": 5042,
            "currency0": tok0,
            "currency1": tok1,
            "fee_bps": float("nan"),
        }
        meta_inf = {
            "pool_id": v4_id,
            "chain_id": 5042,
            "currency0": tok0,
            "currency1": tok1,
            "fee_bps": float("inf"),
        }
        assert _validate_v4_metadata(v4_id, meta_nan, expected_tokens=(tok0, tok1), expected_chain_id=5042) is None
        assert _validate_v4_metadata(v4_id, meta_inf, expected_tokens=(tok0, tok1), expected_chain_id=5042) is None

    def test_fee_ppm_out_of_range_rejected(self) -> None:
        """ppm fee 越界反例 (<0 或 >1000000) 严格拒绝."""
        v4_id = "0x" + "88" * 32
        tok0 = "0x" + "22" * 20
        tok1 = "0x" + "33" * 20
        meta_neg = {
            "pool_id": v4_id,
            "chain_id": 5042,
            "currency0": tok0,
            "currency1": tok1,
            "fee": -1,
        }
        meta_oor = {
            "pool_id": v4_id,
            "chain_id": 5042,
            "currency0": tok0,
            "currency1": tok1,
            "fee": 1_000_001,
        }
        assert _validate_v4_metadata(v4_id, meta_neg, expected_tokens=(tok0, tok1), expected_chain_id=5042) is None
        assert _validate_v4_metadata(v4_id, meta_oor, expected_tokens=(tok0, tok1), expected_chain_id=5042) is None

    def test_conflicting_fee_and_fee_bps_rejected(self) -> None:
        """两种表示冲突 (fee=3000 ppm vs fee_bps=5.0) 严格拒绝."""
        v4_id = "0x" + "88" * 32
        tok0 = "0x" + "22" * 20
        tok1 = "0x" + "33" * 20
        meta_conflict = {
            "pool_id": v4_id,
            "chain_id": 5042,
            "currency0": tok0,
            "currency1": tok1,
            "fee": 3000,
            "fee_bps": 5.0,
        }
        assert _validate_v4_metadata(v4_id, meta_conflict, expected_tokens=(tok0, tok1), expected_chain_id=5042) is None

    # ---------------- 5. 有效正向控制 (Positive Controls) ----------------
    def test_valid_fee_bps_representation_accepted(self) -> None:
        """正向控制: 合法 fee_bps 浮点表示 (30.0 bps) 正确采纳且零 RPC 调用."""
        mock_rpc = MagicMock()
        v4_id = "0x" + "88" * 32
        tok0 = "0x" + "22" * 20
        tok1 = "0x" + "33" * 20
        v4_pool = {
            "dex": "uniswap-v4",
            "name": "BPS / USDG 0.3%",
            "addr": v4_id,
            "tvl": 100_000.0,
            "chain_id": 5042,
            "token0_address": tok0,
            "token1_address": tok1,
        }
        valid_meta = {
            v4_id: {
                "pool_id": v4_id,
                "chain_id": 5042,
                "currency0": tok0,
                "currency1": tok1,
                "fee_bps": 30.0,
            }
        }
        scanned = scan_pools(
            min_tvl=50_000.0,
            raw_pools=[v4_pool],
            rpc=mock_rpc,
            v4_metadata=valid_meta,
        )
        assert len(scanned) == 1
        assert scanned[0].fee_bps == pytest.approx(30.0)
        assert mock_rpc.call.call_count == 0

    def test_valid_numeric_zero_in_metadata_accepted(self) -> None:
        """正向控制: 合法数值 0 费率 (fee=0 或 fee_bps=0.0) 正常采纳."""
        v4_id = "0x" + "88" * 32
        tok0 = "0x" + "22" * 20
        tok1 = "0x" + "33" * 20
        meta_zero_ppm = {
            "pool_id": v4_id,
            "chain_id": 5042,
            "currency0": tok0,
            "currency1": tok1,
            "fee": 0,
        }
        meta_zero_bps = {
            "pool_id": v4_id,
            "chain_id": 5042,
            "currency0": tok0,
            "currency1": tok1,
            "fee_bps": 0.0,
        }
        res_ppm = _validate_v4_metadata(v4_id, meta_zero_ppm, expected_tokens=(tok0, tok1), expected_chain_id=5042)
        res_bps = _validate_v4_metadata(v4_id, meta_zero_bps, expected_tokens=(tok0, tok1), expected_chain_id=5042)
        assert res_ppm == 0.0
        assert res_bps == 0.0

    def test_historical_research_chain_4663_accepted(self) -> None:
        """正向控制: 历史研究链 Robinhood 4663 正确采纳, 证明 5042 未被错误全量硬编码."""
        mock_rpc = MagicMock()
        v4_id = "0x" + "99" * 32
        tok0 = "0x" + "22" * 20
        tok1 = "0x" + "33" * 20
        v4_pool = {
            "dex": "uniswap-v4",
            "name": "ROBINHOOD / USDG 0.3%",
            "addr": v4_id,
            "tvl": 100_000.0,
            "chain_id": 4663,  # 历史研究链
            "token0_address": tok0,
            "token1_address": tok1,
        }
        rh_meta = {
            v4_id: {
                "pool_id": v4_id,
                "chain_id": 4663,
                "currency0": tok0,
                "currency1": tok1,
                "fee": 3000,
            }
        }
        scanned = scan_pools(
            min_tvl=50_000.0,
            raw_pools=[v4_pool],
            rpc=mock_rpc,
            v4_metadata=rh_meta,
        )
        assert len(scanned) == 1
        assert scanned[0].fee_bps == pytest.approx(30.0)
        assert mock_rpc.call.call_count == 0
