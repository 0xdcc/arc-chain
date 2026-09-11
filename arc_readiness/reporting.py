"""Report generation engines for Arc network readiness and market catalog diagnostics."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from arc_readiness.catalog import ArcMarketCatalog
from arc_readiness.models import (
    ArcBalanceObservation,
    ArcFeeObservation,
    ArcNetworkIdentity,
)


def build_network_readiness_report(
    identity: ArcNetworkIdentity,
    balances: Sequence[ArcBalanceObservation],
    fees: Sequence[ArcFeeObservation],
) -> dict[str, Any]:
    """Construct an aggregated JSON-ready Arc network readiness report."""
    total_balances = len(balances)
    consistent_balances = sum(1 for b in balances if b.verified_consistency)

    total_fees = len(fees)
    receipt_fees = sum(1 for f in fees if f.fee_source == "receipt")

    readiness_status = "READINESS_OBSERVED"
    if total_balances > 0 and consistent_balances == 0:
        readiness_status = "DUAL_INTERFACE_INCONSISTENCY_DETECTED"
    elif identity.verification_status == "failed":
        readiness_status = "NETWORK_IDENTITY_FAILED"

    return {
        "status": readiness_status,
        "network_identity": identity.to_dict(),
        "summary": {
            "total_balance_observations": total_balances,
            "consistent_balance_observations": consistent_balances,
            "total_fee_observations": total_fees,
            "receipt_fee_observations": receipt_fees,
        },
        "balance_observations": [b.to_dict() for b in balances],
        "fee_observations": [f.to_dict() for f in fees],
    }


def build_market_catalog_report(catalog: ArcMarketCatalog) -> dict[str, Any]:
    """Construct an aggregated JSON-ready Arc market catalog diagnostic report."""
    return catalog.export_summary()
