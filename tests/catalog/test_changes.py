"""C17-C19 synthetic causal replay, lifecycle safety and anti-future-peeking regressions."""

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from itertools import permutations
from pathlib import Path
from typing import Any, cast, get_args

import pytest

from arbitrage_contracts.identity import AssetRef, PoolKey, TokenKey
from market_catalog.changes import (
    CatalogChangeEvent,
    CatalogSnapshot,
    ChangeEvidence,
    ChangeJournal,
    ChangeType,
    EvidenceKind,
    PoolState,
)

CHAIN = 4663
PAIR = (
    AssetRef.erc20(TokenKey(CHAIN, "0x" + "11" * 20)),
    AssetRef.erc20(TokenKey(CHAIN, "0x" + "22" * 20)),
)
OLD = PoolKey(CHAIN, "synthetic-v2", "factory", "0x" + "aa" * 20, "address", "0x" + "bb" * 20)
NEW = replace(OLD, pool_id="0x" + "cc" * 20)
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/catalog/v1"


def block(n: int) -> str:
    return f"0x{n:064x}"


def event(
    kind: str = "DISCOVERY", number: int = 10, pool: PoolKey = OLD, **kwargs: Any
) -> CatalogChangeEvent:
    defaults: dict[str, Any] = (
        {"currencies": PAIR, "liquidity": 1000, "price": "2"} if kind == "DISCOVERY" else {}
    )
    defaults.update(kwargs)
    return CatalogChangeEvent(
        cast(ChangeType, kind),
        CHAIN,
        number,
        block(number),
        block(number + 1000),
        defaults.pop("transaction_index", 0),
        defaults.pop("log_index", 0),
        defaults.pop("sequence", 0),
        "synthetic:w1d",
        pool=pool,
        **defaults,
    )


def journal(*events: CatalogChangeEvent, proofs: tuple[ChangeEvidence, ...] = ()) -> ChangeJournal:
    result = ChangeJournal(CHAIN, trusted_evidence_hashes=tuple(p.sha256 for p in proofs))
    for e in events:
        result.append(e)
    return result


def proof_for(
    e: CatalogChangeEvent, kind: str = "FACTORY_SHUTDOWN", **kwargs: Any
) -> ChangeEvidence:
    assert e.pool is not None
    return ChangeEvidence(
        cast(EvidenceKind, kind),
        e.pool,
        e.block_number,
        e.block_hash,
        e.transaction_hash,
        "synthetic:authenticated-authority",
        "ab" * 32,
        **kwargs,
    )


def required_pool(snapshot: CatalogSnapshot, key: PoolKey) -> PoolState:
    """Assert the expected subject exists before checking its lifecycle fields."""
    state = snapshot.get_pool(key)
    assert state is not None
    return state


def required_proof(event: CatalogChangeEvent) -> ChangeEvidence:
    """Assert the positive fixture supplies an explicit scoped proof."""
    proof = event.evidence
    assert proof is not None
    return proof


def retirement(number: int = 20) -> CatalogChangeEvent:
    e = event("RETIREMENT", number)
    return replace(e, evidence=proof_for(e))


def migration_case() -> tuple[CatalogChangeEvent, ...]:
    first = event()
    drop = event("LIQUIDITY", 20, liquidity=0, price="0.5")
    new = event(number=21, pool=NEW)
    migration = event("MIGRATION", 22, related_pool=NEW)
    proof = proof_for(
        migration, "MIGRATION_CONTRACT", related_pool=NEW, causes=(drop.event_id, new.event_id)
    )
    return first, drop, new, replace(migration, evidence=proof)


def test_missing_pool_not_retired() -> None:
    j = journal(event(), event("INACTIVE", 20, reason="API_MISSING_PAGE"))
    state = required_pool(j.replay(), OLD)
    assert state is not None
    assert state.status in ("INACTIVE_OBSERVED", "UNKNOWN")
    assert state.retirement_event is None
    assert len(j.get_pool_history(OLD)) == 2


def test_liquidity_drop_not_confirmed_migration() -> None:
    first, drop, new, _ = migration_case()
    snapshot = journal(new, first, drop).replay()
    assert len(snapshot.migrations) == 1
    assert snapshot.migrations[0].status == "MIGRATION_CANDIDATE"
    assert snapshot.migrations[0].confirmation_event is None
    assert set(snapshot.migrations[0].causes) == {drop.event_id, new.event_id}


def test_as_of_never_looks_into_future() -> None:
    first, drop, new, migration = migration_case()
    retired = retirement(30)
    reviewed = event("REVIEW", 25, review_status="rejected")
    j = journal(
        first,
        drop,
        new,
        migration,
        reviewed,
        retired,
        proofs=(required_proof(migration), required_proof(retired)),
    )
    snapshot = j.as_of(10, block(10))
    state = required_pool(snapshot, OLD)
    assert state is not None
    assert state.status == "ACTIVE"
    assert state.price == "2" and state.liquidity == 1000
    assert state.review_status == "unknown"
    assert snapshot.get_pool(NEW) is None and not snapshot.migrations
    assert all(e.block_number <= 10 for e in snapshot.events)
    assert required_pool(j.replay(), OLD).status == "POOL_RETIRED"


@pytest.mark.parametrize(
    "reason",
    [
        "API_MISSING_PAGE",
        "API_TRUNCATED",
        "ZERO_SWAPS_24H",
        "LOW_TVL",
        "ZERO_LIQUIDITY",
        "ONE_WAY_REVERT",
    ],
)
@pytest.mark.parametrize("kind", ["INACTIVE", "RETIREMENT"])
def test_weak_observations_cannot_retire(reason: str, kind: str) -> None:
    state = required_pool(journal(event(), event(kind, 20, reason=reason)).replay(), OLD)
    assert state.status in ("INACTIVE_OBSERVED", "UNKNOWN")
    assert state.retirement_event is None


@pytest.mark.parametrize("kind", ["FACTORY_SHUTDOWN", "POOL_DESTROYED", "ADMIN_RETIREMENT"])
def test_explicit_authenticated_retirement_preserves_history(kind: str) -> None:
    e = event("RETIREMENT", 20)
    proof = proof_for(e, kind)
    e = replace(e, evidence=proof)
    j = journal(event(), e, proofs=(proof,))
    before = j.as_of(10, block(10))
    assert before.list_active_pools() == (OLD,)
    assert j.list_active_pools() == ()
    assert required_pool(j.replay(), OLD).status == "POOL_RETIRED"
    assert required_pool(j.replay(), OLD).retirement_event == e.event_id
    assert j.get_pool_history(OLD) == (event(), e)
    assert required_pool(before, OLD).status == "ACTIVE"


@pytest.mark.parametrize(
    "fault", ["unpinned", "pool", "block_hash", "transaction_hash", "kind", "block_number"]
)
def test_retirement_proof_scope_is_enforced(fault: str) -> None:
    e = retirement()
    p = required_proof(e)
    if fault == "pool":
        p = replace(p, pool=NEW)
    elif fault in ("block_hash", "transaction_hash"):
        updates: dict[str, Any] = {fault: block(999)}
        p = replace(p, **updates)
    elif fault == "kind":
        p = replace(p, kind="MIGRATION_CONTRACT")
    elif fault == "block_number":
        p = replace(p, block_number=19)
    e = replace(e, evidence=p)
    j = journal(event(), e, proofs=() if fault == "unpinned" else (p,))
    assert required_pool(j.replay(), OLD).status != "POOL_RETIRED"


def test_retirement_is_not_undone_by_rediscovery_or_review() -> None:
    e = retirement()
    j = journal(
        event(),
        e,
        event(number=21),
        event("REVIEW", 22, review_status="approved"),
        event("LIQUIDITY", 23, liquidity=123),
        event("INACTIVE", 24),
        proofs=(required_proof(e),),
    )
    assert required_pool(j.replay(), OLD).status == "POOL_RETIRED"
    assert OLD not in j.list_active_pools()


@pytest.mark.parametrize("kind", ["MIGRATION_CONTRACT", "AUTHORIZED_MIGRATION"])
def test_migration_requires_pinned_causal_proof(kind: str) -> None:
    first, drop, new, migration = migration_case()
    proof = replace(required_proof(migration), kind=cast(EvidenceKind, kind))
    migration = replace(migration, evidence=proof)
    snapshot = journal(first, drop, new, migration, proofs=(proof,)).replay()
    state = snapshot.migrations[0]
    assert state.status == "MIGRATION_CONFIRMED"
    assert state.old_pool == OLD and state.new_pool == NEW
    assert state.confirmation_event == migration.event_id
    assert required_pool(snapshot, OLD).status != "POOL_RETIRED"


@pytest.mark.parametrize(
    "fault",
    [
        "unpinned",
        "no_proof",
        "wrong_target",
        "wrong_source",
        "wrong_kind",
        "missing_cause",
        "fake_cause",
        "future_cause",
        "wrong_tx",
    ],
)
def test_unproven_migration_never_promoted(fault: str) -> None:
    first, drop, new, migration = migration_case()
    proof = required_proof(migration)
    if fault == "no_proof":
        migration = replace(migration, evidence=None)
    else:
        if fault == "wrong_target":
            proof = replace(proof, related_pool=replace(NEW, pool_id="0x" + "dd" * 20))
        elif fault == "wrong_source":
            proof = replace(proof, pool=replace(OLD, pool_id="0x" + "dd" * 20))
        elif fault == "wrong_kind":
            proof = replace(proof, kind="FACTORY_SHUTDOWN")
        elif fault == "missing_cause":
            proof = replace(proof, causes=(drop.event_id,))
        elif fault == "fake_cause":
            proof = replace(proof, causes=("fake", new.event_id))
        elif fault == "future_cause":
            future = event("LIQUIDITY", 30, liquidity=0)
            proof = replace(proof, causes=(future.event_id, new.event_id))
        elif fault == "wrong_tx":
            proof = replace(proof, transaction_hash=block(999))
        migration = replace(migration, evidence=proof)
    snapshot = journal(
        first, drop, new, migration, proofs=() if fault == "unpinned" else (proof,)
    ).replay()
    assert snapshot.migrations[0].status == "MIGRATION_CANDIDATE"


@pytest.mark.parametrize("removed_index", [1, 2, 3])
def test_removed_causal_log_rolls_back_confirmation(removed_index: int) -> None:
    events = migration_case()
    j = journal(*events, proofs=(required_proof(events[3]),))
    before = j.replay()
    assert before.migrations[0].status == "MIGRATION_CONFIRMED"
    assert j.append(replace(events[removed_index], removed=True))
    after = j.replay()
    assert all(m.status != "MIGRATION_CONFIRMED" for m in after.migrations)
    assert before.migrations[0].status == "MIGRATION_CONFIRMED"
    assert len(j.events) == 5


def test_removed_retirement_restores_pool_and_is_idempotent() -> None:
    e = retirement()
    j = journal(event(), e, proofs=(required_proof(e),))
    before = j.replay()
    tombstone = replace(e, removed=True)
    assert j.append(tombstone)
    assert not j.append(tombstone) and not j.append(e)
    assert required_pool(j.replay(), OLD).status == "ACTIVE"
    assert j.list_active_pools() == (OLD,)
    assert required_pool(before, OLD).status == "POOL_RETIRED"
    assert j.get_pool_history(OLD) == (event(), e, tombstone)


def test_removed_before_original_never_resurrects() -> None:
    e = retirement()
    j = journal(replace(e, removed=True), event(), e, proofs=(required_proof(e),))
    assert required_pool(j.replay(), OLD).status == "ACTIVE"
    assert (
        j.replay()
        == journal(event(), e, replace(e, removed=True), proofs=(required_proof(e),)).replay()
    )


def test_explicit_reorg_is_dated_and_retains_audit() -> None:
    e = retirement()
    j = journal(event(), e, proofs=(required_proof(e),))
    before = j.replay()
    assert j.reorg_rollback(e.block_hash, block_number=30, block_hash=block(30))
    assert not j.reorg_rollback(e.block_hash, block_number=30, block_hash=block(30))
    assert required_pool(j.replay(), OLD).status == "ACTIVE"
    assert j.as_of(20, block(20)) == before
    assert len(j.get_pool_history(OLD)) == 3


def test_same_height_replacement_hash_is_not_deduplicated() -> None:
    old = event("LIQUIDITY", 20, liquidity=1)
    new = replace(old, block_hash=block(200), liquidity=999)
    j = journal(event(), old, replace(old, removed=True), new)
    assert old.event_id != new.event_id and len(j.events) == 4
    assert required_pool(j.replay(), OLD).liquidity == 999
    assert required_pool(j.as_of(20, old.block_hash), OLD).liquidity == 1000


def test_competing_tip_hash_selects_exact_branch() -> None:
    a = event("LIQUIDITY", 20, liquidity=1)
    b = replace(a, block_hash=block(200), liquidity=9)
    j = journal(event(), a, b)
    assert required_pool(j.as_of(20, a.block_hash), OLD).liquidity == 1
    assert required_pool(j.as_of(20, b.block_hash), OLD).liquidity == 9
    with pytest.raises(ValueError, match="ambiguous"):
        j.replay()
    with pytest.raises(ValueError, match="Ambiguous ancestor"):
        j.as_of(21, block(21))


def test_reorg_is_exact_hash_and_can_itself_be_revoked() -> None:
    a = event("LIQUIDITY", 20, liquidity=1)
    b = replace(a, block_hash=block(200), liquidity=9)
    j = journal(event(), a, b)
    j.reorg_rollback(a.block_hash, block_number=21, block_hash=block(21))
    assert required_pool(j.replay(), OLD).liquidity == 9
    j.reorg_rollback(block(21), block_number=22, block_hash=block(22))
    with pytest.raises(ValueError, match="Ambiguous ancestor"):
        j.replay()


def test_deterministic_permutations_and_duplicate_replay() -> None:
    e = retirement()
    events = (event(), e, replace(e, removed=True), event("REVIEW", 21, review_status="approved"))
    baseline = journal(*events, proofs=(required_proof(e),)).replay()
    for order in permutations(events):
        j = journal(*order, *order, proofs=(required_proof(e),))
        assert j.replay() == baseline and j.replay().sha256 == baseline.sha256
        assert journal(*j.events, proofs=(required_proof(e),)).replay() == baseline


@pytest.mark.parametrize("field", ["transaction_index", "log_index", "sequence"])
def test_cursor_order_overrides_arrival_order(field: str) -> None:
    a = event("REVIEW", 20, review_status="approved")
    updates: dict[str, Any] = {field: 1}
    b = replace(a, **updates, review_status="rejected")
    j = journal(b, event(), a)
    assert required_pool(j.replay(), OLD).review_status == "rejected"
    assert j.events[-2:] == (a, b)


def test_conflicting_duplicate_is_atomic() -> None:
    e = event()
    j = journal(e)
    for forged in (replace(e, price="99"), replace(e, removed=True, price="99")):
        with pytest.raises(ValueError, match="Conflicting event cursor"):
            j.append(forged)
    assert j.events == (e,)


def test_snapshot_and_payload_are_deeply_immutable() -> None:
    currencies = list(PAIR)
    e = event(currencies=currencies)
    j = journal(e)
    snapshot = j.replay()
    digest = snapshot.sha256
    currencies.clear()
    raw = e.to_dict()
    raw["currencies"].clear()
    j.append(event("REVIEW", 20, review_status="approved"))
    assert snapshot.sha256 == digest and required_pool(snapshot, OLD).review_status == "unknown"
    for value, field, replacement in (
        (e, "price", "9"),
        (snapshot, "block_number", 999),
        (snapshot.pools[0], "status", "POOL_RETIRED"),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(value, field, replacement)


def test_case_normalization_has_equal_identity_and_hash() -> None:
    mixed = replace(OLD, pool_id="0x" + "BB" * 20, venue_address="0x" + "AA" * 20)
    assert event(pool=mixed) == event()
    assert journal(event(pool=mixed)).replay().sha256 == journal(event()).replay().sha256


def test_migration_does_not_merge_native_wrapped_or_other_pair() -> None:
    first, drop, new, _ = migration_case()
    for currencies in (
        (AssetRef.native(CHAIN, "ETH"), PAIR[1]),
        (PAIR[0], AssetRef.erc20(TokenKey(CHAIN, "0x" + "33" * 20))),
    ):
        assert not journal(first, drop, replace(new, currencies=currencies)).replay().migrations


def test_pair_identity_includes_chain_and_venue() -> None:
    first, drop, new, _ = migration_case()
    other = replace(NEW, venue_address="0x" + "dd" * 20)
    snapshot = journal(first, drop, new, replace(new, pool=other, sequence=1)).replay()
    assert len(snapshot.pools) == 3 and len(snapshot.migrations) == 2
    with pytest.raises(ValueError, match="chain"):
        journal(replace(first, chain_id=56))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": "BOGUS"},
        {"block_number": -1},
        {"transaction_index": True},
        {"log_index": -1},
        {"sequence": -1},
        {"block_hash": "bad"},
        {"removed": 1},
        {"source_ref": ""},
        {"currencies": ()},
        {"price": "NaN"},
        {"price": "Infinity"},
        {"liquidity": -1},
    ],
)
def test_malformed_event_rejected(kwargs: dict[str, Any]) -> None:
    with pytest.raises((TypeError, ValueError)):
        replace(event(), **kwargs)


def test_future_evidence_rejected_before_journal_entry() -> None:
    e = retirement()
    with pytest.raises(ValueError, match="Future evidence"):
        replace(e, evidence=replace(required_proof(e), block_number=21))


def test_future_reorg_target_and_hash_height_mismatch_rejected() -> None:
    j = journal(event())
    with pytest.raises(ValueError, match="single height"):
        j.append(replace(event(), block_number=9))
    with pytest.raises(ValueError, match="future block"):
        j.reorg_rollback(block(10), block_number=9, block_hash=block(9))
    with pytest.raises(ValueError, match="hash/height"):
        j.as_of(20, block(10))
    with pytest.raises(ValueError, match="Unknown anchor"):
        j.as_of(10, block(999))


def test_empty_and_pre_discovery_snapshot() -> None:
    j = journal()
    assert j.as_of(0, block(0)).pools == ()
    with pytest.raises(ValueError, match="Empty journal"):
        j.replay()
    j.append(event())
    assert j.as_of(9, block(9)).pools == ()


def test_fixture_roundtrip_hash_and_complete_enumeration() -> None:
    raw = (FIXTURES / "synthetic_changes.jsonl").read_bytes()
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    assert hashlib.sha256(raw).hexdigest() == manifest["partitions"]["synthetic_changes.jsonl"]
    records = [json.loads(line) for line in raw.splitlines()]
    events = tuple(CatalogChangeEvent.from_dict(row) for row in records)
    assert [e.to_dict() for e in events] == records
    assert {e.kind for e in events} == set(get_args(ChangeType.__value__))
    assert any(e.removed for e in events)
    proofs = tuple(e.evidence for e in events if e.evidence is not None)
    j = journal(*events, proofs=proofs)
    assert journal(*reversed(events), proofs=proofs).replay().sha256 == j.replay().sha256
    assert required_pool(j.replay(), OLD).status == "INACTIVE_OBSERVED"
    assert j.as_of(22, block(22)).migrations[0].status == "MIGRATION_CONFIRMED"


@pytest.mark.parametrize("fault", ["extra", "missing", "pool_alias", "token_alias"])
def test_fixture_parser_rejects_ambiguous_shape(fault: str) -> None:
    raw = event().to_dict()
    if fault == "extra":
        raw["verified"] = True
    elif fault == "missing":
        raw.pop("removed")
    elif fault == "pool_alias":
        raw["pool"]["canonical_pool_id"] = NEW.pool_id
    else:
        raw["currencies"][0]["token_key"]["raw_address"] = NEW.pool_id
    with pytest.raises(ValueError):
        CatalogChangeEvent.from_dict(raw)


@pytest.mark.parametrize("index", [1, 2, 3])
def test_explicit_reorg_rolls_back_migration_cause(index: int) -> None:
    events = migration_case()
    j = journal(*events, proofs=(required_proof(events[3]),))
    assert j.replay().migrations[0].status == "MIGRATION_CONFIRMED"
    j.reorg_rollback(events[index].block_hash, block_number=30, block_hash=block(30))
    assert all(m.status != "MIGRATION_CONFIRMED" for m in j.replay().migrations)
    assert j.as_of(22, block(22)).migrations[0].status == "MIGRATION_CONFIRMED"


def test_new_pool_before_drop_also_remains_candidate() -> None:
    first, drop, new, _ = migration_case()
    new = replace(new, block_number=19, block_hash=block(19))
    snapshot = journal(drop, new, first).replay()
    assert snapshot.migrations[0].status == "MIGRATION_CANDIDATE"
    assert set(snapshot.migrations[0].causes) == {drop.event_id, new.event_id}


@pytest.mark.parametrize("liquidity", [1000, 2000])
def test_unchanged_or_increased_liquidity_is_not_a_drop(liquidity: int) -> None:
    first, drop, new, _ = migration_case()
    snapshot = journal(first, replace(drop, liquidity=liquidity), new).replay()
    assert not snapshot.migrations


def test_removed_price_and_review_restore_previous_values() -> None:
    price = event("LIQUIDITY", 20, liquidity=42, price="999")
    review = event("REVIEW", 21, review_status="rejected")
    j = journal(event(), price, review)
    assert required_pool(j.replay(), OLD).price == "999"
    j.append(replace(price, removed=True))
    j.append(replace(review, removed=True))
    state = required_pool(j.replay(), OLD)
    assert (state.price, state.liquidity, state.review_status) == ("2", 1000, "unknown")


def test_removal_is_log_precise_within_one_block() -> None:
    a = event("LIQUIDITY", 20, liquidity=1)
    b = event("REVIEW", 20, review_status="approved", log_index=1)
    j = journal(event(), a, b, replace(a, removed=True))
    state = required_pool(j.replay(), OLD)
    assert state.liquidity == 1000 and state.review_status == "approved"


def test_migration_proof_before_its_causes_is_not_retroactively_confirmed() -> None:
    first, drop, new, migration = migration_case()
    proof = replace(required_proof(migration), block_number=19, block_hash=block(19))
    migration = replace(migration, block_number=19, block_hash=block(19), evidence=proof)
    snapshot = journal(first, migration, drop, new, proofs=(proof,)).replay()
    assert snapshot.migrations[0].status == "MIGRATION_CANDIDATE"


def test_reorg_before_target_arrival_is_still_effective() -> None:
    j = journal(event())
    retired = retirement()
    j.reorg_rollback(retired.block_hash, block_number=30, block_hash=block(30))
    j.append(retired)
    assert required_pool(j.replay(), OLD).status == "ACTIVE"
    assert retired in j.get_pool_history(OLD)


def test_future_event_changes_do_not_change_past_hash() -> None:
    events = migration_case()
    proof = required_proof(events[3])
    j = journal(events[0], proofs=(proof,))
    past = j.as_of(10, block(10))
    for e in events[1:]:
        j.append(e)
        assert j.as_of(10, block(10)).sha256 == past.sha256
    assert past.events == (events[0],)
