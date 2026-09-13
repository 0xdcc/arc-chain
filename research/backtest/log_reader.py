"""Explicit offline log range reader and overflow bisection adapter.

Provides safe, offline event log querying with adaptive bisection for research
and historical backtesting. Injects ReadOnlyRpcTransport without touching production
scanners or live network pipelines.
"""

from __future__ import annotations

from typing import Any

from arc_readiness.errors import ArcValidationError
from arc_readiness.rpc_readonly import ReadOnlyRpcTransport

# Standard JSON-RPC page limit default for EVM nodes
DEFAULT_LOG_LIMIT: int = 10000


def is_log_overflow_error(err: Any) -> bool:
    """Strictly classify whether an error represents an RPC log overflow (-32000 / exceeds limit).

    Safety guarantees:
    - Positively matches EVM -32000 code or phrases like 'exceeds limit', 'max result size'.
    - Excludes timeouts (e.g. 'ReadTimeout after 10000ms', 'timed out'), rate limits (HTTP 429),
      connection drops ('reset by peer'), and invalid parameter codes (-32602).
    - Prevents naive substring matching on '10000' from causing recursive bisection storms.
    """
    if err is None:
        return False

    code: int | None = None
    msg: str = ""

    if isinstance(err, dict):
        raw_code = err.get("code")
        if isinstance(raw_code, int) and not isinstance(raw_code, bool):
            code = raw_code
        msg = str(err.get("message", ""))
    else:
        if hasattr(err, "code"):
            raw_code = err.code
            if isinstance(raw_code, int) and not isinstance(raw_code, bool):
                code = raw_code
        if hasattr(err, "message"):
            msg = str(err.message)
        if not msg:
            msg = str(err)

    msg_lower = msg.lower()

    # Strict exclusions: timeouts, rate limits, network resets, invalid params
    exclusion_markers = (
        "timed out",
        "timeout",
        "429",
        "too many requests",
        "reset by peer",
        "connection reset",
        "connection refused",
        "invalid params",
    )
    if any(marker in msg_lower for marker in exclusion_markers):
        return False

    if code == -32602:
        return False

    # Positive identification: explicit overflow phrasing or -32000 code
    overflow_markers = (
        "exceeds limit",
        "exceed limit",
        "query returned more than",
        "response size exceeded",
        "log response size exceeded",
        "more than 10000 results",
        "more than 10,000 results",
        "max result size",
    )
    if any(m in msg_lower for m in overflow_markers):
        return True

    if code == -32000:
        return True

    return False


def _derive_log_identity_key(log: dict[str, Any]) -> tuple[int, str, int]:
    """Derive mandatory identity key for an EVM log: (blockNumber, transactionHash, logIndex).

    Safety guarantees:
    - Every log identity field (blockNumber, transactionHash, logIndex) is strictly required.
    - Does NOT fill missing or invalid identity fields with default 0; raises ArcValidationError.
    - Preserves true cross-transaction identity: different transactions never share an identity key.
    """
    blk = log.get("blockNumber")
    if blk is None:
        raise ArcValidationError("Missing required log identity field: 'blockNumber'")
    if isinstance(blk, bool):
        raise ArcValidationError("Invalid 'blockNumber' type in log: bool")
    elif isinstance(blk, int):
        blk_norm = blk
    elif isinstance(blk, str):
        try:
            blk_norm = int(blk, 16) if blk.startswith(("0x", "0X")) else int(blk)
        except (ValueError, TypeError) as exc:
            raise ArcValidationError(f"Invalid 'blockNumber' value in log: {blk!r}") from exc
    else:
        raise ArcValidationError(f"Invalid 'blockNumber' type in log: {type(blk).__name__}")

    tx_hash = log.get("transactionHash")
    if tx_hash is None:
        raise ArcValidationError("Missing required log identity field: 'transactionHash'")
    if not isinstance(tx_hash, str) or not tx_hash.strip():
        raise ArcValidationError(f"Invalid 'transactionHash' value in log: {tx_hash!r}")
    tx_hash_norm = tx_hash.lower()

    idx = log.get("logIndex")
    if idx is None:
        raise ArcValidationError("Missing required log identity field: 'logIndex'")
    if isinstance(idx, bool):
        raise ArcValidationError("Invalid 'logIndex' type in log: bool")
    elif isinstance(idx, int):
        idx_norm = idx
    elif isinstance(idx, str):
        try:
            idx_norm = int(idx, 16) if idx.startswith(("0x", "0X")) else int(idx)
        except (ValueError, TypeError) as exc:
            raise ArcValidationError(f"Invalid 'logIndex' value in log: {idx!r}") from exc
    else:
        raise ArcValidationError(f"Invalid 'logIndex' type in log: {type(idx).__name__}")

    return (blk_norm, tx_hash_norm, idx_norm)


def _extract_log_payload(log: dict[str, Any]) -> tuple[Any, ...]:
    """Extract canonical normalized effective payload for conflict detection.

    Normalized fields compared:
    - address: lowercase string or None
    - data: lowercase hex string or None
    - topics: tuple of lowercase hex strings or None
    - removed: boolean or None
    - transactionIndex: integer or None
    """
    addr = log.get("address")
    addr_norm: str | None = str(addr).lower() if addr is not None else None

    data = log.get("data")
    data_norm: str | None = str(data).lower() if data is not None else None

    topics = log.get("topics")
    if topics is not None:
        if isinstance(topics, (list, tuple)):
            topics_norm: tuple[str, ...] | None = tuple(str(t).lower() for t in topics)
        else:
            topics_norm = (str(topics).lower(),)
    else:
        topics_norm = None

    removed = log.get("removed")
    removed_norm: bool | None = bool(removed) if removed is not None else None

    tx_idx = log.get("transactionIndex")
    if tx_idx is not None:
        if isinstance(tx_idx, bool):
            raise ArcValidationError("Invalid 'transactionIndex' type in log: bool")
        elif isinstance(tx_idx, int):
            tx_idx_norm: int | None = tx_idx
        elif isinstance(tx_idx, str):
            try:
                tx_idx_norm = (
                    int(tx_idx, 16)
                    if tx_idx.startswith(("0x", "0X"))
                    else int(tx_idx)
                )
            except (ValueError, TypeError) as exc:
                raise ArcValidationError(
                    f"Invalid 'transactionIndex' value in log: {tx_idx!r}"
                ) from exc
        else:
            raise ArcValidationError(
                f"Invalid 'transactionIndex' type in log: {type(tx_idx).__name__}"
            )
    else:
        tx_idx_norm = None

    return (addr_norm, data_norm, topics_norm, removed_norm, tx_idx_norm)


def _log_sort_key(log: dict[str, Any]) -> tuple[int, int, int]:
    """Chronological sort key for EVM event logs: (blockNumber, transactionIndex, logIndex)."""
    blk = log.get("blockNumber")
    if isinstance(blk, bool):
        raise ArcValidationError("Invalid blockNumber type in log: bool")
    elif isinstance(blk, int):
        blk_int = blk
    elif isinstance(blk, str):
        try:
            blk_int = int(blk, 16) if blk.startswith(("0x", "0X")) else int(blk)
        except (ValueError, TypeError) as exc:
            raise ArcValidationError(f"Invalid or missing blockNumber in log: {blk!r}") from exc
    else:
        raise ArcValidationError(f"Invalid or missing blockNumber in log: {blk!r}")

    tx_idx = log.get("transactionIndex")
    if tx_idx is not None:
        if isinstance(tx_idx, bool):
            raise ArcValidationError("Invalid transactionIndex type in log: bool")
        elif isinstance(tx_idx, int):
            tx_idx_int = tx_idx
        elif isinstance(tx_idx, str):
            try:
                tx_idx_int = int(tx_idx, 16) if tx_idx.startswith(("0x", "0X")) else int(tx_idx)
            except (ValueError, TypeError) as exc:
                raise ArcValidationError(f"Invalid transactionIndex in log: {tx_idx!r}") from exc
        else:
            raise ArcValidationError(f"Invalid transactionIndex in log: {tx_idx!r}")
    else:
        tx_idx_int = 0

    idx = log.get("logIndex")
    if isinstance(idx, bool):
        raise ArcValidationError("Invalid logIndex type in log: bool")
    elif isinstance(idx, int):
        idx_int = idx
    elif isinstance(idx, str):
        try:
            idx_int = int(idx, 16) if idx.startswith(("0x", "0X")) else int(idx)
        except (ValueError, TypeError) as exc:
            raise ArcValidationError(f"Invalid or missing logIndex in log: {idx!r}") from exc
    else:
        raise ArcValidationError(f"Invalid or missing logIndex in log: {idx!r}")

    return (blk_int, tx_idx_int, idx_int)


class OfflineLogRangeReader:
    """Explicit offline log range reader with adaptive bisection.

    Used strictly in research/backtest environments. Explicitly injects ReadOnlyRpcTransport
    to ensure read-only execution without network side effects or altering production scanners.
    """

    def __init__(self, transport: ReadOnlyRpcTransport) -> None:
        if not isinstance(transport, ReadOnlyRpcTransport):
            raise ArcValidationError(
                f"transport must be an instance of ReadOnlyRpcTransport, got {type(transport).__name__}"
            )
        self.transport = transport

    def get_logs(
        self,
        address: str | None,
        topics: list[str | None],
        from_block: int,
        to_block: int,
        max_span: int = 50000,
        min_span: int = 1,
        max_depth: int = 32,
        limit_threshold: int = DEFAULT_LOG_LIMIT,
    ) -> list[dict[str, Any]]:
        """Fetch event logs within a block range, bisecting on overflow and deduplicating stably.

        Contract & Defense Rules:
        1. Address defense:
           - None: queries all addresses.
           - Valid 66-character hex (bytes32 Uniswap V4 PoolId): EVM eth_getLogs does not support
             bytes32 poolId in address field. Per legacy interface contract, explicitly short-circuits
             and returns [] with zero RPC handler calls. Clearly documented: this is an unsupported
             parameter short-circuit, NOT a claim that the pool has no logs.
           - Valid 42-character hex (0x...): standard EVM contract address.
           - Any other address format: strictly rejected with ArcValidationError without normalization.
        2. Parameter validation:
           - from_block >= 0, to_block >= 0, from_block <= to_block.
           - min_span >= 1, max_span >= min_span, max_depth >= 1.
        3. Adaptive bisection:
           - On -32000 overflow exception or dict error: bisect [start_b, end_b].
           - On successful response with count >= limit_threshold: bisect [start_b, end_b].
        4. Termination & Fail-Closed:
           - If span <= 1 (single block) and overflow occurs (error or count >= limit_threshold):
             raises ArcValidationError rather than returning [] (no silent data loss).
           - If span <= min_span (when min_span > 1) and overflow occurs: raises ArcValidationError.
           - If recursion depth > max_depth: raises ArcValidationError.
           - Non-overflow errors (ReadTimeout, HTTP 429, reset by peer) are NOT bisected and are re-raised.
        5. Response shape & Entry verification:
           - None response or missing result/error in dict strictly rejected with ArcValidationError.
           - Legitimate [] returned for valid queries with no logs.
           - Every log entry must be a dict with mandatory identity fields (blockNumber, transactionHash, logIndex).
           - Missing identity fields are strictly rejected and never filled with default 0.
        6. Stable deduplication & Conflict detection:
           - Logs with same identity key but conflicting payload (data, topics, address, removed)
             strictly fail-closed with ArcValidationError.
           - Completely identical duplicate logs are cleanly deduplicated preserving first occurrence.
           - Stable sort by (blockNumber, transactionIndex, logIndex).
        """
        # Validate address
        if address is not None:
            if not isinstance(address, str):
                raise ArcValidationError(
                    f"address must be a string or None, got {type(address).__name__}"
                )

            # Check for valid bytes32 PoolId (66 characters: 0x + 64 hex characters)
            is_bytes32 = (
                len(address) == 66
                and address.startswith(("0x", "0X"))
                and all(c in "0123456789abcdefABCDEF" for c in address[2:])
            )
            if is_bytes32:
                # Unsupported parameter short-circuit: zero RPC calls, returns []
                return []

            # Check for valid EVM 20-byte address (42 characters: 0x + 40 hex characters)
            is_valid_evm_address = (
                len(address) == 42
                and address.startswith(("0x", "0X"))
                and all(c in "0123456789abcdefABCDEF" for c in address[2:])
            )
            if not is_valid_evm_address:
                raise ArcValidationError(
                    f"Invalid address format: {address!r}. Must be a 42-character hex EVM address "
                    "or a 66-character bytes32 poolId."
                )

        # Validate range & parameters
        if (
            isinstance(from_block, bool)
            or not isinstance(from_block, int)
            or isinstance(to_block, bool)
            or not isinstance(to_block, int)
        ):
            raise ArcValidationError(
                f"Block numbers must be integers: from_block={from_block!r}, to_block={to_block!r}"
            )
        if from_block < 0 or to_block < 0:
            raise ArcValidationError(
                f"Block numbers cannot be negative: from_block={from_block}, to_block={to_block}"
            )
        if from_block > to_block:
            raise ArcValidationError(
                f"Invalid block range: from_block={from_block} > to_block={to_block}"
            )
        if isinstance(min_span, bool) or not isinstance(min_span, int) or min_span < 1:
            raise ArcValidationError(f"min_span must be an integer >= 1, got {min_span!r}")
        if isinstance(max_span, bool) or not isinstance(max_span, int) or max_span < min_span:
            raise ArcValidationError(
                f"max_span must be an integer >= min_span ({min_span}), got {max_span!r}"
            )
        if isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 1:
            raise ArcValidationError(f"max_depth must be an integer >= 1, got {max_depth!r}")
        if (
            isinstance(limit_threshold, bool)
            or not isinstance(limit_threshold, int)
            or limit_threshold < 1
        ):
            raise ArcValidationError(
                f"limit_threshold must be an integer >= 1, got {limit_threshold!r}"
            )
        if not isinstance(topics, list):
            raise ArcValidationError(f"topics must be a list, got {type(topics).__name__}")

        def _fetch_sub_range(start_b: int, end_b: int, depth: int) -> list[dict[str, Any]]:
            span = end_b - start_b + 1
            param: dict[str, Any] = {
                "topics": topics,
                "fromBlock": hex(start_b),
                "toBlock": hex(end_b),
            }
            if address is not None:
                param["address"] = address

            try:
                res = self.transport.request("eth_getLogs", [param])
            except Exception as exc:
                if is_log_overflow_error(exc):
                    if span <= 1:
                        raise ArcValidationError(
                            f"Block {start_b} exceeds RPC log limit and cannot be bisected further. "
                            "Failing closed to prevent silent log loss."
                        ) from exc
                    if span <= min_span:
                        raise ArcValidationError(
                            f"Block range [{start_b}, {end_b}] (span {span} <= min_span {min_span}) "
                            "exceeds RPC log limit and cannot be bisected further."
                        ) from exc
                    if depth >= max_depth:
                        raise ArcValidationError(
                            f"Recursion depth limit ({max_depth}) exceeded during log bisection "
                            f"for range [{start_b}, {end_b}]."
                        ) from exc
                    mid = (start_b + end_b) // 2
                    left = _fetch_sub_range(start_b, mid, depth + 1)
                    right = _fetch_sub_range(mid + 1, end_b, depth + 1)
                    return left + right
                # Non-overflow exception: do not swallow, do not bisect
                raise

            # Validate RPC response structure
            if res is None:
                raise ArcValidationError("Invalid RPC response: received None from transport")

            if isinstance(res, dict):
                if "error" in res:
                    err_data = res["error"]
                    if is_log_overflow_error(err_data):
                        if span <= 1:
                            raise ArcValidationError(
                                f"Block {start_b} exceeds RPC log limit and cannot be bisected further. "
                                "Failing closed to prevent silent log loss."
                            )
                        if span <= min_span:
                            raise ArcValidationError(
                                f"Block range [{start_b}, {end_b}] (span {span} <= min_span {min_span}) "
                                "exceeds RPC log limit and cannot be bisected further."
                            )
                        if depth >= max_depth:
                            raise ArcValidationError(
                                f"Recursion depth limit ({max_depth}) exceeded during log bisection "
                                f"for range [{start_b}, {end_b}]."
                            )
                        mid = (start_b + end_b) // 2
                        left = _fetch_sub_range(start_b, mid, depth + 1)
                        right = _fetch_sub_range(mid + 1, end_b, depth + 1)
                        return left + right
                    raise ArcValidationError(f"RPC returned error in log query: {err_data!r}")

                if "result" not in res:
                    raise ArcValidationError(
                        f"Invalid RPC response: missing 'result' in response dict: {res!r}"
                    )

                logs_raw = res["result"]
                if not isinstance(logs_raw, list):
                    raise ArcValidationError(
                        f"Invalid RPC response: 'result' field must be a list, got {type(logs_raw).__name__}"
                    )
            elif isinstance(res, list):
                logs_raw = res
            else:
                raise ArcValidationError(
                    f"Invalid RPC response: expected dict or list, got {type(res).__name__}: {res!r}"
                )

            # Validate each log entry type and identity structure
            for item in logs_raw:
                if not isinstance(item, dict):
                    raise ArcValidationError(
                        f"Invalid RPC response: log entry must be a dict, got {type(item).__name__}: {item!r}"
                    )
                # Verify required identity fields exist and are valid (no default 0 substitution)
                _derive_log_identity_key(item)
                if "transactionIndex" in item:
                    tx_i = item["transactionIndex"]
                    if isinstance(tx_i, bool):
                        raise ArcValidationError("Invalid 'transactionIndex' type in log: bool")
                    elif isinstance(tx_i, int):
                        pass
                    elif isinstance(tx_i, str):
                        try:
                            int(tx_i, 16) if tx_i.startswith(("0x", "0X")) else int(tx_i)
                        except (ValueError, TypeError) as exc:
                            raise ArcValidationError(
                                f"Invalid 'transactionIndex' value in log: {tx_i!r}"
                            ) from exc
                    elif tx_i is not None:
                        raise ArcValidationError(
                            f"Invalid 'transactionIndex' type in log: {type(tx_i).__name__}"
                        )

            # Check if returned count reached limit threshold (possible truncation)
            if len(logs_raw) >= limit_threshold:
                if span <= 1:
                    raise ArcValidationError(
                        f"Block {start_b} returned {len(logs_raw)} logs (>= limit {limit_threshold}) "
                        "and cannot be bisected further. Failing closed to prevent silent log loss."
                    )
                if span <= min_span:
                    raise ArcValidationError(
                        f"Block range [{start_b}, {end_b}] returned {len(logs_raw)} logs (>= limit {limit_threshold}) "
                        f"with span <= min_span ({min_span}) and cannot be bisected further."
                    )
                if depth >= max_depth:
                    raise ArcValidationError(
                        f"Recursion depth limit ({max_depth}) exceeded during log bisection "
                        f"for range [{start_b}, {end_b}]."
                    )
                mid = (start_b + end_b) // 2
                left = _fetch_sub_range(start_b, mid, depth + 1)
                right = _fetch_sub_range(mid + 1, end_b, depth + 1)
                return left + right

            return logs_raw

        # Top-level chunking by max_span
        raw_logs: list[dict[str, Any]] = []
        curr = from_block
        while curr <= to_block:
            end = min(curr + max_span - 1, to_block)
            chunk = _fetch_sub_range(curr, end, depth=0)
            raw_logs.extend(chunk)
            curr = end + 1

        # Deduplicate preserving identity across transactions, detecting conflicts
        deduped: list[dict[str, Any]] = []
        seen_identities: dict[tuple[int, str, int], tuple[Any, ...]] = {}
        for item in raw_logs:
            key = _derive_log_identity_key(item)
            payload = _extract_log_payload(item)
            if key in seen_identities:
                existing_payload = seen_identities[key]
                if existing_payload != payload:
                    raise ArcValidationError(
                        f"Conflicting payload detected for log identity {key}: "
                        f"existing={existing_payload!r}, conflicting={payload!r}"
                    )
                # Exact duplicate with matching payload: keep first occurrence, deduplicate cleanly
                continue
            seen_identities[key] = payload
            deduped.append(item)

        # Stable chronological sort
        deduped.sort(key=_log_sort_key)
        return deduped
