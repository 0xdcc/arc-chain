"""Bridge adapters mapping Arc readiness models to public arbitrage_contracts."""

from __future__ import annotations

from arbitrage_contracts.identity import (
    Amount,
    AssetRef,
    PoolKey,
    TokenKey,
)
from arbitrage_contracts.state import StateVersion
from arc_readiness.errors import ArcContractBridgeError, ArcValidationError
from arc_readiness.models import (
    ArcAssetEligibilityDraft,
    ArcBalanceObservation,
    ArcMarketEligibilityDraft,
)


def bridge_to_public_asset_ref(
    draft: ArcAssetEligibilityDraft,
    chain_id: int,
) -> AssetRef:
    """Convert an ArcAssetEligibilityDraft to a public, immutable AssetRef."""
    domain_id = f"arc:{chain_id}:usdc_canonical" if draft.is_usdc_native_domain else None

    if draft.interface_kind == "native":
        return AssetRef.native(
            chain_id=chain_id,
            native_identifier=draft.symbol,
            balance_domain_id=domain_id,
        )
    elif draft.interface_kind == "erc20":
        if not draft.contract_address:
            raise ArcContractBridgeError("ERC-20 draft lacks contract address for TokenKey")
        token_key = TokenKey(
            chain_id=chain_id,
            address=draft.contract_address,
        )
        return AssetRef.erc20(
            token_key=token_key,
            balance_domain_id=domain_id,
        )
    else:
        raise ArcValidationError(f"Unknown interface_kind: {draft.interface_kind!r}")


def bridge_to_public_amount(
    atoms: int,
    draft: ArcAssetEligibilityDraft,
    chain_id: int,
) -> Amount:
    """Convert an atomic integer and asset draft to a public Amount."""
    asset_ref = bridge_to_public_asset_ref(draft, chain_id)
    return Amount(
        asset_ref=asset_ref,
        atoms=atoms,
        decimals=draft.decimals,
    )


def bridge_to_public_pool_key(
    market: ArcMarketEligibilityDraft,
    chain_id: int,
    factory_or_manager_address: str | None = None,
) -> PoolKey:
    """Convert an ArcMarketEligibilityDraft to a public PoolKey."""
    venue_kind = "manager" if "v4" in market.protocol_id.lower() else "factory"
    venue_addr = factory_or_manager_address or market.pool_address
    return PoolKey(
        chain_id=chain_id,
        protocol_id=market.protocol_id,
        venue_kind=venue_kind,
        venue_address=venue_addr,
        pool_id_kind="address",
        pool_id=market.pool_address,
    )


def bridge_to_public_state_version(
    chain_id: int,
    block_number: int,
    block_hash: str,
    received_at_ms: int,
    block_timestamp_s: int | None = None,
    source_ref: str | None = None,
    stale_reasons: tuple[str, ...] = (),
) -> StateVersion:
    """Construct a public StateVersion for Arc, enforcing block_domain='l1'."""
    return StateVersion(
        chain_id=chain_id,
        block_number=block_number,
        block_hash=block_hash,
        received_at_ms=received_at_ms,
        block_domain="l1",  # Mandatory invariant for Arc
        block_timestamp_s=block_timestamp_s,
        source_ref=source_ref,
        stale_reasons=stale_reasons,
        completeness="bootstrapping",
        finality="unknown",
    )


def bridge_balance_observation_to_amounts(
    obs: ArcBalanceObservation,
    draft_native: ArcAssetEligibilityDraft,
    draft_erc20: ArcAssetEligibilityDraft,
) -> dict[str, Amount | None]:
    """Convert observed dual-interface balance to public Amount values."""
    native_amt: Amount | None = None
    if obs.native_atoms is not None:
        native_amt = bridge_to_public_amount(obs.native_atoms, draft_native, obs.chain_id)

    erc20_amt: Amount | None = None
    if obs.erc20_atoms is not None:
        erc20_amt = bridge_to_public_amount(obs.erc20_atoms, draft_erc20, obs.chain_id)

    return {
        "native": native_amt,
        "erc20": erc20_amt,
    }
