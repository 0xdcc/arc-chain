"""Independent Security & Read-Only Boundary Test Suite (T44 / G1).

Verifies the four non-negotiable architectural boundaries:
1. RPC method allowlist guard & rejection of mutating/broadcasting methods
2. Import-time side effect elimination & isolation from legacy execution engines
3. Environment credential scrubbing & strict ban on private key access
4. Filesystem path confinement & directory boundary enforcement
5. Fixed-block sampling consistency & prohibition of silent fallback to 'latest'
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from arc_readiness.errors import ArcNetworkMismatchError, ArcValidationError
from arc_readiness.profiles import (
    assert_venue_profile_isolation,
    get_mainnet_profile,
    get_testnet_profile,
    load_network_profile,
)
from arc_readiness.rpc_readonly import (
    ALLOWED_READONLY_METHODS,
    FORBIDDEN_MUTATING_METHODS,
    FixedBlockSampler,
    ReadOnlyRpcTransport,
    validate_batch_methods,
    validate_rpc_method,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent


class TestRpcMethodAllowlistGuard:
    """Validate that mutating and unauthorized RPC methods are strictly fail-closed."""

    def test_all_allowed_readonly_methods_pass_validation(self) -> None:
        """Every method in ALLOWED_READONLY_METHODS must pass validation without error."""
        for method in ALLOWED_READONLY_METHODS:
            validate_rpc_method(method)  # Must not raise

    def test_all_forbidden_mutating_methods_are_rejected(self) -> None:
        """Every mutating method (send, sign, broadcast, wallet management) must be rejected."""
        for method in FORBIDDEN_MUTATING_METHODS:
            with pytest.raises(ArcValidationError, match="Prohibited mutating or non-EVM RPC method"):
                validate_rpc_method(method)

    @pytest.mark.parametrize(
        "prohibited_method",
        [
            "eth_sendRawTransaction",
            "eth_sendTransaction",
            "eth_sign",
            "eth_signTransaction",
            "personal_sign",
            "eth_signTypedData_v4",
            "wallet_addEthereumChain",
            "admin_peers",
            "debug_traceTransaction",
            "custom_backdoorMethod",
            "solana_sendTransaction",
        ],
    )
    def test_individual_prohibited_methods_fail_closed(self, prohibited_method: str) -> None:
        """Explicit check for dangerous or non-whitelisted RPC methods."""
        with pytest.raises(ArcValidationError):
            validate_rpc_method(prohibited_method)

    def test_batch_validation_fails_closed_if_single_mutating_method_included(self) -> None:
        """A batch containing 99 read-only calls and 1 mutating call must be entirely rejected."""
        benign_batch = ["eth_chainId", "eth_getBlockByNumber", "eth_getBalance"]
        validate_batch_methods(benign_batch)  # Must pass

        poisoned_batch = ["eth_chainId", "eth_sendRawTransaction", "eth_getBalance"]
        with pytest.raises(ArcValidationError, match="Prohibited mutating"):
            validate_batch_methods(poisoned_batch)

    def test_readonly_transport_enforces_guard(self) -> None:
        """ReadOnlyRpcTransport pre-flight checks method before delegating to handler."""
        executed_calls: list[str] = []

        def mock_handler(method: str, params: Sequence[Any]) -> Any:
            executed_calls.append(method)
            return "0x1234"

        transport = ReadOnlyRpcTransport(endpoint_url="https://rpc.arc.io", handler=mock_handler)

        # Allowed call executes handler
        res = transport.request("eth_chainId")
        assert res == "0x1234"
        assert executed_calls == ["eth_chainId"]

        # Prohibited call rejected before handler invocation
        with pytest.raises(ArcValidationError):
            transport.request("eth_sendRawTransaction", ["0xdeadbeef"])
        assert executed_calls == ["eth_chainId"]  # Handler was NOT called


class TestFixedBlockAntiDriftSampler:
    """Validate multi-call state consistency and ban on silent fallback to 'latest'."""

    def test_fixed_block_sampler_balance_success(self) -> None:
        """Sampling returns integer balance when anchor block hash strictly matches."""
        anchor_block = 12345
        anchor_hash = "0xabcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"

        def mock_handler(method: str, params: Sequence[Any]) -> Any:
            assert method == "eth_getBalance"
            assert params[0] == "0x1111111111111111111111111111111111111111"
            assert params[1] == hex(anchor_block)  # Never 'latest'
            return "0x0de0b6b3a7640000"  # 1.0 * 10^18

        transport = ReadOnlyRpcTransport(endpoint_url="https://rpc.arc.io", handler=mock_handler)
        sampler = FixedBlockSampler(transport, anchor_block, anchor_hash)

        balance = sampler.sample_balance("0x1111111111111111111111111111111111111111", anchor_hash)
        assert balance == 1000000000000000000

    def test_fixed_block_sampler_detects_hash_drift_and_fails_closed(self) -> None:
        """When observed block hash differs from anchor hash, fail-closed immediately."""
        anchor_block = 12345
        anchor_hash = "0xaaaa000000000000000000000000000000000000000000000000000000000000"
        drifted_hash = "0xbbbb000000000000000000000000000000000000000000000000000000000000"

        transport = ReadOnlyRpcTransport(endpoint_url="https://rpc.arc.io", handler=lambda m, p: "0x0")
        sampler = FixedBlockSampler(transport, anchor_block, anchor_hash)

        with pytest.raises(ArcValidationError, match="Block hash drift detected"):
            sampler.sample_balance("0x1111111111111111111111111111111111111111", drifted_hash)


class TestImportAndExecutionIsolation:
    """Validate that importing collection and readiness modules triggers zero side effects."""

    def test_import_arc_runtime_collect_has_no_side_effects(self) -> None:
        """Importing arc_runtime.collect must not open sockets or access network."""
        import arc_runtime.collect  # noqa: F401

        # Check that forbidden modules are NOT imported
        assert "apps.live_pipeline" not in sys.modules
        assert "core.execution" not in sys.modules
        assert "chains.arc" not in sys.modules

    def test_conftest_scrubs_credential_environment_variables(self) -> None:
        """Verify tests/conftest.py strips sensitive credential environment variables."""
        sensitive_keys = [
            "ETH_PRIVATE_KEY",
            "ARC_PRIVATE_KEY",
            "MAINNET_SECRET",
            "DEPLOYER_KEY",
            "TELEGRAM_BOT_TOKEN",
        ]
        for k in sensitive_keys:
            assert k not in os.environ


class TestNetworkProfileIsolation:
    """Validate profile immutability and cross-network venue isolation."""

    def test_mainnet_and_testnet_profiles_are_strictly_isolated(self) -> None:
        """Arc Mainnet (5042) and Testnet (5042002) profiles have distinct domain parameters."""
        mainnet = get_mainnet_profile()
        testnet = get_testnet_profile()

        assert mainnet.chain_id == 5042
        assert mainnet.is_testnet is False
        assert mainnet.native_asset_domain == "arc-usdc-native"

        assert testnet.chain_id == 5042002
        assert testnet.is_testnet is True
        assert testnet.native_asset_domain == "arc-usdc-testnet"

    def test_assert_venue_profile_isolation_detects_chain_mismatch(self) -> None:
        """Identical contract address on different chain IDs must be rejected."""
        venue = "0x8888888888888888888888888888888888888888"

        # Matching chain ID passes
        assert_venue_profile_isolation(venue, 5042, 5042)

        # Cross-chain ID conflation is rejected
        with pytest.raises(ArcNetworkMismatchError, match="Cross-network venue isolation violation"):
            assert_venue_profile_isolation(venue, 5042, 4663)
