"""Explicit cost units; unqualified symbols are not live asset identities."""

from __future__ import annotations

from dataclasses import dataclass

from arbitrage_contracts.arc_extensions import CostEvidence
from arbitrage_contracts.identity import AssetRef
from arbitrage_contracts.quote import DataMode


@dataclass(frozen=True)
class DenominatedCostEvidence(CostEvidence):
    """A cost bound to an asset, precision and the quote's canonical state reference."""

    asset_ref: AssetRef | None = None
    decimals: int | None = None
    state_ref: str | None = None


def rescale_cost_atoms(atoms: int, source_decimals: int, target_decimals: int) -> int:
    """Convert a nonnegative cost, rounding UP when the destination is coarser."""
    if type(atoms) is not int or atoms < 0:
        raise ValueError("Cost atoms must be a nonnegative integer")
    if any(type(d) is not int or not 0 <= d <= 255 for d in (source_decimals, target_decimals)):
        raise ValueError("Cost decimals must be explicit integers in [0, 255]")
    if source_decimals <= target_decimals:
        return atoms * 10 ** (target_decimals - source_decimals)
    scale = 10 ** (source_decimals - target_decimals)
    return (atoms + scale - 1) // scale


def cost_in_base_atoms(
    cost: CostEvidence,
    base: AssetRef,
    base_decimals: int,
    state_ref: str | None,
    data_mode: str,
    expected_kind: str,
) -> int:
    """Reject a mismatched denomination instead of subtracting unrelated integers."""
    if cost.kind != expected_kind or not cost.source:
        raise ValueError("Missing cost source or wrong cost kind")
    if type(cost.cost_atoms) is not int or cost.cost_atoms < 0:
        raise ValueError("Invalid cost atoms")
    if isinstance(cost, DenominatedCostEvidence):
        asset = cost.asset_ref
        if asset is None or cost.decimals is None:
            raise ValueError("Missing cost asset or decimals")
        same_domain = bool(
            asset.chain_id == base.chain_id
            and asset.balance_domain_id
            and asset.balance_domain_id == base.balance_domain_id
        )
        if asset != base and not same_domain:
            raise ValueError("Cost asset differs from base; conversion evidence is required")
        if data_mode != DataMode.SYNTHETIC and (not state_ref or cost.state_ref != state_ref):
            raise ValueError("Cost state reference does not match quote")
        return rescale_cost_atoms(cost.cost_atoms, cost.decimals, base_decimals)
    # Preserve the old synthetic micro-USDC fixtures, never promote them to live evidence.
    if (
        data_mode == DataMode.SYNTHETIC
        and cost.currency == "USDC"
        and base.balance_domain_id == "usdc_shared"
        and base_decimals == 6
    ):
        return cost.cost_atoms
    raise ValueError("Unqualified cost currency/precision; live denomination is UNKNOWN")
