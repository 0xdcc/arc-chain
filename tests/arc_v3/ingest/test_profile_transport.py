"""Tests for T07: Explicit Arc Mainnet/Testnet Profiles and Read-Only Transport."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from typing import Any
from unittest.mock import MagicMock

import pytest

from arbitrage_contracts.arc_extensions import BlockDomain, NetworkProfile
from arc_readiness.errors import (
    ArcNetworkMismatchError,
    ArcValidationError,
)
from arc_readiness.http_readonly import (
    ArcCircuitBreakerTrippedError,
    HttpReadOnlyRpcTransport,
)
from arc_readiness.network import (
    ARC_MAINNET_CHAIN_ID,
    ARC_TESTNET_CHAIN_ID,
    validate_block_timestamp_order,
    validate_network_identity,
    validate_profile_alignment,
)
from arc_readiness.profiles import (
    assert_venue_profile_isolation,
    get_mainnet_profile,
    get_testnet_profile,
    load_network_profile,
)
from arc_readiness.rpc_readonly import (
    ReadOnlyRpcTransport,
    validate_batch_methods,
    validate_rpc_method,
)


class TestArcProfileAndTransport:
    """Test suite for explicit Arc Network Profiles, isolation guards, and read-only transport."""

    # 1. Profile Invariants & Separation
    def test_mainnet_testnet_profiles_distinct(self) -> None:
        mainnet = get_mainnet_profile()
        testnet = get_testnet_profile()

        assert mainnet.chain_id == 5042
        assert mainnet.is_testnet is False
        assert mainnet.block_domain == BlockDomain.L1

        assert testnet.chain_id == 5042002
        assert testnet.is_testnet is True
        assert testnet.block_domain == BlockDomain.L1

        assert mainnet.chain_id != testnet.chain_id
        assert mainnet.native_asset_domain != testnet.native_asset_domain

    def test_mainnet_profile_rejects_testnet_flag(self) -> None:
        with pytest.raises(ValueError, match="Mainnet chain_id 5042 cannot be flagged as is_testnet=True"):
            NetworkProfile(
                chain_id=5042,
                name="fake-mainnet",
                block_domain=BlockDomain.L1,
                rpc_endpoints=("https://rpc.arc.io",),
                native_asset_domain="arc-usdc-native",
                is_testnet=True,
            )

    def test_testnet_profile_requires_testnet_flag(self) -> None:
        with pytest.raises(ValueError, match="Testnet chain_id 5042002 must have is_testnet=True"):
            NetworkProfile(
                chain_id=5042002,
                name="fake-testnet",
                block_domain=BlockDomain.L1,
                rpc_endpoints=("https://rpc.testnet.arc.io",),
                native_asset_domain="arc-usdc-testnet",
                is_testnet=False,
            )

    # 2. Network Identity Validation & Fallback Ban
    def test_validate_network_identity_exact(self) -> None:
        validate_network_identity(5042, expected_chain_id=5042)
        validate_network_identity(5042002, expected_chain_id=5042002)

    def test_validate_network_identity_mismatch_fails_closed(self) -> None:
        with pytest.raises(ArcNetworkMismatchError, match="Silent fallback between mainnet and testnet"):
            validate_network_identity(5042002, expected_chain_id=5042)

        with pytest.raises(ArcNetworkMismatchError, match="Silent fallback between mainnet and testnet"):
            validate_network_identity(4663, expected_chain_id=5042)

    def test_validate_profile_alignment_rules(self) -> None:
        mainnet_prof = get_mainnet_profile()
        validate_profile_alignment(mainnet_prof, 5042)

        with pytest.raises(ArcNetworkMismatchError, match="does not match observed chain"):
            validate_profile_alignment(mainnet_prof, 5042002)

    def test_venue_profile_isolation(self) -> None:
        shared_addr = "0x1111111111111111111111111111111111111111"
        assert_venue_profile_isolation(shared_addr, 5042, 5042)  # pass

        with pytest.raises(ArcNetworkMismatchError, match="Cross-network venue isolation violation"):
            assert_venue_profile_isolation(shared_addr, 5042, 5042002)

    # 3. Block Timestamp Order Invariants
    def test_validate_block_timestamp_order(self) -> None:
        # Sub-second blocks can have equal timestamps:
        validate_block_timestamp_order(prev_timestamp=1000, curr_timestamp=1000, prev_block=10, curr_block=11)
        validate_block_timestamp_order(prev_timestamp=1000, curr_timestamp=1001, prev_block=10, curr_block=11)

        # Regressing timestamp:
        with pytest.raises(ArcValidationError, match="timestamps must be non-decreasing"):
            validate_block_timestamp_order(prev_timestamp=1001, curr_timestamp=1000, prev_block=10, curr_block=11)

        # Non-increasing block:
        with pytest.raises(ArcValidationError, match="Block numbers must be strictly increasing"):
            validate_block_timestamp_order(prev_timestamp=1000, curr_timestamp=1001, prev_block=10, curr_block=10)

    # 4. RPC Allowlist & Mutating Method Blocking
    def test_rpc_allowlist_permits_readonly(self) -> None:
        for m in ("eth_getBlockByNumber", "eth_getBalance", "eth_call", "eth_getLogs"):
            validate_rpc_method(m)

    def test_rpc_allowlist_rejects_mutating(self) -> None:
        mutating = ("eth_sendRawTransaction", "eth_sendTransaction", "eth_sign", "personal_sign")
        for m in mutating:
            with pytest.raises(ArcValidationError, match="Write operations strictly forbidden"):
                validate_rpc_method(m)

    def test_batch_methods_rejects_poisoned_batch(self) -> None:
        safe_batch = ["eth_getBlockByNumber", "eth_call", "eth_getBalance"]
        validate_batch_methods(safe_batch)

        poisoned_batch = ["eth_getBlockByNumber", "eth_sendRawTransaction", "eth_call"]
        with pytest.raises(ArcValidationError, match="Write operations strictly forbidden"):
            validate_batch_methods(poisoned_batch)

    # 5. HttpReadOnlyRpcTransport Security Controls
    def test_http_transport_offline_default_blocks_network(self) -> None:
        transport = HttpReadOnlyRpcTransport(
            endpoint_url="https://rpc.arc.io",
            allow_network=False,  # default
        )
        with pytest.raises(ArcValidationError, match="Network access not authorized"):
            transport.request("eth_getBlockByNumber", ["latest", False])

        with pytest.raises(ArcValidationError, match="Network access not authorized"):
            transport.request_batch([("eth_getBlockByNumber", ["latest", False])])

    def test_http_transport_circuit_breaker_tripping(self) -> None:
        # Mock opener that raises network error
        mock_opener = MagicMock()
        mock_opener.open.side_effect = urllib.error.URLError("Connection refused")

        transport = HttpReadOnlyRpcTransport(
            endpoint_url="https://rpc.arc.io",
            max_consecutive_failures=3,
            allow_network=True,
            opener=mock_opener,
        )

        assert not transport.is_circuit_broken

        # First failure:
        with pytest.raises(ArcValidationError, match="RPC request failed"):
            transport.request("eth_getBlockByNumber", ["0x1", False])
        assert not transport.is_circuit_broken

        # Second failure:
        with pytest.raises(ArcValidationError, match="RPC request failed"):
            transport.request("eth_getBlockByNumber", ["0x1", False])
        assert not transport.is_circuit_broken

        # Third failure: trips breaker
        with pytest.raises(ArcCircuitBreakerTrippedError, match="Circuit Breaker TRIPPED"):
            transport.request("eth_getBlockByNumber", ["0x1", False])
        assert bool(transport.is_circuit_broken)

        # Subsequent attempts are instantly halted without network calls:
        with pytest.raises(ArcCircuitBreakerTrippedError, match="Refusing to loop indefinitely"):
            transport.request("eth_getBlockByNumber", ["0x1", False])

    def test_http_transport_successful_response(self) -> None:
        mock_opener = MagicMock()
        resp_payload = {"jsonrpc": "2.0", "id": 1, "result": "0x13b2"}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(resp_payload).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_opener.open.return_value = mock_resp

        transport = HttpReadOnlyRpcTransport(
            endpoint_url="https://rpc.arc.io",
            allow_network=True,
            opener=mock_opener,
        )

        res = transport.request("eth_chainId")
        assert res == "0x13b2"
        assert not transport.is_circuit_broken

    def test_load_network_profile_from_config(self) -> None:
        cfg = {
            "chain_id": 5042,
            "name": "arc-mainnet",
            "block_domain": "l1",
            "rpc_endpoints": ["https://rpc.arc.io"],
            "native_asset_domain": "arc-usdc-native",
            "max_trade_usd": 500.0,
            "is_testnet": False,
        }
        prof = load_network_profile(cfg)
        assert prof.chain_id == 5042
        assert prof.name == "arc-mainnet"
        assert prof.max_trade_usd == 500.0
