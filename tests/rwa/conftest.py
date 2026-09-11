"""Pytest configuration and shared fixtures for W7 RWA research tests."""

from __future__ import annotations

import pytest

from arbitrage_contracts import (
    AssetRef,
    PoolDescriptor,
    PoolKey,
    StateVersion,
    TokenKey,
)
from rwa_research.models import (
    AmountQuotePoint,
    EligibilityMatrix,
    EquityReference,
    InstrumentBinding,
    OracleObservation,
    RwaResearchRecord,
)


@pytest.fixture
def sample_token_key() -> TokenKey:
    return TokenKey(chain_id=4663, address="0x1111111111111111111111111111111111111111")


@pytest.fixture
def sample_usdg_key() -> TokenKey:
    return TokenKey(chain_id=4663, address="0x5fc5360d0400a0fd4f2af552add042d716f1d168")


@pytest.fixture
def sample_instrument(sample_token_key: TokenKey) -> InstrumentBinding:
    return InstrumentBinding(
        token_key=sample_token_key,
        issuer_id="robinhood_rhj",
        underlier_id="NVDA",
        feed_address="0x2222222222222222222222222222222222222222",
        quote_currency="USD",
        token_decimals=18,
        feed_decimals=8,
        evidence_refs=("doc:rh_contracts_v1",),
        valid_from_ms=1700000000000,
    )


@pytest.fixture
def sample_state_version() -> StateVersion:
    return StateVersion(
        chain_id=4663,
        block_number=100000,
        block_hash="0x" + "aa" * 32,
        received_at_ms=1700000005000,
        block_domain="l2",
        completeness="ready",
        complete_through_block=100000,
    )


@pytest.fixture
def sample_oracle(sample_state_version: StateVersion) -> OracleObservation:
    return OracleObservation(
        round_id=18446744073709551617,
        answer=12550000000,  # $125.50
        started_at_s=1700000000,
        updated_at_s=1700000000,
        answered_in_round=18446744073709551617,
        multiplier_uint=1050000000000000000,  # 1.05e18
        state_version=sample_state_version,
        oracle_paused=False,
        heartbeat_s=86400,
        session_kind="regular",
        sequencer_status="up",
        grace_period_s=3600,
    )


@pytest.fixture
def sample_equity_ref() -> EquityReference:
    return EquityReference(
        underlier_id="NVDA",
        currency="USD",
        bid_price="120.50",
        ask_price="120.60",
        price_basis="underlying_share",
        generated_at_ms=1700000000000,
        received_at_ms=1700000001000,
        session_kind="regular",
        trading_halt=False,
        source_ref="rest:/rhj/prices/NVDA",
    )


@pytest.fixture
def sample_quote_point(sample_token_key: TokenKey, sample_usdg_key: TokenKey) -> AmountQuotePoint:
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
    return AmountQuotePoint(
        pool_key=pk,
        direction="zero_for_one",
        asset_in=ain,
        asset_out=aout,
        amount_in_atoms=1000000000000000000,  # 1 token
        amount_out_atoms=126000000,  # 126 USDG
        quote_id="quote:test:1",
        fee_included="yes",
        impact_included="yes",
        gas_estimate=120000,
        gas_evidence_kind="quoter_estimate",
        status="quoted",
        data_mode="synthetic",
        evidence_level="local_quote",
    )


@pytest.fixture
def sample_record(
    sample_instrument: InstrumentBinding,
    sample_oracle: OracleObservation,
    sample_equity_ref: EquityReference,
    sample_quote_point: AmountQuotePoint,
    sample_token_key: TokenKey,
) -> RwaResearchRecord:
    el = EligibilityMatrix(
        token_key=sample_token_key,
        capabilities={"hold": "verified_true", "primary_redeem": "verified_false"},
        evidence_refs=("kyb:issuer_rules",),
    )
    return RwaResearchRecord(
        record_id="rec:nvda:test:001",
        instrument=sample_instrument,
        as_of_ms=1700000000000,
        observed_at_ms=1700000005000,
        oracle=sample_oracle,
        equity_ref=sample_equity_ref,
        quotes=(sample_quote_point,),
        eligibility=el,
        data_mode="synthetic",
        reasons=("test_run",),
    )
