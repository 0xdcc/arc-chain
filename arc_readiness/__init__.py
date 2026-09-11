"""arc_readiness: Domain models, adapters, and read-only tooling for Arc chain."""

from __future__ import annotations

from arc_readiness.balances import (
    SCALE_FACTOR,
    calculate_native_bounds_from_erc20,
    check_spending_permission,
    reconcile_dual_interface_balance,
    validate_trading_pair_domain,
)
from arc_readiness.catalog import ArcMarketCatalog
from arc_readiness.contract_bridge import (
    bridge_balance_observation_to_amounts,
    bridge_to_public_amount,
    bridge_to_public_asset_ref,
    bridge_to_public_pool_key,
    bridge_to_public_state_version,
)
from arc_readiness.eligibility import (
    ARC_CANONICAL_EURC_ADDRESS,
    ARC_CANONICAL_FX_ESCROW_ADDRESS,
    ARC_CANONICAL_USYC_ADDRESS,
    evaluate_asset_eligibility,
    evaluate_market_eligibility,
)
from arc_readiness.errors import (
    ArcContractBridgeError,
    ArcDualInterfaceMismatchError,
    ArcEventDeduplicationError,
    ArcMarketIneligibleError,
    ArcNetworkMismatchError,
    ArcReadinessError,
    ArcValidationError,
)
from arc_readiness.events import (
    TRANSFER_TOPIC,
    ZERO_ADDRESS,
    ArcEventJournal,
    deduplicate_transaction_events,
    is_ignorable_zero_or_self_transfer,
    parse_raw_event_log,
)
from arc_readiness.fees import (
    atoms_to_decimal_string,
    calculate_receipt_fee_atoms,
    estimate_max_fee_atoms,
    validate_fee_calculation,
)
from arc_readiness.models import (
    ARC_LEGACY_AUTHORITY_PRECOMPILE,
    ARC_SYSTEM_TRANSFER_EMITTER,
    ARC_TESTNET_CHAIN_ID,
    ARC_USDC_ERC20_ADDRESS,
    ArcAssetEligibilityDraft,
    ArcBalanceObservation,
    ArcEventRecordDraft,
    ArcFeeObservation,
    ArcMarketEligibilityDraft,
    ArcNetworkIdentity,
    ArcPermissionStatus,
)
from arc_readiness.network import (
    validate_block_timestamp_order,
    validate_network_identity,
)
from arc_readiness.quotes import (
    SingleHopQuoteRequest,
    SingleHopQuoteResult,
    execute_bidirectional_single_hop_quotes,
)
from arc_readiness.recording import (
    classify_rpc_failure,
    sanitize_rpc_payload,
)
from arc_readiness.reporting import (
    build_market_catalog_report,
    build_network_readiness_report,
)
from arc_readiness.rpc_readonly import (
    ALLOWED_READONLY_METHODS,
    FixedBlockSampler,
    ReadOnlyRpcTransport,
)

__all__ = [
    "ALLOWED_READONLY_METHODS",
    "ARC_CANONICAL_EURC_ADDRESS",
    "ARC_CANONICAL_FX_ESCROW_ADDRESS",
    "ARC_CANONICAL_USYC_ADDRESS",
    "ARC_LEGACY_AUTHORITY_PRECOMPILE",
    "ARC_SYSTEM_TRANSFER_EMITTER",
    "ARC_TESTNET_CHAIN_ID",
    "ARC_USDC_ERC20_ADDRESS",
    "SCALE_FACTOR",
    "TRANSFER_TOPIC",
    "ZERO_ADDRESS",
    "ArcAssetEligibilityDraft",
    "ArcBalanceObservation",
    "ArcContractBridgeError",
    "ArcDualInterfaceMismatchError",
    "ArcEventDeduplicationError",
    "ArcEventJournal",
    "ArcEventRecordDraft",
    "ArcFeeObservation",
    "ArcMarketCatalog",
    "ArcMarketEligibilityDraft",
    "ArcMarketIneligibleError",
    "ArcNetworkIdentity",
    "ArcNetworkMismatchError",
    "ArcPermissionStatus",
    "ArcReadinessError",
    "ArcValidationError",
    "FixedBlockSampler",
    "ReadOnlyRpcTransport",
    "SingleHopQuoteRequest",
    "SingleHopQuoteResult",
    "atoms_to_decimal_string",
    "bridge_balance_observation_to_amounts",
    "bridge_to_public_amount",
    "bridge_to_public_asset_ref",
    "bridge_to_public_pool_key",
    "bridge_to_public_state_version",
    "build_market_catalog_report",
    "build_network_readiness_report",
    "calculate_native_bounds_from_erc20",
    "calculate_receipt_fee_atoms",
    "check_spending_permission",
    "classify_rpc_failure",
    "deduplicate_transaction_events",
    "estimate_max_fee_atoms",
    "evaluate_asset_eligibility",
    "evaluate_market_eligibility",
    "execute_bidirectional_single_hop_quotes",
    "is_ignorable_zero_or_self_transfer",
    "parse_raw_event_log",
    "reconcile_dual_interface_balance",
    "sanitize_rpc_payload",
    "validate_block_timestamp_order",
    "validate_fee_calculation",
    "validate_network_identity",
    "validate_trading_pair_domain",
]
