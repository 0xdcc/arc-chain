"""全 DEX 池费率链上真实读取与强校验单元测试 (M4C 技术接缝适配版).

遵循 CODEX_FIX_FEE_SOURCE_TASK.md 与 AGENTS.md 规范:
1. V3 fork 池 (uniswap-v3, up-v3, giga-v3, ramses-v3) 必须通过 eth_call 调 fee() 读链上真实费率;
2. fee() 返回值单位为 bps, 严格以链上值为准并覆盖名称解析值;
3. fee() 失败 (revert / 超时 / 空数据) 必须跳过该池 (fail-closed), 记 [FEE_UNVERIFIED], 严禁用 30.0 或名称兜底;
4. 链上值与名称差异 > 1 bps 时必须记 [FEE_MISMATCH] 告警且以链上值为准;
5. V4 池必须从 PoolKey 或 StateView 读取, 读不到同样跳过;
6. V2 池固定 30.0 bps (仅 Uniswap V2 规范允许使用此常量);
7. 所有 mock 测试真实可证伪, 禁止让 mock 返回与名称相同的值.

M4C 适配与夹具矛盾仲裁说明:
- 技术接缝适配: 从已验证的 research.market_data.fee_verification 与 research.market_data.fee_scan 导入;
- 夹具单位矛盾修正 (test_giga_and_up_v3_real_scenario_verification):
  原用例传入 mock_rpc(v3_fee_return=100), 实际编码为 uint24 100 ppm (= 1.0 bps), 与池名 "0.01%" (= 1.0 bps)
  完全相等, 无法触发 [FEE_MISMATCH] 且与 fee_bps == 100.0 断言矛盾 (旧代码未除以 100 时的历史遗留)。
  为保证 100.0 bps 业务测试目标, 候选施工将 fixture 调整为 10000 ppm (对应 100.0 bps)。
- 夹具身份补全 (test_v4_pool_reads_fee_from_manifest_or_stateview):
  按 M4C 规范补全候选池 expected chain_id (5042) 与代币地址, 建立与元数据的强身份绑定.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest
from eth_abi import encode as abi_encode

from research.market_data.fee_scan import (
    parse_pool_name,
    scan_pools,
)
from research.market_data.fee_verification import (
    V2_STANDARD_FEE_BPS,
    V3_FEE_SELECTOR,
    read_v3_pool_fee,
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


class TestV3FeeOnChainVerification:
    """V3 fork 池链上真实费率读取与覆盖校验."""

    def test_mock_fee_returns_100_while_name_says_001_overrides_to_100(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """核心断言 1: mock fee() 返回 10000 ppm (100.0 bps) 而名称写 0.01% 时, 断言 fee_bps == 100.0, 且覆盖名称解析值 1.0."""
        caplog.set_level(logging.WARNING)
        # 构造名义上标注为 0.01% 的池 (parse_pool_name 解析出 1.0 bps)
        pool_name = "TOKEN / WETH 0.01%"
        parsed_t0, parsed_t1, parsed_fee = parse_pool_name(pool_name)
        assert parsed_fee == pytest.approx(1.0), (
            "名称解析值必须为 1.0 bps 以验证被链上真实值 100 覆盖"
        )

        raw_pool: dict[str, Any] = {
            "dex": "uniswap-v3",
            "name": pool_name,
            "addr": "0x1111111111111111111111111111111111111111",
            "tvl": 100_000.0,
            "vol24h": 0.0,
        }

        # mock fee() 调用的返回值为 10000 (100.0 bps), 差异达 100 倍
        mock_rpc = _make_mock_rpc(v3_fee_return=10000)

        scanned = scan_pools(
            min_tvl=50_000.0,
            raw_pools=[raw_pool],
            rpc=mock_rpc,
        )

        # 1. 断言池子正常返回
        assert len(scanned) == 1
        result_pool = scanned[0]

        # 2. 强校验: 最终 fee_bps 必须等于链上读取的 100.0, 绝不是名称里的 1.0 或默认的 30.0
        assert result_pool.fee_bps == pytest.approx(100.0)
        assert result_pool.fee_bps != pytest.approx(1.0)
        assert result_pool.fee_bps != pytest.approx(30.0)

        # 3. 校验 selector 0xddca3f43 调用参数
        mock_rpc.call.assert_called_once()
        args = mock_rpc.call.call_args[0]
        assert args[0] == "eth_call"
        assert args[1][0]["data"] == V3_FEE_SELECTOR
        assert args[1][0]["to"].lower() == raw_pool["addr"].lower()

        # 4. 校验 FEE_MISMATCH 告警输出
        assert "[FEE_MISMATCH]" in caplog.text
        assert "名称=1.00" in caplog.text
        assert "链上=100.00" in caplog.text

    def test_mock_fee_exception_skips_pool_fail_closed(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """核心断言 2: mock fee() 抛异常时, 断言该池被跳过 (fail-closed), 且 fee_bps 绝非 30.0 兜底."""
        caplog.set_level(logging.WARNING)
        raw_pool: dict[str, Any] = {
            "dex": "uniswap-v3",
            "name": "REVERT / WETH 0.01%",
            "addr": "0x2222222222222222222222222222222222222222",
            "tvl": 100_000.0,
            "vol24h": 0.0,
        }

        # 模拟链上调用异常 (例如 revert, 超时或 RPC 宕机)
        mock_rpc = _make_mock_rpc(raise_exc=RuntimeError("execution reverted: 0x"))

        scanned = scan_pools(
            min_tvl=50_000.0,
            raw_pools=[raw_pool],
            rpc=mock_rpc,
        )

        # 1. 强校验: 调用失败必须跳过该池, 返回列表必须为空
        assert len(scanned) == 0, "fee() 失败时该池必须被剔除, 绝不入库"

        # 2. 强校验: 严禁保留该池并赋予 30.0 兜底值
        assert not any(p.addr.lower() == raw_pool["addr"].lower() for p in scanned)

        # 3. 校验 FEE_UNVERIFIED 告警输出
        assert "[FEE_UNVERIFIED]" in caplog.text
        assert raw_pool["addr"].lower() in caplog.text.lower()

    def test_fee_returns_zero_or_empty_skips_pool(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """fee() 返回 0x 或空数据时被视为非法/未响应, fail-closed 跳过该池."""
        caplog.set_level(logging.WARNING)
        raw_pool: dict[str, Any] = {
            "dex": "giga-v3",
            "name": "EMPTY / WETH 0.05%",
            "addr": "0x3333333333333333333333333333333333333333",
            "tvl": 100_000.0,
            "vol24h": 0.0,
        }

        mock_rpc = _make_mock_rpc(v3_fee_return="0x")
        scanned = scan_pools(
            min_tvl=50_000.0,
            raw_pools=[raw_pool],
            rpc=mock_rpc,
        )

        assert len(scanned) == 0
        assert "[FEE_UNVERIFIED]" in caplog.text

    def test_giga_and_up_v3_real_scenario_verification(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """验证任务书中实测已定位的真实池子 (giga-v3 100bps, up-v3 53bps)."""
        caplog.set_level(logging.WARNING)
        # giga-v3 实测: 名称标 0.01% (1.0 bps), 业务测试目标链上真实 fee() = 100.0 bps (10000 ppm)
        # [M4C FIXTURE AUDIT NOTE]:
        # 原测试夹具中写 mock_rpc(v3_fee_return=100), 但 100 ppm 对应 1.0 bps, 与池名一致导致无法触发
        # 任何 mismatch 且断言 scanned[0].fee_bps == 100.0 无法通过。
        # 确认为编写测试时的单位混淆 Bug (ppm vs bps), 修正为 10000 ppm (100.0 bps) 以忠实还原 100 bps 业务校验目标。
        giga_pool = {
            "dex": "giga-v3",
            "name": "WETH / USDG 0.01%",
            "addr": "0xb2a6ad51b3ea3cdc8d3508cca147a43471382e53",
            "tvl": 200_000.0,
        }
        mock_rpc = _make_mock_rpc(v3_fee_return=10000)
        scanned = scan_pools(min_tvl=50_000.0, raw_pools=[giga_pool], rpc=mock_rpc)

        assert len(scanned) == 1
        assert scanned[0].fee_bps == pytest.approx(100.0)
        assert scanned[0].fee_bps != pytest.approx(1.0)
        assert "[FEE_MISMATCH]" in caplog.text


class TestDirectFeeReaders:
    """底层 read_v3_pool_fee 与 read_v4_pool_fee 函数单测."""

    def test_read_v3_pool_fee_success(self) -> None:
        """read_v3_pool_fee 成功解析 16 进制 32 字节返回值 (100 ppm -> 1.0 bps)."""
        mock_rpc = MagicMock()
        mock_rpc.call.return_value = {
            "result": "0x0000000000000000000000000000000000000000000000000000000000000064"
        }
        fee = read_v3_pool_fee("0x1111111111111111111111111111111111111111", rpc=mock_rpc)
        assert fee == pytest.approx(1.0)

    def test_read_v3_pool_fee_raises_on_revert(self) -> None:
        """read_v3_pool_fee 在 RPC 失败时抛出 RuntimeError."""
        mock_rpc = MagicMock()
        mock_rpc.call.side_effect = RuntimeError("RPC call failed: execution reverted")
        with pytest.raises(RuntimeError):
            read_v3_pool_fee("0x1111111111111111111111111111111111111111", rpc=mock_rpc)

    def test_read_v3_pool_fee_accepts_numeric_zero(self) -> None:
        """Complete ABI zero differs from empty RPC data; no live zero-tier claim."""
        mock_rpc = _make_mock_rpc(v3_fee_return=0)
        fee = read_v3_pool_fee("0x" + "1" * 40, rpc=mock_rpc)
        assert fee == 0.0


class TestV2AndV4PoolFeeRules:
    """V2 与 V4 池的费率提取规范."""

    def test_v2_pool_fixed_30bps_and_no_rpc_called(self) -> None:
        """V2 池固定 30.0 bps, 且绝不发起链上 fee() 调用 (V2 无 fee 接口)."""
        mock_rpc = MagicMock()
        v2_pool = {
            "dex": "uniswap-v2",
            "name": "Nasduck / WETH",
            "addr": "0x4444444444444444444444444444444444444444",
            "tvl": 100_000.0,
        }
        scanned = scan_pools(min_tvl=50_000.0, raw_pools=[v2_pool], rpc=mock_rpc)
        assert len(scanned) == 1
        assert scanned[0].fee_bps == pytest.approx(V2_STANDARD_FEE_BPS)
        assert scanned[0].fee_bps == pytest.approx(30.0)
        # 确认未对 V2 池发起 eth_call
        assert mock_rpc.call.call_count == 0

    def test_v4_pool_reads_fee_from_manifest_or_stateview(self) -> None:
        """V4 池从 PoolKey 固化元数据获取真实费率 (补全候选池身份以建立绑定)."""
        # 0x4be9657ec9002e528f4f17a5c43edc525a07f888f7b180c2afbf75e096c4f38a 为已知 V4 池 (fee=3000 -> 30 bps)
        v4_pool_id = "0x4be9657ec9002e528f4f17a5c43edc525a07f888f7b180c2afbf75e096c4f38a"
        v4_pool = {
            "dex": "uniswap-v4",
            "name": "PONS / USDG 0.3%",
            "addr": v4_pool_id,
            "tvl": 500_000.0,
            "chain_id": 5042,
            "token0_address": "0x39dBED3a2bd333467115dE45665cC57F813C4571",
            "token1_address": "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168",
        }
        mock_rpc = MagicMock()
        scanned = scan_pools(min_tvl=50_000.0, raw_pools=[v4_pool], rpc=mock_rpc)
        assert len(scanned) == 1
        assert scanned[0].fee_bps == pytest.approx(30.0)

    def test_v4_pool_stateview_slot0_fallback(self) -> None:
        """V4 池不在 manifest 时, 从 StateView.getSlot0 读取 lpFee."""
        mock_rpc = MagicMock()
        # 构造 StateView.getSlot0 返回数据: (uint160 sqrtPriceX96, int24 tick, uint24 protocolFee, uint24 lpFee)
        # lpFee 设为 7000 (70.0 bps = 0.70%)
        encoded = abi_encode(["uint160", "int24", "uint24", "uint24"], [2**96, 0, 0, 7000])
        mock_rpc.call.return_value = {"result": "0x" + encoded.hex()}

        unknown_v4_id = "0x" + "aa" * 32
        v4_pool = {
            "dex": "uniswap-v4",
            "name": "UNKNOWN / USDG 0.7%",
            "addr": unknown_v4_id,
            "tvl": 100_000.0,
        }
        scanned = scan_pools(min_tvl=50_000.0, raw_pools=[v4_pool], rpc=mock_rpc)
        assert len(scanned) == 1
        assert scanned[0].fee_bps == pytest.approx(70.0)

    def test_v4_pool_unverified_skips_pool(self, caplog: pytest.LogCaptureFixture) -> None:
        """V4 池在 PoolKey 和 StateView 均读不到时必须跳过 (fail-closed), 绝不兜底."""
        caplog.set_level(logging.WARNING)
        mock_rpc = MagicMock()
        mock_rpc.call.side_effect = RuntimeError("StateView call reverted")

        unknown_v4_id = "0x" + "bb" * 32
        v4_pool = {
            "dex": "uniswap-v4",
            "name": "UNREADABLE / USDG 0.3%",
            "addr": unknown_v4_id,
            "tvl": 100_000.0,
        }
        scanned = scan_pools(min_tvl=50_000.0, raw_pools=[v4_pool], rpc=mock_rpc)
        assert len(scanned) == 0
        assert "[FEE_UNVERIFIED]" in caplog.text
