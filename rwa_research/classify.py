"""Classification engine for dual-track RWA research and candidate material extraction."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from arbitrage_contracts import PoolKey, TokenKey

from .models import RwaResearchRecord
from .normalize import calculate_signed_basis
from .quote_inputs import assemble_bidirectional_quotes, validate_quote_point
from .validity import validate_equity_reference, validate_oracle_observation


@dataclass(frozen=True, slots=True)
class CrossPoolCandidate:
    """Candidate material for same-token cross-pool arbitrage to be verified by W2."""

    token_key: TokenKey
    buy_pool_key: PoolKey
    sell_pool_key: PoolKey
    amount_in_atoms: int
    buy_effective_price: Fraction
    sell_effective_price: Fraction
    raw_spread_ratio: Fraction
    quote_currency: str
    state_version_ref: str | None
    evidence_level: str = "candidate_for_w2_validation"
    oracle_status: str = "unknown"
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EquityBasisResearch:
    """Read-only research observation of token basis against traditional equity or issuer."""

    underlier_id: str
    token_key: TokenKey
    reference_price_usd: Fraction
    observed_token_price_usd: Fraction | None
    signed_basis: Fraction | None
    session_kind: str
    research_category: str = "RESEARCH_ONLY"
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResearchClassification:
    """Structured dual-track classification output for an RWA research record."""

    record_id: str
    cross_pool_candidates: tuple[CrossPoolCandidate, ...]
    basis_research: EquityBasisResearch | None
    rejected_reasons: tuple[str, ...]
    is_cross_pool_found: bool


def classify_research_record(
    record: RwaResearchRecord,
    quote_currency: str = "USDG",
) -> ResearchClassification:
    """Classify RwaResearchRecord into CrossPoolCandidate and read-only EquityBasisResearch (C24-C27)."""
    rejected_reasons: list[str] = list(record.reasons)
    candidates: list[CrossPoolCandidate] = []

    # 1. Oracle & Equity Reference validation
    oracle_validity = None
    oracle_status_str = "no_oracle"
    if record.oracle is not None:
        oracle_validity = validate_oracle_observation(record.oracle, record.as_of_ms)
        oracle_status_str = oracle_validity.status
        if not oracle_validity.is_valid:
            rejected_reasons.extend(oracle_validity.reasons)

    equity_validity = None
    if record.equity_ref is not None:
        equity_validity = validate_equity_reference(record.equity_ref, record.as_of_ms)
        if not equity_validity.is_valid:
            rejected_reasons.extend(equity_validity.reasons)

    # 2. Track 1: Same-token cross-independent-pool candidate search (C25, C26)
    pool_pairs = assemble_bidirectional_quotes(record.quotes, quote_currency=quote_currency)

    # C25: Dedup physical pools (ensure different PoolKey)
    valid_pools: list[tuple[PoolKey, Any, Any]] = []
    for pk, pair in pool_pairs.items():
        if pair.is_bidirectional_valid and pair.zero_for_one_quote and pair.one_for_zero_quote:
            res_sell = validate_quote_point(pair.zero_for_one_quote, quote_currency=quote_currency)
            res_buy = validate_quote_point(pair.one_for_zero_quote, quote_currency=quote_currency)
            if res_sell.is_valid and res_buy.is_valid and res_sell.effective_price_point and res_buy.effective_price_point:
                valid_pools.append((pk, res_sell.effective_price_point, res_buy.effective_price_point))
        else:
            rejected_reasons.extend(pair.reasons)

    # Cross-compare valid independent pools for same token
    for i in range(len(valid_pools)):
        for j in range(len(valid_pools)):
            if i == j:
                continue  # C25: Do not compare pool against itself
            pk_buy, _, pt_buy = valid_pools[i]
            pk_sell, pt_sell, _ = valid_pools[j]

            # Buying on Pool i at pt_buy.effective_token_price_in_quote
            # Selling on Pool j at pt_sell.effective_token_price_in_quote
            buy_price = pt_buy.effective_token_price_in_quote
            sell_price = pt_sell.effective_token_price_in_quote

            if sell_price > buy_price:
                spread_ratio = (sell_price - buy_price) / buy_price
                cand = CrossPoolCandidate(
                    token_key=record.instrument.token_key,
                    buy_pool_key=pk_buy,
                    sell_pool_key=pk_sell,
                    amount_in_atoms=pt_buy.amount_in_atoms,
                    buy_effective_price=buy_price,
                    sell_effective_price=sell_price,
                    raw_spread_ratio=spread_ratio,
                    quote_currency=quote_currency,
                    state_version_ref=pool_pairs[pk_buy].state_version_ref,
                    evidence_level="candidate_for_w2_validation",
                    oracle_status=oracle_status_str,
                    reasons=(f"Gross spread: {float(spread_ratio * 100):+.2f}%",),
                )
                candidates.append(cand)

    # 3. Track 2: Equity Basis Research (C24: strictly RESEARCH_ONLY, never trade order)
    basis_res: EquityBasisResearch | None = None
    if record.equity_ref is not None:
        ref_price_usd = Fraction(record.equity_ref.bid_price)
        observed_price_usd: Fraction | None = None
        signed_basis: Fraction | None = None

        if oracle_validity is not None and oracle_validity.is_valid and record.oracle:
            observed_price_usd = Fraction(record.oracle.answer, 10**record.instrument.feed_decimals)
            if ref_price_usd > 0:
                signed_basis = calculate_signed_basis(observed_price_usd, ref_price_usd)

        basis_reasons = []
        if signed_basis is not None:
            basis_reasons.append(f"Signed basis: {float(signed_basis * 100):+.2f}%")
        if equity_validity and not equity_validity.is_valid:
            basis_reasons.extend(equity_validity.reasons)

        basis_res = EquityBasisResearch(
            underlier_id=record.instrument.underlier_id,
            token_key=record.instrument.token_key,
            reference_price_usd=ref_price_usd,
            observed_token_price_usd=observed_price_usd,
            signed_basis=signed_basis,
            session_kind=record.equity_ref.session_kind,
            research_category="RESEARCH_ONLY",
            reasons=tuple(basis_reasons),
        )

    # C27: Honest accounting - if no candidates found, document in rejected_reasons
    if not candidates:
        rejected_reasons.append("No profitable cross-pool candidates found among eligible independent pools")

    return ResearchClassification(
        record_id=record.record_id,
        cross_pool_candidates=tuple(candidates),
        basis_research=basis_res,
        rejected_reasons=tuple(sorted(set(rejected_reasons))),
        is_cross_pool_found=len(candidates) > 0,
    )
