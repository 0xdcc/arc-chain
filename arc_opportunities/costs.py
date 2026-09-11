"""Arc Financial Cost Evidence and Breakdown Contracts (T20)

Provides:
- Deterministic CostEvidence creation with explicit provenance and payer/beneficiary separation
- ArcCostBreakdown encapsulating on-chain gas, DEX fees, and external OTC financing
- Fail-closed UNKNOWN propagation: missing gas evidence NEVER defaults to 0 or profitable
- Strict Decimal / integer atom arithmetic without floating point drift
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal

from arbitrage_contracts.arc_extensions import CostEvidence
from arbitrage_contracts.identity import AssetRef
from arc_opportunities.cost_units import DenominatedCostEvidence


class CostInputError(ValueError):
    """Raised for malformed cost evidence inputs."""


@dataclass(frozen=True, slots=True)
class ArcCostBreakdown:
    """Audit-ready financial breakdown for an Arc opportunity."""

    base_asset: AssetRef
    gross_delta_atoms: int
    dex_fee_included_in_quote: bool
    gas_cost: CostEvidence | None
    otc_cost: CostEvidence | None = None
    additional_costs: tuple[CostEvidence, ...] = ()
    gas_payer: str | None = None
    beneficiary: str | None = None
    net_atoms: int | None = None
    economic_status: str = "unknown"  # "profitable", "unprofitable", "unknown"
    net_usd: Decimal | None = None
    calculation_notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.gross_delta_atoms) is not int or isinstance(self.gross_delta_atoms, bool):
            raise CostInputError("gross_delta_atoms must be an integer")
        if self.economic_status not in ("profitable", "unprofitable", "unknown", "evaluated"):
            raise CostInputError(f"Invalid economic_status: {self.economic_status}")


def create_gas_cost_evidence(
    cost_atoms: int,
    currency: str = "USDC",
    estimated_or_observed: str = "estimated",
    source: str = "arc_gas_model",
    gas_payer: str | None = None,
    beneficiary: str | None = None,
    as_of_timestamp: float = 0.0,
    *,
    asset_ref: AssetRef | None = None,
    decimals: int | None = None,
    state_ref: str | None = None,
) -> CostEvidence:
    """Create verified CostEvidence for gas costs, rejecting negative or made-up zero amounts."""
    if type(cost_atoms) is not int or isinstance(cost_atoms, bool):
        raise CostInputError(f"cost_atoms must be an integer, got {type(cost_atoms).__name__}")
    if cost_atoms < 0:
        raise CostInputError("cost_atoms cannot be negative")
    if not source:
        raise CostInputError("source cannot be empty")
    cost = CostEvidence(
        currency=currency,
        cost_atoms=cost_atoms,
        source=source,
        kind="gas",
        estimated_or_observed=estimated_or_observed,
        gas_payer=gas_payer,
        beneficiary=beneficiary,
        as_of_timestamp=as_of_timestamp,
    )
    if asset_ref is not None or decimals is not None or state_ref is not None:
        return DenominatedCostEvidence(
            **asdict(cost), asset_ref=asset_ref, decimals=decimals, state_ref=state_ref
        )
    return cost


def create_otc_cost_evidence(
    cost_atoms: int,
    currency: str = "USDC",
    estimated_or_observed: str = "estimated",
    source: str = "otc_channel",
    venue: str = "offchain_otc",
    as_of_timestamp: float = 0.0,
    *,
    asset_ref: AssetRef | None = None,
    decimals: int | None = None,
    state_ref: str | None = None,
) -> CostEvidence:
    """Create CostEvidence for external OTC / financing costs, kept distinct from on-chain gas."""
    if type(cost_atoms) is not int or isinstance(cost_atoms, bool):
        raise CostInputError("cost_atoms must be an integer")
    if cost_atoms < 0:
        raise CostInputError("cost_atoms cannot be negative")
    cost = CostEvidence(
        currency=currency,
        cost_atoms=cost_atoms,
        source=f"{source}:{venue}",
        kind="otc_channel",
        estimated_or_observed=estimated_or_observed,
        as_of_timestamp=as_of_timestamp,
    )
    if asset_ref is not None or decimals is not None or state_ref is not None:
        return DenominatedCostEvidence(
            **asdict(cost), asset_ref=asset_ref, decimals=decimals, state_ref=state_ref
        )
    return cost
