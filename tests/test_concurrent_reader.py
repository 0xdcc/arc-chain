"""多线程并发读池与 Block Skew 跨块高度熔断单元测试 (无真实网络依赖)."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from research.graph import (
    DirectedEdge,
    TokenGraph,
    calculate_triangular_path,
    find_triangular_opportunities,
)
from research.market_data.pool_reader import (
    AnyPool,
    PoolReader,
    PoolSpec,
    PriceQuote,
    V4PoolSpec,
    probe_proxy,
)
from research.market_data.read_round import ArbitrageRoundCoordinator as ArbitrageDaemon


def make_v3_pool(suffix: str, label: str = "test-v3", fee_bps: float = 30.0) -> PoolSpec:
    """构造测试用 20 字节 V3 池规格."""
    addr = "0x" + suffix.rjust(40, "0")
    return PoolSpec(
        address=addr,
        label=label,
        fee_bps=fee_bps,
        token0="0x" + "a" * 40,
        token1="0x" + "b" * 40,
        dec0=18,
        dec1=18,
    )


def make_v4_pool(suffix: str, label: str = "test-v4", fee_bps: float = 30.0) -> V4PoolSpec:
    """构造测试用 32 字节 V4 池规格."""
    addr = "0x" + suffix.rjust(64, "0")
    return V4PoolSpec(
        address=addr,
        label=label,
        fee_bps=fee_bps,
        token0="0x" + "a" * 40,
        token1="0x" + "b" * 40,
        dec0=18,
        dec1=18,
    )


class TestConcurrentPoolReader:
    """PoolReader.batch_quote 多线程并发读池与容错测试."""

    def test_batch_quote_concurrency_and_speed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """测试多线程并发读取加速效果 (5 个池并发耗时远小于串行)."""
        pools: list[AnyPool] = [make_v3_pool(f"{i:02d}", label=f"pool-{i}") for i in range(5)]

        reader = PoolReader.__new__(PoolReader)
        reader.proxy_url = None
        reader.batch_strategy = "thread"
        mock_rpc = MagicMock()
        mock_rpc.block_number.return_value = 12345
        mock_rpc.throttle = 0.0
        reader._rpc = mock_rpc

        def slow_quote(p: AnyPool, block_number: int | None = None) -> PriceQuote:
            time.sleep(0.08)
            p_spec = p if isinstance(p, PoolSpec) else p
            return PriceQuote(
                pool=p,
                base=p_spec.token0,
                quote=p_spec.token1,
                price=100.0,
                raw_price_t1_per_t0=100.0,
                block_number=block_number or 0,
            )

        monkeypatch.setattr(reader, "quote", slow_quote)

        start_t = time.time()
        quotes = reader.batch_quote(pools, max_workers=5)
        elapsed = time.time() - start_t

        assert len(quotes) == 5
        # 串行需要 5 * 0.08 = 0.4s，并发应在 0.25s 内完成
        assert elapsed < 0.25
        for q in quotes:
            assert q.block_number == 12345

    def test_batch_quote_error_isolation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """测试单池读取失败时容错隔离，不阻断其他池的收集."""
        pools: list[AnyPool] = [make_v3_pool(f"{i:02d}", label=f"pool-{i}") for i in range(4)]

        reader = PoolReader.__new__(PoolReader)
        reader.proxy_url = None
        reader.batch_strategy = "thread"
        mock_rpc = MagicMock()
        mock_rpc.block_number.return_value = 20000
        mock_rpc.throttle = 0.0
        reader._rpc = mock_rpc

        def flappy_quote(p: AnyPool, block_number: int | None = None) -> PriceQuote:
            if p.label in ("pool-1", "pool-3"):
                raise RuntimeError(f"RPC simulation failure for {p.label}")
            p_spec = p if isinstance(p, PoolSpec) else p
            return PriceQuote(
                pool=p,
                base=p_spec.token0,
                quote=p_spec.token1,
                price=1.0,
                raw_price_t1_per_t0=1.0,
                block_number=block_number or 0,
            )

        monkeypatch.setattr(reader, "quote", flappy_quote)

        quotes = reader.batch_quote(pools, max_workers=4)
        # pool-1 与 pool-3 失败跳过，应保留 pool-0 与 pool-2
        assert len(quotes) == 2
        labels = [q.pool.label for q in quotes]
        assert labels == ["pool-0", "pool-2"]
        assert all(q.block_number == 20000 for q in quotes)

    def test_batch_quote_empty_list(self) -> None:
        """测试空池列表输入返回空列表."""
        reader = PoolReader.__new__(PoolReader)
        reader.proxy_url = None
        reader.batch_strategy = "thread"
        mock_rpc = MagicMock()
        reader._rpc = mock_rpc
        assert reader.batch_quote([]) == []

    def test_batch_quote_restores_throttle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """测试并发读池执行完后恢复原本的节流参数."""
        reader = PoolReader.__new__(PoolReader)
        reader.proxy_url = None
        reader.batch_strategy = "thread"
        mock_rpc = MagicMock()
        mock_rpc.throttle = 0.6
        mock_rpc.block_number.return_value = 100
        reader._rpc = mock_rpc

        p = make_v3_pool("01")
        monkeypatch.setattr(
            reader,
            "quote",
            MagicMock(
                return_value=PriceQuote(
                    pool=p,
                    base=p.token0,
                    quote=p.token1,
                    price=1.0,
                    raw_price_t1_per_t0=1.0,
                    block_number=100,
                )
            ),
        )

        quotes = reader.batch_quote([p], max_workers=2)
        assert len(quotes) == 1
        assert reader._rpc.throttle == 0.6

    def test_proxy_detection_explicit(self) -> None:
        """测试显式传入 proxy_url 正确保留与配置."""
        custom_proxy = "http://127.0.0.1:9999"
        res = probe_proxy(proxy_url=custom_proxy)
        assert res == custom_proxy

        mock_rpc = MagicMock()
        mock_rpc.proxy_url = None
        mock_rpc._session = MagicMock()
        mock_rpc._session.proxies = {}

        reader = PoolReader(rpc=mock_rpc, proxy_url=custom_proxy)
        assert reader.proxy_url == custom_proxy
        assert mock_rpc._session.proxies["http"] == custom_proxy

    def test_proxy_detection_fallback_when_none_reachable(self) -> None:
        """测试无可用代理时优雅降级为直连 (None)."""
        with (
            patch("os.environ.get", return_value=""),
            patch(
                "research.market_data.pool_reader._is_proxy_reachable",
                return_value=False,
            ),
        ):
            res = probe_proxy(proxy_url=None)
            assert res is None


class TestBlockSkewCircuitBreaker:
    """Block Skew 跨块高度熔断契约校验 (硬风控: skew > 1 立即熔断丢弃)."""

    def _make_directed_edge(
        self,
        from_token: str,
        to_token: str,
        rate: float,
        block_number: int,
        fee_bps: float = 10.0,
    ) -> DirectedEdge:
        pool = make_v3_pool("01", label=f"{from_token}/{to_token}", fee_bps=fee_bps)
        eff = rate * (1.0 - fee_bps / 10000.0)
        return DirectedEdge(
            from_token=from_token,
            to_token=to_token,
            pool=pool,
            rate=rate,
            fee_bps=fee_bps,
            effective_rate=eff,
            weight=0.0,
            from_symbol=from_token,
            to_symbol=to_token,
            block_number=block_number,
        )

    def test_block_skew_greater_than_one_fused(self) -> None:
        """跨块高度差 > 1 时 calculate_triangular_path 必须熔断返回 None."""
        # 产生收益的环路汇率配置: 0.0004 * 2550 * 1.0 = 1.02 (盈利 2%)
        e1 = self._make_directed_edge("USDG", "WETH", 0.0004, block_number=100)
        e2 = self._make_directed_edge("WETH", "PONS", 2550.0, block_number=102)
        e3 = self._make_directed_edge("PONS", "USDG", 1.0, block_number=100)

        # skew = 102 - 100 = 2 > 1 -> 熔断
        alert = calculate_triangular_path(e1, e2, e3)
        assert alert is None

    def test_block_skew_large_gap_fused(self) -> None:
        """测试更大区块高度跨度 (如相差 10 块) 严格熔断."""
        e1 = self._make_directed_edge("USDG", "WETH", 0.0004, block_number=100)
        e2 = self._make_directed_edge("WETH", "PONS", 2550.0, block_number=110)
        e3 = self._make_directed_edge("PONS", "USDG", 1.0, block_number=105)

        alert = calculate_triangular_path(e1, e2, e3)
        assert alert is None

    def test_block_skew_equal_to_one_allowed(self) -> None:
        """跨块高度差 == 1 时正常放行并正确记录 block_skew 与 block_number."""
        e1 = self._make_directed_edge("USDG", "WETH", 0.000403, block_number=500)
        e2 = self._make_directed_edge("WETH", "PONS", 2550.0, block_number=501)
        e3 = self._make_directed_edge("PONS", "USDG", 1.0, block_number=500)

        alert = calculate_triangular_path(e1, e2, e3, slippage_buffer_pct=0.2)
        assert alert is not None
        assert alert.block_skew == 1
        assert alert.block_number == 501
        assert "Block #501 (skew=1)" in str(alert)

    def test_block_skew_zero_allowed(self) -> None:
        """同一区块高度 (skew == 0) 正常放行."""
        e1 = self._make_directed_edge("USDG", "WETH", 0.000403, block_number=600)
        e2 = self._make_directed_edge("WETH", "PONS", 2550.0, block_number=600)
        e3 = self._make_directed_edge("PONS", "USDG", 1.0, block_number=600)

        alert = calculate_triangular_path(e1, e2, e3, slippage_buffer_pct=0.2)
        assert alert is not None
        assert alert.block_skew == 0
        assert alert.block_number == 600
        assert "Block #600 (skew=0)" in str(alert)

    def test_token_graph_propagates_block_number(self) -> None:
        """测试 TokenGraph.add_quote 时 quote.block_number 正确透传给 DirectedEdge."""
        pool = make_v3_pool("01", label="USDG/WETH")
        quote = PriceQuote(
            pool=pool,
            base="0x" + "a" * 40,
            quote="0x" + "b" * 40,
            price=2500.0,
            raw_price_t1_per_t0=2500.0,
            base_symbol="USDG",
            quote_symbol="WETH",
            block_number=99999,
        )

        graph = TokenGraph()
        graph.add_quote(quote)

        edge_fwd = graph.get_best_edge(quote.base.lower(), quote.quote.lower())
        assert edge_fwd is not None
        assert edge_fwd.block_number == 99999

        edge_rev = graph.get_best_edge(quote.quote.lower(), quote.base.lower())
        assert edge_rev is not None
        assert edge_rev.block_number == 99999

    def test_find_triangular_opportunities_filters_skewed_cycles(self) -> None:
        """测试 find_triangular_opportunities 会自动过滤掉 skew > 1 的机会."""
        p1 = make_v3_pool("01", label="USDG/WETH", fee_bps=1.0)
        p2 = make_v3_pool("02", label="WETH/PONS", fee_bps=1.0)
        p3 = make_v3_pool("03", label="PONS/USDG", fee_bps=1.0)

        t_usdg = "0x" + "1" * 40
        t_weth = "0x" + "2" * 40
        t_pons = "0x" + "3" * 40

        # 存在明显盈利闭环: USDG -> WETH -> PONS -> USDG
        # 1 USDG = 0.000405 WETH; 1 WETH = 2550 PONS; 1 PONS = 1 USDG
        q1 = PriceQuote(
            pool=p1,
            base=t_usdg,
            quote=t_weth,
            price=0.000405,
            raw_price_t1_per_t0=0.000405,
            base_symbol="USDG",
            quote_symbol="WETH",
            block_number=100,
        )
        q2 = PriceQuote(
            pool=p2,
            base=t_weth,
            quote=t_pons,
            price=2550.0,
            raw_price_t1_per_t0=2550.0,
            base_symbol="WETH",
            quote_symbol="PONS",
            block_number=103,  # 跨块高度差 103 - 100 = 3 > 1
        )
        q3 = PriceQuote(
            pool=p3,
            base=t_pons,
            quote=t_usdg,
            price=1.0,
            raw_price_t1_per_t0=1.0,
            base_symbol="PONS",
            quote_symbol="USDG",
            block_number=100,
        )

        alerts_skewed = find_triangular_opportunities(
            [q1, q2, q3], base_token="USDG", slippage_buffer_pct=0.2
        )
        assert len(alerts_skewed) == 0

        # 调整为同一或相邻区块高度 (skew == 1)
        q2_synced = PriceQuote(
            pool=p2,
            base=t_weth,
            quote=t_pons,
            price=2550.0,
            raw_price_t1_per_t0=2550.0,
            base_symbol="WETH",
            quote_symbol="PONS",
            block_number=101,  # 101 - 100 = 1 <= 1
        )
        alerts_synced = find_triangular_opportunities(
            [q1, q2_synced, q3], base_token="USDG", slippage_buffer_pct=0.2
        )
        assert len(alerts_synced) == 1
        assert alerts_synced[0].block_skew == 1
        assert alerts_synced[0].block_number == 101


class TestDaemonScanRoundAlignment:
    """守护进程 scan_round 对齐 batch_quote 测试."""

    def test_scan_round_invokes_batch_quote(self) -> None:
        """验证 ArbitrageDaemon.scan_round 调用 batch_quote 并传入 max_workers=15."""
        daemon = ArbitrageDaemon(mode="spread", min_tvl=100000.0)
        p1 = make_v3_pool("01")
        p2 = make_v3_pool("02")
        daemon.pools = [p1, p2]
        daemon.running = True

        q1 = PriceQuote(
            pool=p1,
            base=p1.token0,
            quote=p1.token1,
            price=100.0,
            raw_price_t1_per_t0=100.0,
            block_number=5000,
        )
        q2 = PriceQuote(
            pool=p2,
            base=p2.token0,
            quote=p2.token1,
            price=105.0,
            raw_price_t1_per_t0=105.0,
            block_number=5000,
        )

        mock_reader = MagicMock()
        mock_reader.batch_quote.return_value = [q1, q2]
        daemon.reader = mock_reader

        with (
            patch.object(daemon, "handle_spread_alert"),
            patch.object(daemon, "update_status"),
        ):
            daemon.scan_round()

        mock_reader.batch_quote.assert_called_once_with(daemon.pools, max_workers=15)
