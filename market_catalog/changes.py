"""Offline, immutable change replay. Evidence trust is supplied out of band by callers.

Literal enums preserve the existing C22 import boundary. Snapshots are canonical
chain history, not wall-clock knowledge: removed logs retract their original
cursor. Dated REORG controls apply only at/after their observation block. Without
headers, ambiguous ancestor forks fail closed; a tip hash is not ancestry proof.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields, replace
from typing import Any, Literal, get_args

from arbitrage_contracts.identity import (
    AssetRef,
    PoolKey,
    TokenKey,
    validate_bytes32,
    validate_non_negative_integer,
    validate_positive_integer,
)

type ChangeType = Literal[
    "DISCOVERY", "REVIEW", "INACTIVE", "RETIREMENT", "LIQUIDITY", "MIGRATION", "REORG"
]
type PoolStatus = Literal["ACTIVE", "UNKNOWN", "INACTIVE_OBSERVED", "POOL_RETIRED"]
type MigrationStatus = Literal["MIGRATION_CANDIDATE", "MIGRATION_CONFIRMED"]
type EvidenceKind = Literal[
    "FACTORY_SHUTDOWN",
    "POOL_DESTROYED",
    "ADMIN_RETIREMENT",
    "MIGRATION_CONTRACT",
    "AUTHORIZED_MIGRATION",
]


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _text(value: str, name: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} must be nonempty text")


def _pool(key: PoolKey) -> PoolKey:
    if not isinstance(key, PoolKey):
        raise TypeError("Expected PoolKey")
    return PoolKey(
        key.chain_id,
        key.protocol_id,
        key.venue_kind,
        key.venue_address.lower(),
        key.pool_id_kind,
        key.pool_id.lower(),
    )


def _asset(asset: AssetRef) -> AssetRef:
    if not isinstance(asset, AssetRef):
        raise TypeError("Expected AssetRef")
    if asset.token_key is not None:
        return AssetRef.erc20(
            TokenKey(asset.chain_id, asset.token_key.address.lower()), asset.balance_domain_id
        )
    return asset


@dataclass(frozen=True, slots=True)
class ChangeEvidence:
    """Scoped on-chain proof; its digest must be independently pinned in the journal.

    The caller authenticates raw_sha256/authority_ref offline; this module never
    fetches a receipt or treats an input's own 'verified' flag as authorization.
    """

    kind: EvidenceKind
    pool: PoolKey
    block_number: int
    block_hash: str
    transaction_hash: str
    authority_ref: str
    raw_sha256: str
    related_pool: PoolKey | None = None
    causes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in get_args(EvidenceKind.__value__):
            raise ValueError("Unsupported proof kind")
        object.__setattr__(self, "pool", _pool(self.pool))
        validate_non_negative_integer(self.block_number, "block_number")
        for name in ("block_hash", "transaction_hash"):
            object.__setattr__(self, name, validate_bytes32(getattr(self, name)).lower())
        _text(self.authority_ref, "authority_ref")
        if (
            type(self.raw_sha256) is not str
            or len(self.raw_sha256) != 64
            or any(c not in "0123456789abcdef" for c in self.raw_sha256)
        ):
            raise ValueError("raw_sha256 must be lowercase SHA-256")
        object.__setattr__(self, "causes", tuple(sorted(set(self.causes))))
        for cause in self.causes:
            _text(cause, "cause")
        if self.related_pool is not None:
            related = _pool(self.related_pool)
            if related == self.pool or related.chain_id != self.pool.chain_id:
                raise ValueError("Proof target must be a distinct pool on the same chain")
            object.__setattr__(self, "related_pool", related)

    @property
    def sha256(self) -> str:
        """Content pin for a separately authenticated proof."""
        return _digest(asdict(self))


@dataclass(frozen=True, slots=True)
class CatalogChangeEvent:
    """One immutable observation; cursor identity includes block hash, never height alone."""

    kind: ChangeType
    chain_id: int
    block_number: int
    block_hash: str
    transaction_hash: str
    transaction_index: int
    log_index: int
    sequence: int
    source_ref: str
    pool: PoolKey | None = None
    currencies: tuple[AssetRef, ...] = ()
    reason: str = ""
    liquidity: int | None = None
    price: str | None = None
    review_status: str | None = None
    related_pool: PoolKey | None = None
    evidence: ChangeEvidence | None = None
    removed: bool = False
    reorg_block_hash: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in get_args(ChangeType.__value__):
            raise ValueError("Unsupported change type")
        validate_positive_integer(self.chain_id, "chain_id")
        for name in ("block_number", "transaction_index", "log_index", "sequence"):
            validate_non_negative_integer(getattr(self, name), name)
        for name in ("block_hash", "transaction_hash"):
            object.__setattr__(self, name, validate_bytes32(getattr(self, name)).lower())
        _text(self.source_ref, "source_ref")
        if type(self.removed) is not bool or type(self.reason) is not str:
            raise TypeError("Invalid removed/reason type")
        for name in ("pool", "related_pool"):
            key = getattr(self, name)
            if key is not None:
                key = _pool(key)
                if key.chain_id != self.chain_id:
                    raise ValueError("Cross-chain event subject")
                object.__setattr__(self, name, key)
        currencies = tuple(
            sorted((_asset(c) for c in self.currencies), key=lambda c: _json(asdict(c)))
        )
        if currencies and (
            len(currencies) != 2
            or currencies[0] == currencies[1]
            or any(c.chain_id != self.chain_id for c in currencies)
        ):
            raise ValueError("Currencies must be two distinct same-chain assets")
        object.__setattr__(self, "currencies", currencies)
        if self.kind == "REORG":
            if self.pool is not None or self.reorg_block_hash is None or self.removed:
                raise ValueError("REORG requires a target hash and no pool/removed flag")
            target = validate_bytes32(self.reorg_block_hash).lower()
            if target == self.block_hash:
                raise ValueError("REORG cannot revoke its own block")
            object.__setattr__(self, "reorg_block_hash", target)
        elif self.pool is None or self.reorg_block_hash is not None:
            raise ValueError("Pool event requires pool and no reorg target")
        if self.kind == "DISCOVERY" and not currencies:
            raise ValueError("Discovery requires exact currency identities")
        if currencies and self.kind != "DISCOVERY":
            raise ValueError("Only discovery defines currencies")
        if self.liquidity is not None:
            validate_non_negative_integer(self.liquidity, "liquidity")
            if self.kind not in ("DISCOVERY", "LIQUIDITY"):
                raise ValueError("Unexpected liquidity payload")
        if self.kind == "LIQUIDITY" and self.liquidity is None:
            raise ValueError("Liquidity event requires liquidity")
        if self.price is not None:
            # Decimal text only, including zero; no binary floats/NaN/Infinity.
            parts = self.price.split(".") if type(self.price) is str else []
            if not 1 <= len(parts) <= 2 or any(
                not p or not p.isascii() or not p.isdecimal() for p in parts
            ):
                raise ValueError("Price must be nonnegative decimal text")
            if self.kind not in ("DISCOVERY", "LIQUIDITY"):
                raise ValueError("Unexpected price payload")
        if self.kind == "REVIEW":
            if self.review_status not in ("approved", "pending_review", "rejected", "unknown"):
                raise ValueError("Invalid review status")
        elif self.review_status is not None:
            raise ValueError("Unexpected review payload")
        if self.kind == "MIGRATION":
            if self.related_pool is None or self.related_pool == self.pool:
                raise ValueError("Migration requires a distinct target")
        elif self.related_pool is not None:
            raise ValueError("Unexpected migration target")
        if self.evidence is not None and not isinstance(self.evidence, ChangeEvidence):
            raise TypeError("Expected ChangeEvidence")
        if self.evidence is not None and self.evidence.block_number > self.block_number:
            raise ValueError("Future evidence cannot be attached to an earlier event")
        if self.evidence is not None and self.kind not in ("RETIREMENT", "MIGRATION"):
            raise ValueError("Unexpected proof payload")

    @property
    def event_id(self) -> str:
        """Stable cursor ID shared by an observation and its removed tombstone."""
        return _digest(
            (
                self.chain_id,
                self.block_hash,
                self.transaction_hash,
                self.transaction_index,
                self.log_index,
                self.sequence,
                self.reorg_block_hash,
            )
        )

    @property
    def order(self) -> tuple[int, int, int, int, str, bool]:
        """Canonical EVM order with deterministic ties for competing fork observations."""
        return (
            self.block_number,
            self.transaction_index,
            self.log_index,
            self.sequence,
            self.event_id,
            self.removed,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a detached JSON-compatible internal event record."""
        result: dict[str, Any] = json.loads(_json(asdict(self)))
        return result

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CatalogChangeEvent:
        """Parse the exact internal fixture shape, rejecting unknown or missing fields."""
        if set(raw) != {f.name for f in fields(cls)}:
            raise ValueError("Incomplete or unknown event fields")
        data = dict(raw)
        for name in ("pool", "related_pool"):
            data[name] = _read_pool(data[name]) if data[name] is not None else None
        assets = []
        for item in data["currencies"]:
            item = dict(item)
            if item["token_key"] is not None:
                token = dict(item["token_key"])
                raw_address = token.pop("raw_address")
                item["token_key"] = TokenKey(**token)
                if raw_address.lower() != item["token_key"].address:
                    raise ValueError("Conflicting token identity")
            assets.append(AssetRef(**item))
        data["currencies"] = tuple(assets)
        if data["evidence"] is not None:
            proof = dict(data["evidence"])
            proof["pool"] = _read_pool(proof["pool"])
            if proof["related_pool"] is not None:
                proof["related_pool"] = _read_pool(proof["related_pool"])
            data["evidence"] = ChangeEvidence(**proof)
        return cls(**data)


def _read_pool(raw: dict[str, Any]) -> PoolKey:
    data = dict(raw)
    venue = data.pop("canonical_venue_address")
    pool_id = data.pop("canonical_pool_id")
    key = PoolKey(**data)
    if (venue, pool_id) != (key.canonical_venue_address, key.canonical_pool_id):
        raise ValueError("Conflicting canonical pool identity")
    return key


@dataclass(frozen=True, slots=True)
class PoolState:
    """Immutable historical identity/activity observation, never execution eligibility."""

    pool: PoolKey
    currencies: tuple[AssetRef, ...] = ()
    status: PoolStatus = "UNKNOWN"
    liquidity: int | None = None
    price: str | None = None
    review_status: str = "unknown"
    discovery_event: str | None = None
    liquidity_drop_event: str | None = None
    retirement_event: str | None = None


@dataclass(frozen=True, slots=True)
class MigrationState:
    """Directed migration hypothesis and the exact surviving causal observations."""

    old_pool: PoolKey
    new_pool: PoolKey
    status: MigrationStatus
    causes: tuple[str, ...]
    confirmation_event: str | None = None


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    """Detached immutable point-in-chain state and its retained audit trail."""

    chain_id: int
    block_number: int
    block_hash: str
    pools: tuple[PoolState, ...]
    migrations: tuple[MigrationState, ...]
    events: tuple[CatalogChangeEvent, ...]
    trusted_evidence_hashes: tuple[str, ...]

    @property
    def sha256(self) -> str:
        """Hash state, anchor, trust configuration and audit in canonical order."""
        return _digest(asdict(self))

    def get_pool(self, pool: PoolKey) -> PoolState | None:
        """Look up an exact pool identity, including retired/unresolved observations."""
        return next((p for p in self.pools if p.pool == pool), None)

    def list_active_pools(self) -> tuple[PoolKey, ...]:
        """List observed active pools; this grants no review or execution approval."""
        return tuple(p.pool for p in self.pools if p.status == "ACTIVE")

    def get_pool_history(self, pool: PoolKey) -> tuple[CatalogChangeEvent, ...]:
        """Retain observations and tombstones, including relevant block revocations."""
        hashes = {e.block_hash for e in self.events if e.pool == pool or e.related_pool == pool}
        return tuple(
            e
            for e in self.events
            if e.pool == pool or e.related_pool == pool or e.reorg_block_hash in hashes
        )


class ChangeJournal:
    """Single-chain deterministic journal; untrusted claims cannot authorize lifecycle changes."""

    def __init__(self, chain_id: int, *, trusted_evidence_hashes: tuple[str, ...] = ()) -> None:
        validate_positive_integer(chain_id, "chain_id")
        self.chain_id = chain_id
        for value in trusted_evidence_hashes:
            if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError("Evidence pins must be lowercase SHA-256")
        self._trusted = tuple(sorted(set(trusted_evidence_hashes)))
        self._events: dict[tuple[str, bool], CatalogChangeEvent] = {}

    @property
    def events(self) -> tuple[CatalogChangeEvent, ...]:
        """Return canonical immutable audit records, including retracted observations."""
        return tuple(sorted(self._events.values(), key=lambda e: e.order))

    def append(self, event: CatalogChangeEvent) -> bool:
        """Append once; conflicting cursor payloads fail atomically instead of last-write wins."""
        if not isinstance(event, CatalogChangeEvent) or event.chain_id != self.chain_id:
            raise ValueError("Journal requires an event from its chain")
        for old in self._events.values():
            if old.block_hash == event.block_hash and old.block_number != event.block_number:
                raise ValueError("Block hash is bound to a single height")
            if old.event_id == event.event_id and replace(old, removed=False) != replace(
                event, removed=False
            ):
                raise ValueError("Conflicting event cursor")
            if (
                event.reorg_block_hash == old.block_hash and old.block_number > event.block_number
            ) or (
                old.reorg_block_hash == event.block_hash and event.block_number > old.block_number
            ):
                raise ValueError("REORG cannot target a future block")
        key = (event.event_id, event.removed)
        if key in self._events:
            return False
        self._events[key] = event
        return True

    def reorg_rollback(self, reorg_block_hash: str, *, block_number: int, block_hash: str) -> bool:
        """Append a dated exact-block revocation; descendants require their own revocations."""
        return self.append(
            CatalogChangeEvent(
                "REORG",
                self.chain_id,
                block_number,
                block_hash,
                "0x" + "00" * 32,
                0,
                0,
                0,
                "journal:explicit-reorg",
                reorg_block_hash=reorg_block_hash,
            )
        )

    def as_of(self, block_number: int, block_hash: str) -> CatalogSnapshot:
        """Replay only bounded events; select the exact tip and reject ambiguous ancestors."""
        validate_non_negative_integer(block_number, "block_number")
        block_hash = validate_bytes32(block_hash).lower()
        bounded = tuple(e for e in self.events if e.block_number <= block_number)
        if any(e.block_hash == block_hash and e.block_number != block_number for e in bounded):
            raise ValueError("Anchor hash/height mismatch")
        # Revocations are replayed backwards so a later REORG can revoke an earlier control.
        invalid: set[str] = set()
        for e in reversed(bounded):
            if e.block_number == block_number and e.block_hash != block_hash:
                continue
            if e.kind == "REORG" and e.block_hash not in invalid:
                assert e.reorg_block_hash is not None
                invalid.add(e.reorg_block_hash)
        if block_hash in invalid:
            raise ValueError("Requested anchor is revoked")
        removed = {e.event_id for e in bounded if e.removed and e.block_hash not in invalid}
        live = tuple(
            e
            for e in bounded
            if e.block_hash not in invalid
            and not e.removed
            and e.event_id not in removed
            and (e.block_number != block_number or e.block_hash == block_hash)
        )
        heights: dict[int, set[str]] = {}
        for e in live:
            heights.setdefault(e.block_number, set()).add(e.block_hash)
        if any(len(hashes) > 1 for hashes in heights.values()):
            raise ValueError("Ambiguous ancestor forks; explicit rollback required")
        known_tip = {e.block_hash for e in bounded if e.block_number == block_number}
        if known_tip and block_hash not in known_tip:
            raise ValueError("Unknown anchor at observed height")
        states: dict[PoolKey, PoolState] = {}
        migrations: dict[tuple[PoolKey, PoolKey], MigrationState] = {}
        applied: dict[str, CatalogChangeEvent] = {}
        for e in live:
            if e.pool is None:
                continue
            old = states.get(e.pool, PoolState(e.pool))
            state = old
            if e.kind == "DISCOVERY":
                if old.currencies and old.currencies != e.currencies:
                    raise ValueError("Conflicting pool currencies")
                state = replace(
                    old,
                    currencies=e.currencies,
                    status="ACTIVE" if old.status != "POOL_RETIRED" else old.status,
                    discovery_event=old.discovery_event or e.event_id,
                )
                if old.discovery_event is None:
                    state = replace(state, liquidity=e.liquidity, price=e.price)
            elif e.kind == "REVIEW":
                assert e.review_status is not None
                state = replace(old, review_status=e.review_status)
            elif e.kind in ("INACTIVE", "RETIREMENT"):
                status: PoolStatus = "INACTIVE_OBSERVED"
                if (
                    e.kind == "RETIREMENT"
                    and self._proof_valid(e)
                    and e.evidence is not None
                    and e.evidence.kind
                    in ("FACTORY_SHUTDOWN", "POOL_DESTROYED", "ADMIN_RETIREMENT")
                ):
                    status = "POOL_RETIRED"
                if old.status == "POOL_RETIRED":
                    status = old.status
                state = replace(
                    old,
                    status=status,
                    retirement_event=e.event_id
                    if status == "POOL_RETIRED" and old.retirement_event is None
                    else old.retirement_event,
                )
            elif e.kind == "LIQUIDITY":
                assert e.liquidity is not None
                drop = old.liquidity is not None and e.liquidity < old.liquidity
                state = replace(
                    old,
                    liquidity=e.liquidity,
                    price=e.price if e.price is not None else old.price,
                    liquidity_drop_event=e.event_id if drop else old.liquidity_drop_event,
                )
                if e.liquidity == 0 and old.status != "POOL_RETIRED":
                    state = replace(state, status="INACTIVE_OBSERVED")
            states[e.pool] = state
            applied[e.event_id] = e
            # A new same-pair pool plus an old-pool drop yields only a hypothesis.
            for a in states.values():
                if a.liquidity_drop_event is None or not a.currencies:
                    continue
                for b in states.values():
                    if (
                        a.pool == b.pool
                        or a.currencies != b.currencies
                        or b.discovery_event is None
                    ):
                        continue
                    if (
                        a.discovery_event is None
                        or applied[b.discovery_event].order <= applied[a.discovery_event].order
                    ):
                        continue
                    key = (a.pool, b.pool)
                    causes = tuple(sorted((a.liquidity_drop_event, b.discovery_event)))
                    if key not in migrations or migrations[key].status != "MIGRATION_CONFIRMED":
                        migrations[key] = MigrationState(
                            a.pool, b.pool, "MIGRATION_CANDIDATE", causes
                        )
            if e.kind == "MIGRATION":
                assert e.related_pool is not None
                key = (e.pool, e.related_pool)
                candidate = migrations.get(key)
                if candidate is None:
                    candidate = MigrationState(e.pool, e.related_pool, "MIGRATION_CANDIDATE", ())
                proof = e.evidence
                if (
                    self._proof_valid(e)
                    and proof is not None
                    and proof.kind in ("MIGRATION_CONTRACT", "AUTHORIZED_MIGRATION")
                    and proof.related_pool == e.related_pool
                    and len(proof.causes) == 2
                    and candidate.causes == proof.causes
                    and all(c in applied and applied[c].order < e.order for c in proof.causes)
                ):
                    candidate = replace(
                        candidate, status="MIGRATION_CONFIRMED", confirmation_event=e.event_id
                    )
                migrations[key] = candidate
        # Audit also retains removed/fork observations; none can exceed the cutoff.
        return CatalogSnapshot(
            self.chain_id,
            block_number,
            block_hash,
            tuple(sorted(states.values(), key=lambda p: _json(asdict(p.pool)))),
            tuple(sorted(migrations.values(), key=lambda m: _json(asdict(m)))),
            bounded,
            self._trusted,
        )

    def _proof_valid(self, event: CatalogChangeEvent) -> bool:
        proof = event.evidence
        return (
            proof is not None
            and proof.sha256 in self._trusted
            and proof.pool == event.pool
            and proof.block_number == event.block_number
            and proof.block_hash == event.block_hash
            and proof.transaction_hash == event.transaction_hash
        )

    def replay(self) -> CatalogSnapshot:
        """Rebuild the latest unambiguous anchor; an empty journal has no implicit chain tip."""
        if not self.events:
            raise ValueError("Empty journal requires an explicit as_of anchor")
        height = max(e.block_number for e in self.events)
        invalid: set[str] = set()
        for event in reversed(self.events):
            if event.kind == "REORG" and event.block_hash not in invalid:
                assert event.reorg_block_hash is not None
                invalid.add(event.reorg_block_hash)
        removed = {e.event_id for e in self.events if e.removed}
        hashes = {
            e.block_hash
            for e in self.events
            if e.block_number == height
            and e.block_hash not in invalid
            and not e.removed
            and e.event_id not in removed
        }
        if not hashes:
            hashes = {
                e.block_hash
                for e in self.events
                if e.block_number == height and e.block_hash not in invalid
            }
        if len(hashes) != 1:
            raise ValueError("Latest anchor is ambiguous; use explicit as_of")
        return self.as_of(height, next(iter(hashes)))

    def list_active_pools(self) -> tuple[PoolKey, ...]:
        """Return latest observed active pool identities."""
        return self.replay().list_active_pools()

    def get_pool_history(self, pool: PoolKey) -> tuple[CatalogChangeEvent, ...]:
        """Return the permanent journal history, including forks and tombstones."""
        hashes = {e.block_hash for e in self.events if e.pool == pool or e.related_pool == pool}
        return tuple(
            e
            for e in self.events
            if e.pool == pool or e.related_pool == pool or e.reorg_block_hash in hashes
        )
