"""Tests for T13: Arc Mainnet Deployment Evidence Registration and Review Lifecycle."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arc_markets.deployments import (
    ArcDeploymentRecord,
    ContractRole,
    DeploymentsRegistry,
    DeploymentStatus,
)
from arc_markets.review_bridge import (
    ArcDeploymentReviewBridge,
)
from arc_readiness.errors import (
    ArcNetworkMismatchError,
    ArcValidationError,
)

_VALID_ABI_HASH = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


class TestArcDeploymentsRegistry:
    """Test suite for deployment evidence registration, review status, and foreign chain guards."""

    def test_default_verified_set_is_empty(self) -> None:
        registry = DeploymentsRegistry(target_chain_id=5042)
        assert registry.list_all() == []
        assert registry.list_verified() == []

    def test_register_pending_and_promote_to_verified(self) -> None:
        registry = DeploymentsRegistry(target_chain_id=5042)
        rec = ArcDeploymentRecord(
            name="uniswap_v3_factory",
            role=ContractRole.FACTORY,
            address="0x0000000000000000000000000000000000005042",
            chain_id=5042,
            abi_hash=_VALID_ABI_HASH,
            source_proof="genesis:arc_mainnet_v3_factory",
            valid_from_block=0,
            is_system_precompile=True,
        )
        registry.register(rec)

        assert rec.status == DeploymentStatus.PENDING_REVIEW
        assert registry.get_verified("uniswap_v3_factory") is None
        assert registry.get_any("uniswap_v3_factory") is not None

        # Promote following independent review:
        registry.promote_to_verified(
            name="uniswap_v3_factory",
            audit_evidence_ref="AUDIT-ARC-V3-FACTORY-G1",
        )

        verified = registry.get_verified("uniswap_v3_factory")
        assert verified is not None
        assert verified.status == DeploymentStatus.VERIFIED
        assert "audit:AUDIT-ARC-V3-FACTORY-G1" in verified.source_proof
        assert len(registry.list_verified()) == 1

    def test_system_precompile_requires_explicit_proof(self) -> None:
        # Invalid proof for system precompile:
        with pytest.raises(ArcValidationError, match="requires explicit genesis/spec/consensus proof"):
            ArcDeploymentRecord(
                name="precompile_no_proof",
                role=ContractRole.CUSTOM,
                address="0x0000000000000000000000000000000000000001",
                chain_id=5042,
                abi_hash=_VALID_ABI_HASH,
                source_proof="scanned_from_somewhere",  # not genesis:/spec:/consensus:
                is_system_precompile=True,
            )

    def test_rejection_of_foreign_robinhood_chain_id(self) -> None:
        config = {
            "chain_id": 5042,
            "venues": [
                {
                    "name": "robinhood_v4_pool_manager",
                    "role": "manager",
                    "address": "0x8366a39CC670B4001A1121B8F6A443A643e40951",
                    "chain_id": 4663,  # Robinhood chain!
                    "abi_hash": _VALID_ABI_HASH,
                }
            ],
        }
        with pytest.raises(ArcNetworkMismatchError, match="Robinhood.*strictly prohibited"):
            ArcDeploymentReviewBridge.load_venues_config(config, target_chain_id=5042)

    def test_rejection_of_cross_network_testnet_pollution(self) -> None:
        config = {
            "chain_id": 5042,
            "venues": [
                {
                    "name": "testnet_router",
                    "role": "router",
                    "address": "0x2222222222222222222222222222222222225042",
                    "chain_id": 5042002,  # Testnet address smuggled into mainnet!
                    "abi_hash": _VALID_ABI_HASH,
                }
            ],
        }
        with pytest.raises(ArcNetworkMismatchError, match="Cross-network address import rejected"):
            ArcDeploymentReviewBridge.load_venues_config(config, target_chain_id=5042)

    def test_rejection_of_duplicate_address_different_role(self) -> None:
        registry = DeploymentsRegistry(target_chain_id=5042)
        rec1 = ArcDeploymentRecord(
            name="venue_1",
            role=ContractRole.ROUTER,
            address="0x1111111111111111111111111111111111111111",
            chain_id=5042,
            abi_hash=_VALID_ABI_HASH,
            source_proof="proof_1",
        )
        rec2 = ArcDeploymentRecord(
            name="venue_2",
            role=ContractRole.FACTORY,  # different role, same address
            address="0x1111111111111111111111111111111111111111",
            chain_id=5042,
            abi_hash=_VALID_ABI_HASH,
            source_proof="proof_2",
        )
        registry.register(rec1)
        with pytest.raises(ArcValidationError, match="Address collision"):
            registry.register(rec2)

    def test_rejection_of_duplicate_alias_across_venues(self) -> None:
        registry = DeploymentsRegistry(target_chain_id=5042)
        rec1 = ArcDeploymentRecord(
            name="venue_a",
            role=ContractRole.ROUTER,
            address="0x1111111111111111111111111111111111111111",
            chain_id=5042,
            abi_hash=_VALID_ABI_HASH,
            source_proof="proof_1",
        )
        rec2 = ArcDeploymentRecord(
            name="venue_b",
            role=ContractRole.ROUTER,
            address="0x2222222222222222222222222222222222222222",
            chain_id=5042,
            abi_hash=_VALID_ABI_HASH,
            source_proof="proof_2",
        )
        registry.register(rec1, aliases=("main_router",))
        with pytest.raises(ArcValidationError, match="Alias collision: 'main_router' already mapped"):
            registry.register(rec2, aliases=("main_router",))

    def test_load_example_venues_config_success(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent.parent.parent
        cfg_path = repo_root / "configs" / "arc" / "venues.example.json"
        registry = ArcDeploymentReviewBridge.load_venues_config(cfg_path, target_chain_id=5042)
        all_venues = registry.list_all()
        assert len(all_venues) == 4
        # All default to pending review:
        assert len(registry.list_verified()) == 0
        assert registry.get_any("uniswap_v3_factory") is not None
        assert registry.get_any("universal_router") is not None
