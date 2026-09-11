"""Arc Protocol Execution Deployment Binding (T25)

Binds reviewed and verified Arc mainnet/testnet deployments (5042 / 5042002) to W5 execution planning:
- Rejects foreign chain IDs (e.g. Robinhood 4663)
- Rejects Robinhood canonical addresses (UniversalRouter, Permit2)
- Integrates with arc_markets.deployments DeploymentsRegistry
- Generates deterministic deployment_fingerprint
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from arc_readiness.errors import ArcNetworkMismatchError, ArcValidationError

_HEX_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Banned Robinhood 4663 Canonical Addresses to guarantee decoupling
BANNED_ROBINHOOD_ADDRESSES = frozenset(
    {
        "0x8876789976decbfcbbbe364623c63652db8c0904".lower(),
        "0x000000000022d473030f116ddee9f6b43ac78ba3".lower(),
    }
)


class DeploymentBindingError(ValueError):
    """Raised when execution deployment parameters violate Arc invariants."""


@dataclass(frozen=True, slots=True)
class ArcExecutionDeploymentBinding:
    """Verified deployment binding for Arc execution planning and encoding."""

    chain_id: int
    router_address: str
    permit2_address: str | None = None
    v4_vault_address: str | None = None
    v4_pool_manager_address: str | None = None
    deployment_fingerprint: str = ""

    def __post_init__(self) -> None:
        if self.chain_id not in (5042, 5042002):
            raise DeploymentBindingError(
                f"Invalid Arc chain_id {self.chain_id}; must be 5042 (mainnet) or 5042002 (testnet)"
            )

        # Validate Router address format and ban Robinhood addresses
        if not _HEX_ADDR_RE.match(self.router_address):
            raise DeploymentBindingError(f"Invalid router address format: {self.router_address}")
        if self.router_address.lower() in BANNED_ROBINHOOD_ADDRESSES:
            raise DeploymentBindingError(
                f"Robinhood canonical router address rejected on Arc: {self.router_address}"
            )

        if self.permit2_address is not None:
            if not _HEX_ADDR_RE.match(self.permit2_address):
                raise DeploymentBindingError(f"Invalid permit2 address format: {self.permit2_address}")
            if self.permit2_address.lower() in BANNED_ROBINHOOD_ADDRESSES:
                raise DeploymentBindingError(
                    f"Robinhood canonical permit2 address rejected on Arc: {self.permit2_address}"
                )

        if self.v4_vault_address is not None and not _HEX_ADDR_RE.match(self.v4_vault_address):
            raise DeploymentBindingError(f"Invalid v4_vault_address format: {self.v4_vault_address}")

        if self.v4_pool_manager_address is not None and not _HEX_ADDR_RE.match(self.v4_pool_manager_address):
            raise DeploymentBindingError(
                f"Invalid v4_pool_manager_address format: {self.v4_pool_manager_address}"
            )

        # Compute deterministic fingerprint
        seed = f"{self.chain_id}:{self.router_address.lower()}:{self.permit2_address or ''}:{self.v4_vault_address or ''}:{self.v4_pool_manager_address or ''}"
        fp = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        object.__setattr__(self, "deployment_fingerprint", fp)
