"""多 RPC 客户端节点故障漂移 (Failover) 与向后兼容测试 (无真实网络依赖).

测试覆盖:
1. 默认 6 节点矩阵初始化与向后兼容单 URL 构造;
2. HTTP 429 限流时瞬时 Failover 漂移到备用节点 (无阻塞 sleep 30s);
3. ConnectionError / Timeout 时自动漂移到备用节点;
4. HTTP 5xx 服务端错误时自动漂移到备用节点;
5. JSON-RPC -32005 节点限流响应触发漂移;
6. call_batch 批量调用时同样享有节点故障漂移;
7. 全部节点轮换与重试耗尽保护;
8. 成功请求后节点连续失败计数与冷却状态重置.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
import requests

from backtest.data.rpc_client import RobinhoodRpc


class FakeResponse:
    """Mock requests.Response 对象."""

    def __init__(
        self,
        json_data: Any = None,
        status_code: int = 200,
        text: str = "",
    ) -> None:
        self._json_data = json_data
        self.status_code = status_code
        self.text = text or ("" if json_data is None else str(json_data))

    def json(self) -> Any:
        if self._json_data is None:
            raise ValueError("No JSON in fake response")
        return self._json_data


class TestMultiRpcConfiguration:
    """测试多 RPC 配置与向后兼容."""

    def test_default_initialization(self) -> None:
        """验证无参构造默认包含 Robinhood 6 大备用节点矩阵."""
        rpc = RobinhoodRpc()
        assert len(rpc.urls) == 6
        assert rpc.urls == RobinhoodRpc.DEFAULT_URLS
        assert rpc.active_url == "https://rpc.mainnet.chain.robinhood.com"
        assert rpc.url == rpc.active_url
        assert len(rpc.health_status) == 6
        for u in rpc.urls:
            status = rpc.health_status[u]
            assert status["fail_count"] == 0
            assert status["consecutive_fails"] == 0

    def test_backward_compatible_single_url(self) -> None:
        """验证只传单 url 时的完全向后兼容 (仅包含该单节点)."""
        custom_url = "https://custom-robinhood-node.example.com"
        rpc = RobinhoodRpc(url=custom_url)
        assert rpc.urls == [custom_url]
        assert rpc.active_url == custom_url
        assert rpc.url == custom_url

    def test_url_setter_compatibility(self) -> None:
        """验证动态给 rpc.url 赋值时能够正确重置活跃节点与健康表."""
        rpc = RobinhoodRpc()
        new_url = "https://assigned-node.example.com"
        rpc.url = new_url
        assert rpc.urls == [new_url]
        assert rpc.active_url == new_url
        assert rpc.url == new_url
        assert list(rpc.health_status.keys()) == [new_url]

    def test_custom_urls_list(self) -> None:
        """验证显式传入 urls 节点列表."""
        custom_list = [
            "https://node-a.com",
            "https://node-b.com",
            "https://node-c.com",
        ]
        rpc = RobinhoodRpc(urls=custom_list)
        assert rpc.urls == custom_list
        assert rpc.active_url == "https://node-a.com"


class TestMultiRpcFailover:
    """测试 RPC 遇到限流与网络故障时的瞬时漂移."""

    def test_failover_on_http_429(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """测试遇到 HTTP 429 限流时瞬时漂移至备用节点 (不休眠 30s)."""
        nodes = ["https://node1.test", "https://node2.test"]
        rpc = RobinhoodRpc(urls=nodes, throttle=0.0)

        called_urls: list[str] = []

        def fake_post(url: str, **kwargs: Any) -> FakeResponse:
            called_urls.append(url)
            if url == "https://node1.test":
                # 节点 1 触发 429 限流
                return FakeResponse(status_code=429, text="Too Many Requests")
            # 节点 2 正常响应
            return FakeResponse(
                json_data={"jsonrpc": "2.0", "result": "0x36b0001", "id": 1},
                status_code=200,
            )

        monkeypatch.setattr(rpc._session, "post", fake_post)

        start_time = time.time()
        res = rpc.call("eth_blockNumber", [])
        elapsed = time.time() - start_time

        assert res.get("result") == "0x36b0001"
        # 验证瞬时漂移耗时极短 (< 0.2s)，绝不 sleep 30s
        assert elapsed < 0.2
        assert called_urls == ["https://node1.test", "https://node2.test"]
        assert rpc.active_url == "https://node2.test"

        # 检查节点 1 被惩罚降权
        node1_health = rpc.health_status["https://node1.test"]
        assert node1_health["fail_count"] == 1
        assert node1_health["consecutive_fails"] == 1
        assert "429" in (node1_health["last_error"] or "")

        # 检查节点 2 健康
        node2_health = rpc.health_status["https://node2.test"]
        assert node2_health["consecutive_fails"] == 0

    def test_failover_on_connection_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """测试连接超时或断开时自动漂移到下一个节点."""
        nodes = ["https://node1.test", "https://node2.test"]
        rpc = RobinhoodRpc(urls=nodes, throttle=0.0)

        def fake_post(url: str, **kwargs: Any) -> FakeResponse:
            if url == "https://node1.test":
                raise requests.exceptions.ConnectionError("Connection timed out")
            return FakeResponse(
                json_data={"jsonrpc": "2.0", "result": "0x123", "id": 1},
                status_code=200,
            )

        monkeypatch.setattr(rpc._session, "post", fake_post)

        res = rpc.call("eth_blockNumber", [])
        assert res.get("result") == "0x123"
        assert rpc.active_url == "https://node2.test"
        assert rpc.health_status["https://node1.test"]["fail_count"] == 1

    def test_failover_on_5xx_server_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """测试 HTTP 502/503 错误时自动漂移到下一个节点."""
        nodes = ["https://node1.test", "https://node2.test"]
        rpc = RobinhoodRpc(urls=nodes, throttle=0.0)

        def fake_post(url: str, **kwargs: Any) -> FakeResponse:
            if url == "https://node1.test":
                return FakeResponse(status_code=502, text="Bad Gateway")
            return FakeResponse(
                json_data={"jsonrpc": "2.0", "result": "0x789", "id": 1},
                status_code=200,
            )

        monkeypatch.setattr(rpc._session, "post", fake_post)

        res = rpc.call("eth_blockNumber", [])
        assert res.get("result") == "0x789"
        assert rpc.active_url == "https://node2.test"

    def test_failover_on_rpc_rate_limit_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """测试 JSON-RPC 返回 -32005 或 rate limit 错误时漂移到下一个节点."""
        nodes = ["https://node1.test", "https://node2.test"]
        rpc = RobinhoodRpc(urls=nodes, throttle=0.0)

        def fake_post(url: str, **kwargs: Any) -> FakeResponse:
            if url == "https://node1.test":
                return FakeResponse(
                    json_data={
                        "jsonrpc": "2.0",
                        "error": {"code": -32005, "message": "rate limit exceeded"},
                        "id": 1,
                    },
                    status_code=200,
                )
            return FakeResponse(
                json_data={"jsonrpc": "2.0", "result": "0xabc", "id": 1},
                status_code=200,
            )

        monkeypatch.setattr(rpc._session, "post", fake_post)

        res = rpc.call("eth_blockNumber", [])
        assert res.get("result") == "0xabc"
        assert rpc.active_url == "https://node2.test"

    def test_call_batch_failover(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """测试 call_batch 批量请求遭遇 429 时无缝漂移到下一个节点."""
        nodes = ["https://node1.test", "https://node2.test"]
        rpc = RobinhoodRpc(urls=nodes, throttle=0.0)

        called_urls: list[str] = []

        def fake_post(url: str, **kwargs: Any) -> FakeResponse:
            called_urls.append(url)
            if url == "https://node1.test":
                return FakeResponse(status_code=429, text="Too Many Requests")
            return FakeResponse(
                json_data=[
                    {"jsonrpc": "2.0", "result": "0x1", "id": 1},
                    {"jsonrpc": "2.0", "result": "0x2", "id": 2},
                ],
                status_code=200,
            )

        monkeypatch.setattr(rpc._session, "post", fake_post)

        batch = [
            {"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1},
            {"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 2},
        ]
        res = rpc.call_batch(batch)

        assert len(res) == 2
        assert called_urls == ["https://node1.test", "https://node2.test"]
        assert rpc.active_url == "https://node2.test"

    def test_all_nodes_rotation_and_exhaustion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """测试全部节点均故障时，依次轮换尝试所有节点并最终抛出异常."""
        nodes = ["https://node1.test", "https://node2.test", "https://node3.test"]
        rpc = RobinhoodRpc(urls=nodes, max_retries=1, throttle=0.0)

        attempted_urls: list[str] = []

        def fake_post(url: str, **kwargs: Any) -> FakeResponse:
            attempted_urls.append(url)
            return FakeResponse(status_code=429, text="Too Many Requests")

        monkeypatch.setattr(rpc._session, "post", fake_post)

        with pytest.raises(RuntimeError, match="RPC call failed after.*across 3 nodes"):
            rpc.call("eth_blockNumber", [])

        # 3 个节点均必须被依次尝试
        assert "https://node1.test" in attempted_urls
        assert "https://node2.test" in attempted_urls
        assert "https://node3.test" in attempted_urls

    def test_success_resets_consecutive_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """测试调用成功后自动清除节点的连续失败与冷却计数."""
        nodes = ["https://node1.test", "https://node2.test"]
        rpc = RobinhoodRpc(urls=nodes, throttle=0.0)

        step = 0

        def fake_post(url: str, **kwargs: Any) -> FakeResponse:
            nonlocal step
            if step == 0 and url == "https://node1.test":
                step += 1
                return FakeResponse(status_code=429, text="Too Many Requests")
            return FakeResponse(
                json_data={"jsonrpc": "2.0", "result": "0x999", "id": 1},
                status_code=200,
            )

        monkeypatch.setattr(rpc._session, "post", fake_post)

        # 第一次触发 failover 到 node2
        rpc.call("eth_blockNumber", [])
        assert rpc.active_url == "https://node2.test"
        assert rpc.health_status["https://node2.test"]["consecutive_fails"] == 0
