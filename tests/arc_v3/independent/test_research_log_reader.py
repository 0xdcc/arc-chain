"""Independent contract tests for explicit offline log range reader and bisection.

Migrates and preserves original test obligations 8, 9, 10 from tests/test_tick_cache.py
without importing top-level backtest or RobinhoodRpc, using ReadOnlyRpcTransport injection.
Adds negative tests for single-block overflow fail-closed defense and intra-block cross-tx
deduplication preservation.
Incorporates security probes verifying fail-closed handling on bad RPC responses and
strict rejection of conflicting payloads sharing the same log identity, while preserving
normal clean deduplication of identical duplicates and legitimate empty results.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from arc_readiness.errors import ArcValidationError
from arc_readiness.rpc_readonly import ReadOnlyRpcTransport
from research.backtest.log_reader import (
    OfflineLogRangeReader,
    is_log_overflow_error,
)

# Standard Swap topic for testing
SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"


def test_rpc_adaptive_bisection_on_limit_overflow() -> None:
    """Preserves Test 8 semantics: recursive bisection on -32000 and count >= 10000.

    Original test verified that when get_logs receives -32000 or returns 10000 items,
    it bisects ranges and produces an exact call sequence:
      (1000, 2000) -> (1000, 1500) -> (1501, 2000) -> (1501, 1750) -> (1751, 2000)
    and deduplicates results into 4 unique records: ["0x111", "0x222", "0x333", "0x444"].
    """
    call_records: list[tuple[int, int]] = []

    def mock_handler(method: str, params: Sequence[Any]) -> Any:
        assert method == "eth_getLogs"
        assert params is not None and len(params) > 0
        p = params[0]
        fb = int(str(p["fromBlock"]), 16)
        tb = int(str(p["toBlock"]), 16)
        call_records.append((fb, tb))

        # 1. Full range [1000, 2000] triggers node -32000 overflow exception
        if fb == 1000 and tb == 2000:
            raise RuntimeError("logs matched by query exceeds limit of 10000 (-32000)")

        # 2. Left half [1000, 1500] succeeds returning 2 logs
        if fb == 1000 and tb == 1500:
            return {
                "result": [
                    {"blockNumber": "0x3e8", "logIndex": "0x0", "transactionHash": "0x111"},
                    {"blockNumber": "0x3e9", "logIndex": "0x1", "transactionHash": "0x222"},
                ]
            }

        # 3. Right half [1501, 2000] returns 10,000 logs triggering bisection
        if fb == 1501 and tb == 2000:
            return {
                "result": [
                    {"blockNumber": "0x5dc", "logIndex": hex(i), "transactionHash": hex(i)}
                    for i in range(10000)
                ]
            }

        # 4. Right sub-range [1501, 1750] succeeds returning 1 log
        if fb == 1501 and tb == 1750:
            return {
                "result": [
                    {"blockNumber": "0x5dc", "logIndex": "0x1", "transactionHash": "0x333"},
                ]
            }

        # 5. Right sub-range [1751, 2000] returns 2 logs (including duplicate 0x111)
        if fb == 1751 and tb == 2000:
            return {
                "result": [
                    {"blockNumber": "0x3e8", "logIndex": "0x0", "transactionHash": "0x111"},
                    {"blockNumber": "0x6a0", "logIndex": "0x0", "transactionHash": "0x444"},
                ]
            }

        return {"result": []}

    transport = ReadOnlyRpcTransport(endpoint_url="mock://arc-offline", handler=mock_handler)
    reader = OfflineLogRangeReader(transport)

    logs = reader.get_logs(
        address="0x1234567890123456789012345678901234567890",
        topics=[SWAP_TOPIC],
        from_block=1000,
        to_block=2000,
        max_span=5000,
        min_span=50,
    )

    # Verify exact recursive bisection call trajectory
    assert (1000, 2000) in call_records
    assert (1000, 1500) in call_records
    assert (1501, 2000) in call_records
    assert (1501, 1750) in call_records
    assert (1751, 2000) in call_records

    # Verify deduplicated and stably sorted results: exactly 4 records
    assert len(logs) == 4
    tx_hashes = [item["transactionHash"] for item in logs]
    assert tx_hashes == ["0x111", "0x222", "0x333", "0x444"]


def test_overflow_semantics_timeout_not_overflow() -> None:
    """Preserves Test 9 semantics: strict classification of overflow vs network errors.

    Timeouts, rate-limiting (429), and connection resets must NEVER be classified as overflow,
    preventing catastrophic exponential bisection storms.
    """
    # Overflow errors: must return True
    assert is_log_overflow_error(
        RuntimeError("-32000: logs matched by query exceeds limit of 10000")
    )
    assert is_log_overflow_error(RuntimeError("query exceeds limit of 10000"))
    assert is_log_overflow_error(
        {"code": -32000, "message": "query returned more than 10000 results"}
    )
    assert is_log_overflow_error({"code": -32000, "message": "logs exceeds limit of 10000"})

    # Non-overflow errors: must return False
    assert not is_log_overflow_error(RuntimeError("Log query timed out: ReadTimeout"))
    assert not is_log_overflow_error(
        RuntimeError("ReadTimeout after 10000ms")
    )  # Guard against naive '10000' match
    assert not is_log_overflow_error(RuntimeError("HTTP 429 Too Many Requests"))
    assert not is_log_overflow_error(RuntimeError("connection reset by peer"))
    assert not is_log_overflow_error({"code": -32602, "message": "Invalid params"})

    # Dynamic behavior: non-overflow errors must be re-raised immediately without bisection
    call_count = 0

    def timeout_handler(method: str, params: Sequence[Any]) -> Any:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("ReadTimeout after 10000ms")

    transport = ReadOnlyRpcTransport(endpoint_url="mock://arc-offline", handler=timeout_handler)
    reader = OfflineLogRangeReader(transport)

    with pytest.raises(RuntimeError, match="ReadTimeout after 10000ms"):
        reader.get_logs(
            address="0x1234567890123456789012345678901234567890",
            topics=[],
            from_block=1000,
            to_block=2000,
        )

    # Assert exactly 1 call was attempted (no recursive bisection storm)
    assert call_count == 1


def test_get_logs_skips_bytes32_poolid() -> None:
    """Preserves Test 10 semantics: 66-character bytes32 PoolId short-circuits to [].

    Uniswap V4 poolId cannot be passed to EVM eth_getLogs address field.
    The client must short-circuit and return [] with zero RPC calls.
    """
    call_count = 0

    def failing_handler(method: str, params: Sequence[Any]) -> Any:
        nonlocal call_count
        call_count += 1
        raise AssertionError("RPC handler must NOT be called for invalid bytes32 address")

    transport = ReadOnlyRpcTransport(endpoint_url="mock://arc-offline", handler=failing_handler)
    reader = OfflineLogRangeReader(transport)

    b32 = "0x" + "a" * 64  # 66 characters
    assert len(b32) == 66

    out = reader.get_logs(
        address=b32,
        topics=[SWAP_TOPIC],
        from_block=1,
        to_block=10,
    )

    assert out == []
    assert call_count == 0  # Zero RPC calls confirmed


def test_single_block_overflow_fails_closed() -> None:
    """Rigorous fail-closed test: single block overflow must raise ArcValidationError.

    Guards against upstream bug where span <= min_span returned [], silently dropping logs
    in congested high-volume blocks and causing false-positive arbitrage profits.
    """

    # 1. Single block raising -32000 overflow error
    def overflow_err_handler(method: str, params: Sequence[Any]) -> Any:
        raise RuntimeError("-32000 logs matched by query exceeds limit of 10000")

    transport = ReadOnlyRpcTransport(
        endpoint_url="mock://arc-offline", handler=overflow_err_handler
    )
    reader = OfflineLogRangeReader(transport)

    with pytest.raises(ArcValidationError, match="cannot be bisected further"):
        reader.get_logs(
            address="0x1234567890123456789012345678901234567890",
            topics=[],
            from_block=100,
            to_block=100,
        )

    # 2. Single block returning >= 10000 logs (truncated)
    def overflow_count_handler(method: str, params: Sequence[Any]) -> Any:
        return [
            {"blockNumber": "0x64", "logIndex": hex(i), "transactionHash": hex(i)}
            for i in range(10000)
        ]

    transport_count = ReadOnlyRpcTransport(
        endpoint_url="mock://arc-offline", handler=overflow_count_handler
    )
    reader_count = OfflineLogRangeReader(transport_count)

    with pytest.raises(ArcValidationError, match="cannot be bisected further"):
        reader_count.get_logs(
            address="0x1234567890123456789012345678901234567890",
            topics=[],
            from_block=100,
            to_block=100,
        )


def test_normal_control_and_parameter_validation() -> None:
    """Verifies normal control flow, range validity checks, and strict bad-address rejection."""

    def normal_handler(method: str, params: Sequence[Any]) -> Any:
        return [
            {"blockNumber": "0x64", "logIndex": "0x0", "transactionHash": "0xaaa"},
            {"blockNumber": "0x65", "logIndex": "0x0", "transactionHash": "0xbbb"},
        ]

    transport = ReadOnlyRpcTransport(endpoint_url="mock://arc-offline", handler=normal_handler)
    reader = OfflineLogRangeReader(transport)

    # Normal successful retrieval
    logs = reader.get_logs(
        address="0x1234567890123456789012345678901234567890",
        topics=[],
        from_block=100,
        to_block=105,
    )
    assert len(logs) == 2

    # Malformed addresses: strictly rejected without normalization
    with pytest.raises(ArcValidationError, match="Invalid address format"):
        reader.get_logs(address="not_an_address", topics=[], from_block=1, to_block=10)

    with pytest.raises(ArcValidationError, match="Invalid address format"):
        reader.get_logs(address="0x123", topics=[], from_block=1, to_block=10)

    with pytest.raises(ArcValidationError, match="Invalid address format"):
        reader.get_logs(address="0x" + "g" * 40, topics=[], from_block=1, to_block=10)

    # Inverted block range: from_block > to_block
    with pytest.raises(ArcValidationError, match="Invalid block range"):
        reader.get_logs(address=None, topics=[], from_block=200, to_block=100)

    # Negative block numbers
    with pytest.raises(ArcValidationError, match="cannot be negative"):
        reader.get_logs(address=None, topics=[], from_block=-1, to_block=10)

    # Invalid min_span
    with pytest.raises(ArcValidationError, match="min_span must be an integer >= 1"):
        reader.get_logs(address=None, topics=[], from_block=1, to_block=10, min_span=0)

    # Invalid max_span < min_span
    with pytest.raises(ArcValidationError, match="max_span must be an integer >= min_span"):
        reader.get_logs(address=None, topics=[], from_block=1, to_block=10, min_span=10, max_span=5)


def test_deduplication_preserves_intra_block_different_tx() -> None:
    """Verifies that deduplication keys on true log identity and preserves cross-tx logs.

    Two logs within the same block sharing the same intra-transaction index (logIndex 0)
    belonging to different transactions must NOT be conflated or merged.
    """

    def multi_tx_handler(method: str, params: Sequence[Any]) -> Any:
        return [
            # Tx A in Block 1000 with logIndex 0
            {
                "blockNumber": "0x3e8",
                "transactionHash": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "transactionIndex": "0x0",
                "logIndex": "0x0",
                "data": "0x11",
            },
            # Tx B in Block 1000 with logIndex 0 (different tx, same index)
            {
                "blockNumber": "0x3e8",
                "transactionHash": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "transactionIndex": "0x1",
                "logIndex": "0x0",
                "data": "0x22",
            },
            # Duplicate of Tx A (from overlapping sub-range fetch)
            {
                "blockNumber": "0x3e8",
                "transactionHash": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "transactionIndex": "0x0",
                "logIndex": "0x0",
                "data": "0x11",
            },
        ]

    transport = ReadOnlyRpcTransport(endpoint_url="mock://arc-offline", handler=multi_tx_handler)
    reader = OfflineLogRangeReader(transport)

    logs = reader.get_logs(address=None, topics=[], from_block=1000, to_block=1000)

    # Assert: Exactly 2 logs retained. Duplicate Tx A removed, distinct Tx B preserved!
    assert len(logs) == 2
    tx_hashes = [item["transactionHash"] for item in logs]
    assert "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" in tx_hashes
    assert "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" in tx_hashes


def test_offline_reader_rejects_non_readonly_transport() -> None:
    """Verifies that OfflineLogRangeReader requires ReadOnlyRpcTransport instance."""

    class DummyTransport:
        pass

    bad_transport: Any = DummyTransport()
    with pytest.raises(ArcValidationError, match="must be an instance of ReadOnlyRpcTransport"):
        OfflineLogRangeReader(bad_transport)


def test_bad_rpc_response_fails_closed_and_not_faked_empty() -> None:
    """PROBE-TO-TEST: Bad/malformed RPC responses must fail closed and never fake empty [].

    Verifies:
    1. Dict missing both 'result' and 'error' raises ArcValidationError.
    2. Response being None raises ArcValidationError.
    3. Response being unexpected non-dict non-list raises ArcValidationError.
    4. Dict 'result' being non-list raises ArcValidationError.
    5. Log entry inside result being non-dict raises ArcValidationError.
    6. Log entry missing required identity fields raises ArcValidationError.
    7. Legitimate empty result [] or {'result': []} correctly returns [].
    """
    # 1. Missing result and error
    def missing_result_handler(method: str, params: Sequence[Any]) -> Any:
        return {"jsonrpc": "2.0", "id": 1, "status": "unknown"}

    t1 = ReadOnlyRpcTransport(endpoint_url="mock://arc-offline", handler=missing_result_handler)
    r1 = OfflineLogRangeReader(t1)
    with pytest.raises(ArcValidationError, match="Invalid RPC response"):
        r1.get_logs(address=None, topics=[], from_block=100, to_block=100)

    # 2. Response is None
    def none_response_handler(method: str, params: Sequence[Any]) -> Any:
        return None

    t2 = ReadOnlyRpcTransport(endpoint_url="mock://arc-offline", handler=none_response_handler)
    r2 = OfflineLogRangeReader(t2)
    with pytest.raises(ArcValidationError, match="Invalid RPC response"):
        r2.get_logs(address=None, topics=[], from_block=100, to_block=100)

    # 3. Non-dict non-list response
    def invalid_type_handler(method: str, params: Sequence[Any]) -> Any:
        return "malformed string response"

    t3 = ReadOnlyRpcTransport(endpoint_url="mock://arc-offline", handler=invalid_type_handler)
    r3 = OfflineLogRangeReader(t3)
    with pytest.raises(ArcValidationError, match="Invalid RPC response"):
        r3.get_logs(address=None, topics=[], from_block=100, to_block=100)

    # 4. Result field is not a list
    def non_list_result_handler(method: str, params: Sequence[Any]) -> Any:
        return {"jsonrpc": "2.0", "result": None}

    t4 = ReadOnlyRpcTransport(endpoint_url="mock://arc-offline", handler=non_list_result_handler)
    r4 = OfflineLogRangeReader(t4)
    with pytest.raises(ArcValidationError, match="Invalid RPC response"):
        r4.get_logs(address=None, topics=[], from_block=100, to_block=100)

    # 5. Entry inside result is not a dict
    def non_dict_entry_handler(method: str, params: Sequence[Any]) -> Any:
        return [12345]

    t5 = ReadOnlyRpcTransport(endpoint_url="mock://arc-offline", handler=non_dict_entry_handler)
    r5 = OfflineLogRangeReader(t5)
    with pytest.raises(ArcValidationError, match="Invalid RPC response"):
        r5.get_logs(address=None, topics=[], from_block=100, to_block=100)

    # 6. Log entry missing required identity field (e.g. missing blockNumber)
    def missing_identity_handler(method: str, params: Sequence[Any]) -> Any:
        return [{"transactionHash": "0xaaa", "logIndex": "0x0", "data": "0x"}]

    t6 = ReadOnlyRpcTransport(endpoint_url="mock://arc-offline", handler=missing_identity_handler)
    r6 = OfflineLogRangeReader(t6)
    with pytest.raises(ArcValidationError, match="Missing required log identity field"):
        r6.get_logs(address=None, topics=[], from_block=100, to_block=100)

    # 7. Legitimate empty result controls: must return [] without error
    def legitimate_empty_list_handler(method: str, params: Sequence[Any]) -> Any:
        return []

    t7 = ReadOnlyRpcTransport(
        endpoint_url="mock://arc-offline", handler=legitimate_empty_list_handler
    )
    r7 = OfflineLogRangeReader(t7)
    assert r7.get_logs(address=None, topics=[], from_block=100, to_block=100) == []

    def legitimate_empty_result_handler(method: str, params: Sequence[Any]) -> Any:
        return {"result": []}

    t8 = ReadOnlyRpcTransport(
        endpoint_url="mock://arc-offline", handler=legitimate_empty_result_handler
    )
    r8 = OfflineLogRangeReader(t8)
    assert r8.get_logs(address=None, topics=[], from_block=100, to_block=100) == []


def test_deduplication_conflict_fails_closed_on_payload_mismatch() -> None:
    """PROBE-TO-TEST: Conflicting payloads sharing same identity key must raise ArcValidationError.

    Guards against silent data loss where subsequent logs with conflicting data, topics,
    or address are silently discarded under naive identity-only deduplication.
    """
    # 1. Conflicting 'data'
    def conflicting_data_handler(method: str, params: Sequence[Any]) -> Any:
        return [
            {
                "blockNumber": "0x3e8",
                "transactionHash": "0xaaa",
                "logIndex": "0x0",
                "data": "0x1111",
            },
            {
                "blockNumber": "0x3e8",
                "transactionHash": "0xaaa",
                "logIndex": "0x0",
                "data": "0x2222",  # Conflicting payload!
            },
        ]

    t1 = ReadOnlyRpcTransport(endpoint_url="mock://arc-offline", handler=conflicting_data_handler)
    r1 = OfflineLogRangeReader(t1)
    with pytest.raises(ArcValidationError, match="conflict"):
        r1.get_logs(
            address="0x1234567890123456789012345678901234567890",
            topics=[],
            from_block=1000,
            to_block=1000,
        )

    # 2. Conflicting 'topics'
    def conflicting_topics_handler(method: str, params: Sequence[Any]) -> Any:
        return [
            {
                "blockNumber": "0x3e8",
                "transactionHash": "0xaaa",
                "logIndex": "0x0",
                "topics": ["0x1111111111111111111111111111111111111111111111111111111111111111"],
            },
            {
                "blockNumber": "0x3e8",
                "transactionHash": "0xaaa",
                "logIndex": "0x0",
                "topics": ["0x2222222222222222222222222222222222222222222222222222222222222222"],
            },
        ]

    t2 = ReadOnlyRpcTransport(
        endpoint_url="mock://arc-offline", handler=conflicting_topics_handler
    )
    r2 = OfflineLogRangeReader(t2)
    with pytest.raises(ArcValidationError, match="conflict"):
        r2.get_logs(
            address="0x1234567890123456789012345678901234567890",
            topics=[],
            from_block=1000,
            to_block=1000,
        )

    # 3. Conflicting 'address'
    def conflicting_address_handler(method: str, params: Sequence[Any]) -> Any:
        return [
            {
                "blockNumber": "0x3e8",
                "transactionHash": "0xaaa",
                "logIndex": "0x0",
                "address": "0x1111111111111111111111111111111111111111",
            },
            {
                "blockNumber": "0x3e8",
                "transactionHash": "0xaaa",
                "logIndex": "0x0",
                "address": "0x2222222222222222222222222222222222222222",
            },
        ]

    t3 = ReadOnlyRpcTransport(
        endpoint_url="mock://arc-offline", handler=conflicting_address_handler
    )
    r3 = OfflineLogRangeReader(t3)
    with pytest.raises(ArcValidationError, match="conflict"):
        r3.get_logs(address=None, topics=[], from_block=1000, to_block=1000)


def test_deduplication_exact_duplicate_control() -> None:
    """Normal duplicate control: Identical duplicates must be cleanly deduplicated without error.

    Verifies that when duplicate logs share the same identity AND matching payload,
    deduplication cleanly keeps the first entry, preserves sorting order, and avoids
    false-positive conflict exceptions.
    """
    def exact_duplicate_handler(method: str, params: Sequence[Any]) -> Any:
        return [
            # Entry 1: Tx A in Block 1000 with logIndex 0
            {
                "blockNumber": "0x3e8",
                "transactionHash": "0xaaa",
                "transactionIndex": "0x0",
                "logIndex": "0x0",
                "address": "0x1234567890123456789012345678901234567890",
                "topics": ["0x1111111111111111111111111111111111111111111111111111111111111111"],
                "data": "0xdeadbeef",
                "removed": False,
            },
            # Entry 2: Exact duplicate of Entry 1 (e.g. from overlapping chunk boundary)
            {
                "blockNumber": "0x3e8",
                "transactionHash": "0xaaa",
                "transactionIndex": "0x0",
                "logIndex": "0x0",
                "address": "0x1234567890123456789012345678901234567890",
                "topics": ["0x1111111111111111111111111111111111111111111111111111111111111111"],
                "data": "0xdeadbeef",
                "removed": False,
            },
            # Entry 3: Distinct log in same block with logIndex 1
            {
                "blockNumber": "0x3e8",
                "transactionHash": "0xaaa",
                "transactionIndex": "0x0",
                "logIndex": "0x1",
                "address": "0x1234567890123456789012345678901234567890",
                "topics": ["0x1111111111111111111111111111111111111111111111111111111111111111"],
                "data": "0xfeedface",
                "removed": False,
            },
        ]

    transport = ReadOnlyRpcTransport(
        endpoint_url="mock://arc-offline", handler=exact_duplicate_handler
    )
    reader = OfflineLogRangeReader(transport)

    logs = reader.get_logs(
        address="0x1234567890123456789012345678901234567890",
        topics=[],
        from_block=1000,
        to_block=1000,
    )

    # Exactly 2 logs retained (Entry 2 deduplicated, Entry 1 and Entry 3 kept)
    assert len(logs) == 2
    assert logs[0]["logIndex"] == "0x0"
    assert logs[0]["data"] == "0xdeadbeef"
    assert logs[1]["logIndex"] == "0x1"
    assert logs[1]["data"] == "0xfeedface"


@pytest.mark.parametrize(
    "param_name",
    [
        "from_block",
        "to_block",
        "min_span",
        "max_span",
        "max_depth",
        "limit_threshold",
    ],
)
@pytest.mark.parametrize("bad_val", [True, False])
def test_request_parameters_reject_bool(param_name: str, bad_val: bool) -> None:
    """Verifies that booleans (True/False) are strictly rejected for all request quantity parameters."""
    transport = ReadOnlyRpcTransport(
        endpoint_url="mock://arc-offline",
        handler=lambda m, p: [],
    )
    reader = OfflineLogRangeReader(transport)

    valid_kwargs: dict[str, Any] = {
        "address": None,
        "topics": [],
        "from_block": 100,
        "to_block": 200,
        "min_span": 1,
        "max_span": 500,
        "max_depth": 16,
        "limit_threshold": 1000,
    }
    valid_kwargs[param_name] = bad_val

    with pytest.raises(ArcValidationError):
        reader.get_logs(**valid_kwargs)


def test_request_parameters_accept_zero_and_one_integers() -> None:
    """Normal control: 0 and 1 integer parameters must be accepted normally."""
    transport = ReadOnlyRpcTransport(
        endpoint_url="mock://arc-offline",
        handler=lambda m, p: [],
    )
    reader = OfflineLogRangeReader(transport)

    # 0 as from_block, 1 as to_block, 1 as min_span/max_depth/limit_threshold
    logs = reader.get_logs(
        address=None,
        topics=[],
        from_block=0,
        to_block=1,
        min_span=1,
        max_depth=1,
        limit_threshold=1,
    )
    assert logs == []

    # 0 as both from_block and to_block
    logs_zero = reader.get_logs(
        address=None,
        topics=[],
        from_block=0,
        to_block=0,
        min_span=1,
    )
    assert logs_zero == []


@pytest.mark.parametrize(
    "field_name",
    ["blockNumber", "logIndex", "transactionIndex"],
)
@pytest.mark.parametrize("bad_val", [True, False])
def test_response_quantity_fields_reject_bool(field_name: str, bad_val: bool) -> None:
    """Verifies that booleans (True/False) in response quantity fields are strictly rejected."""
    base_log: dict[str, Any] = {
        "blockNumber": 100,
        "transactionHash": "0x" + "a" * 64,
        "transactionIndex": 0,
        "logIndex": 0,
        "address": "0x" + "1" * 40,
        "data": "0x1234",
        "topics": [],
        "removed": False,
    }
    bad_log = dict(base_log)
    bad_log[field_name] = bad_val

    transport = ReadOnlyRpcTransport(
        endpoint_url="mock://arc-offline",
        handler=lambda m, p: [bad_log],
    )
    reader = OfflineLogRangeReader(transport)

    with pytest.raises(ArcValidationError):
        reader.get_logs(address=None, topics=[], from_block=100, to_block=100)


@pytest.mark.parametrize(
    ("blk", "tx_idx", "log_idx"),
    [
        (0, 0, 0),
        (1, 1, 1),
        ("0x0", "0x0", "0x0"),
        ("0x1", "0x1", "0x1"),
        ("0x64", "0x2", "0x3"),
    ],
)
def test_response_quantity_fields_normal_control_integers_and_hex(
    blk: Any, tx_idx: Any, log_idx: Any
) -> None:
    """Normal control: 0/1 integers and standard hex strings are correctly accepted and sorted."""
    raw_log = {
        "blockNumber": blk,
        "transactionHash": "0x" + "b" * 64,
        "transactionIndex": tx_idx,
        "logIndex": log_idx,
        "address": "0x" + "2" * 40,
        "data": "0xfeed",
        "topics": [],
        "removed": False,
    }

    transport = ReadOnlyRpcTransport(
        endpoint_url="mock://arc-offline",
        handler=lambda m, p: [raw_log],
    )
    reader = OfflineLogRangeReader(transport)

    logs = reader.get_logs(address=None, topics=[], from_block=0, to_block=200)
    assert len(logs) == 1
    assert logs[0]["transactionHash"] == raw_log["transactionHash"]


@pytest.mark.parametrize("removed_val", [True, False])
def test_payload_removed_bool_is_valid_semantics(removed_val: bool) -> None:
    """Verifies that payload 'removed' boolean is legitimate semantics and not mistakenly rejected."""
    raw_log = {
        "blockNumber": "0x64",
        "transactionHash": "0x" + "c" * 64,
        "transactionIndex": "0x0",
        "logIndex": "0x0",
        "address": "0x" + "3" * 40,
        "data": "0xabcd",
        "topics": [],
        "removed": removed_val,
    }

    transport = ReadOnlyRpcTransport(
        endpoint_url="mock://arc-offline",
        handler=lambda m, p: [raw_log],
    )
    reader = OfflineLogRangeReader(transport)

    logs = reader.get_logs(address=None, topics=[], from_block=100, to_block=100)
    assert len(logs) == 1
    assert logs[0]["removed"] is removed_val


def test_payload_removed_bool_conflict_detection() -> None:
    """Verifies that conflicting 'removed' booleans on the same log identity raise ArcValidationError."""
    log1 = {
        "blockNumber": "0x64",
        "transactionHash": "0x" + "d" * 64,
        "transactionIndex": "0x0",
        "logIndex": "0x0",
        "address": "0x" + "4" * 40,
        "data": "0x",
        "topics": [],
        "removed": False,
    }
    log2 = dict(log1)
    log2["removed"] = True

    transport = ReadOnlyRpcTransport(
        endpoint_url="mock://arc-offline",
        handler=lambda m, p: [log1, log2],
    )
    reader = OfflineLogRangeReader(transport)

    with pytest.raises(ArcValidationError, match="conflict"):
        reader.get_logs(address=None, topics=[], from_block=100, to_block=100)
