"""Offline historical RPC interface; no network client or credentials are loaded."""
from collections.abc import Sequence
from typing import Any

from arc_readiness.rpc_readonly import ReadOnlyRpcTransport
from research.backtest.log_reader import OfflineLogRangeReader, is_log_overflow_error


class RpcError(RuntimeError):
    def __init__(self, code: int, message: str, raw: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.raw = raw


class RobinhoodRpc:
    """Historical calling convention for explicitly injected offline evidence only."""

    def __init__(self) -> None:
        self._reader = OfflineLogRangeReader(
            ReadOnlyRpcTransport("offline://historical", handler=self._dispatch)
        )

    def _dispatch(self, method: str, params: Sequence[Any]) -> Any:
        return self.call(method, params)

    def call(self, method: str, params: Sequence[Any] | None = None) -> Any:
        raise RpcError(-1, "Offline RPC requires an explicitly injected evidence handler")

    def get_logs(self, address: str | None, topics: list[str | None],
                 from_block: int, to_block: int, max_span: int = 50000,
                 min_span: int = 1) -> list[dict[str, Any]]:
        return self._reader.get_logs(address, topics, from_block, to_block,
                                     max_span=max_span, min_span=min_span)

    @staticmethod
    def _is_log_overflow_error(error: Exception) -> bool:
        return is_log_overflow_error(error)
