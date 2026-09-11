"""Deterministic identity catalog with explicit, scoped review gates."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import TextIO

from arbitrage_contracts.eligibility import (
    AssetEligibility,
    PoolCapability,
    RestrictionStatus,
    ReviewStatus,
    SourceEvidence,
)
from arbitrage_contracts.identity import (
    AssetRef,
    PoolDescriptor,
    PoolKey,
    TokenKey,
    validate_non_negative_integer,
    validate_positive_integer,
)

from .inputs import (
    LoadedInputs,
    RawPoolRecord,
    RawReviewDecision,
    RawTokenRecord,
    ReviewTrust,
    load_inputs,
    load_review_manifest,
)

# Missing observations stay UNKNOWN. Even VERIFIED_TRUE is conservatively unavailable here.
_REQUIRED_RESTRICTIONS = ("tax", "rebase", "pause", "blacklist", "whitelist")
type Subject = AssetRef | PoolKey


class CatalogRegistry:
    """Keep discovery, identity review and quote eligibility as separate queries."""

    def __init__(self, inputs: LoadedInputs, *, domain: str) -> None:
        if domain not in ("synthetic", "production"):
            raise ValueError("Unsupported catalog domain")
        canonical = load_inputs(StringIO(inputs.raw_bytes.decode("utf-8")), format=inputs.format)
        if canonical != inputs:
            raise ValueError("Discovery records do not match authenticated bytes")
        self._domain = domain
        self._records_sha256 = inputs.sha256
        self._tokens: dict[TokenKey, AssetRef] = {}
        self._assets: dict[AssetRef, RawTokenRecord] = {}
        self._pools: dict[PoolKey, PoolDescriptor] = {}
        self._subjects: dict[str, Subject] = {}
        self._reviews: dict[Subject, RawReviewDecision] = {}
        self._evidence: dict[str, SourceEvidence] = {}
        for record in canonical.records:
            if record.record_id in self._subjects:
                raise ValueError("Duplicate record_id")
            if isinstance(record, RawTokenRecord):
                self._register_token(record)
                self._subjects[record.record_id] = record.asset
            elif isinstance(record, RawPoolRecord):
                self._register_pool(record)
                self._subjects[record.record_id] = record.pool.key
            else:
                raise TypeError("Unsupported discovery record")

    def _register_token(self, record: RawTokenRecord) -> None:
        asset = record.asset
        if asset.token_key is not None:
            previous_asset = self._tokens.get(asset.token_key)
            if previous_asset is not None and previous_asset != asset:
                raise ValueError("Conflicting balance domain for TokenKey")
            self._tokens.setdefault(asset.token_key, asset)
        old = self._assets.get(asset)
        if old is not None and (old.symbol, old.decimals, old.issuer_id, old.bridge_version) != (
            record.symbol,
            record.decimals,
            record.issuer_id,
            record.bridge_version,
        ):
            raise ValueError("Conflicting asset metadata")
        self._assets.setdefault(asset, record)

    def _register_pool(self, record: RawPoolRecord) -> None:
        pool = record.pool
        old = self._pools.get(pool.key)
        if old is not None and old != pool:
            raise ValueError("Conflicting pool metadata")
        self._pools.setdefault(pool.key, pool)

    def apply_review_manifest(self, source: Path | TextIO, *, trust: ReviewTrust) -> None:
        """Atomically install a pinned review snapshot; discovery cannot auto-approve."""
        if trust.domain != self._domain:
            raise ValueError("Review trust does not match catalog domain")
        manifest = load_review_manifest(source, trust=trust)
        if manifest.records_sha256 != self._records_sha256:
            raise ValueError("Review subject content mismatch")
        reviews: dict[Subject, RawReviewDecision] = {}
        for decision in manifest.decisions:
            subject = self._subjects.get(decision.subject_key)
            if subject is None:
                raise ValueError("Review references an unresolved subject")
            if subject.chain_id != decision.scope.chain_id:
                raise ValueError("Review chain does not match subject")
            if subject in reviews:
                raise ValueError("Multiple decisions alias the same subject")
            reviews[subject] = decision
        self._reviews = reviews
        self._evidence = {e.evidence_id: e for e in manifest.evidence}

    def get_token(self, key: TokenKey) -> AssetRef | None:
        """Return only ERC20 identity; native assets never enter this index."""
        return self._tokens.get(key)

    def get_pool(self, key: PoolKey) -> PoolDescriptor | None:
        """Resolve the full canonical pool key."""
        return self._pools.get(key)

    def list_assets(self) -> list[AssetRef]:
        """Return all discovered assets including native balance domains."""
        return list(self._assets)

    def list_pools(self) -> list[PoolDescriptor]:
        """Return discoveries including pools with unresolved endpoint assets."""
        return list(self._pools.values())

    def get_review_status(self, subject: Subject) -> ReviewStatus:
        """Return identity review status, without implying quote eligibility."""
        review = self._reviews.get(subject)
        return review.status if review else ReviewStatus.PENDING_REVIEW

    def list_pending_reviews(self) -> list[TokenKey | AssetRef | PoolKey]:
        """Keep pending, discovered and unknown subjects queryable."""
        pending = {ReviewStatus.PENDING_REVIEW, ReviewStatus.DISCOVERED, ReviewStatus.UNKNOWN}
        return [
            subject.token_key if isinstance(subject, AssetRef) and subject.token_key else subject
            for subject in list(self._assets) + list[Subject](self._pools)
            if self.get_review_status(subject) in pending
        ]

    def list_unresolved_pools(self) -> list[PoolDescriptor]:
        """Return pools whose exact asset identities have not been discovered."""
        return [
            pool
            for pool in self._pools.values()
            if pool.currency0 not in self._assets or pool.currency1 not in self._assets
        ]

    def list_identity_approved_pools(self) -> list[PoolDescriptor]:
        """List identity-reviewed pools, not a usable/quoteable pool set."""
        return [
            pool
            for pool in self._pools.values()
            if self.get_review_status(pool.key) == ReviewStatus.APPROVED
        ]

    def get_asset_eligibility(self, key: TokenKey | AssetRef) -> AssetEligibility | None:
        """Expose the public eligibility contract without inventing execution approval."""
        asset = self._tokens.get(key) if isinstance(key, TokenKey) else key
        if asset is None or asset not in self._assets:
            return None
        record = self._assets[asset]
        review = self._reviews.get(asset)
        if review is None:
            return AssetEligibility(asset, review_status=ReviewStatus.PENDING_REVIEW)
        return AssetEligibility(
            asset,
            issuer_id=record.issuer_id,
            issuance_or_bridge_version=record.bridge_version,
            decimals_status="verified"
            if review.decimals_evidence_ref in self._evidence
            else "unknown",
            decimals_evidence_ref=review.decimals_evidence_ref,
            contract_restrictions=review.restrictions,
            review_status=review.status,
            reviewer_ref=review.reviewer_ref,
            reviewed_at_ms=review.reviewed_at_ms,
            subject_scope=review.scope.subject_kind,
            evidence_refs=review.evidence_refs,
            validity=f"[{review.scope.valid_from_ms},{review.scope.valid_until_ms})",
        )

    def _evidence_resolves(self, refs: tuple[str, ...], review: RawReviewDecision) -> bool:
        if not refs:
            return False
        for ref in refs:
            evidence = self._evidence.get(ref)
            if evidence is None or evidence.chain_id != review.scope.chain_id:
                return False
            if evidence.captured_at_ms <= 0 or evidence.captured_at_ms > review.reviewed_at_ms:
                return False
            if evidence.block_ref is None or not evidence.block_ref.isdecimal():
                return False
            if not review.scope.block_from <= int(evidence.block_ref) <= review.scope.block_to:
                return False
        return True

    def _approved_at(
        self,
        subject: Subject,
        *,
        chain_id: int,
        block: int,
        at_ms: int,
        subject_kind: str,
        subject_ref: str | None,
    ) -> bool:
        review = self._reviews.get(subject)
        return bool(
            review is not None
            and review.status == ReviewStatus.APPROVED
            and review.reviewed_at_ms <= at_ms
            and review.scope.applies(
                domain=self._domain,
                chain_id=chain_id,
                block=block,
                at_ms=at_ms,
                subject_kind=subject_kind,
                subject_ref=subject_ref,
            )
            and self._evidence_resolves(review.evidence_refs, review)
        )

    def _asset_usable(self, asset: AssetRef) -> bool:
        review = self._reviews.get(asset)
        if asset not in self._assets or review is None or review.decimals_evidence_ref is None:
            return False
        restrictions = dict(review.restrictions)
        return (
            all(
                restrictions.get(name, RestrictionStatus.UNKNOWN)
                == RestrictionStatus.VERIFIED_FALSE
                for name in _REQUIRED_RESTRICTIONS
            )
            and all(value == RestrictionStatus.VERIFIED_FALSE for value in restrictions.values())
            and review.decimals_evidence_ref in review.evidence_refs
            and self._evidence_resolves((review.decimals_evidence_ref,), review)
        )

    def list_eligible_pools(
        self,
        *,
        chain_id: int,
        block: int,
        at_ms: int,
        subject_kind: str = "public",
        subject_ref: str | None = None,
    ) -> list[PoolDescriptor]:
        """Select explicitly reviewed quote candidates for one caller/block/time only."""
        validate_positive_integer(chain_id, "chain_id")
        validate_non_negative_integer(block, "block")
        validate_non_negative_integer(at_ms, "at_ms")
        if subject_kind not in ("public", "wallet_ref", "role"):
            raise ValueError("Unsupported subject kind")
        if (subject_kind == "public") != (subject_ref is None):
            raise ValueError("Subject kind/ref mismatch")
        result: list[PoolDescriptor] = []
        for pool in self._pools.values():
            if not all(
                self._approved_at(
                    subject,
                    chain_id=chain_id,
                    block=block,
                    at_ms=at_ms,
                    subject_kind=subject_kind,
                    subject_ref=subject_ref,
                )
                for subject in (pool.key, pool.currency0, pool.currency1)
            ):
                continue
            if not self._asset_usable(pool.currency0) or not self._asset_usable(pool.currency1):
                continue
            review = self._reviews[pool.key]
            if (
                not review.can_quote
                or any(
                    value != RestrictionStatus.VERIFIED_FALSE for _, value in review.restrictions
                )
                or pool.deployment_status != "deployed"
                or not self._evidence_resolves(pool.identity_evidence_refs, review)
            ):
                continue
            result.append(pool)
        return result

    def get_pool_capability(
        self,
        key: PoolKey,
        *,
        chain_id: int,
        block: int,
        at_ms: int,
        subject_kind: str = "public",
        subject_ref: str | None = None,
    ) -> PoolCapability | None:
        """Keep simulation and atomic execution unknown, even for quote candidates."""
        if key not in self._pools:
            return None
        eligible = self.list_eligible_pools(
            chain_id=chain_id,
            block=block,
            at_ms=at_ms,
            subject_kind=subject_kind,
            subject_ref=subject_ref,
        )
        supported = any(pool.key == key for pool in eligible)
        review = self._reviews.get(key)
        return PoolCapability(
            key,
            can_quote="supported" if supported else "unknown",
            evidence_refs=review.evidence_refs if review else (),
        )
