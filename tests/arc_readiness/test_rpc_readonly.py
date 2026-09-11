"""Tests covering readonly RPC transport, failure classification, and fixed-block sampling (C14-C17)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from arc_readiness.errors import ArcValidationError
from arc_readiness.models import ArcNetworkIdentity
from arc_readiness.recording import classify_rpc_failure, sanitize_rpc_payload
from arc_readiness.rpc_readonly import (
    ALLOWED_READONLY_METHODS,
    FixedBlockSampler,
    ReadOnlyRpcTransport,
)

TEST_ADDR = "0x" + "11" * 20
ANCHOR_HASH = "0x" + "aa" * 32
ANCHOR_BLOCK = 12345


# ==============================================================================
# C14: Structured Failure Classification & Sanitization
# ==============================================================================


@pytest.mark.parametrize(
    "status,rpc_err,msg,expected",
    [
        (429, None, "Too Many Requests", "RATE_LIMITED"),
        (None, None, "rate limit reached", "RATE_LIMITED"),
        (408, None, "Request Timeout", "TIMEOUT"),
        (None, None, "Connection timed out", "TIMEOUT"),
        (None, -32601, "the method eth_foo does not exist", "METHOD_NOT_FOUND"),
        (None, 3, "execution reverted: zero address not allowed", "EXECUTION_REVERTED"),
        (500, None, "Internal Server Error", "TRANSPORT_FAILURE"),
    ],
)
def test_c14_failure_classification(
    status: int | None, rpc_err: int | None, msg: str, expected: str
) -> None:
    """C14: RPC errors are classified deterministically without collapsing into generic revert."""
    res = classify_rpc_failure(
        status_code=status,
        rpc_error_code=rpc_err,
        error_message=msg,
    )
    assert res == expected


def test_c14_credential_sanitization() -> None:
    """C14: Sensitive keys are redacted recursively from RPC payloads."""
    dirty_payload = {
        "method": "eth_call",
        "params": [{"to": TEST_ADDR}],
        "private_key": "0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
        "nested": {
            "Authorization": "Bearer secret-token-12345",
            "safe_field": 100,
        },
    }
    clean = sanitize_rpc_payload(dirty_payload)
    assert clean["private_key"] == "<REDACTED>"
    assert clean["nested"]["Authorization"] == "<REDACTED>"
    assert clean["nested"]["safe_field"] == 100


# ==============================================================================
# C15: Readonly Allowlist Enforcement & Mutating Method Rejection
# ==============================================================================


def test_c15_readonly_methods_allowed() -> None:
    """C15: Allowed readonly methods dispatch to handler."""

    def mock_handler(method: str, params: Sequence[Any]) -> Any:
        return f"result_of_{method}"

    transport = ReadOnlyRpcTransport(
        endpoint_url="https://rpc.testnet.arc.io",
        handler=mock_handler,
    )

    for m in ALLOWED_READONLY_METHODS:
        res = transport.request(m, [])
        assert res == f"result_of_{m}"


@pytest.mark.parametrize(
    "mutating_method",
    [
        "eth_sendRawTransaction",
        "eth_sendTransaction",
        "eth_sign",
        "personal_sign",
        "wallet_addEthereumChain",
        "arbitrary_dangerous_call",
    ],
)
def test_c15_mutating_methods_intercepted(mutating_method: str) -> None:
    """C15: Mutating or non-allowlisted RPC methods fail-closed before network dispatch."""
    transport = ReadOnlyRpcTransport(endpoint_url="https://rpc.testnet.arc.io")
    with pytest.raises(ArcValidationError, match="Prohibited"):
        transport.request(mutating_method, [])


# ==============================================================================
# C16: Fixed Block Sampling & Drift Detection
# ==============================================================================


def test_c16_fixed_block_sampling_and_drift_rejection() -> None:
    """C16: Fixed block sampling asserts hash equality; hash drift rejects fallback to latest."""

    def mock_handler(method: str, params: Sequence[Any]) -> Any:
        assert method == "eth_getBalance"
        assert params[1] == hex(ANCHOR_BLOCK)
        return "0xde0b6b3a7640000"  # 1 USDC in atoms

    transport = ReadOnlyRpcTransport(
        endpoint_url="https://rpc.testnet.arc.io",
        handler=mock_handler,
    )
    sampler = FixedBlockSampler(
        transport=transport,
        anchor_block_number=ANCHOR_BLOCK,
        anchor_block_hash=ANCHOR_HASH,
    )

    # Sampling with matching block hash succeeds
    bal = sampler.sample_balance(account_address=TEST_ADDR, observed_block_hash=ANCHOR_HASH)
    assert bal == 10**18

    # Sampling with drifted block hash fails-closed
    drifted_hash = "0x" + "ff" * 32
    with pytest.raises(ArcValidationError, match="Block hash drift detected"):
        sampler.sample_balance(account_address=TEST_ADDR, observed_block_hash=drifted_hash)


# ==============================================================================
# C17: Synthetic Data vs Live Verified Boundaries
# ==============================================================================


def test_c17_synthetic_data_cannot_claim_readonly_verified() -> None:
    """C17: Synthetic models must remain unverified until corroborated against real endpoints."""
    ident = ArcNetworkIdentity(
        chain_id=5042002,
        network_name="Arc Synthetic",
        rpc_endpoint="http://localhost:8545",
        verification_status="synthetic_verified",  # Not readonly_verified
    )
    assert ident.verification_status != "readonly_verified"


def test_fixture_driven_rpc_vectors() -> None:
    """Verify C14 taxonomy cases from rpc_vectors.json fixture."""
    fixture_path = (
        Path(__file__).resolve().parents[1] / "fixtures" / "arc_readiness" / "rpc_vectors.json"
    )
    data = json.loads(fixture_path.read_text(encoding="utf-8"))

    c14_group = next(v for v in data["vectors"] if v["id"] == "C14_failure_taxonomy_cases")
    for case in c14_group["cases"]:
        res = classify_rpc_failure(
            status_code=case.get("status_code"),
            rpc_error_code=case.get("rpc_error_code"),
            error_message=case.get("error_message"),
        )
        assert res == case["expected_category"]
