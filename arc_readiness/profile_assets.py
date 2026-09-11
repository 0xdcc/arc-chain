"""Arc Asset Profile definitions, network affinity validation, and verified asset registry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from arc_readiness.errors import ArcNetworkMismatchError, ArcValidationError
from arc_readiness.network import ARC_MAINNET_CHAIN_ID, ARC_TESTNET_CHAIN_ID

ARC_UNIFIED_USDC_BALANCE_DOMAIN = "arc_usdc_unified"

# Canonical Arc Mainnet addresses
ARC_MAINNET_USDC_ERC20 = "0x0000000000000000000000000000000000000001"
ARC_MAINNET_NATIVE_IDENTIFIER = "usdc_native"

# Testnet constants (must never be accepted as verified on mainnet)
ARC_TESTNET_USDC_ERC20 = "0x0000000000000000000000000000000005042002"
ARC_TESTNET_EURC_ERC20 = "0x000000000000000000000000000000000e042002"


@dataclass(frozen=True)
class ArcAssetProfile:
    """Immutable metadata profile binding an asset to its chain and decimal structure."""

    symbol: str
    chain_id: int
    contract_address: str | None
    native_identifier: str | None
    decimals: int
    balance_domain_id: str
    is_testnet: bool

    def __post_init__(self) -> None:
        if self.chain_id not in (ARC_MAINNET_CHAIN_ID, ARC_TESTNET_CHAIN_ID):
            raise ArcNetworkMismatchError(f"Unsupported Arc chain_id: {self.chain_id}")
        if self.chain_id == ARC_MAINNET_CHAIN_ID and self.is_testnet:
            raise ArcValidationError("Mainnet asset cannot be flagged as is_testnet=True.")
        if self.chain_id == ARC_TESTNET_CHAIN_ID and not self.is_testnet:
            raise ArcValidationError("Testnet asset must have is_testnet=True.")
        if self.contract_address is None and self.native_identifier is None:
            raise ArcValidationError("Asset must specify either contract_address or native_identifier.")


class ArcAssetRegistry:
    """Maintains verified assets for Arc profiles, enforcing complete network segregation."""

    def __init__(self, chain_id: int = ARC_MAINNET_CHAIN_ID) -> None:
        self.chain_id = chain_id
        self._assets: dict[str, ArcAssetProfile] = {}
        self._initialize_defaults()

    def _initialize_defaults(self) -> None:
        if self.chain_id == ARC_MAINNET_CHAIN_ID:
            self._register(
                ArcAssetProfile(
                    symbol="USDC",
                    chain_id=ARC_MAINNET_CHAIN_ID,
                    contract_address=ARC_MAINNET_USDC_ERC20,
                    native_identifier=None,
                    decimals=6,
                    balance_domain_id=ARC_UNIFIED_USDC_BALANCE_DOMAIN,
                    is_testnet=False,
                )
            )
            self._register(
                ArcAssetProfile(
                    symbol="USDC_NATIVE",
                    chain_id=ARC_MAINNET_CHAIN_ID,
                    contract_address=None,
                    native_identifier=ARC_MAINNET_NATIVE_IDENTIFIER,
                    decimals=18,
                    balance_domain_id=ARC_UNIFIED_USDC_BALANCE_DOMAIN,
                    is_testnet=False,
                )
            )
        else:
            self._register(
                ArcAssetProfile(
                    symbol="USDC",
                    chain_id=ARC_TESTNET_CHAIN_ID,
                    contract_address=ARC_TESTNET_USDC_ERC20,
                    native_identifier=None,
                    decimals=6,
                    balance_domain_id="arc_testnet_usdc_unified",
                    is_testnet=True,
                )
            )

    def _register(self, profile: ArcAssetProfile) -> None:
        key = (profile.contract_address or profile.native_identifier or "").lower()
        self._assets[key] = profile

    def lookup_asset(self, identifier: str) -> ArcAssetProfile | None:
        return self._assets.get(identifier.lower())

    def assert_asset_allowed(self, identifier: str, target_chain_id: int) -> ArcAssetProfile:
        """Validate an asset identifier belongs strictly to target_chain_id."""
        if target_chain_id != self.chain_id:
            raise ArcNetworkMismatchError(
                f"Asset lookup chain mismatch: target={target_chain_id}, registry={self.chain_id}"
            )
        # Prevent testnet EURC or testnet USDC from masquerading as verified mainnet assets
        norm = identifier.lower()
        if target_chain_id == ARC_MAINNET_CHAIN_ID:
            if norm in (ARC_TESTNET_EURC_ERC20.lower(), ARC_TESTNET_USDC_ERC20.lower()):
                raise ArcNetworkMismatchError(
                    f"Testnet asset constant {identifier} cannot be automatically verified on mainnet (5042)."
                )

        prof = self.lookup_asset(identifier)
        if prof is None:
            raise ArcValidationError(f"Asset '{identifier}' is unverified in Arc chain {self.chain_id} registry.")
        return prof
