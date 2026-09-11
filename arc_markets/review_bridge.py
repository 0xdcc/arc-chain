"""Bridge connecting discovery configurations to the Arc Deployments Registry."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from arc_markets.deployments import (
    ArcDeploymentRecord,
    ContractRole,
    DeploymentStatus,
    DeploymentsRegistry,
)
from arc_readiness.errors import ArcNetworkMismatchError, ArcValidationError

FORBIDDEN_FOREIGN_CHAIN_IDS: frozenset[int] = frozenset({4663, 1, 56, 137, 8453, 42161})


class ArcDeploymentReviewBridge:
    """Parses, validates, and stages Arc deployment discovery venues.

    Enforces:
    - Zero Robinhood (4663) or foreign chain address importation.
    - Strict chain_id match (no testnet addresses in mainnet registry).
    - Rejection of duplicate venue aliases or frontend mirror registrations.
    """

    @classmethod
    def load_venues_config(
        cls,
        config_source: str | Path | dict[str, Any],
        target_chain_id: int = 5042,
    ) -> DeploymentsRegistry:
        """Parse configuration and populate a DeploymentsRegistry with PENDING_REVIEW records."""
        raw_data: dict[str, Any]
        if isinstance(config_source, (str, Path)):
            p = Path(config_source)
            if not p.exists():
                raise ArcValidationError(f"Venues config file not found: {p}")
            with open(p, encoding="utf-8") as f:
                raw_data = json.load(f)
        elif isinstance(config_source, dict):
            raw_data = config_source
        else:
            raise ArcValidationError(f"Unsupported config source: {type(config_source).__name__}")

        declared_chain_id = int(raw_data.get("chain_id", target_chain_id))
        if declared_chain_id != target_chain_id:
            raise ArcNetworkMismatchError(
                f"Config declared chain_id {declared_chain_id} does not match target registry chain {target_chain_id}"
            )

        registry = DeploymentsRegistry(target_chain_id=target_chain_id)
        venues = raw_data.get("venues", [])
        if not isinstance(venues, list):
            raise ArcValidationError("'venues' field must be a list of deployment entries")

        seen_token_symbols: set[str] = set()

        for idx, venue in enumerate(venues):
            if not isinstance(venue, dict):
                raise ArcValidationError(f"Venue item {idx} must be a dictionary")

            name = str(venue.get("name", "")).strip()
            raw_role = str(venue.get("role", "")).lower()
            try:
                role = ContractRole(raw_role)
            except ValueError as e:
                raise ArcValidationError(f"Venue '{name}' has invalid role '{raw_role}'") from e

            address = str(venue.get("address", "")).strip()
            item_chain_id = int(venue.get("chain_id", target_chain_id))

            # Red line: foreign chain detection
            if item_chain_id in FORBIDDEN_FOREIGN_CHAIN_IDS:
                raise ArcNetworkMismatchError(
                    f"Forbidden foreign chain ID {item_chain_id} in venue '{name}'. "
                    "Robinhood (4663) and external addresses are strictly prohibited from entering Arc."
                )

            if item_chain_id != target_chain_id:
                raise ArcNetworkMismatchError(
                    f"Cross-network address import rejected: venue '{name}' belongs to chain {item_chain_id}, "
                    f"cannot be staged for Arc target chain {target_chain_id}."
                )

            # Check duplicate token symbol registrations
            token_symbol = venue.get("token_symbol")
            if token_symbol:
                norm_sym = str(token_symbol).strip().upper()
                if norm_sym in seen_token_symbols:
                    raise ArcValidationError(
                        f"Duplicate token symbol '{norm_sym}' detected across independent venues. "
                        "Same-symbol tokens must be uniquely disambiguated."
                    )
                seen_token_symbols.add(norm_sym)

            abi_hash = str(venue.get("abi_hash", ""))
            source_proof = str(venue.get("source_proof", "discovery:raw_config"))
            impl_addr = venue.get("implementation_address")
            valid_from = int(venue.get("valid_from_block", 0))
            is_precompile = bool(venue.get("is_system_precompile", False))
            aliases = tuple(str(a) for a in venue.get("aliases", []))

            record = ArcDeploymentRecord(
                name=name,
                role=role,
                address=address,
                chain_id=item_chain_id,
                abi_hash=abi_hash,
                source_proof=source_proof,
                implementation_address=impl_addr,
                valid_from_block=valid_from,
                status=DeploymentStatus.PENDING_REVIEW,
                is_system_precompile=is_precompile,
            )
            registry.register(record, aliases=aliases)

        return registry
