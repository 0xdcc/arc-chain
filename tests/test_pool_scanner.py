"""全 DEX 池矩阵与代币对扫描器单元测试 (无网络依赖)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from research.market_data.fee_scan import (
    ZERO_ADDRESS,
    ScannedPool,
    get_overlapping_pools,
    group_by_pair,
    load_pools_metadata_cache,
    parse_pool_name,
    save_pools_metadata_cache,
    scan_pools,
    scanned_to_pool_spec,
)
from research.market_data.multicall import PoolSpec
from research.market_data.v4_reader import V4PoolSpec


class TestPoolNameParser:
    """代币对名称与费率解析."""

    def test_parse_v3_low_fee(self) -> None:
        """标准低费率池解析."""
        t0, t1, fee_bps = parse_pool_name("WETH / USDG 0.01%")
        assert t0 == "WETH"
        assert t1 == "USDG"
        assert fee_bps == pytest.approx(1.0)

    def test_parse_v4_high_fee(self) -> None:
        """高费率土狗/meme池解析."""
        t0, t1, fee_bps = parse_pool_name("MEME / USDG 0.7%", dex="uniswap-v4")
        assert t0 == "MEME"
        assert t1 == "USDG"
        assert fee_bps == pytest.approx(70.0)

    def test_parse_v2_default_fee(self) -> None:
        """V2 池未标百分比时默认 30 bps (0.3%)."""
        t0, t1, fee_bps = parse_pool_name("Nasduck / WETH", dex="uniswap-v2")
        assert t0 == "NASDUCK"
        assert t1 == "WETH"
        assert fee_bps == pytest.approx(30.0)

    def test_parse_fractional_fee(self) -> None:
        """非典型费率档位 (0.009%, 0.125%)."""
        t0, t1, fee_bps = parse_pool_name("WETH / USDG 0.009%")
        assert fee_bps == pytest.approx(0.9)

        t0, t1, fee_bps = parse_pool_name("CASHCAT / PONS 0.125%")
        assert fee_bps == pytest.approx(12.5)

    def test_malformed_name_returns_empty(self) -> None:
        """非标准格式安全返回空字符与默认费率."""
        t0, t1, fee_bps = parse_pool_name("INVALID_NAME_WITHOUT_SLASH")
        assert t0 == ""
        assert t1 == ""
        assert fee_bps == 30.0


class TestScannedPoolProperties:
    """ScannedPool 属性校验."""

    def test_is_v4_detection(self) -> None:
        """根据 66 字符 PoolId 正确识别 V4 池."""
        v4_p = ScannedPool(
            dex="uniswap-v4",
            name="MEME / USDG 0.7%",
            addr="0x" + "aa" * 32,
            tvl_usd=100000.0,
            fee_bps=70.0,
            token0_symbol="MEME",
            token1_symbol="USDG",
        )
        assert v4_p.is_v4 is True

        v3_p = ScannedPool(
            dex="uniswap-v3",
            name="WETH / USDG 0.01%",
            addr="0x" + "bb" * 20,
            tvl_usd=1000000.0,
            fee_bps=1.0,
            token0_symbol="WETH",
            token1_symbol="USDG",
        )
        assert v3_p.is_v4 is False

    def test_canonical_pair_ordering(self) -> None:
        """无论传入顺序如何，标准化币对始终按字母排序."""
        p1 = ScannedPool(
            dex="uniswap-v3",
            name="WETH / USDG",
            addr="0x01",
            tvl_usd=1.0,
            fee_bps=1.0,
            token0_symbol="WETH",
            token1_symbol="USDG",
        )
        assert p1.canonical_pair == ("USDG", "WETH")

        p2 = ScannedPool(
            dex="uniswap-v3",
            name="USDG / WETH",
            addr="0x02",
            tvl_usd=1.0,
            fee_bps=1.0,
            token0_symbol="USDG",
            token1_symbol="WETH",
        )
        assert p2.canonical_pair == ("USDG", "WETH")


class TestGroupingAndOverlapping:
    """重叠池归并逻辑."""

    def test_finds_overlapping_pairs(self) -> None:
        """提取具有 >= 2 个池的币对."""
        p1 = ScannedPool(
            dex="uniswap-v3",
            name="USDG/WETH 0.01%",
            addr="0x01",
            tvl_usd=100000.0,
            fee_bps=1.0,
            token0_symbol="USDG",
            token1_symbol="WETH",
        )
        p2 = ScannedPool(
            dex="uniswap-v4",
            name="USDG/WETH 0.05%",
            addr="0x02",
            tvl_usd=200000.0,
            fee_bps=5.0,
            token0_symbol="USDG",
            token1_symbol="WETH",
        )
        solo = ScannedPool(
            dex="uniswap-v3",
            name="SOLO/USDG",
            addr="0x03",
            tvl_usd=50000.0,
            fee_bps=30.0,
            token0_symbol="SOLO",
            token1_symbol="USDG",
        )

        overlapping = get_overlapping_pools([p1, p2, solo], min_pools=2)
        assert ("USDG", "WETH") in overlapping
        assert len(overlapping[("USDG", "WETH")]) == 2
        assert ("SOLO", "USDG") not in overlapping


class TestPoolSpecAdaptation:
    """ScannedPool 到 PoolSpec / V4PoolSpec 转换适配."""

    def test_adapts_20byte_to_pool_spec(self) -> None:
        """20 字节池转换为 PoolSpec."""
        p = ScannedPool(
            dex="uniswap-v3",
            name="USDG/WETH 0.01%",
            addr="0x" + "01".rjust(40, "0"),
            tvl_usd=100000.0,
            fee_bps=1.0,
            token0_symbol="USDG",
            token1_symbol="WETH",
        )
        spec = scanned_to_pool_spec(p)
        assert isinstance(spec, PoolSpec)
        assert spec.address == p.addr

    def test_adapts_32byte_to_v4_pool_spec(self) -> None:
        """32 字节池转换为 V4PoolSpec."""
        p = ScannedPool(
            dex="uniswap-v4",
            name="MEME/USDG 0.7%",
            addr="0x" + "02".rjust(64, "0"),
            tvl_usd=200000.0,
            fee_bps=70.0,
            token0_symbol="MEME",
            token1_symbol="USDG",
            tick_spacing=60,
            hooks=ZERO_ADDRESS,
        )
        spec = scanned_to_pool_spec(p)
        assert isinstance(spec, V4PoolSpec)
        assert spec.address == p.addr


class TestZeroAndInvalidAddressFiltering:
    """零地址与非法地址扫描防御测试."""

    def test_scan_pools_filters_zero_address_tokens(self, caplog: pytest.LogCaptureFixture) -> None:
        """零地址代币池在扫描阶段被剔除并记 INVALID_TOKEN_ADDR 告警."""
        from unittest.mock import MagicMock

        caplog.set_level("WARNING")
        zero_token_pool = {
            "dex": "uniswap-v3",
            "name": "ZERO / WETH 0.3%",
            "addr": "0x" + "11" * 20,
            "tvl": 100_000.0,
            "token0_address": "0x0000000000000000000000000000000000000000",
            "token1_address": "0x0bd7d308f8e1639fab988df18a8011f41eacad73",
        }
        valid_pool = {
            "dex": "uniswap-v3",
            "name": "WETH / USDG 0.01%",
            "addr": "0x" + "22" * 20,
            "tvl": 100_000.0,
            "token0_address": "0x0bd7d308f8e1639fab988df18a8011f41eacad73",
            "token1_address": "0x5fc5360d0400a0fd4f2af552add042d716f1d168",
        }
        mock_rpc = MagicMock()
        mock_rpc.call.return_value = {"result": "0x" + hex(3000)[2:].zfill(64)}
        scanned = scan_pools(
            min_tvl=50_000.0, raw_pools=[zero_token_pool, valid_pool], rpc=mock_rpc
        )
        addrs = [p.addr for p in scanned]
        assert str(zero_token_pool["addr"]).lower() not in addrs
        assert str(valid_pool["addr"]).lower() in addrs
        assert "[INVALID_TOKEN_ADDR]" in caplog.text

    def test_scan_pools_filters_invalid_length_tokens(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """非 42 字符代币地址池被剔除."""
        from unittest.mock import MagicMock

        caplog.set_level("WARNING")
        short_addr_pool = {
            "dex": "uniswap-v3",
            "name": "SHORT / WETH 0.3%",
            "addr": "0x" + "33" * 20,
            "tvl": 100_000.0,
            "token0_address": "0x1234",
            "token1_address": "0x0bd7d308f8e1639fab988df18a8011f41eacad73",
        }
        mock_rpc = MagicMock()
        mock_rpc.call.return_value = {"result": "0x" + hex(3000)[2:].zfill(64)}
        scanned = scan_pools(min_tvl=50_000.0, raw_pools=[short_addr_pool], rpc=mock_rpc)
        assert len(scanned) == 0
        assert "[INVALID_TOKEN_ADDR]" in caplog.text

    def test_scan_pools_filters_invalid_pool_address(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """非标长度池地址被直接剔除."""
        caplog.set_level("WARNING")
        bad_v3 = {
            "dex": "uniswap-v3",
            "name": "WETH / USDG 0.01%",
            "addr": "0x123",
            "tvl": 100_000.0,
        }
        bad_v4 = {
            "dex": "uniswap-v4",
            "name": "PONS / USDG 0.3%",
            "addr": "0x" + "aa" * 20,
            "tvl": 100_000.0,
        }
        scanned = scan_pools(min_tvl=50_000.0, raw_pools=[bad_v3, bad_v4])
        assert len(scanned) == 0
        assert "[INVALID_TOKEN_ADDR]" in caplog.text

    def test_scanned_to_pool_spec_rejects_zero_address(self) -> None:
        """scanned_to_pool_spec 对零地址 token 抛出 ValueError 并记告警."""
        bad_pool = ScannedPool(
            dex="uniswap-v3",
            name="ZERO / WETH 0.3%",
            addr="0x" + "44" * 20,
            tvl_usd=100000.0,
            fee_bps=30.0,
            token0_symbol="ZERO",
            token1_symbol="WETH",
            token0_address="0x0000000000000000000000000000000000000000",
            token1_address="0x0bd7d308f8e1639fab988df18a8011f41eacad73",
        )
        with pytest.raises(ValueError, match="invalid token addresses"):
            scanned_to_pool_spec(bad_pool)

    def test_load_pools_metadata_cache_filters_zero_address(
        self, tmp_path: pytest.TempPathFactory
    ) -> None:
        """元数据缓存加载时剔除 token0 或 token1 为零地址的池."""
        cache_dir = Path(str(tmp_path))
        cache_file = cache_dir / "test_cache.json"

        data = {
            "updated_at": time.time(),
            "pools": {
                "0x" + "11" * 20: {
                    "address": "0x" + "11" * 20,
                    "label": "ZERO/WETH 0.3%",
                    "token0": "0x0000000000000000000000000000000000000000",
                    "token1": "0x0bd7d308f8e1639fab988df18a8011f41eacad73",
                    "dex": "uniswap-v3",
                },
                "0x" + "22" * 20: {
                    "address": "0x" + "22" * 20,
                    "label": "USDG/WETH 0.01%",
                    "token0": "0x5fc5360d0400a0fd4f2af552add042d716f1d168",
                    "token1": "0x0bd7d308f8e1639fab988df18a8011f41eacad73",
                    "dex": "uniswap-v3",
                },
            },
        }
        with cache_file.open("w", encoding="utf-8") as f:
            json.dump(data, f)

        loaded = load_pools_metadata_cache(cache_file)
        assert loaded is not None
        assert ("0x" + "11" * 20).lower() not in loaded
        assert ("0x" + "22" * 20).lower() in loaded


class TestV4PoolKeyContractFailClosed:
    """Uniswap V4 PoolKey 契约刚性防线: 缺显式 hooks / tick_spacing / fee 拒绝转换."""

    def test_scanned_to_pool_spec_rejects_missing_hooks(self) -> None:
        """缺失显式 hooks 时拒绝转换为 V4PoolSpec，未知 Hook 绝不默认零地址."""
        p = ScannedPool(
            dex="uniswap-v4",
            name="MEME/USDG 0.7%",
            addr="0x" + "aa" * 32,
            tvl_usd=200000.0,
            fee_bps=70.0,
            token0_symbol="MEME",
            token1_symbol="USDG",
            tick_spacing=60,
            hooks=None,
        )
        with pytest.raises(ValueError, match="cannot determine V4 pool key: missing explicit hooks"):
            scanned_to_pool_spec(p)

    def test_scanned_to_pool_spec_rejects_missing_tick_spacing(self) -> None:
        """缺失显式 tick_spacing 时拒绝转换为 V4PoolSpec，绝不隐式默认 60."""
        p = ScannedPool(
            dex="uniswap-v4",
            name="MEME/USDG 0.7%",
            addr="0x" + "aa" * 32,
            tvl_usd=200000.0,
            fee_bps=70.0,
            token0_symbol="MEME",
            token1_symbol="USDG",
            tick_spacing=None,
            hooks=ZERO_ADDRESS,
        )
        with pytest.raises(ValueError, match="cannot determine V4 pool key: missing explicit tick_spacing"):
            scanned_to_pool_spec(p)

    def test_scanned_to_pool_spec_rejects_missing_fee(self) -> None:
        """缺失有效费率 (NaN) 时拒绝转换为 V4PoolSpec，绝不伪造默认费率 0."""
        p = ScannedPool(
            dex="uniswap-v4",
            name="MEME/USDG 0.7%",
            addr="0x" + "aa" * 32,
            tvl_usd=200000.0,
            fee_bps=float("nan"),
            token0_symbol="MEME",
            token1_symbol="USDG",
            tick_spacing=60,
            hooks=ZERO_ADDRESS,
        )
        with pytest.raises(ValueError, match="cannot determine V4 pool key: missing explicit fee"):
            scanned_to_pool_spec(p)

    def test_scanned_to_pool_spec_rejects_invalid_hooks_address(self) -> None:
        """非零且非合规 42 字符的 hooks 地址拒绝转换."""
        p = ScannedPool(
            dex="uniswap-v4",
            name="MEME/USDG 0.7%",
            addr="0x" + "aa" * 32,
            tvl_usd=200000.0,
            fee_bps=70.0,
            token0_symbol="MEME",
            token1_symbol="USDG",
            tick_spacing=60,
            hooks="0xinvalid",
        )
        with pytest.raises(ValueError, match="invalid hooks address"):
            scanned_to_pool_spec(p)

    def test_group_by_pair_rejects_none(self) -> None:
        """group_by_pair 传入 None 时 fail-closed 明确拒绝."""
        from typing import Any

        none_pools: Any = None
        with pytest.raises(ValueError, match="pools cannot be None"):
            group_by_pair(none_pools)

    def test_get_overlapping_pools_rejects_none(self) -> None:
        """get_overlapping_pools 传入 None 时 fail-closed 明确拒绝."""
        from typing import Any

        none_pools: Any = None
        with pytest.raises(ValueError, match="pools cannot be None"):
            get_overlapping_pools(none_pools)

    def test_group_by_pair_and_cache_save(self, tmp_path: pytest.TempPathFactory) -> None:
        """验证 group_by_pair 与 save_pools_metadata_cache 契约接口可用性."""
        p1 = ScannedPool(
            dex="uniswap-v3",
            name="USDG/WETH 0.01%",
            addr="0x" + "01".rjust(40, "0"),
            tvl_usd=100000.0,
            fee_bps=1.0,
            token0_symbol="USDG",
            token1_symbol="WETH",
        )
        grouped = group_by_pair([p1])
        assert ("USDG", "WETH") in grouped
        assert len(grouped[("USDG", "WETH")]) == 1

        cache_file = Path(str(tmp_path)) / "saved_cache.json"
        save_pools_metadata_cache(cache_file, {"0x" + "01".rjust(40, "0"): {"dex": "uniswap-v3"}})
        assert cache_file.is_file()


class TestMultiPoolIsolation:
    """混合架构多池扫描与元数据跨池隔离回归测试.

    安全与门禁准则:
    - 显式使用离线 mock / synthetic 夹具，不假造或混同链上 live 数据。
    - 验证 V4->V3 与 V3->V4 混合顺序下，元数据不发生跨池继承与泄漏。
    - 验证 V3 池绝不继承前序 V4 池的 tick_spacing 与 hooks。
    """

    def test_mixed_order_v4_then_v3_isolation_no_metadata_leak(self) -> None:
        """V4 -> V3 顺序下，V3 池必须重置元数据，严禁继承 V4 池的 tick_spacing 与 hooks."""
        from unittest.mock import MagicMock

        # 显式离线合成数据夹具 (Synthetic offline fixtures, not live data)
        v4_addr = "0x" + "44" * 32
        v3_addr = "0x" + "33" * 20
        tok0 = "0x" + "aa" * 20
        tok1 = "0x" + "bb" * 20

        raw_v4 = {
            "dex": "uniswap-v4",
            "name": "MOCKV4 / USDG 0.05%",
            "addr": v4_addr,
            "tvl": 100_000.0,
            "token0_address": tok0,
            "token1_address": tok1,
            "chain_id": 5042,
        }
        raw_v3 = {
            "dex": "uniswap-v3",
            "name": "MOCKV3 / USDG 0.3%",
            "addr": v3_addr,
            "tvl": 100_000.0,
            "token0_address": tok0,
            "token1_address": tok1,
            "chain_id": 5042,
        }
        mock_v4_metadata = {
            v4_addr.lower(): {
                "pool_id": v4_addr,
                "chain_id": 5042,
                "currency0": tok0,
                "currency1": tok1,
                "fee": 500,
                "tick_spacing": 10,
                "hooks": "0x" + "11" * 20,
            }
        }
        mock_rpc = MagicMock()
        mock_rpc.call.return_value = {"result": "0x" + hex(3000)[2:].zfill(64)}

        scanned = scan_pools(
            min_tvl=50_000.0,
            raw_pools=[raw_v4, raw_v3],
            rpc=mock_rpc,
            v4_metadata=mock_v4_metadata,
        )

        assert len(scanned) == 2
        pool_v4 = scanned[0]
        pool_v3 = scanned[1]

        # V4 池正常提取其显式配置
        assert pool_v4.addr == v4_addr.lower()
        assert pool_v4.tick_spacing == 10
        assert pool_v4.hooks == "0x" + "11" * 20

        # V3 池严格隔离：绝不能继承前一池的 tick_spacing 或 hooks
        assert pool_v3.addr == v3_addr.lower()
        assert pool_v3.tick_spacing is None
        assert pool_v3.hooks is None

    def test_mixed_order_v3_then_v4_isolation_no_unbound_error(self) -> None:
        """V3 -> V4 顺序下，首池 V3 正常处理无 UnboundLocalError，后池 V4 正确获取自身元数据."""
        from unittest.mock import MagicMock

        # 显式离线合成数据夹具 (Synthetic offline fixtures, not live data)
        v4_addr = "0x" + "44" * 32
        v3_addr = "0x" + "33" * 20
        tok0 = "0x" + "aa" * 20
        tok1 = "0x" + "bb" * 20

        raw_v3 = {
            "dex": "uniswap-v3",
            "name": "MOCKV3 / USDG 0.3%",
            "addr": v3_addr,
            "tvl": 100_000.0,
            "token0_address": tok0,
            "token1_address": tok1,
            "chain_id": 5042,
        }
        raw_v4 = {
            "dex": "uniswap-v4",
            "name": "MOCKV4 / USDG 0.05%",
            "addr": v4_addr,
            "tvl": 100_000.0,
            "token0_address": tok0,
            "token1_address": tok1,
            "chain_id": 5042,
        }
        mock_v4_metadata = {
            v4_addr.lower(): {
                "pool_id": v4_addr,
                "chain_id": 5042,
                "currency0": tok0,
                "currency1": tok1,
                "fee": 500,
                "tick_spacing": 10,
                "hooks": "0x" + "11" * 20,
            }
        }
        mock_rpc = MagicMock()
        mock_rpc.call.return_value = {"result": "0x" + hex(3000)[2:].zfill(64)}

        scanned = scan_pools(
            min_tvl=50_000.0,
            raw_pools=[raw_v3, raw_v4],
            rpc=mock_rpc,
            v4_metadata=mock_v4_metadata,
        )

        assert len(scanned) == 2
        pool_v3 = scanned[0]
        pool_v4 = scanned[1]

        # V3 首池隔离验证
        assert pool_v3.addr == v3_addr.lower()
        assert pool_v3.tick_spacing is None
        assert pool_v3.hooks is None

        # V4 次池正常解析
        assert pool_v4.addr == v4_addr.lower()
        assert pool_v4.tick_spacing == 10
        assert pool_v4.hooks == "0x" + "11" * 20

    def test_mixed_order_v4_metadata_dict_cross_pool_isolation(self) -> None:
        """通过 v4_metadata 字典注入时，V4 元数据绝不向后续 V3/V2 池逃逸."""
        from unittest.mock import MagicMock

        # 显式离线合成数据夹具 (Synthetic offline fixtures, not live data)
        v4_addr = "0x" + "44" * 32
        v3_addr = "0x" + "33" * 20
        v2_addr = "0x" + "22" * 20
        tok0 = "0x" + "aa" * 20
        tok1 = "0x" + "bb" * 20

        raw_v4 = {
            "dex": "uniswap-v4",
            "name": "MOCKV4 / USDG 0.05%",
            "addr": v4_addr,
            "tvl": 100_000.0,
            "token0_address": tok0,
            "token1_address": tok1,
            "chain_id": 5042,
        }
        raw_v3 = {
            "dex": "uniswap-v3",
            "name": "MOCKV3 / USDG 0.3%",
            "addr": v3_addr,
            "tvl": 100_000.0,
            "token0_address": tok0,
            "token1_address": tok1,
            "chain_id": 5042,
        }
        raw_v2 = {
            "dex": "uniswap-v2",
            "name": "MOCKV2 / USDG",
            "addr": v2_addr,
            "tvl": 100_000.0,
            "token0_address": tok0,
            "token1_address": tok1,
        }

        mock_v4_metadata = {
            v4_addr.lower(): {
                "pool_id": v4_addr,
                "chain_id": 5042,
                "currency0": tok0,
                "currency1": tok1,
                "fee": 500,
                "tick_spacing": 20,
                "hooks": ZERO_ADDRESS,
            }
        }
        mock_rpc = MagicMock()
        mock_rpc.call.return_value = {"result": "0x" + hex(3000)[2:].zfill(64)}

        # V4 -> V3 -> V2
        scanned = scan_pools(
            min_tvl=50_000.0,
            raw_pools=[raw_v4, raw_v3, raw_v2],
            rpc=mock_rpc,
            v4_metadata=mock_v4_metadata,
        )

        assert len(scanned) == 3
        assert scanned[0].addr == v4_addr.lower()
        assert scanned[0].tick_spacing == 20
        assert scanned[0].hooks == ZERO_ADDRESS

        assert scanned[1].addr == v3_addr.lower()
        assert scanned[1].tick_spacing is None
        assert scanned[1].hooks is None

        assert scanned[2].addr == v2_addr.lower()
        assert scanned[2].tick_spacing is None
        assert scanned[2].hooks is None
