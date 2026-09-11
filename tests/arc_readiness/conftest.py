"""Pytest fixtures and configuration for arc_readiness test suite."""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture
def sample_network_identity_data() -> dict[str, Any]:
    """Sample valid Arc testnet identity dictionary."""
    return {
        "chain_id": 5042002,
        "network_name": "Arc Testnet",
        "rpc_endpoint": "https://rpc.testnet.arc.io",
        "native_currency": "USDC",
        "native_decimals": 18,
        "erc20_usdc_decimals": 6,
        "erc20_usdc_address": "0x3600000000000000000000000000000000000000",
        "verification_status": "unverified",
        "known_stage": "testnet",
        "source_evidence_ref": "https://docs.arc.io/arc/references/connect-to-arc",
    }


@pytest.fixture
def sample_balance_observation_data() -> dict[str, Any]:
    """Sample valid dual-interface balance observation."""
    return {
        "account_address": "0x1111111111111111111111111111111111111111",
        "chain_id": 5042002,
        "block_number": 1234567,
        "block_hash": "0x" + "aa" * 32,
        "native_atoms": 1000000000000000123,
        "erc20_atoms": 1000000,
        "balance_domain_id": "arc:5042002:usdc_canonical",
        "dust_atoms": 123,
        "verified_consistency": True,
        "stale_or_incomplete_reasons": [],
    }
