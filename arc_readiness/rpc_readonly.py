"""Strictly read-only RPC transport, method allowlist guard, and fixed-block sampling."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from arc_readiness.errors import ArcValidationError

ALLOWED_READONLY_METHODS = frozenset(
    {
        "eth_chainId",
        "eth_getBlockByNumber",
        "eth_getBlockByHash",
        "eth_getCode",
        "eth_getBalance",
        "eth_call",
        "eth_getLogs",
        "eth_getTransactionByHash",
        "eth_getTransactionReceipt",
        "eth_gasPrice",
        "eth_feeHistory",
    }
)

FORBIDDEN_MUTATING_METHODS = frozenset(
    {
        "eth_sendRawTransaction",
        "eth_sendTransaction",
        "eth_sign",
        "eth_signTransaction",
        "personal_sign",
        "wallet_addEthereumChain",
        "wallet_switchEthereumChain",
    }
)


class ReadOnlyRpcTransport:
    """Safe read-only JSON-RPC transport wrapper enforcing pre-flight method allowlists."""

    def __init__(
        self,
        endpoint_url: str,
        handler: Callable[[str, Sequence[Any]], Any] | None = None,
    ) -> None:
        self.endpoint_url = endpoint_url
        self._handler = handler

    def request(self, method: str, params: Sequence[Any] | None = None) -> Any:
        """Execute a JSON-RPC request after strictly verifying it is a read-only method."""
        if not isinstance(method, str):
            raise ArcValidationError(f"RPC method must be a string, got {type(method).__name__}")

        if method in FORBIDDEN_MUTATING_METHODS or not method.startswith("eth_"):
            raise ArcValidationError(
                f"Prohibited mutating or non-EVM RPC method: {method}. Write operations strictly forbidden."
            )

        if method not in ALLOWED_READONLY_METHODS:
            raise ArcValidationError(
                f"Prohibited RPC method: {method}. Allowed methods are: {sorted(ALLOWED_READONLY_METHODS)}"
            )

        safe_params = tuple(params) if params is not None else ()

        if self._handler is not None:
            return self._handler(method, safe_params)

        raise ArcValidationError("No RPC execution handler configured for offline transport")


class FixedBlockSampler:
    """Ensures multi-call consistency anchored to a fixed block number and block hash."""

    def __init__(
        self,
        transport: ReadOnlyRpcTransport,
        anchor_block_number: int,
        anchor_block_hash: str,
    ) -> None:
        self.transport = transport
        self.anchor_block_number = anchor_block_number
        self.anchor_block_hash = anchor_block_hash.lower()

    def sample_balance(self, account_address: str, observed_block_hash: str) -> int:
        """Query account balance asserting strict block hash invariance."""
        if observed_block_hash.lower() != self.anchor_block_hash:
            raise ArcValidationError(
                f"Block hash drift detected: expected {self.anchor_block_hash}, got {observed_block_hash}. "
                "Silent fallback to latest block is strictly forbidden."
            )

        # In fixed-block sampling, block parameter must never be 'latest'
        result = self.transport.request(
            "eth_getBalance",
            [account_address, hex(self.anchor_block_number)],
        )
        if isinstance(result, str) and result.startswith("0x"):
            return int(result, 16)
        if isinstance(result, int):
            return result
        raise ArcValidationError(f"Unexpected getBalance response format: {result!r}")
