"""Asset qualification, permission gates, and market eligibility evaluation for Arc."""

from __future__ import annotations

from arc_readiness.errors import ArcValidationError
from arc_readiness.models import (
    ARC_USDC_ERC20_ADDRESS,
    ArcAssetEligibilityDraft,
    ArcMarketEligibilityDraft,
    validate_address,
    validate_string,
)

ARC_CANONICAL_EURC_ADDRESS = "0x89b50855aa3be2f677cd6303cec089b5f319d72a"
ARC_CANONICAL_USYC_ADDRESS = "0xe9185f0c5f296ed1797aae4238d26ccabeadb86c"
ARC_CANONICAL_FX_ESCROW_ADDRESS = "0xd68256f4d69c6bbecb873d8588ae0dc6b8e22e10"

SYSTEM_PRECOMPILES = frozenset(
    {
        "0x1800000000000000000000000000000000000000",
        "0x3600000000000000000000000000000000000000",
        "0xfffffffffffffffffffffffffffffffffffffffe",
    }
)


def evaluate_asset_eligibility(
    asset_id: str,
    symbol: str,
    decimals: int,
    interface_kind: str,
    contract_address: str | None = None,
    code_size: int | None = None,
    has_entitlements: bool = False,
) -> ArcAssetEligibilityDraft:
    """Evaluate and qualify an asset candidate on Arc chain."""
    v_asset_id = validate_string(asset_id, "asset_id")
    v_symbol = validate_string(symbol, "symbol")
    addr = validate_address(contract_address, "contract_address") if contract_address else None

    reasons: list[str] = []
    review_status = "discovered"
    decimals_status = "verified"
    is_usdc_domain = False

    if interface_kind == "native":
        if v_symbol.upper() == "USDC":
            is_usdc_domain = True
            review_status = "verified"
            reasons.append("native_gas_token")
    elif interface_kind == "erc20":
        if addr == ARC_USDC_ERC20_ADDRESS:
            is_usdc_domain = True
            review_status = "verified"
            reasons.append("canonical_erc20_usdc_predeploy")
        elif addr == ARC_CANONICAL_EURC_ADDRESS:
            review_status = "verified"
            reasons.append("canonical_eurc_euro_stablecoin")
            # EURC is Euro-denominated, never USD pegged
        elif addr == ARC_CANONICAL_USYC_ADDRESS:
            reasons.append("institutional_yield_token_fund_share")
            if not has_entitlements:
                review_status = "provisional"
                reasons.append("USYC_RESTRICTED_INVESTMENT_ALLOWLIST_REQUIRED")
            else:
                review_status = "verified"
        else:
            # General ERC-20 token
            if code_size is not None and code_size == 0 and addr not in SYSTEM_PRECOMPILES:
                review_status = "rejected"
                reasons.append("DEPLOYMENT_CODE_EMPTY")
            else:
                review_status = "discovered"
    else:
        raise ArcValidationError(f"Invalid interface_kind: {interface_kind!r}")

    return ArcAssetEligibilityDraft(
        asset_id=v_asset_id,
        symbol=v_symbol,
        decimals=decimals,
        contract_address=addr,
        interface_kind=interface_kind,
        review_status=review_status,
        decimals_status=decimals_status,
        is_usdc_native_domain=is_usdc_domain,
        reasons=tuple(reasons),
    )


def evaluate_market_eligibility(
    market_id: str,
    protocol_id: str,
    pool_address: str,
    base_asset: ArcAssetEligibilityDraft,
    quote_asset: ArcAssetEligibilityDraft,
    is_deprecated_venue: bool = False,
    hooks_verified: bool = True,
) -> ArcMarketEligibilityDraft:
    """Evaluate candidate pool/market eligibility on Arc chain."""
    # 1. Domain circularity check
    if base_asset.is_usdc_native_domain and quote_asset.is_usdc_native_domain:
        raise ArcValidationError(
            f"Market {market_id} pairs identical underlying USDC domains: circular pair forbidden"
        )

    if base_asset.asset_id == quote_asset.asset_id:
        raise ArcValidationError("Market base and quote assets cannot be identical")

    reasons: list[str] = []
    can_quote = "supported"
    can_simulate = "unknown"
    can_atomic_execute = "unsupported"

    # Deprecated / Closed market check
    if is_deprecated_venue:
        reasons.append("DEPRECATED_MARKET_VENUE")
        return ArcMarketEligibilityDraft(
            market_id=market_id,
            protocol_id=protocol_id,
            pool_address=validate_address(pool_address, "pool_address"),
            base_asset_id=base_asset.asset_id,
            quote_asset_id=quote_asset.asset_id,
            can_quote="unsupported",
            can_simulate="unsupported",
            can_atomic_execute="unsupported",
            reasons=tuple(reasons),
        )

    # Permissioned RFQ / StableFX check
    if protocol_id.lower() == "stablefx":
        reasons.append("permissioned_rfq_not_atomic_amm")
        # StableFX requires off-chain API authentication and settlement escrow
        can_quote = "unknown"
        can_simulate = "unknown"
        can_atomic_execute = "unsupported"

    # Hooks verification
    if not hooks_verified:
        reasons.append("UNVERIFIED_DYNAMIC_HOOKS")
        can_quote = "unknown"

    # Asset eligibility dependency
    if base_asset.review_status == "rejected" or quote_asset.review_status == "rejected":
        reasons.append("CONTAINS_REJECTED_ASSET")
        can_quote = "unsupported"

    return ArcMarketEligibilityDraft(
        market_id=market_id,
        protocol_id=protocol_id,
        pool_address=validate_address(pool_address, "pool_address"),
        base_asset_id=base_asset.asset_id,
        quote_asset_id=quote_asset.asset_id,
        can_quote=can_quote,
        can_simulate=can_simulate,
        can_atomic_execute=can_atomic_execute,
        reasons=tuple(reasons),
    )
