"""Fixed-block multi-call sampling, block hash consistency anchors, and safe fallback guards."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from arc_readiness.errors import ArcValidationError
from arc_readiness.rpc_readonly import ReadOnlyRpcTransport


class SamplingStatus(StrEnum):
    SUCCESS = "SUCCESS"
    INCOMPLETE = "INCOMPLETE"
    NODE_LIMITATION = "NODE_LIMITATION"
    HASH_DRIFT = "HASH_DRIFT"


@dataclass(frozen=True)
class BlockAnchor:
    """Immutable block reference binding block number, block hash, and timing metadata."""

    block_number: int
    block_hash: str
    parent_hash: str
    timestamp: int
    chain_id: int

    def __post_init__(self) -> None:
        if self.block_number < 0:
            raise ArcValidationError(f"block_number cannot be negative: {self.block_number}")
        norm_hash = self.block_hash.lower()
        if not norm_hash.startswith("0x") or len(norm_hash) != 66:
            raise ArcValidationError(f"Invalid 66-char hex block_hash: {self.block_hash}")
        norm_parent = self.parent_hash.lower()
        if not norm_parent.startswith("0x") or len(norm_parent) != 66:
            raise ArcValidationError(f"Invalid 66-char hex parent_hash: {self.parent_hash}")
        if self.timestamp < 0:
            raise ArcValidationError(f"timestamp cannot be negative: {self.timestamp}")
        if self.chain_id not in (5042, 5042002):
            raise ArcValidationError(f"Invalid Arc chain_id: {self.chain_id}")


@dataclass(frozen=True)
class FixedBlockSampleResult:
    """Result envelope of a fixed-block observation containing multi-call responses and consistency proof."""

    anchor: BlockAnchor
    status: SamplingStatus
    observations: dict[str, Any]
    error_message: str | None = None
    pre_block_hash: str = ""
    post_block_hash: str = ""

    @property
    def is_consistent(self) -> bool:
        return self.status == SamplingStatus.SUCCESS and self.pre_block_hash == self.post_block_hash


class FixedBlockSampler:
    """Safely samples multi-call state anchored to a fixed block height.

    Invariants:
    - Pre- and Post-query block hash consistency checks.
    - Zero fallback to 'latest': if historical state or block is unavailable,
      reports NODE_LIMITATION or INCOMPLETE, never substitutes latest.
    - EIP-1898 support: accepts explicit blockNumber hex parameter.
    """

    def __init__(
        self,
        transport: ReadOnlyRpcTransport,
        chain_id: int = 5042,
    ) -> None:
        self.transport = transport
        self.chain_id = chain_id

    def get_block_anchor(self, block_identifier: int | str) -> BlockAnchor:
        """Fetch and return an immutable BlockAnchor for a specific block height."""
        if isinstance(block_identifier, int):
            if block_identifier < 0:
                raise ArcValidationError(f"block_number cannot be negative: {block_identifier}")
            hex_block = hex(block_identifier)
        elif isinstance(block_identifier, str):
            if block_identifier.lower() == "latest":
                raise ArcValidationError(
                    "Explicit block number required for BlockAnchor: 'latest' is strictly forbidden."
                )
            hex_block = block_identifier
        else:
            raise ArcValidationError(f"Invalid block_identifier type: {type(block_identifier).__name__}")

        raw_block = self.transport.request("eth_getBlockByNumber", [hex_block, False])
        if not raw_block or not isinstance(raw_block, dict):
            raise ArcValidationError(f"Block {block_identifier} not found or node returned empty response")

        try:
            b_num = int(raw_block["number"], 16) if isinstance(raw_block["number"], str) else int(raw_block["number"])
            b_hash = str(raw_block["hash"])
            p_hash = str(raw_block["parentHash"])
            b_ts = int(raw_block["timestamp"], 16) if isinstance(raw_block["timestamp"], str) else int(raw_block["timestamp"])
        except (KeyError, ValueError) as e:
            raise ArcValidationError(f"Malformed block response: {e}") from e

        return BlockAnchor(
            block_number=b_num,
            block_hash=b_hash,
            parent_hash=p_hash,
            timestamp=b_ts,
            chain_id=self.chain_id,
        )

    def sample_state_multicall(
        self,
        anchor: BlockAnchor,
        calls: Sequence[tuple[str, str, Sequence[Any]]],  # (call_key, rpc_method, params_without_block)
    ) -> FixedBlockSampleResult:
        """Execute a series of state queries anchored to the designated BlockAnchor.

        Performs pre-call and post-call hash validation.
        """
        hex_block = hex(anchor.block_number)

        # 1. Pre-call hash verification: query block hash at height
        try:
            pre_block = self.transport.request("eth_getBlockByNumber", [hex_block, False])
            if not pre_block or not isinstance(pre_block, dict):
                return FixedBlockSampleResult(
                    anchor=anchor,
                    status=SamplingStatus.NODE_LIMITATION,
                    observations={},
                    error_message=f"Node cannot serve historical block {anchor.block_number}",
                )
            pre_hash = str(pre_block.get("hash", "")).lower()
        except Exception as e:
            return FixedBlockSampleResult(
                anchor=anchor,
                status=SamplingStatus.NODE_LIMITATION,
                observations={},
                error_message=f"RPC error during pre-call verification: {e}",
            )

        if pre_hash != anchor.block_hash.lower():
            return FixedBlockSampleResult(
                anchor=anchor,
                status=SamplingStatus.HASH_DRIFT,
                observations={},
                error_message=f"Pre-call block hash drift: expected {anchor.block_hash}, got {pre_hash}",
                pre_block_hash=pre_hash,
            )

        # 2. Execute all calls bound to hex_block
        observations: dict[str, Any] = {}
        for call_key, method, base_params in calls:
            # Enforce that hex_block is appended to params, never 'latest'
            call_params = list(base_params) + [hex_block]
            try:
                res = self.transport.request(method, call_params)
                observations[call_key] = res
            except Exception as e:
                # If historical query is unsupported, node limitation
                err_str = str(e)
                if "historical" in err_str.lower() or "pruned" in err_str.lower():
                    return FixedBlockSampleResult(
                        anchor=anchor,
                        status=SamplingStatus.NODE_LIMITATION,
                        observations=observations,
                        error_message=f"Node limitation: {e}",
                        pre_block_hash=pre_hash,
                    )
                return FixedBlockSampleResult(
                    anchor=anchor,
                    status=SamplingStatus.INCOMPLETE,
                    observations=observations,
                    error_message=f"Call '{call_key}' failed: {e}",
                    pre_block_hash=pre_hash,
                )

        # 3. Post-call hash verification: re-verify block hash at height
        try:
            post_block = self.transport.request("eth_getBlockByNumber", [hex_block, False])
            post_hash = str(post_block.get("hash", "")).lower() if isinstance(post_block, dict) else ""
        except Exception as e:
            return FixedBlockSampleResult(
                anchor=anchor,
                status=SamplingStatus.INCOMPLETE,
                observations=observations,
                error_message=f"RPC error during post-call verification: {e}",
                pre_block_hash=pre_hash,
            )

        if post_hash != anchor.block_hash.lower():
            return FixedBlockSampleResult(
                anchor=anchor,
                status=SamplingStatus.HASH_DRIFT,
                observations=observations,
                error_message=f"Post-call block hash drift: expected {anchor.block_hash}, got {post_hash}",
                pre_block_hash=pre_hash,
                post_block_hash=post_hash,
            )

        return FixedBlockSampleResult(
            anchor=anchor,
            status=SamplingStatus.SUCCESS,
            observations=observations,
            pre_block_hash=pre_hash,
            post_block_hash=post_hash,
        )
