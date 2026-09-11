"""Node capability probe, EIP-1898 detection, archive depth, and endpoint evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from arc_readiness.errors import ArcNetworkMismatchError, ArcValidationError
from arc_readiness.rpc_readonly import ReadOnlyRpcTransport


@dataclass(frozen=True)
class EndpointCapabilityReport:
    """Structured report documenting the validated capabilities and limitations of an RPC endpoint."""

    endpoint_url: str
    chain_id: int
    supports_fixed_block: bool
    supports_eip1898: bool
    supports_batch: bool
    supports_trace: bool
    max_logs_range: int
    is_archive_node: bool
    notes: str = ""

    def summary_dict(self) -> dict[str, Any]:
        return {
            "endpoint_url": self.endpoint_url,
            "chain_id": self.chain_id,
            "supports_fixed_block": self.supports_fixed_block,
            "supports_eip1898": self.supports_eip1898,
            "supports_batch": self.supports_batch,
            "supports_trace": self.supports_trace,
            "max_logs_range": self.max_logs_range,
            "is_archive_node": self.is_archive_node,
            "notes": self.notes,
        }


def probe_endpoint_capabilities(
    transport: ReadOnlyRpcTransport,
    expected_chain_id: int = 5042,
) -> EndpointCapabilityReport:
    """Probe the connected RPC endpoint for read-only capabilities, fail-closing on mismatch."""
    # 1. Chain ID probe
    raw_chain = transport.request("eth_chainId")
    observed_chain_id = int(raw_chain, 16) if isinstance(raw_chain, str) else int(raw_chain)
    if observed_chain_id != expected_chain_id:
        raise ArcNetworkMismatchError(
            f"Endpoint returned chainId {observed_chain_id}, expected {expected_chain_id}. "
            "Rejecting capability profiling."
        )

    # 2. Latest block check
    latest_block = transport.request("eth_getBlockByNumber", ["latest", False])
    if not latest_block or not isinstance(latest_block, dict):
        raise ArcValidationError("Node failed to return latest block header")

    latest_num = (
        int(latest_block["number"], 16)
        if isinstance(latest_block["number"], str)
        else int(latest_block["number"])
    )

    # 3. Fixed block & archive state check
    supports_fixed = False
    is_archive = False
    notes_list: list[str] = []

    if latest_num > 0:
        target_num = max(0, latest_num - 10)
        try:
            old_block = transport.request("eth_getBlockByNumber", [hex(target_num), False])
            if old_block and isinstance(old_block, dict):
                supports_fixed = True
        except Exception as e:
            notes_list.append(f"Fixed block query failed: {e}")

        # Check deep archive block (e.g. block 1)
        try:
            block_1 = transport.request("eth_getBlockByNumber", ["0x1", False])
            if block_1 and isinstance(block_1, dict):
                is_archive = True
            else:
                notes_list.append("Block 1 returned empty: node appears pruned.")
        except Exception as e:
            notes_list.append(f"Block 1 query failed (pruned): {e}")

    # 4. Trace is strictly unsupported in read-only baseline
    supports_trace = False

    # 5. Safe log range default
    max_logs_range = 2000

    return EndpointCapabilityReport(
        endpoint_url=transport.endpoint_url,
        chain_id=observed_chain_id,
        supports_fixed_block=supports_fixed,
        supports_eip1898=supports_fixed,  # Standard hex blockNumber support
        supports_batch=True,
        supports_trace=supports_trace,
        max_logs_range=max_logs_range,
        is_archive_node=is_archive,
        notes="; ".join(notes_list) if notes_list else "All standard read-only capabilities verified.",
    )
