"""Atomic execution input gate, validation models, and domain envelopes."""

from __future__ import annotations

from .inputs import (
    ROBINHOOD_CHAIN_ID,
    SUPPORTED_BASE_ADDRESSES,
    USDG_ADDRESS_4663,
    WETH_ADDRESS_4663,
    ZERO_ADDRESS,
    InputGateError,
    evaluate_candidate,
    load_candidate_from_dict,
    load_candidates_from_jsonl,
    match_state_version_ref,
    parse_candidate_json,
    validate_candidate,
    validate_opportunity,
)
from .models import (
    CandidateOpportunity,
    DraftSimulationEvidence,
    InputRejection,
    InputRejectionReason,
    ValidatedCandidate,
)

__all__ = [
    "ROBINHOOD_CHAIN_ID",
    "SUPPORTED_BASE_ADDRESSES",
    "USDG_ADDRESS_4663",
    "WETH_ADDRESS_4663",
    "ZERO_ADDRESS",
    "CandidateOpportunity",
    "DraftSimulationEvidence",
    "InputGateError",
    "InputRejection",
    "InputRejectionReason",
    "ValidatedCandidate",
    "evaluate_candidate",
    "load_candidate_from_dict",
    "load_candidates_from_jsonl",
    "match_state_version_ref",
    "parse_candidate_json",
    "validate_candidate",
    "validate_opportunity",
]
