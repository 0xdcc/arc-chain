"""Unit tests verifying lossless contract bridge and StateVersion.applied_cursor serialization."""

from __future__ import annotations

from arbitrage_contracts import ContractRecord
from arbitrage_contracts.identity import (
    Amount,
    AssetRef,
    FeeModel,
    PoolDescriptor,
    PoolKey,
    TokenKey,
)
from arbitrage_contracts.quote import (
    ActorScope,
    DataMode,
    EvidenceLevel,
    HopRef,
    QuoteEvidence,
    RouteRef,
)
from arbitrage_contracts.serialization import (
    canonical_record_hash,
    decode_record_json,
    encode_record_json,
)
from arbitrage_contracts.state import Cursor, StateVersion


def test_applied_cursor_lossless_roundtrip() -> None:
    """Verifies that StateVersion with non-null applied_cursor roundtrips losslessly."""
    cursor = Cursor(
        block_hash="0x" + "aa" * 32,
        transaction_hash="0x" + "bb" * 32,
        transaction_index=12,
        log_index=45,
    )
    state = StateVersion(
        chain_id=4663,
        block_number=58239320,
        block_hash="0x" + "cc" * 32,
        received_at_ms=1725900010000,
        applied_cursor=cursor,
        complete_through_block=58239320,
        completeness="ready",
    )

    record = ContractRecord(
        schema_id="arbitrage-evidence",
        schema_version="1.0.0",
        record_type="state_version",
        run_id="run-bridge-01",
        data_mode=DataMode.CONFIRMED_CHAIN_HISTORY,
        provenance={"witness": "w4_bridge_test"},
        payload=state,
    )

    encoded = encode_record_json(record)
    assert '"applied_cursor":' in encoded
    assert '"log_index":45' in encoded
    assert '"transaction_index":12' in encoded

    decoded = decode_record_json(encoded)
    assert isinstance(decoded.payload, StateVersion)
    assert decoded.payload.applied_cursor is not None
    assert isinstance(decoded.payload.applied_cursor, Cursor)

    dec_cursor = decoded.payload.applied_cursor
    assert dec_cursor.block_hash == cursor.block_hash
    assert dec_cursor.transaction_hash == cursor.transaction_hash
    assert dec_cursor.transaction_index == cursor.transaction_index
    assert dec_cursor.log_index == cursor.log_index
    assert dec_cursor == cursor

    assert decoded.payload.block_number == state.block_number
    assert decoded.payload.block_hash == state.block_hash
    assert decoded.payload.completeness == "ready"

    # Canonical hash stability
    assert canonical_record_hash(record) == canonical_record_hash(decoded)


def test_applied_cursor_collision_resistance() -> None:
    """Verifies that different cursor values yield distinct record hashes."""
    c1 = Cursor(block_hash="0x" + "aa" * 32, log_index=1)
    c2 = Cursor(block_hash="0x" + "aa" * 32, log_index=2)

    s1 = StateVersion(
        chain_id=4663,
        block_number=100,
        block_hash="0x" + "cc" * 32,
        received_at_ms=1000,
        applied_cursor=c1,
    )
    s2 = StateVersion(
        chain_id=4663,
        block_number=100,
        block_hash="0x" + "cc" * 32,
        received_at_ms=1000,
        applied_cursor=c2,
    )

    r1 = ContractRecord(
        schema_id="arbitrage-evidence",
        schema_version="1.0.0",
        record_type="state_version",
        run_id="run-01",
        data_mode=DataMode.SYNTHETIC,
        provenance={},
        payload=s1,
    )
    r2 = ContractRecord(
        schema_id="arbitrage-evidence",
        schema_version="1.0.0",
        record_type="state_version",
        run_id="run-01",
        data_mode=DataMode.SYNTHETIC,
        provenance={},
        payload=s2,
    )

    assert canonical_record_hash(r1) != canonical_record_hash(r2)


def test_state_version_with_null_cursor() -> None:
    """Verifies that StateVersion with applied_cursor=None encodes and decodes properly."""
    state = StateVersion(
        chain_id=4663,
        block_number=100,
        block_hash="0x" + "cc" * 32,
        received_at_ms=1000,
        applied_cursor=None,
    )
    record = ContractRecord(
        schema_id="arbitrage-evidence",
        schema_version="1.0.0",
        record_type="state_version",
        run_id="run-02",
        data_mode=DataMode.SYNTHETIC,
        provenance={},
        payload=state,
    )

    encoded = encode_record_json(record)
    assert '"applied_cursor":null' in encoded
    decoded = decode_record_json(encoded)
    assert decoded.payload.applied_cursor is None


def test_quote_evidence_bridge() -> None:
    """Verifies that QuoteEvidence with RouteRef roundtrips through contract serialization."""
    weth = AssetRef("erc20", 4663, TokenKey(4663, "0x" + "1" * 40))
    usdg = AssetRef("erc20", 4663, TokenKey(4663, "0x" + "2" * 40))
    pk1 = PoolKey(4663, "uniswap_v3", "factory", "0x" + "a" * 40, "address", "0x" + "b" * 40)
    pk2 = PoolKey(4663, "uniswap_v3", "factory", "0x" + "a" * 40, "address", "0x" + "c" * 40)

    desc1 = PoolDescriptor(pk1, weth, usdg, FeeModel.static(500))
    desc2 = PoolDescriptor(pk2, weth, usdg, FeeModel.static(3000))

    hop1 = HopRef(pk1, weth, usdg, "zero_for_one", desc1)
    hop2 = HopRef(pk2, usdg, weth, "one_for_zero", desc2)
    route = RouteRef(4663, weth, (hop1, hop2), max_hops=3)

    amount_in = Amount(weth, 10**18, 18)
    amount_out = Amount(weth, 10**18 + 5000, 18)

    evidence = QuoteEvidence(
        quote_id="quote-w4-bridge-01",
        route_ref=route,
        amount_in=amount_in,
        amount_out=amount_out,
        delta_atoms=5000,
        evidence_level=EvidenceLevel.LOCAL_QUOTE,
        data_mode=DataMode.SYNTHETIC,
        actor_scope=ActorScope.OWN_AUTHORIZED,
    )

    record = ContractRecord(
        schema_id="arbitrage-evidence",
        schema_version="1.0.0",
        record_type="quote_evidence",
        run_id="run-03",
        data_mode=DataMode.SYNTHETIC,
        provenance={},
        payload=evidence,
    )

    encoded = encode_record_json(record)
    decoded = decode_record_json(encoded)
    assert isinstance(decoded.payload, QuoteEvidence)
    assert decoded.payload.quote_id == "quote-w4-bridge-01"
    assert decoded.payload.delta_atoms == 5000
    assert decoded.payload.route_ref.route_id == route.route_id
