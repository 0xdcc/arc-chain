"""Unit tests for W7 RWA research models, codecs, fail-closed boundaries, and roundtrip parity."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from arbitrage_contracts import AssetRef, PoolKey, StateVersion, TokenKey
from rwa_research import (
    AmountQuotePoint,
    EligibilityMatrix,
    EquityReference,
    InstrumentBinding,
    OracleObservation,
    RwaResearchRecord,
    canonical_hash,
    from_dict,
    to_canonical_json,
    to_dict,
)


def test_models_creation_and_fields(
    sample_instrument: InstrumentBinding,
    sample_oracle: OracleObservation,
    sample_equity_ref: EquityReference,
    sample_quote_point: AmountQuotePoint,
    sample_record: RwaResearchRecord,
) -> None:
    """Verify basic field access and properties of sample record."""
    assert sample_record.record_id == "rec:nvda:test:001"
    assert sample_record.schema_id == "w7-rwa-research"
    assert sample_record.schema_version == "0.1.0-draft"
    assert sample_record.is_draft is True
    assert sample_record.instrument.underlier_id == "NVDA"
    assert sample_record.oracle is not None
    assert sample_record.oracle.answer == 12550000000
    assert sample_record.oracle.multiplier_uint == 1050000000000000000
    assert sample_record.equity_ref is not None
    assert sample_record.equity_ref.bid_price == "120.50"
    assert len(sample_record.quotes) == 1
    assert sample_record.quotes[0].amount_in_atoms == 1000000000000000000


def test_codec_full_roundtrip(sample_record: RwaResearchRecord) -> None:
    """Verify full lossless roundtrip: record -> to_dict -> from_dict == record."""
    serialized = to_dict(sample_record)
    assert isinstance(serialized, dict)
    assert serialized["schema_id"] == "w7-rwa-research"
    assert serialized["schema_version"] == "0.1.0-draft"

    deserialized = from_dict(serialized)
    assert deserialized == sample_record
    assert deserialized.record_id == sample_record.record_id
    assert deserialized.instrument.token_key == sample_record.instrument.token_key
    assert deserialized.oracle == sample_record.oracle
    assert deserialized.equity_ref == sample_record.equity_ref
    assert deserialized.quotes == sample_record.quotes
    assert deserialized.eligibility == sample_record.eligibility


def test_codec_optional_sub_models_roundtrip(sample_instrument: InstrumentBinding) -> None:
    """Verify record with minimal required fields (None oracle/equity_ref/eligibility, empty quotes)."""
    minimal = RwaResearchRecord(
        record_id="rec:minimal:001",
        instrument=sample_instrument,
        as_of_ms=1700000000000,
        observed_at_ms=1700000001000,
    )
    serialized = to_dict(minimal)
    assert serialized["oracle"] is None
    assert serialized["equity_ref"] is None
    assert serialized["eligibility"] is None
    assert serialized["quotes"] == []

    deserialized = from_dict(serialized)
    assert deserialized == minimal
    assert deserialized.oracle is None
    assert deserialized.equity_ref is None


def test_canonical_json_and_hash_determinism(sample_record: RwaResearchRecord) -> None:
    """Verify canonical JSON produces sorted keys without whitespace, deterministic hash."""
    canonical_1 = to_canonical_json(sample_record)
    canonical_2 = to_canonical_json(sample_record)
    assert canonical_1 == canonical_2

    hash_1 = canonical_hash(sample_record)
    hash_2 = canonical_hash(sample_record)
    assert hash_1 == hash_2
    assert hash_1.startswith("sha256:")
    assert len(hash_1) == 7 + 64

    # Verify no whitespace around colons/commas
    assert ": " not in canonical_1
    assert ", " not in canonical_1


def test_rejection_of_floats_and_nan_inf(sample_record: RwaResearchRecord) -> None:
    """Verify fail-closed rejection when floats, NaN, or Inf are injected."""
    raw = to_dict(sample_record)

    # Injected float in as_of_ms
    raw_float = dict(raw)
    raw_float["as_of_ms"] = 1700000000000.5
    with pytest.raises(ValueError, match="Float values are strictly forbidden"):
        from_dict(raw_float)

    # Injected float in nested instrument
    raw_inst_float = dict(raw)
    raw_inst_float["instrument"] = dict(raw["instrument"])
    raw_inst_float["instrument"]["token_decimals"] = 18.0
    with pytest.raises(ValueError, match="Float values are strictly forbidden"):
        from_dict(raw_inst_float)


def test_rejection_of_boolean_atoms(sample_token_key: TokenKey, sample_usdg_key: TokenKey) -> None:
    """Verify booleans cannot masquerade as integer atoms."""
    pk = PoolKey(
        chain_id=4663,
        protocol_id="uniswap_v4",
        venue_kind="manager",
        venue_address="0x3333333333333333333333333333333333333333",
        pool_id_kind="bytes32",
        pool_id="0x" + "bb" * 32,
    )
    ain = AssetRef.erc20(sample_token_key)
    aout = AssetRef.erc20(sample_usdg_key)

    with pytest.raises(TypeError, match="amount_in_atoms must be an integer, got bool"):
        AmountQuotePoint(
            pool_key=pk,
            direction="zero_for_one",
            asset_in=ain,
            asset_out=aout,
            amount_in_atoms=True,
            quote_id="quote:bad:bool",
        )


def test_rejection_of_negative_or_zero_multiplier(sample_state_version: StateVersion) -> None:
    """Verify oracle multiplier must be strictly positive."""
    with pytest.raises(ValueError, match="multiplier_uint must be strictly positive"):
        OracleObservation(
            round_id=1,
            answer=100,
            started_at_s=100,
            updated_at_s=100,
            answered_in_round=1,
            multiplier_uint=0,
        )

    with pytest.raises(ValueError, match="multiplier_uint must be non-negative"):
        OracleObservation(
            round_id=1,
            answer=100,
            started_at_s=100,
            updated_at_s=100,
            answered_in_round=1,
            multiplier_uint=-1000,
        )


def test_rejection_of_invalid_schema_or_draft_tampering(sample_record: RwaResearchRecord) -> None:
    """Verify fail-closed rejection on schema_id or is_draft tampering."""
    raw = to_dict(sample_record)

    # Invalid schema_id
    raw_schema = dict(raw)
    raw_schema["schema_id"] = "arbitrage-evidence"
    with pytest.raises(ValueError, match="Invalid schema_id"):
        from_dict(raw_schema)

    # Invalid schema_version
    raw_ver = dict(raw)
    raw_ver["schema_version"] = "1.0.0"
    with pytest.raises(ValueError, match="Invalid schema_version"):
        from_dict(raw_ver)

    # is_draft=False tampering
    raw_draft = dict(raw)
    raw_draft["is_draft"] = False
    with pytest.raises(ValueError, match="is_draft must be True"):
        from_dict(raw_draft)

    # data_mode="confirmed_execution" tampering
    raw_exec = dict(raw)
    raw_exec["data_mode"] = "confirmed_execution"
    with pytest.raises(ValueError, match="data_mode cannot be 'confirmed_execution'"):
        from_dict(raw_exec)


def test_rejection_of_undeclared_extra_keys(sample_record: RwaResearchRecord) -> None:
    """Verify fail-closed rejection on undeclared keys in any payload section."""
    raw = to_dict(sample_record)

    # Extra key at root
    raw_root = dict(raw)
    raw_root["unexpected_field"] = "malicious"
    with pytest.raises(ValueError, match="Undeclared extra fields in RwaResearchRecord"):
        from_dict(raw_root)

    # Extra key in instrument
    raw_inst = dict(raw)
    raw_inst["instrument"] = dict(raw["instrument"])
    raw_inst["instrument"]["rogue_key"] = 123
    with pytest.raises(ValueError, match="Undeclared extra fields in InstrumentBinding"):
        from_dict(raw_inst)


def test_rejection_of_invalid_decimal_strings() -> None:
    """Verify equity bid/ask prices require strict finite decimal format."""
    with pytest.raises(ValueError, match="Invalid decimal format"):
        EquityReference(
            underlier_id="NVDA",
            currency="USD",
            bid_price="not_a_number",
            ask_price="120.00",
        )

    with pytest.raises(ValueError, match="Invalid decimal format for bid_price"):
        EquityReference(
            underlier_id="NVDA",
            currency="USD",
            bid_price="-12.50",
            ask_price="120.00",
        )


def test_fixture_models_load_and_manifest_integrity() -> None:
    """Verify that tests/fixtures/rwa/v1/models.json loads and matches manifest."""
    fixtures_dir = Path(__file__).resolve().parent.parent / "fixtures" / "rwa" / "v1"
    manifest_path = fixtures_dir / "manifest.json"
    assert manifest_path.exists()

    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest_data["format"] == "w7-fixture-manifest-v1"

    for entry in manifest_data["files"]:
        fpath = fixtures_dir / entry["relative_path"]
        assert fpath.exists()
        actual_sha = hashlib.sha256(fpath.read_bytes()).hexdigest()
        assert actual_sha == entry["sha256"]

    # Verify models.json parses into valid RwaResearchRecord objects
    models_path = fixtures_dir / "models.json"
    models_content = json.loads(models_path.read_text(encoding="utf-8"))
    assert models_content["format"] == "w7-rwa-models-fixture-v1"
    assert len(models_content["records"]) == 3

    for rec_dict in models_content["records"]:
        record = from_dict(rec_dict)
        assert isinstance(record, RwaResearchRecord)
        # Roundtrip check
        assert to_dict(record) == rec_dict
