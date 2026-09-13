"""Arc Network Profile definitions, isolation guards, and configuration loaders."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from arbitrage_contracts.arc_extensions import BlockDomain, NetworkProfile
from arc_readiness.errors import ArcNetworkMismatchError, ArcValidationError
from arc_readiness.network import ARC_MAINNET_CHAIN_ID, ARC_TESTNET_CHAIN_ID


def get_mainnet_profile(
    rpc_endpoints: tuple[str, ...] = ("https://rpc.arc.io",),
    max_trade_usd: float = 500.0,
) -> NetworkProfile:
    """Instantiate the standard Arc Mainnet profile (5042, L1)."""
    return NetworkProfile(
        chain_id=ARC_MAINNET_CHAIN_ID,
        name="arc-mainnet",
        block_domain=BlockDomain.L1,
        rpc_endpoints=rpc_endpoints,
        native_asset_domain="arc-usdc-native",
        max_trade_usd=max_trade_usd,
        is_testnet=False,
    )


def get_testnet_profile(
    rpc_endpoints: tuple[str, ...] = ("https://rpc.testnet.arc.io",),
    max_trade_usd: float = 500.0,
) -> NetworkProfile:
    """Instantiate the standard Arc Testnet profile (5042002, L1)."""
    return NetworkProfile(
        chain_id=ARC_TESTNET_CHAIN_ID,
        name="arc-testnet",
        block_domain=BlockDomain.L1,
        rpc_endpoints=rpc_endpoints,
        native_asset_domain="arc-usdc-testnet",
        max_trade_usd=max_trade_usd,
        is_testnet=True,
    )


def load_network_profile(
    source: str | Path | dict[str, Any],
) -> NetworkProfile:
    """Load and validate a NetworkProfile from a JSON file path or dictionary.

    Fails-closed on invalid schema, missing fields, or cross-chain configuration pollution.
    """
    raw_data: dict[str, Any]
    if isinstance(source, (str, Path)):
        cfg_path = Path(source)
        if not cfg_path.exists():
            raise ArcValidationError(f"Network profile configuration file not found: {cfg_path}")
        try:
            with open(cfg_path, encoding="utf-8") as f:
                raw_data = json.load(f)
        except Exception as e:
            raise ArcValidationError(f"Failed to parse network profile JSON from {cfg_path}: {e}") from e
    elif isinstance(source, dict):
        raw_data = source
    else:
        raise ArcValidationError(f"Unsupported profile source type: {type(source).__name__}")

    try:
        chain_id = int(raw_data["chain_id"])
        name = str(raw_data["name"])
        raw_domain = str(raw_data.get("block_domain", "l1")).lower()
        block_domain = BlockDomain(raw_domain)
        raw_endpoints = raw_data.get("rpc_endpoints", [])
        rpc_endpoints = tuple(str(ep) for ep in raw_endpoints)
        native_asset_domain = str(raw_data["native_asset_domain"])
        max_trade_usd = float(raw_data.get("max_trade_usd", 500.0))
        is_testnet = bool(raw_data.get("is_testnet", chain_id == ARC_TESTNET_CHAIN_ID))
    except (KeyError, ValueError, TypeError) as e:
        raise ArcValidationError(f"Invalid network profile payload: {e}") from e

    return NetworkProfile(
        chain_id=chain_id,
        name=name,
        block_domain=block_domain,
        rpc_endpoints=rpc_endpoints,
        native_asset_domain=native_asset_domain,
        max_trade_usd=max_trade_usd,
        is_testnet=is_testnet,
    )


def assert_venue_profile_isolation(
    venue_address: str,
    chain_id_a: int,
    chain_id_b: int,
) -> None:
    """Ensure identical addresses across different networks are never conflated."""
    if chain_id_a != chain_id_b:
        raise ArcNetworkMismatchError(
            f"Cross-network venue isolation violation: Venue address {venue_address} "
            f"cannot be shared between chain {chain_id_a} and chain {chain_id_b}."
        )
