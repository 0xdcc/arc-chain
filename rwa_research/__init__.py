"""RWA research domain models, discrete liquidity observation, and strict codecs."""

from __future__ import annotations

from .classify import (
    CrossPoolCandidate,
    EquityBasisResearch,
    ResearchClassification,
    classify_research_record,
)
from .codec import canonical_hash, from_dict, to_canonical_json, to_dict
from .models import (
    AmountQuotePoint,
    EligibilityMatrix,
    EquityReference,
    InstrumentBinding,
    OracleObservation,
    RwaResearchRecord,
)
from .normalize import (
    ExecutionPricePoint,
    NormalizedHoldings,
    NormalizedOraclePrice,
    NormalizedRestQuote,
    calculate_execution_price_point,
    calculate_signed_basis,
    normalize_holdings,
    normalize_oracle_price,
    normalize_rest_equity_quote,
    validate_corporate_action_continuity,
)
from .quote_inputs import (
    DiscreteQuotePair,
    QuoteValidationResult,
    assemble_bidirectional_quotes,
    parse_quote_point_from_dict,
    validate_quote_point,
)
from .report import generate_jsonl_records, generate_markdown_report
from .validity import (
    CapabilityCheckResult,
    EquityValidityResult,
    OracleValidityResult,
    ValidityStatus,
    check_capability_eligibility,
    validate_equity_reference,
    validate_oracle_observation,
)

__all__ = [
    "AmountQuotePoint",
    "CapabilityCheckResult",
    "CrossPoolCandidate",
    "DiscreteQuotePair",
    "EligibilityMatrix",
    "EquityBasisResearch",
    "EquityReference",
    "EquityValidityResult",
    "ExecutionPricePoint",
    "InstrumentBinding",
    "NormalizedHoldings",
    "NormalizedOraclePrice",
    "NormalizedRestQuote",
    "OracleObservation",
    "OracleValidityResult",
    "QuoteValidationResult",
    "ResearchClassification",
    "RwaResearchRecord",
    "ValidityStatus",
    "assemble_bidirectional_quotes",
    "calculate_execution_price_point",
    "calculate_signed_basis",
    "canonical_hash",
    "check_capability_eligibility",
    "classify_research_record",
    "from_dict",
    "generate_jsonl_records",
    "generate_markdown_report",
    "normalize_holdings",
    "normalize_oracle_price",
    "normalize_rest_equity_quote",
    "parse_quote_point_from_dict",
    "to_canonical_json",
    "to_dict",
    "validate_corporate_action_continuity",
    "validate_equity_reference",
    "validate_oracle_observation",
    "validate_quote_point",
]
