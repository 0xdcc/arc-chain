"""Independent reference parity comparator between local quotes and external reference quotes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from arbitrage_contracts.quote import QuoteEvidence, QuoteStatus
from arbitrage_contracts.state import is_canonical_state_ref


class ParityStatus(StrEnum):
    """Classification status of a quote parity comparison."""

    MATCH = "match"
    MISMATCH = "mismatch"
    REF_UNAVAILABLE = "ref_unavailable"
    REF_INPUT_MISMATCH = "ref_input_mismatch"
    REF_ERROR = "ref_error"


@dataclass(frozen=True, slots=True)
class ParityResult:
    """Structured result of comparing a local quote against an independent reference."""

    route_id: str
    local_evidence: QuoteEvidence
    reference_evidence: QuoteEvidence | None
    status: ParityStatus
    delta_diff_atoms: int | None
    error: str | None = None

    @property
    def is_match(self) -> bool:
        """Returns True if local quote matches the reference quote exactly."""
        return self.status == ParityStatus.MATCH


def compare_reference(
    local_quote: QuoteEvidence,
    reference_quote: QuoteEvidence | None,
) -> ParityResult:
    """Compares a local quote against an independent reference quote.

    Enforces:
    - Exact input alignment: route_id, input asset, and input atoms must be 100% identical.
    - Zero assumption on missing references: if reference is None, returns REF_UNAVAILABLE.
    - Preserves external errors: if reference returned an error or revert, returns REF_ERROR.
    - Exact atom delta matching: diff = local_delta - reference_delta.
    """
    route_id = local_quote.route_ref.route_id

    if reference_quote is None:
        return ParityResult(
            route_id=route_id,
            local_evidence=local_quote,
            reference_evidence=None,
            status=ParityStatus.REF_UNAVAILABLE,
            delta_diff_atoms=None,
            error="Independent reference quote unavailable",
        )

    # Validate exact input alignment
    if (
        local_quote.route_ref.route_id != reference_quote.route_ref.route_id
        or local_quote.amount_in.atoms != reference_quote.amount_in.atoms
        or local_quote.amount_in.asset_ref != reference_quote.amount_in.asset_ref
        or local_quote.amount_in.decimals != reference_quote.amount_in.decimals
        or local_quote.quote_id == reference_quote.quote_id
    ):
        return ParityResult(
            route_id=route_id,
            local_evidence=local_quote,
            reference_evidence=reference_quote,
            status=ParityStatus.REF_INPUT_MISMATCH,
            delta_diff_atoms=None,
            error="Input mismatch between local quote and reference quote",
        )

    # F02-C: State reference parity guard
    if (
        not is_canonical_state_ref(local_quote.state_version_ref)
        or not is_canonical_state_ref(reference_quote.state_version_ref)
        or local_quote.state_version_ref != reference_quote.state_version_ref
    ):
        return ParityResult(
            route_id=route_id,
            local_evidence=local_quote,
            reference_evidence=reference_quote,
            status=ParityStatus.REF_INPUT_MISMATCH,
            delta_diff_atoms=None,
            error=(
                f"State version mismatch: local quote ({local_quote.state_version_ref}) "
                f"!= reference quote ({reference_quote.state_version_ref})"
            ),
        )

    # Handle reference errors fail-closed
    if reference_quote.status != QuoteStatus.QUOTED:
        return ParityResult(
            route_id=route_id,
            local_evidence=local_quote,
            reference_evidence=reference_quote,
            status=ParityStatus.REF_ERROR,
            delta_diff_atoms=None,
            error=reference_quote.error or f"Reference failed with status {reference_quote.status}",
        )

    # Handle local quote failure
    if local_quote.status != QuoteStatus.QUOTED:
        return ParityResult(
            route_id=route_id,
            local_evidence=local_quote,
            reference_evidence=reference_quote,
            status=ParityStatus.MISMATCH,
            delta_diff_atoms=None,
            error=local_quote.error or f"Local quote failed with status {local_quote.status}",
        )

    assert local_quote.delta_atoms is not None
    assert reference_quote.delta_atoms is not None

    diff = local_quote.delta_atoms - reference_quote.delta_atoms
    status = ParityStatus.MATCH if diff == 0 else ParityStatus.MISMATCH

    return ParityResult(
        route_id=route_id,
        local_evidence=local_quote,
        reference_evidence=reference_quote,
        status=status,
        delta_diff_atoms=diff,
        error=None if status == ParityStatus.MATCH else f"Delta discrepancy of {diff} atoms",
    )
