"""Comprehensive tests for M7 Read-Only Monitor Application and Feed Scheduling.

Verifies:
1. Static AST code audit proving strict physical isolation from execution/signing.
2. Dynamic runtime assertion verifying sys.modules remains clean of execution components.
3. Negative control verifying exploding trade mock does not trigger or crash read-only monitor.
4. Functional test verifying poll_once detects spread candidates and generates valid reports.
5. Feed worker debounce coalescing, bounded queue, and backpressure observability.
"""

from __future__ import annotations

import ast
import json
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from apps.monitor.feed_worker import FeedEventWorker
from apps.monitor.service import ReadOnlyMonitorService
from arbitrage.domain.types import (
    MarketSnapshot,
    PoolIdentity,
    PoolStateSnapshot,
    TokenIdentity,
)
from arbitrage.market_data.catalog import get_verified_token
from arbitrage.market_data.pool_reader import SnapshotCoordinator

_Q96 = Decimal(2**96)


def _price_to_sqrt_price_x96(price_t1_per_t0: float | Decimal, dec0: int, dec1: int) -> int:
    """Convert human price (token1 units per token0 unit) into Uniswap sqrt_price_x96."""
    raw_ratio = Decimal(str(price_t1_per_t0)) * Decimal(10 ** (dec1 - dec0))
    sqrt_ratio = raw_ratio.sqrt()
    return int(sqrt_ratio * _Q96)


# ==============================================================================
# 1. Static AST Source Code Audit (Physical Isolation)
# ==============================================================================


class TestPhysicalIsolationStaticAST:
    """Static AST audit verifying apps/monitor/ strictly contains no trade execution or signer logic."""

    FORBIDDEN_KEYWORDS = (
        "private_key",
        "broadcast",
        "sign_transaction",
        "send_raw",
    )

    FORBIDDEN_IMPORT_MODULES = (
        "execution",
        "wallet_guard",
        "chains.robinhood",
    )

    def test_ast_audit_apps_monitor_files(self) -> None:
        """Parse all Python files in apps/monitor/ and assert zero forbidden nodes or tokens."""
        monitor_dir = Path(__file__).resolve().parent.parent / "apps" / "monitor"
        assert monitor_dir.exists(), f"Directory not found: {monitor_dir}"

        py_files = list(monitor_dir.glob("*.py"))
        assert len(py_files) >= 3, (
            f"Expected at least 3 py files in {monitor_dir}, found {len(py_files)}"
        )

        for file_path in py_files:
            source_text = file_path.read_text(encoding="utf-8")
            tree = ast.parse(source_text, filename=str(file_path))

            for node in ast.walk(tree):
                # Check module imports
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        for forbidden in self.FORBIDDEN_IMPORT_MODULES:
                            assert forbidden not in alias.name, (
                                f"Forbidden module import '{alias.name}' in {file_path.name}:{node.lineno}"
                            )
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    for forbidden in self.FORBIDDEN_IMPORT_MODULES:
                        assert forbidden not in mod, (
                            f"Forbidden 'from {mod} import ...' in {file_path.name}:{node.lineno}"
                        )

                # Check identifiers and attribute accesses
                tokens_to_check: list[str] = []
                if isinstance(node, ast.Name):
                    tokens_to_check.append(node.id)
                elif isinstance(node, ast.Attribute):
                    tokens_to_check.append(node.attr)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    tokens_to_check.append(node.name)
                elif isinstance(node, ast.arg):
                    tokens_to_check.append(node.arg)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    tokens_to_check.append(node.value)

                for token in tokens_to_check:
                    token_lower = token.lower()
                    for forbidden in self.FORBIDDEN_KEYWORDS:
                        assert forbidden not in token_lower, (
                            f"Sensitive keyword '{forbidden}' detected in {file_path.name}:{getattr(node, 'lineno', 0)} "
                            f"(token: '{token}')"
                        )


# ==============================================================================
# 2. Dynamic Environment Assertion
# ==============================================================================


class TestPhysicalIsolationDynamicRuntime:
    """Dynamic runtime assertion verifying poll_once does not load execution or broadcasting components."""

    FORBIDDEN_EXECUTION_COMPONENTS = (
        "execution.weth_arbitrage_executor",
        "execution.usdg_arbitrage_executor",
        "chains.robinhood.executor",
    )

    def test_sys_modules_clean_after_poll_once(self) -> None:
        import sys

        before_modules = set(sys.modules.keys())

        dummy_coordinator = MagicMock(spec=SnapshotCoordinator)
        dummy_coordinator.read_market_snapshot.return_value = MarketSnapshot(
            chain_id=4663,
            block_number=9999,
            captured_at=1000.0,
            pools={},
        )

        service = ReadOnlyMonitorService(coordinator=dummy_coordinator)
        result = service.poll_once(pools=[])

        assert result["status"] == "success"

        # Assert no trade executors or transaction broadcasting components were loaded dynamically
        new_modules = set(sys.modules.keys()) - before_modules
        for forbidden in self.FORBIDDEN_EXECUTION_COMPONENTS:
            assert forbidden not in new_modules, (
                f"Violation: execution component '{forbidden}' loaded dynamically in sys.modules during read-only operation!"
            )

        for mod_name in new_modules:
            assert "broadcast" not in mod_name.lower(), (
                f"Violation: broadcasting component '{mod_name}' loaded in sys.modules!"
            )
            if "execution." in mod_name:
                assert "executor" not in mod_name.lower(), (
                    f"Violation: trading executor module '{mod_name}' loaded in sys.modules!"
                )


# ==============================================================================
# 3. Negative Control Test
# ==============================================================================


class TestNegativeControl:
    """Negative control ensuring read-only monitor never touches or invokes trade execution components."""

    def test_exploding_executor_never_triggered(self) -> None:
        class ExplodingTradeExecutor:
            """Trap object that raises immediately upon any attribute access or invocation."""

            def __getattr__(self, name: str) -> Any:
                raise AssertionError(
                    f"CRITICAL VIOLATION: Read-only monitor attempted to access trade attribute '{name}'!"
                )

            def __call__(self, *args: Any, **kwargs: Any) -> Any:
                raise AssertionError(
                    "CRITICAL VIOLATION: Read-only monitor attempted to execute a trade transaction!"
                )

        exploding_target = ExplodingTradeExecutor()

        dummy_coordinator = MagicMock(spec=SnapshotCoordinator)
        dummy_coordinator.read_market_snapshot.return_value = MarketSnapshot(
            chain_id=4663,
            block_number=10001,
            captured_at=2000.0,
            pools={},
        )

        # Inject exploding executor into the service
        service = ReadOnlyMonitorService(
            coordinator=dummy_coordinator,
            dummy_executor=exploding_target,
        )

        # Execution should succeed without any interaction with exploding_target
        result = service.poll_once(pools=[])
        assert result["status"] == "success"
        assert result["block_number"] == 10001
        assert result["candidates"] == []


# ==============================================================================
# 4. Functional Test: Single Polling Pass & Candidate Discovery
# ==============================================================================


class TestReadOnlyMonitorFunctional:
    """Functional test verifying single polling pass accurately discovers spread candidates and renders reports."""

    def test_poll_once_discovers_spread_and_formats_reports(self) -> None:
        token_weth = get_verified_token("WETH")
        token_usdg = get_verified_token("USDG")

        # Pool V3: WETH / USDG (token0 = WETH, token1 = USDG)
        pool_v3 = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v3",
            pool_id="0x" + "11" * 20,
            token0=token_weth.address,
            token1=token_usdg.address,
            fee_bps=5.0,
            tick_spacing=10,
        )
        pool_v4 = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v4",
            pool_id="0x" + "22" * 32,
            token0=token_weth.address,
            token1=token_usdg.address,
            fee_bps=12.5,
            tick_spacing=13,
        )

        # Price disparity: Pool V3 has WETH @ 2000 USDG, Pool V4 has WETH @ 2050 USDG
        # Gross spread = 2050 / 2000 - 1 = 2.5% = 250 bps
        sqrt_v3 = _price_to_sqrt_price_x96(2000.0, token_weth.decimals, token_usdg.decimals)
        sqrt_v4 = _price_to_sqrt_price_x96(2050.0, token_weth.decimals, token_usdg.decimals)

        snap_v3 = PoolStateSnapshot(
            pool=pool_v3,
            block_number=123456,
            block_timestamp=1700000000,
            sqrt_price_x96=sqrt_v3,
            liquidity=10_000_000,
            tick=0,
        )
        snap_v4 = PoolStateSnapshot(
            pool=pool_v4,
            block_number=123456,
            block_timestamp=1700000000,
            sqrt_price_x96=sqrt_v4,
            liquidity=10_000_000,
            tick=0,
        )

        synthetic_snapshot = MarketSnapshot(
            chain_id=4663,
            block_number=123456,
            captured_at=1700000000.0,
            pools={pool_v3.pool_id: snap_v3, pool_v4.pool_id: snap_v4},
        )

        mock_coordinator = MagicMock(spec=SnapshotCoordinator)
        mock_coordinator.read_market_snapshot.return_value = synthetic_snapshot

        service = ReadOnlyMonitorService(
            coordinator=mock_coordinator,
            base_token=token_usdg,
            min_gross_bps=10.0,
        )

        result = service.poll_once(pools=[pool_v3, pool_v4])

        # Assertions
        assert result["status"] == "success"
        assert result["block_number"] == 123456
        assert len(result["spread_candidates"]) >= 1
        assert len(result["candidates"]) >= 1

        cand = result["spread_candidates"][0]
        assert cand.observed_gross_bps > 10.0

        # Validate reporting outputs
        assert len(result["reports"]) == len(result["candidates"])
        assert len(result["reports_json"]) == len(result["reports"])
        assert len(result["reports_text"]) == len(result["reports"])

        # Check JSON formatting validity
        for r_json in result["reports_json"]:
            parsed = json.loads(r_json)
            assert "report_id" in parsed
            assert "route" in parsed
            assert "metadata" in parsed
            assert parsed["metadata"]["block_number"] == 123456

        # Check plain text report validity
        first_text = result["reports_text"][0]
        assert "Arbitrage Opportunity Report" in first_text or "two_hop_spread" in first_text


# ==============================================================================
# 5. Debounce and Backpressure Observability Tests
# ==============================================================================


class TestFeedWorkerDebounceAndBackpressure:
    """Tests for FeedEventWorker debounce coalescing, bounded capacity, and backpressure metrics."""

    def test_same_pool_high_frequency_debounce(self) -> None:
        """High frequency updates for the same pool coalesce into the latest state."""
        worker = FeedEventWorker(max_queue_size=100)

        # Enqueue 5 successive events for pool A
        for seq in range(1, 6):
            worker.enqueue(
                {
                    "pool_id": "0xpool_alpha",
                    "seq": seq,
                    "block_number": 100 + seq,
                    "sqrt_price_x96": 1000 * seq,
                }
            )

        # Due to debounce coalescing, queue_size must be exactly 1
        assert worker.queue_size() == 1

        # Enqueue event for pool B
        worker.enqueue(
            {
                "pool_id": "0xpool_beta",
                "seq": 10,
                "block_number": 200,
            }
        )
        assert worker.queue_size() == 2

        # Pop all events
        events = worker.pop_events()
        assert len(events) == 2
        assert worker.queue_size() == 0

        # Verify pool alpha event retained the latest state (seq=5)
        pool_alpha_event = next(e for e in events if e["pool_id"] == "0xpool_alpha")
        assert pool_alpha_event["seq"] == 5
        assert pool_alpha_event["sqrt_price_x96"] == 5000

    def test_backpressure_and_busy_threshold(self) -> None:
        """Verify is_busy flags correctly under buffer saturation and clears on pop."""
        max_size = 5
        busy_thresh = 3
        worker = FeedEventWorker(max_queue_size=max_size, busy_threshold=busy_thresh)

        assert not worker.is_busy()
        assert worker.queue_size() == 0

        # Push 2 distinct pools (below threshold)
        worker.enqueue({"pool_id": "0xpool_1"})
        worker.enqueue({"pool_id": "0xpool_2"})
        assert not worker.is_busy()

        # Push 3rd pool (reaches busy_threshold 3)
        worker.enqueue({"pool_id": "0xpool_3"})
        assert worker.is_busy()

        # Pop 2 events, relieving backpressure
        popped = worker.pop_events(max_count=2)
        assert len(popped) == 2
        assert worker.queue_size() == 1
        assert not worker.is_busy()

    def test_bounded_queue_overflow_drop_oldest(self) -> None:
        """Bounded queue limits size and tracks dropped event counts upon overflow."""
        worker = FeedEventWorker(max_queue_size=3, overflow_policy="drop_oldest")

        worker.enqueue({"pool_id": "0xpool_1", "val": 1})
        worker.enqueue({"pool_id": "0xpool_2", "val": 2})
        worker.enqueue({"pool_id": "0xpool_3", "val": 3})
        assert worker.queue_size() == 3

        # Pushing a 4th distinct pool causes overflow and drops the oldest
        worker.enqueue({"pool_id": "0xpool_4", "val": 4})
        assert worker.queue_size() == 3

        metrics = worker.get_metrics()
        assert metrics["total_dropped"] == 1
        assert metrics["total_received"] == 4

        # Verify pool_1 was dropped and remaining are pool_2, 3, 4
        popped = worker.pop_events()
        pool_ids = [p["pool_id"] for p in popped]
        assert pool_ids == ["0xpool_2", "0xpool_3", "0xpool_4"]

    def test_thread_safe_concurrency(self) -> None:
        """Multiple threads concurrently enqueueing events maintain state integrity without deadlocks."""
        worker = FeedEventWorker(max_queue_size=500)
        num_threads = 8
        events_per_thread = 50

        def worker_task(thread_id: int) -> None:
            for i in range(events_per_thread):
                # 3 shared pools across all threads + 1 dedicated pool per thread
                pool_id = (
                    f"0xshared_pool_{i % 3}" if i % 2 == 0 else f"0xthread_{thread_id}_pool_{i}"
                )
                worker.enqueue({"pool_id": pool_id, "thread": thread_id, "idx": i})

        threads = [threading.Thread(target=worker_task, args=(tid,)) for tid in range(num_threads)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        metrics = worker.get_metrics()
        assert metrics["total_received"] == num_threads * events_per_thread
        assert metrics["total_debounced"] > 0
        assert worker.queue_size() <= 500

        # Draining the queue empties it completely
        all_events = worker.pop_events()
        assert len(all_events) == worker.queue_size() + len(all_events)
        assert worker.queue_size() == 0
