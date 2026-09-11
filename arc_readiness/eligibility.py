"""Asset qualification, permission gates, and market eligibility evaluation for Arc."""

from __future__ import annotations

from arc_readiness.errors import ArcNetworkMismatchError, ArcValidationError
from arc_readiness.models import (
    ARC_USDC_ERC20_ADDRESS,
    ArcAssetEligibilityDraft,
    ArcMarketEligibilityDraft,
    validate_address,
    validate_string,
)
from arc_readiness.network import ARC_MAINNET_CHAIN_ID, ARC_TESTNET_CHAIN_ID

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
    chain_id: int = ARC_MAINNET_CHAIN_ID,
    contract_address: str | None = None,
    code_size: int | None = None,
    has_entitlements: bool = False,
    independent_audit_proof: str | None = None,
) -> ArcAssetEligibilityDraft:
    """Evaluate and qualify an asset candidate on Arc chain.

    Invariants:
    - Merely matching a constant testnet/historical address does NOT grant automatic VERIFIED status on mainnet.
    - Assets require explicit independent review proofs.
    - Zero double counting between Native and ERC-20 views.
    """
    if chain_id not in (ARC_MAINNET_CHAIN_ID, ARC_TESTNET_CHAIN_ID):
        raise ArcNetworkMismatchError(f"Unsupported Arc chain_id: {chain_id}")

    v_asset_id = validate_string(asset_id, "asset_id")
    v_symbol = validate_string(symbol, "symbol")
    addr = validate_address(contract_address, "contract_address") if contract_address else None

    reasons: list[str] = []
    review_status = "discovered"
    decimals_status = "verified"
    is_usdc_native_domain = False

    if interface_kind == "native":
        if v_symbol.upper() in ("USDC", "USDC_NATIVE"):
            is_usdc_native_domain = True
            review_status = "verified"
            reasons.append("native_gas_token")
    elif interface_kind == "erc20":
        if addr == ARC_USDC_ERC20_ADDRESS:
            is_usdc_native_domain = True
            review_status = "verified"
            reasons.append("canonical_erc20_usdc_predeploy")
        elif addr == ARC_CANONICAL_EURC_ADDRESS:
            # T10 fix: Testnet EURC constant address is NOT automatically verified on mainnet!
            if chain_id == ARC_MAINNET_CHAIN_ID and not independent_audit_proof:
                review_status = "discovered"
                reasons.append("EURC_TESTNET_CONSTANT_REQUIRES_MAINNET_AUDIT_PROOF")
            elif independent_audit_proof:
                review_status = "verified"
                reasons.append("canonical_eurc_euro_stablecoin")
            else:
                review_status = "provisional"
                reasons.append("testnet_eurc_constant")
        elif addr == ARC_CANONICAL_USYC_ADDRESS:
            reasons.append("institutional_yield_token_fund_share")
            if not has_entitlements:
                review_status = "provisional"
                reasons.append("USYC_RESTRICTED_INVESTMENT_ALLOWLIST_REQUIRED")
            else:
                review_status = "verified" if independent_audit_proof else "provisional"
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
        interface_kind=interface_kind,
        contract_address=addr,
        review_status=review_status,
        decimals_status=decimals_status,
        is_usdc_native_domain=is_usdc_native_domain,
        reasons=tuple(reasons),
    )


def evaluate_market_eligibility(
    market_id: str,
    protocol_id: str,
    pool_address: str,
    asset0: ArcAssetEligibilityDraft,
    asset1: ArcAssetEligibilityDraft,
    chain_id: int = ARC_MAINNET_CHAIN_ID,
) -> ArcMarketEligibilityDraft:
    """Evaluate market pair eligibility for Arc operations."""
    reasons: list[str] = []

    # Red line: Same asset balance domain pairs (e.g. native USDC vs ERC20 USDC) are strictly rejected!
    if asset0.is_usdc_native_domain and asset1.is_usdc_native_domain:
        return ArcMarketEligibilityDraft(
            market_id=market_id,
            protocol_id=protocol_id,
            pool_address=pool_address,
            base_asset_id=asset0.asset_id,
            quote_asset_id=asset1.asset_id,
            can_quote="unsupported",
            can_simulate="unsupported",
            can_atomic_execute="unsupported",
            reasons=("SELF_PAIRING_SAME_BALANCE_DOMAIN_REJECTED",),
        )

    if asset0.review_status == "verified" and asset1.review_status == "verified":
        can_quote = "supported"
    else:
        can_quote = "unsupported"
        reasons.append("ASSET_NOT_VERIFIED")

    return ArcMarketEligibilityDraft(
        market_id=market_id,
        protocol_id=protocol_id,
        pool_address=pool_address,
        base_asset_id=asset0.asset_id,
        quote_asset_id=asset1.asset_id,
        can_quote=can_quote,
        can_simulate="unknown",
        can_atomic_execute="unsupported",
        reasons=tuple(reasons),
    )
