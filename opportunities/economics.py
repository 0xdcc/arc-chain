"""Pure, offline economic evaluation of quote evidence.

The evaluator deliberately does not trust an existing economic assessment.  It
requires explicit, exact-asset conversion evidence and returns unknown whenever
execution cost or valuation evidence is incomplete.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from arbitrage_contracts.identity import AssetRef
from arbitrage_contracts.quote import (
    EconomicAssessment,
    GasEvidence,
    PriceEvidence,
    QuoteEvidence,
    QuoteStatus,
    TriState,
)

BASE_TO_QUOTE = "base_to_quote"
QUOTE_TO_BASE = "quote_to_base"
_CONVERSION_DIRECTIONS = frozenset({BASE_TO_QUOTE, QUOTE_TO_BASE})
_CONVERSION_PRICE_TYPES = frozenset({"actual_fill"})
_GAS_KINDS_REQUIRING_COMPLETE_COST = frozenset(
    {"rpc_estimate", "atomic_simulation", "confirmed_actual"}
)


class EconomicsInputError(ValueError):
    """Raised for malformed caller-supplied conversion or evaluation input."""


@dataclass(frozen=True, slots=True)
class AssetConversionInput:
    """Directional price evidence for converting one exact asset to another."""

    price_evidence: PriceEvidence
    direction: str

    def __post_init__(self) -> None:
        if not isinstance(self.price_evidence, PriceEvidence):
            raise EconomicsInputError("price_evidence must be PriceEvidence")
        if self.direction not in _CONVERSION_DIRECTIONS:
            raise EconomicsInputError(f"Invalid conversion direction: {self.direction!r}")
        if self.price_evidence.base_asset == self.price_evidence.quote_asset:
            raise EconomicsInputError("Conversion assets must be distinct")
        if self.price_evidence.price_type not in _CONVERSION_PRICE_TYPES:
            raise EconomicsInputError("Conversion price_type must be actual_fill")
        if (
            self.price_evidence.price_numerator is None
            or self.price_evidence.price_denominator is None
        ):
            raise EconomicsInputError("Conversion has no rational price")
        if not _has_source(self.price_evidence):
            raise EconomicsInputError("Conversion has no provenance")
        if self.price_evidence.market_status != "active":
            raise EconomicsInputError("Conversion market is not active")
        if (
            self.price_evidence.timestamp_ms is None
            or self.price_evidence.valid_until_ms is None
            or self.price_evidence.valid_until_ms < self.price_evidence.timestamp_ms
        ):
            raise EconomicsInputError("Conversion lacks valid timestamp and expiry")


def _has_source(evidence: PriceEvidence | GasEvidence) -> bool:
    return bool(evidence.source_refs) or bool(evidence.evidence_ref)


def _validated_conversion(
    conversion_inputs: Sequence[AssetConversionInput],
) -> dict[tuple[AssetRef, AssetRef], AssetConversionInput]:
    conversions: dict[tuple[AssetRef, AssetRef], AssetConversionInput] = {}
    for conversion in conversion_inputs:
        evidence = conversion.price_evidence
        source = (
            evidence.base_asset if conversion.direction == BASE_TO_QUOTE else evidence.quote_asset
        )
        target = (
            evidence.quote_asset if conversion.direction == BASE_TO_QUOTE else evidence.base_asset
        )
        key = (source, target)
        if key in conversions:
            raise EconomicsInputError(f"Duplicate conversion for asset pair: {key!r}")
        conversions[key] = conversion
    return conversions


def _usable_conversion(
    conversions: dict[tuple[AssetRef, AssetRef], AssetConversionInput],
    source: AssetRef,
    target: AssetRef,
    now_ms: int,
) -> tuple[int, int] | None:
    if source == target:
        return (1, 1)
    conversion = conversions.get((source, target))
    if conversion is None:
        return None
    evidence = conversion.price_evidence
    if (
        evidence.valid_until_ms is None
        or now_ms > evidence.valid_until_ms
        or (evidence.timestamp_ms is not None and now_ms < evidence.timestamp_ms)
    ):
        return None

    numerator = evidence.price_numerator
    denominator = evidence.price_denominator
    if numerator is None or denominator is None:
        return None
    if conversion.direction == BASE_TO_QUOTE:
        return numerator, denominator
    return denominator, numerator


def _convert_rational(
    amount_atoms: int,
    ratio: tuple[int, int],
    *,
    cost: bool,
) -> int:
    if amount_atoms == 0:
        return 0
    numerator, denominator = ratio
    product = amount_atoms * numerator
    if cost:
        if product >= 0:
            return -(-product // denominator)
        return -product // denominator
    return product // denominator


def _economic_status(net_atoms: int | None) -> str:
    if net_atoms is None:
        return "unknown"
    if net_atoms > 0:
        return "profitable"
    if net_atoms < 0:
        return "unprofitable"
    return "evaluated"


def _calculation_refs(
    quote_evidence: QuoteEvidence,
    gas_evidence: GasEvidence | None,
    conversion_inputs: Sequence[AssetConversionInput],
) -> tuple[str, ...]:
    refs: list[str] = [*quote_evidence.source_refs]
    if gas_evidence is not None:
        refs.extend(gas_evidence.source_refs)
        if gas_evidence.evidence_ref is not None:
            refs.append(gas_evidence.evidence_ref)
    for conversion in conversion_inputs:
        refs.extend(conversion.price_evidence.source_refs)
        if conversion.price_evidence.evidence_ref is not None:
            refs.append(conversion.price_evidence.evidence_ref)
    return tuple(dict.fromkeys(ref for ref in refs if ref))


def _gas_cost_atoms(
    quote_evidence: QuoteEvidence,
    base_asset: AssetRef,
    now_ms: int,
    conversions: dict[tuple[AssetRef, AssetRef], AssetConversionInput],
) -> int | None:
    gas_evidence = quote_evidence.gas_evidence
    if gas_evidence is None or gas_evidence.gas_kind not in _GAS_KINDS_REQUIRING_COMPLETE_COST:
        return None
    if quote_evidence.actor_scope == "third_party":
        return None
    if not gas_evidence.is_applicable:
        return None
    if gas_evidence.payer_asset is None or not gas_evidence.payer_subject:
        return None
    if gas_evidence.gas_units is None or gas_evidence.gas_price_atoms is None:
        return None
    if gas_evidence.l1_fee_atoms is None:
        return None
    if gas_evidence.valid_until_ms is None or now_ms > gas_evidence.valid_until_ms:
        return None
    if gas_evidence.quoted_at_ms is not None and now_ms < gas_evidence.quoted_at_ms:
        return None
    if not gas_evidence.block_ref or not _has_source(gas_evidence):
        return None

    source_assessment = quote_evidence.economic_assessment
    source_components = source_assessment.fee_components if source_assessment else ()
    gas_components = [
        component for component in source_components if component.deduction_stage == "gas"
    ]
    if len(gas_components) > 1:
        raise EconomicsInputError("More than one gas fee component is not allowed")
    if not gas_components:
        return None
    gas_component = gas_components[0]
    if gas_component.asset != gas_evidence.payer_asset:
        raise EconomicsInputError("Gas component asset does not match gas payer asset")
    if gas_component.component_id in gas_evidence.already_included_components:
        raise EconomicsInputError("Already-included gas component must not be extra fee_components")

    gas_evidence_cost = gas_evidence.gas_units * gas_evidence.gas_price_atoms + (
        gas_evidence.l1_fee_atoms or 0
    )
    if gas_component.amount_atoms != gas_evidence_cost:
        raise EconomicsInputError("Gas component amount does not match complete gas evidence")
    if gas_evidence_cost == 0:
        return 0

    component_ratio = _usable_conversion(conversions, gas_component.asset, base_asset, now_ms)
    if component_ratio is None:
        return None
    converted_component = _convert_rational(gas_component.amount_atoms, component_ratio, cost=True)
    return converted_component


def evaluate_quote_evidence(
    quote_evidence: QuoteEvidence,
    now_ms: int,
    conversion_inputs: Sequence[AssetConversionInput],
    usd_asset: AssetRef | None = None,
) -> EconomicAssessment:
    """Evaluate a quote without network, wallet, or prior assessment trust.

    ``net_atoms`` is denominated in the route base asset.  Revenue is rounded
    down and costs are rounded away from zero.  Malformed conversion input and
    duplicate gas components raise :class:`EconomicsInputError`; missing or
    unusable economic evidence instead returns an unknown assessment.
    """
    if type(now_ms) is not int or isinstance(now_ms, bool) or now_ms < 0:
        raise EconomicsInputError("now_ms must be a non-negative integer")
    if usd_asset is not None and usd_asset == quote_evidence.route_ref.base_asset:
        raise EconomicsInputError("usd_asset must be distinct from the route base asset")
    conversions = _validated_conversion(conversion_inputs)

    source_assessment = quote_evidence.economic_assessment
    existing_components = source_assessment.fee_components if source_assessment else ()
    if quote_evidence.status != QuoteStatus.QUOTED or quote_evidence.delta_atoms is None:
        return EconomicAssessment(
            net_atoms=None,
            net_usd_micros=None,
            economic_status="unknown",
            fee_components=existing_components,
            calculation_refs=_calculation_refs(
                quote_evidence, quote_evidence.gas_evidence, conversion_inputs
            ),
            is_estimated=True,
        )
    if quote_evidence.fee_included == TriState.UNKNOWN:
        return EconomicAssessment(
            net_atoms=None,
            net_usd_micros=None,
            economic_status="unknown",
            fee_components=existing_components,
            calculation_refs=_calculation_refs(
                quote_evidence, quote_evidence.gas_evidence, conversion_inputs
            ),
            is_estimated=True,
        )
    if quote_evidence.impact_included == TriState.UNKNOWN:
        return EconomicAssessment(
            net_atoms=None,
            net_usd_micros=None,
            economic_status="unknown",
            fee_components=existing_components,
            calculation_refs=_calculation_refs(
                quote_evidence, quote_evidence.gas_evidence, conversion_inputs
            ),
            is_estimated=True,
        )

    base_asset = quote_evidence.route_ref.base_asset
    base_delta = quote_evidence.delta_atoms
    assert base_delta is not None
    total_cost_atoms: int | None = 0
    for component in existing_components:
        if component.deduction_stage == "gas":
            continue
        ratio = _usable_conversion(conversions, component.asset, base_asset, now_ms)
        if ratio is None:
            total_cost_atoms = None
            break
        converted = _convert_rational(component.amount_atoms, ratio, cost=True)
        total_cost_atoms = (total_cost_atoms or 0) - converted

    gas_cost_atoms = (
        None
        if total_cost_atoms is None
        else _gas_cost_atoms(quote_evidence, base_asset, now_ms, conversions)
    )
    if total_cost_atoms is None or gas_cost_atoms is None:
        total_cost_atoms = None
    else:
        total_cost_atoms = (total_cost_atoms or 0) - gas_cost_atoms

    net_atoms = None if total_cost_atoms is None else base_delta + total_cost_atoms
    net_usd_micros: int | None = None
    if net_atoms is not None and usd_asset is not None:
        usd_ratio = _usable_conversion(conversions, base_asset, usd_asset, now_ms)
        if usd_ratio is not None:
            if net_atoms >= 0:
                net_usd_micros = _convert_rational(net_atoms, usd_ratio, cost=False)
            else:
                net_usd_micros = -_convert_rational(-net_atoms, usd_ratio, cost=False)

    return EconomicAssessment(
        net_atoms=net_atoms,
        net_usd_micros=net_usd_micros,
        economic_status=_economic_status(net_atoms),
        fee_components=existing_components,
        gas_cost_atoms=gas_cost_atoms,
        calculation_refs=_calculation_refs(
            quote_evidence, quote_evidence.gas_evidence, conversion_inputs
        ),
        is_estimated=True,
    )
