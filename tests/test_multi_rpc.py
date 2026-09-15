"""多 RPC 客户端节点故障漂移 (Failover) 与向后兼容测试 (无真实网络依赖).

测试覆盖:
1. 默认 6 节点矩阵初始化与向后兼容单 URL 构造;
2. HTTP 429 限流时瞬时 Failover 漂移到备用节点 (无阻塞 sleep 30s);
3. ConnectionError / Timeout 时自动漂移到备用节点;
4. HTTP 5xx 服务端错误时自动漂移到备用节点;
5. JSON-RPC -32005 节点限流响应触发漂移;
6. call_batch 批量调用时同样享有节点故障漂移;
7. 全部节点轮换与重试耗尽保护;
8. 成功请求后节点连续失败计数与冷却状态重置;
9. 新增: 只读方法白名单检查与写操作请求前拦截拒发;
10. 新增: 批量调用响应 ID 精确对齐, 拒绝混乱/重复/未知/缺失 ID;
11. 新增: 业务合约 revert 不做无意义重试与跨节点漂移;
12. 新增: 单节点 3 次连续失败触发熔断器隔离;
13. 新增: 生产 RpcPoolClient 强制显式注入 urls 与 session.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

import pytest
import requests

from arc_readiness.errors import ArcValidationError
from research.market_data.rpc_pool import (
    ArcCircuitBreakerTrippedError,
    RpcPoolClient,
)

# Historical Robinhood 6-node endpoints used strictly for backward compatibility test fixture
HISTORICAL_ROBINHOOD_URLS: list[str] = [
    "https://rpc.mainnet.chain.robinhood.com",
    "https://rpc.secondary.chain.robinhood.com",
    "https://rpc.backup1.chain.robinhood.com",
    "https://rpc.backup2.chain.robinhood.com",
    "https://rpc.backup3.chain.robinhood.com",
    "https://rpc.backup4.chain.robinhood.com",
]


class RobinhoodRpc(RpcPoolClient):
    """Test fixture factory providing historical Robinhood defaults to real RpcPoolClient.

    Does NOT implement any failover algorithm; delegates 100% to production RpcPoolClient.
    """

    DEFAULT_URLS: list[str] = list(HISTORICAL_ROBINHOOD_URLS)

    def __init__(
        self,
        urls: Sequence[str] | None = None,
        url: str | None = None,
        session: Any | None = None,
        timeout: float = 10.0,
        throttle: float = 0.0,
        max_retries: int | None = None,
    ) -> None:
        if urls is None and url is None:
            urls = list(self.DEFAULT_URLS)
        if session is None:
            session = requests.Session()
        super().__init__(
            urls=urls,
            url=url,
            session=session,
            timeout=timeout,
            throttle=throttle,
            max_retries=max_retries,
        )


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


class TestMultiRpcSecurityAndIntegrity:
    """新增安全白名单、批处理 ID 完整性与熔断控制断言."""

    def test_write_method_rejection_before_post(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """验证状态修改型 RPC 请求在 session.post 之前被严格阻断."""
        nodes = ["https://node1.test"]
        rpc = RobinhoodRpc(urls=nodes)

        post_called = False

        def fake_post(url: str, **kwargs: Any) -> FakeResponse:
            nonlocal post_called
            post_called = True
            return FakeResponse(
                status_code=200,
                json_data={"jsonrpc": "2.0", "result": "0x1", "id": 1},
            )

        monkeypatch.setattr(rpc._session, "post", fake_post)

        with pytest.raises(ArcValidationError, match="Write operations strictly forbidden"):
            rpc.call("eth_sendRawTransaction", ["0x123456"])

        assert not post_called

    def test_batch_write_method_rejection_before_post(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """验证批处理中若掺杂写操作请求，在 session.post 之前整批阻断."""
        nodes = ["https://node1.test"]
        rpc = RobinhoodRpc(urls=nodes)

        post_called = False

        def fake_post(url: str, **kwargs: Any) -> FakeResponse:
            nonlocal post_called
            post_called = True
            return FakeResponse(status_code=200, json_data=[])

        monkeypatch.setattr(rpc._session, "post", fake_post)

        batch = [
            {"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1},
            {"jsonrpc": "2.0", "method": "eth_sendTransaction", "params": [{}], "id": 2},
        ]
        with pytest.raises(ArcValidationError, match="Write operations strictly forbidden"):
            rpc.call_batch(batch)

        assert not post_called

    def test_batch_out_of_order_responses_matched_by_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """验证服务器乱序返回批量响应时，客户端精确根据 ID 对齐而非盲目 zip 猜测."""
        nodes = ["https://node1.test"]
        rpc = RobinhoodRpc(urls=nodes)

        def fake_post(url: str, **kwargs: Any) -> FakeResponse:
            # Server returns id 2 first, then id 1
            return FakeResponse(
                json_data=[
                    {"jsonrpc": "2.0", "result": "resp_for_2", "id": 2},
                    {"jsonrpc": "2.0", "result": "resp_for_1", "id": 1},
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
        assert res[0]["id"] == 1
        assert res[0]["result"] == "resp_for_1"
        assert res[1]["id"] == 2
        assert res[1]["result"] == "resp_for_2"

    def test_batch_duplicate_request_id_rejected(self) -> None:
        """验证请求批处理中包含重复 ID 时直接拒绝."""
        rpc = RobinhoodRpc(urls=["https://node1.test"])
        batch = [
            {"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1},
            {"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1},
        ]
        with pytest.raises(ValueError, match="Duplicate request ID in batch"):
            rpc.call_batch(batch)

    def test_batch_duplicate_response_id_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """验证服务端返回重复 ID 时抛出错误."""
        rpc = RobinhoodRpc(urls=["https://node1.test"])

        def fake_post(url: str, **kwargs: Any) -> FakeResponse:
            return FakeResponse(
                json_data=[
                    {"jsonrpc": "2.0", "result": "0x1", "id": 1},
                    {"jsonrpc": "2.0", "result": "0x2", "id": 1},
                ],
                status_code=200,
            )

        monkeypatch.setattr(rpc._session, "post", fake_post)

        batch = [
            {"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1},
            {"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 2},
        ]
        with pytest.raises(RuntimeError, match="Duplicate response ID in batch"):
            rpc.call_batch(batch)

    def test_batch_unknown_or_missing_response_id_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """验证服务端返回未知 ID 或丢失预期 ID 时抛错."""
        rpc = RobinhoodRpc(urls=["https://node1.test"])

        def fake_post(url: str, **kwargs: Any) -> FakeResponse:
            return FakeResponse(
                json_data=[
                    {"jsonrpc": "2.0", "result": "0x1", "id": 1},
                    {"jsonrpc": "2.0", "result": "0x99", "id": 99},  # Unknown ID
                ],
                status_code=200,
            )

        monkeypatch.setattr(rpc._session, "post", fake_post)

        batch = [
            {"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1},
            {"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 2},
        ]
        with pytest.raises(RuntimeError, match="Unknown response ID in batch"):
            rpc.call_batch(batch)

    def test_business_revert_not_retried_on_other_nodes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """验证业务合约 revert (如余额不足或执行失败) 不触发节点漂移与重试."""
        nodes = ["https://node1.test", "https://node2.test"]
        rpc = RobinhoodRpc(urls=nodes)

        attempted_urls: list[str] = []

        def fake_post(url: str, **kwargs: Any) -> FakeResponse:
            attempted_urls.append(url)
            return FakeResponse(
                json_data={
                    "jsonrpc": "2.0",
                    "error": {"code": 3, "message": "execution reverted: custom revert"},
                    "id": 1,
                },
                status_code=200,
            )

        monkeypatch.setattr(rpc._session, "post", fake_post)

        res = rpc.call("eth_call", [{"to": "0x123"}, "latest"])
        assert "error" in res
        assert res["error"]["message"] == "execution reverted: custom revert"
        # 绝不漂移到 node2
        assert attempted_urls == ["https://node1.test"]
        assert rpc.active_url == "https://node1.test"
        assert rpc.health_status["https://node1.test"]["fail_count"] == 0

    def test_single_node_consecutive_failures_circuit_breaker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """验证单节点在连续 3 次失败后触发熔断器阻断后续请求."""
        nodes = ["https://node1.test"]
        rpc = RobinhoodRpc(urls=nodes)

        def fake_post(url: str, **kwargs: Any) -> FakeResponse:
            return FakeResponse(status_code=500, text="Internal Server Error")

        monkeypatch.setattr(rpc._session, "post", fake_post)

        # First call fails (consecutive_fails = 1)
        with pytest.raises(RuntimeError, match="RPC call failed after"):
            rpc.call("eth_blockNumber", [])
        assert rpc.health_status["https://node1.test"]["consecutive_fails"] == 1

        # Second call fails (consecutive_fails = 2)
        with pytest.raises(RuntimeError, match="RPC call failed after"):
            rpc.call("eth_blockNumber", [])
        assert rpc.health_status["https://node1.test"]["consecutive_fails"] == 2

        # Third call fails (consecutive_fails = 3, trips circuit breaker)
        with pytest.raises(RuntimeError, match="RPC call failed after"):
            rpc.call("eth_blockNumber", [])
        assert rpc.health_status["https://node1.test"]["consecutive_fails"] == 3

        # Fourth call is blocked immediately by circuit breaker before network dispatch
        with pytest.raises(ArcCircuitBreakerTrippedError, match="Circuit breaker tripped"):
            rpc.call("eth_blockNumber", [])

    def test_rpc_pool_client_requires_explicit_urls_and_session(self) -> None:
        """验证生产 RpcPoolClient 类严格要求显式传入 urls 与 session, 拒绝默认生产端点."""
        # 1. Reject missing URLs
        with pytest.raises(ValueError, match="Explicit urls or url must be provided"):
            RpcPoolClient(session=requests.Session())

        with pytest.raises(ValueError, match="urls must be a non-empty sequence"):
            RpcPoolClient(urls=[], session=requests.Session())

        # 2. Reject missing session
        with pytest.raises(
            ValueError, match="Explicit session or read-only transport must be injected"
        ):
            RpcPoolClient(urls=["https://rpc.example.com"], session=None)
