"""Unit tests for research classification engine and dual-track reporting (C24-C27)."""

from __future__ import annotations

from fractions import Fraction

import pytest

from arbitrage_contracts import AssetRef, PoolKey, TokenKey
from rwa_research import (
    AmountQuotePoint,
    EquityReference,
    InstrumentBinding,
    OracleObservation,
    RwaResearchRecord,
    classify_research_record,
    generate_jsonl_records,
    generate_markdown_report,
)


@pytest.fixture
def token_nvda() -> TokenKey:
    return TokenKey(4663, "0x1111111111111111111111111111111111111111")


@pytest.fixture
def token_usdg() -> TokenKey:
    return TokenKey(4663, "0x5fc5360d0400a0fd4f2af552add042d716f1d168")


@pytest.fixture
def sample_instrument(token_nvda: TokenKey) -> InstrumentBinding:
    return InstrumentBinding(
        token_key=token_nvda,
        issuer_id="robinhood_rhj",
        underlier_id="NVDA",
        feed_address="0x2222222222222222222222222222222222222222",
        quote_currency="USD",
        token_decimals=18,
        feed_decimals=8,
        valid_from_ms=1700000000000,
    )


def test_c24_equity_basis_strictly_research_only_no_orders(sample_instrument: InstrumentBinding) -> None:
    """C24: Equity basis (even large discount/premium) is strictly RESEARCH_ONLY with no execution orders."""
    # Underlier price $120.00, Token price $100.00 (large -16.67% discount)
    oracle = OracleObservation(
        round_id=1,
        answer=10000000000,  # $100.00
        started_at_s=1700000000,
        updated_at_s=1700000000,
        answered_in_round=1,
        multiplier_uint=1000000000000000000,  # 1.0
        heartbeat_s=86400,
    )
    eq = EquityReference(
        underlier_id="NVDA",
        currency="USD",
        bid_price="120.00",
        ask_price="120.50",
        generated_at_ms=1700000000000,
        received_at_ms=1700000001000,
    )
    rec = RwaResearchRecord(
        record_id="rec:basis:001",
        instrument=sample_instrument,
        as_of_ms=1700000001000,
        observed_at_ms=1700000002000,
        oracle=oracle,
        equity_ref=eq,
        quotes=(),
    )

    clf = classify_research_record(rec)
    assert clf.basis_research is not None
    assert clf.basis_research.research_category == "RESEARCH_ONLY"
    assert clf.basis_research.signed_basis == (Fraction(100, 1) - Fraction(120, 1)) / Fraction(120, 1)
    # Never produces execution plan or orders
    assert len(clf.cross_pool_candidates) == 0


def test_c25_physical_pool_deduplication_no_self_arbitrage(
    sample_instrument: InstrumentBinding, token_nvda: TokenKey, token_usdg: TokenKey
) -> None:
    """C25: A single physical pool referenced twice cannot form a cross-pool candidate."""
    pk = PoolKey(
        chain_id=4663,
        protocol_id="uniswap_v4",
        venue_kind="manager",
        venue_address="0x2222222222222222222222222222222222222222",
        pool_id_kind="bytes32",
        pool_id="0x" + "aa" * 32,
    )
    ain = AssetRef.erc20(token_nvda)
    aout = AssetRef.erc20(token_usdg)

    # Quotes on the exact same pool
    q_sell = AmountQuotePoint(
        pool_key=pk,
        direction="zero_for_one",
        asset_in=ain,
        asset_out=aout,
        amount_in_atoms=10**18,
        amount_out_atoms=125 * 10**6,
        quote_id="q_sell",
    )
    q_buy = AmountQuotePoint(
        pool_key=pk,
        direction="one_for_zero",
        asset_in=aout,
        asset_out=ain,
        amount_in_atoms=120 * 10**6,
        amount_out_atoms=10**18,
        quote_id="q_buy",
    )

    rec = RwaResearchRecord(
        record_id="rec:self_arb:002",
        instrument=sample_instrument,
        as_of_ms=1700000000000,
        observed_at_ms=1700000001000,
        quotes=(q_sell, q_buy),
    )

    clf = classify_research_record(rec)
    # Deduped: cannot buy and sell on the exact same pool key
    assert len(clf.cross_pool_candidates) == 0
    assert clf.is_cross_pool_found is False


def test_c26_cross_independent_pools_allowed_even_when_oracle_paused(
    sample_instrument: InstrumentBinding, token_nvda: TokenKey, token_usdg: TokenKey
) -> None:
    """C26: Two distinct independent pools can yield cross-pool candidates even when oracle is paused."""
    pk1 = PoolKey(
        chain_id=4663,
        protocol_id="uniswap_v4",
        venue_kind="manager",
        venue_address="0x2222222222222222222222222222222222222222",
        pool_id_kind="bytes32",
        pool_id="0x" + "11" * 32,
    )
    pk2 = PoolKey(
        chain_id=4663,
        protocol_id="uniswap_v4",
        venue_kind="manager",
        venue_address="0x3333333333333333333333333333333333333333",  # Distinct pool
        pool_id_kind="bytes32",
        pool_id="0x" + "22" * 32,
    )
    ain = AssetRef.erc20(token_nvda)
    aout = AssetRef.erc20(token_usdg)

    # Pool 1 quotes: Buy at 120 USDG
    q1_sell = AmountQuotePoint(
        pool_key=pk1, direction="zero_for_one", asset_in=ain, asset_out=aout,
        amount_in_atoms=10**18, amount_out_atoms=119 * 10**6, quote_id="q1_s"
    )
    q1_buy = AmountQuotePoint(
        pool_key=pk1, direction="one_for_zero", asset_in=aout, asset_out=ain,
        amount_in_atoms=120 * 10**6, amount_out_atoms=10**18, quote_id="q1_b"
    )

    # Pool 2 quotes: Sell at 125 USDG
    q2_sell = AmountQuotePoint(
        pool_key=pk2, direction="zero_for_one", asset_in=ain, asset_out=aout,
        amount_in_atoms=10**18, amount_out_atoms=125 * 10**6, quote_id="q2_s"
    )
    q2_buy = AmountQuotePoint(
        pool_key=pk2, direction="one_for_zero", asset_in=aout, asset_out=ain,
        amount_in_atoms=126 * 10**6, amount_out_atoms=10**18, quote_id="q2_b"
    )

    # Oracle is paused
    oracle_paused = OracleObservation(
        round_id=2, answer=12000000000, started_at_s=1700000000, updated_at_s=1700000000,
        answered_in_round=2, multiplier_uint=10**18, oracle_paused=True,
    )

    rec = RwaResearchRecord(
        record_id="rec:cross_pool:003",
        instrument=sample_instrument,
        as_of_ms=1700000000000,
        observed_at_ms=1700000001000,
        oracle=oracle_paused,
        quotes=(q1_sell, q1_buy, q2_sell, q2_buy),
    )

    clf = classify_research_record(rec)
    assert clf.is_cross_pool_found is True
    assert len(clf.cross_pool_candidates) == 1
    cand = clf.cross_pool_candidates[0]
    assert cand.buy_pool_key == pk1
    assert cand.sell_pool_key == pk2
    assert cand.evidence_level == "candidate_for_w2_validation"
    assert cand.oracle_status == "oracle_paused"
    assert cand.raw_spread_ratio == (Fraction(125, 1) - Fraction(120, 1)) / Fraction(120, 1)


def test_c27_honest_accounting_when_all_quotes_negative_or_reverted(
    sample_instrument: InstrumentBinding, token_nvda: TokenKey, token_usdg: TokenKey
) -> None:
    """C27: When all quotes fail or are negative, document reasons honestly; never fake positive opportunities."""
    pk = PoolKey(
        chain_id=4663,
        protocol_id="uniswap_v4",
        venue_kind="manager",
        venue_address="0x2222222222222222222222222222222222222222",
        pool_id_kind="bytes32",
        pool_id="0x" + "ff" * 32,
    )
    ain = AssetRef.erc20(token_nvda)
    aout = AssetRef.erc20(token_usdg)

    # Quoting reverted due to empty pool
    q_revert = AmountQuotePoint(
        pool_key=pk, direction="zero_for_one", asset_in=ain, asset_out=aout,
        amount_in_atoms=10**18, amount_out_atoms=None, quote_id="q_rev",
        status="reverted", error="NotEnoughLiquidity()",
    )

    rec = RwaResearchRecord(
        record_id="rec:all_failed:004",
        instrument=sample_instrument,
        as_of_ms=1700000000000,
        observed_at_ms=1700000001000,
        quotes=(q_revert,),
    )

    clf = classify_research_record(rec)
    assert clf.is_cross_pool_found is False
    assert len(clf.cross_pool_candidates) == 0
    assert any("No profitable cross-pool" in r for r in clf.rejected_reasons)

    # Test reporting functions
    md_rep = generate_markdown_report(clf, rec)
    assert "No cross-pool arbitrage candidates found" in md_rep
    assert "rec:all_failed:004" in md_rep

    jsonl = generate_jsonl_records([rec])
    assert "w7-rwa-research" in jsonl
    assert jsonl.endswith("\n")
