"""Unit tests for domain eligibility, provenance evidence, and capability contracts."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from arbitrage_contracts.eligibility import (
    AssetEligibility,
    CapabilityStatus,
    EvidenceLevel,
    PoolCapability,
    RestrictionStatus,
    ReviewStatus,
    SourceEvidence,
    validate_sha256_hex,
)
from arbitrage_contracts.identity import (
    AssetRef,
    PoolIdKind,
    PoolKey,
    TokenKey,
    VenueKind,
)


class TestSourceEvidence(unittest.TestCase):
    """Test suite verifying provenance metadata and evidence integrity."""

    def test_valid_source_evidence_creation(self) -> None:
        """Verify standard source evidence attributes and defensive tuple copying."""
        digest = "a" * 64
        evidence = SourceEvidence(
            evidence_id="evidence-001",
            source_type="deployment_registry",
            source_locator="contracts/deployments.json",
            raw_sha256=digest,
            captured_at_ms=1700000000000,
            chain_id=1,
            limitations=["read_only", "synthetic_mock"],
            parent_refs=["parent-000"],
        )
        self.assertEqual(evidence.evidence_id, "evidence-001")
        self.assertEqual(evidence.raw_sha256, digest)
        self.assertEqual(evidence.limitations, ("read_only", "synthetic_mock"))
        self.assertEqual(evidence.parent_refs, ("parent-000",))

    def test_sha256_validation(self) -> None:
        """Verify invalid SHA256 lengths, non-hex characters, and non-strings are rejected."""
        valid_digest = "b" * 64
        self.assertEqual(validate_sha256_hex(valid_digest), valid_digest)

        for invalid_digest in ["b" * 63, "b" * 65, "g" * 64, "", "0x" + "b" * 62]:
            with self.assertRaises(ValueError, msg=f"Should reject {invalid_digest!r}"):
                validate_sha256_hex(invalid_digest)

        with self.assertRaises(TypeError):
            validate_sha256_hex(12345)  # type: ignore[arg-type]

    def test_rejection_of_invalid_fields(self) -> None:
        """Verify empty strings or negative timestamps are rejected fail-closed."""
        with self.assertRaises(ValueError):
            SourceEvidence(evidence_id="", source_type="official_docs", source_locator="url")
        with self.assertRaises(ValueError):
            SourceEvidence(evidence_id="id", source_type="", source_locator="url")
        with self.assertRaises(ValueError):
            SourceEvidence(evidence_id="id", source_type="official_docs", source_locator="")
        with self.assertRaises(ValueError):
            SourceEvidence(
                evidence_id="id",
                source_type="official_docs",
                source_locator="url",
                captured_at_ms=-1,
            )


class TestAssetEligibility(unittest.TestCase):
    """Test suite verifying asset review status, approval barriers, and restriction tracking."""

    def setUp(self) -> None:
        self.token_key = TokenKey(1, "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48")
        self.asset_ref = AssetRef.erc20(self.token_key)

    def test_default_status_is_discovered_not_approved(self) -> None:
        """Verify newly discovered assets default to 'discovered' and are never approved implicitly."""
        eligibility = AssetEligibility(asset_ref=self.asset_ref)
        self.assertEqual(eligibility.review_status, ReviewStatus.DISCOVERED)
        self.assertFalse(eligibility.is_approved())
        self.assertEqual(eligibility.decimals_status, "unknown")
        self.assertEqual(eligibility.subject_scope, "unknown")

    def test_approved_requires_explicit_reviewer_and_evidence(self) -> None:
        """Verify asset cannot attain approved status without reviewer, timestamp, and evidence."""
        with self.assertRaises(ValueError, msg="Approved without reviewer should fail"):
            AssetEligibility(
                asset_ref=self.asset_ref,
                review_status=ReviewStatus.APPROVED,
                reviewer_ref=None,
                reviewed_at_ms=1700000000000,
                evidence_refs=["evidence-audit-01"],
            )

        with self.assertRaises(ValueError, msg="Approved without reviewed_at_ms should fail"):
            AssetEligibility(
                asset_ref=self.asset_ref,
                review_status=ReviewStatus.APPROVED,
                reviewer_ref="security_auditor_alpha",
                reviewed_at_ms=None,
                evidence_refs=["evidence-audit-01"],
            )

        with self.assertRaises(ValueError, msg="Approved without evidence_refs should fail"):
            AssetEligibility(
                asset_ref=self.asset_ref,
                review_status=ReviewStatus.APPROVED,
                reviewer_ref="security_auditor_alpha",
                reviewed_at_ms=1700000000000,
                evidence_refs=[],
            )

    def test_cannot_approve_unsupported_decimals(self) -> None:
        """Verify asset with unsupported decimals cannot attain approved status."""
        with self.assertRaises(ValueError):
            AssetEligibility(
                asset_ref=self.asset_ref,
                decimals_status="unsupported",
                review_status=ReviewStatus.APPROVED,
                reviewer_ref="security_auditor_alpha",
                reviewed_at_ms=1700000000000,
                evidence_refs=["evidence-audit-01"],
            )

    def test_valid_approval_attainment(self) -> None:
        """Verify valid approval configuration achieves approved status."""
        eligibility = AssetEligibility(
            asset_ref=self.asset_ref,
            decimals_status="verified",
            review_status=ReviewStatus.APPROVED,
            reviewer_ref="security_auditor_alpha",
            reviewed_at_ms=1700000000000,
            evidence_refs=["evidence-audit-01"],
        )
        self.assertTrue(eligibility.is_approved())
        self.assertEqual(eligibility.reviewer_ref, "security_auditor_alpha")

    def test_restrictions_defensive_mapping_and_missing_is_unknown(self) -> None:
        """Verify contract restrictions return unknown for missing fields and resist mutation."""
        restrictions_input = {
            "transfer_tax": RestrictionStatus.VERIFIED_FALSE,
            "rebase": RestrictionStatus.VERIFIED_FALSE,
        }
        eligibility = AssetEligibility(
            asset_ref=self.asset_ref,
            contract_restrictions=restrictions_input,
        )
        self.assertEqual(eligibility.get_restriction("transfer_tax"), "verified_false")
        self.assertEqual(eligibility.get_restriction("rebase"), "verified_false")
        self.assertEqual(eligibility.get_restriction("pause"), "unknown")

        restrictions_input["pause"] = RestrictionStatus.VERIFIED_TRUE
        self.assertEqual(eligibility.get_restriction("pause"), "unknown")

    def test_immutability(self) -> None:
        """Verify AssetEligibility attributes cannot be mutated after instantiation."""
        eligibility = AssetEligibility(asset_ref=self.asset_ref)
        with self.assertRaises((FrozenInstanceError, AttributeError)):
            eligibility.review_status = ReviewStatus.APPROVED  # type: ignore[misc]


class TestPoolCapability(unittest.TestCase):
    """Test suite verifying separation of powers between quote, simulate, and atomic execute."""

    def setUp(self) -> None:
        self.pool_key = PoolKey(
            chain_id=1,
            protocol_id="uniswap_v3",
            venue_kind=VenueKind.FACTORY,
            venue_address="0x1F98431c8aD98523631AE4a59f267346ea31F984",
            pool_id_kind=PoolIdKind.ADDRESS,
            pool_id="0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640",
        )

    def test_separation_of_powers_can_quote_does_not_infer_can_atomic(self) -> None:
        """Verify can_quote=supported does not deduce can_atomic_execute=supported."""
        capability = PoolCapability(
            pool_key=self.pool_key,
            can_quote=CapabilityStatus.SUPPORTED,
            can_simulate=CapabilityStatus.SUPPORTED,
            can_atomic_execute=CapabilityStatus.UNKNOWN,
            evidence_refs=["quoter_contract_verified"],
        )
        self.assertEqual(capability.can_quote, "supported")
        self.assertEqual(capability.can_simulate, "supported")
        self.assertEqual(capability.can_atomic_execute, "unknown")
        self.assertNotEqual(capability.can_quote, capability.can_atomic_execute)

    def test_rejection_of_invalid_capability_states(self) -> None:
        """Verify unknown status strings are rejected fail-closed."""
        for invalid_status in ["true", "false", "yes", "no", "allowed"]:
            with self.assertRaises(ValueError):
                PoolCapability(pool_key=self.pool_key, can_quote=invalid_status)

    def test_defensive_evidence_copying(self) -> None:
        """Verify external modification of evidence list does not mutate PoolCapability."""
        evidence_list = ["quote_verified"]
        capability = PoolCapability(pool_key=self.pool_key, evidence_refs=evidence_list)
        self.assertEqual(capability.evidence_refs, ("quote_verified",))
        evidence_list.append("atomic_verified")
        self.assertEqual(capability.evidence_refs, ("quote_verified",))


if __name__ == "__main__":
    unittest.main()
