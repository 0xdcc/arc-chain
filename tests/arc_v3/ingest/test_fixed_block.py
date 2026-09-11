"""Tests for T08: Fixed-Block Multi-Call Sampling, Block Anchors, and Capability Probes."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from unittest.mock import MagicMock

import pytest

from arc_readiness.capabilities import (
    EndpointCapabilityReport,
    probe_endpoint_capabilities,
)
from arc_readiness.errors import (
    ArcNetworkMismatchError,
    ArcValidationError,
)
from arc_readiness.fixed_block import (
    BlockAnchor,
    FixedBlockSampler,
    SamplingStatus,
)
from arc_readiness.rpc_readonly import ReadOnlyRpcTransport

_SAMPLE_BLOCK_HASH = "0x" + "a1" * 32
_SAMPLE_PARENT_HASH = "0x" + "b2" * 32


class TestFixedBlockSampling:
    """Test suite for T08 fixed-block multi-call sampling and hash anchoring."""

    def test_block_anchor_immutability_and_validation(self) -> None:
        anchor = BlockAnchor(
            block_number=100,
            block_hash=_SAMPLE_BLOCK_HASH,
            parent_hash=_SAMPLE_PARENT_HASH,
            timestamp=1700000000,
            chain_id=5042,
        )
        assert anchor.block_number == 100
        assert anchor.chain_id == 5042

        # Invalid chain_id:
        with pytest.raises(ArcValidationError, match="Invalid Arc chain_id"):
            BlockAnchor(
                block_number=100,
                block_hash=_SAMPLE_BLOCK_HASH,
                parent_hash=_SAMPLE_PARENT_HASH,
                timestamp=1700000000,
                chain_id=4663,
            )

        # Invalid block hash:
        with pytest.raises(ArcValidationError, match="Invalid 66-char hex block_hash"):
            BlockAnchor(
                block_number=100,
                block_hash="0xshort",
                parent_hash=_SAMPLE_PARENT_HASH,
                timestamp=1700000000,
                chain_id=5042,
            )

    def test_get_block_anchor_success(self) -> None:
        mock_handler = MagicMock()
        mock_handler.return_value = {
            "number": "0x64",  # 100
            "hash": _SAMPLE_BLOCK_HASH,
            "parentHash": _SAMPLE_PARENT_HASH,
            "timestamp": "0x654321",
        }
        transport = ReadOnlyRpcTransport("https://rpc.arc.io", handler=mock_handler)
        sampler = FixedBlockSampler(transport, chain_id=5042)

        anchor = sampler.get_block_anchor(100)
        assert anchor.block_number == 100
        assert anchor.block_hash == _SAMPLE_BLOCK_HASH

    def test_get_block_anchor_rejects_latest(self) -> None:
        transport = ReadOnlyRpcTransport("https://rpc.arc.io", handler=MagicMock())
        sampler = FixedBlockSampler(transport, chain_id=5042)

        with pytest.raises(ArcValidationError, match="'latest' is strictly forbidden"):
            sampler.get_block_anchor("latest")

    def test_sample_state_multicall_success_and_consistency(self) -> None:
        mock_handler = MagicMock()
        block_dict = {
            "number": "0x64",
            "hash": _SAMPLE_BLOCK_HASH,
            "parentHash": _SAMPLE_PARENT_HASH,
            "timestamp": "0x654321",
        }

        def fake_handler(method: str, params: Sequence[Any]) -> Any:
            if method == "eth_getBlockByNumber":
                return block_dict
            if method == "eth_getBalance":
                return "0x1bc16d674ec80000"  # 2 ETH
            if method == "eth_getCode":
                return "0x60806040"
            raise ValueError(f"Unexpected method {method}")

        mock_handler.side_effect = fake_handler
        transport = ReadOnlyRpcTransport("https://rpc.arc.io", handler=mock_handler)
        sampler = FixedBlockSampler(transport, chain_id=5042)

        anchor = BlockAnchor(
            block_number=100,
            block_hash=_SAMPLE_BLOCK_HASH,
            parent_hash=_SAMPLE_PARENT_HASH,
            timestamp=1700000000,
            chain_id=5042,
        )

        calls = [
            ("user_balance", "eth_getBalance", ["0x1111111111111111111111111111111111111111"]),
            ("pool_code", "eth_getCode", ["0x2222222222222222222222222222222222222222"]),
        ]
        result = sampler.sample_state_multicall(anchor, calls)

        assert result.status == SamplingStatus.SUCCESS
        assert result.is_consistent
        assert "user_balance" in result.observations
        assert "pool_code" in result.observations

    def test_sample_state_multicall_detects_hash_drift(self) -> None:
        mock_handler = MagicMock()
        drifted_hash = "0x" + "ff" * 32

        def fake_handler(method: str, params: Sequence[Any]) -> Any:
            if method == "eth_getBlockByNumber":
                # Returns drifted hash
                return {
                    "number": "0x64",
                    "hash": drifted_hash,
                    "parentHash": _SAMPLE_PARENT_HASH,
                    "timestamp": "0x654321",
                }
            return "0x0"

        mock_handler.side_effect = fake_handler
        transport = ReadOnlyRpcTransport("https://rpc.arc.io", handler=mock_handler)
        sampler = FixedBlockSampler(transport, chain_id=5042)

        anchor = BlockAnchor(
            block_number=100,
            block_hash=_SAMPLE_BLOCK_HASH,
            parent_hash=_SAMPLE_PARENT_HASH,
            timestamp=1700000000,
            chain_id=5042,
        )

        result = sampler.sample_state_multicall(anchor, [("balance", "eth_getBalance", ["0x1111111111111111111111111111111111111111"])])
        assert result.status == SamplingStatus.HASH_DRIFT
        assert not result.is_consistent
        assert "drift" in (result.error_message or "").lower()

    def test_probe_endpoint_capabilities_success(self) -> None:
        def fake_handler(method: str, params: Sequence[Any]) -> Any:
            if method == "eth_chainId":
                return hex(5042)
            if method == "eth_getBlockByNumber":
                return {
                    "number": hex(1000),
                    "hash": _SAMPLE_BLOCK_HASH,
                    "parentHash": _SAMPLE_PARENT_HASH,
                    "timestamp": "0x654321",
                }
            raise ValueError(f"Unexpected method {method}")

        mock_handler = MagicMock(side_effect=fake_handler)
        transport = ReadOnlyRpcTransport("https://rpc.arc.io", handler=mock_handler)

        report = probe_endpoint_capabilities(transport, expected_chain_id=5042)
        assert report.chain_id == 5042
        assert report.supports_fixed_block is True
        assert report.supports_trace is False  # trace strictly false by default

    def test_probe_endpoint_capabilities_rejects_wrong_chain(self) -> None:
        mock_handler = MagicMock()
        mock_handler.return_value = hex(5042002)  # Testnet chain
        transport = ReadOnlyRpcTransport("https://rpc.testnet.arc.io", handler=mock_handler)

        with pytest.raises(ArcNetworkMismatchError, match="Endpoint returned chainId 5042002, expected 5042"):
            probe_endpoint_capabilities(transport, expected_chain_id=5042)
