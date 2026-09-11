"""Fail-closed strategy attribution for W3 settled-cycle research records."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from arbitrage_contracts.identity import AssetRef
from research.settled_cycles.models import (
    ActionKind,
    AttributionStatus,
    CycleAction,
    EconomicStatus,
    ExecutionCostBreakdown,
    FeeComponentRecord,
    SubjectBalanceDelta,
    TransactionSubjects,
)

_COMPLEX_ACTIONS: Final[frozenset[ActionKind]] = frozenset(
    {ActionKind.MINT, ActionKind.BURN, ActionKind.LP, ActionKind.UNKNOWN}
)
_LOAN_ACTIONS: Final[frozenset[ActionKind]] = frozenset({ActionKind.BORROW, ActionKind.REPAY})
_INDEPENDENT_FEE_TOKENS: Final[frozenset[str]] = frozenset(
    {"none", "independent", "receipt", "trace"}
)
_ALREADY_IN_DELTA_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "effective_gas_price",
        "internal_payment",
        "subject_delta",
        "tx_state_diff",
        "vector",
    }
)
_TRACE_BASES: Final[frozenset[str]] = frozenset({"events", "trace_transfer"})

AssetIdentity = tuple[str, int, str | None, str | None]


@dataclass(frozen=True, slots=True)
class AttributionResult:
    """Immutable attribution result consumed by ``SettledCycleRecord``."""

    attribution_status: AttributionStatus
    economic_status: EconomicStatus
    attributed_net_atoms: int | None
    attributed_net_usd: Decimal | None
    attributed_asset: AssetRef | None
    reasons: tuple[str, ...]
    unexplained_flows: tuple[SubjectBalanceDelta, ...]


def _address(value: str) -> str:
    """Normalize a validated EVM address for comparisons."""
    return value.lower()


def _asset_identity(asset: AssetRef) -> tuple[str, int, str | None, str | None]:
    """Return a stable identity for an asset reference."""
    return (
        asset.interface_kind,
        asset.chain_id,
        asset.token_key.address if asset.token_key is not None else None,
        asset.native_identifier,
    )


def _principal_asset_key(asset: AssetRef) -> str:
    """Return the string identity used by the caller-provided price map."""
    if asset.token_key is not None:
        return asset.token_key.address
    return asset.native_identifier or ""


def _endpoints(direction: str | None) -> tuple[str, str] | None:
    """Return normalized transfer endpoints when direction is explicit."""
    if direction is None or direction.count("->") != 1:
        return None
    source, destination = direction.split("->", 1)
    if not source or not destination:
        return None
    return source.lower(), destination.lower()


def _successful(actions: tuple[CycleAction, ...]) -> tuple[CycleAction, ...]:
    """Return only successful actions; reverted branches never create profit."""
    return tuple(action for action in actions if action.execution_status == "success")


def _cycle_asset(actions: tuple[CycleAction, ...]) -> AssetRef | None:
    """Return the principal asset when successful swaps form one same-asset chain."""
    swaps = [action for action in actions if action.action_kind is ActionKind.SWAP]
    if not swaps:
        return None
    start = swaps[0].asset_in
    end = swaps[-1].asset_out
    if start is None or end is None or _asset_identity(start) != _asset_identity(end):
        return None
    return start


def _actor_deltas(
    subject_deltas: tuple[SubjectBalanceDelta, ...], actor: str
) -> dict[AssetIdentity, SubjectBalanceDelta]:
    """Group complete actor deltas by asset identity, rejecting duplicates."""
    result: dict[AssetIdentity, SubjectBalanceDelta] = {}
    duplicate = False
    for delta in subject_deltas:
        if _address(delta.subject_address) != _address(actor):
            continue
        if delta.completeness != "complete" or delta.delta_atoms is None:
            continue
        identity = _asset_identity(delta.asset)
        if identity in result:
            duplicate = True
        result[identity] = delta
    if duplicate:
        return {}
    return result


def _validate_conservation(
    subject_deltas: tuple[SubjectBalanceDelta, ...],
    fees: tuple[FeeComponentRecord, ...],
) -> tuple[bool, tuple[str, ...]]:
    """Check complete per-basis vectors, except native vectors that omit gas."""
    reasons: list[str] = []
    groups: dict[
        tuple[AssetIdentity, str],
        list[SubjectBalanceDelta],
    ] = defaultdict(list)
    native_independent_fee = any(
        component.asset.interface_kind == "native"
        and component.counted_in.lower() in _INDEPENDENT_FEE_TOKENS
        for component in fees
    )
    for delta in subject_deltas:
        identity = _asset_identity(delta.asset)
        groups[(identity, delta.basis)].append(delta)

    for (identity, basis), deltas in groups.items():
        if basis == "tx_state_diff" or not all(
            delta.completeness == "complete" for delta in deltas
        ):
            continue
        if identity[0] == "native" and native_independent_fee and basis in _TRACE_BASES:
            continue
        addresses = [delta.subject_address.lower() for delta in deltas]
        if len(addresses) != len(set(addresses)):
            reasons.append("duplicate_subject_delta")
        if any(delta.delta_atoms is None for delta in deltas):
            continue
        total = sum(delta.delta_atoms or 0 for delta in deltas)
        if total != 0:
            reasons.append(f"conservation_failed:{identity}")
        if any(delta.reconciliation_diff_atoms not in (0, None) for delta in deltas):
            reasons.append("reconciliation_diff_nonzero")
    return not reasons, tuple(dict.fromkeys(reasons))


def _validate_fees(
    fees: tuple[FeeComponentRecord, ...],
    subject_deltas: tuple[SubjectBalanceDelta, ...],
    delta_basis: str,
) -> tuple[bool, tuple[str, ...]]:
    """Validate dedup-key uniqueness and counted-in references."""
    reasons: list[str] = []
    dedup_counts: Counter[str] = Counter(
        component.dedup_key for component in fees if component.dedup_key
    )
    reasons.extend(
        f"dedup_key_conflict:{dedup_key}"
        for dedup_key, count in dedup_counts.items()
        if count != 1
    )

    subjects = {_address(delta.subject_address) for delta in subject_deltas}
    for component in fees:
        counted_in = component.counted_in.lower()
        if counted_in.startswith("subject_delta:"):
            referenced = counted_in.split(":", 1)[1]
            if referenced not in subjects:
                reasons.append(f"counted_in_dangling:{component.component_id}")
            continue
        if counted_in in _INDEPENDENT_FEE_TOKENS:
            continue
        if counted_in in _ALREADY_IN_DELTA_TOKENS:
            if delta_basis in _TRACE_BASES and counted_in not in {
                "effective_gas_price",
                "internal_payment",
            }:
                reasons.append(f"counted_in_basis_mismatch:{component.component_id}")
            continue
        reasons.append(f"counted_in_dangling:{component.component_id}")
    return not reasons, tuple(dict.fromkeys(reasons))


def _flash_loans(
    actions: tuple[CycleAction, ...], providers: frozenset[str]
) -> tuple[
    dict[AssetIdentity, int],
    dict[AssetIdentity, int],
    bool,
]:
    """Return borrow and repay principal totals plus counterparty identifiability."""
    provider_addresses = {_address(address) for address in providers}
    borrows: dict[AssetIdentity, int] = defaultdict(int)
    repays: dict[AssetIdentity, int] = defaultdict(int)
    identified = True
    for action in actions:
        if action.action_kind not in _LOAN_ACTIONS:
            continue
        endpoints = _endpoints(action.direction)
        asset = action.asset_in if action.action_kind is ActionKind.BORROW else action.asset_out
        amount = (
            action.amount_in_atoms
            if action.action_kind is ActionKind.BORROW
            else action.amount_out_atoms
        )
        if endpoints is None or asset is None or amount is None:
            identified = False
            break
        source, destination = endpoints
        identity = _asset_identity(asset)
        is_borrow_edge = (
            source in provider_addresses
            and destination not in provider_addresses
            and action.action_kind is ActionKind.BORROW
        )
        is_repay_edge = (
            destination in provider_addresses
            and source not in provider_addresses
            and action.action_kind is ActionKind.REPAY
        )
        if not provider_addresses or not (is_borrow_edge or is_repay_edge):
            identified = False
            break
        target = borrows if action.action_kind is ActionKind.BORROW else repays
        target[identity] += amount
    return dict(borrows), dict(repays), identified


def _capital_inflows(
    actions: tuple[CycleAction, ...], actor: str, providers: frozenset[str]
) -> tuple[dict[AssetIdentity, int], bool]:
    """Return explicit non-loan capital transfers into the strategy actor."""
    provider_addresses = {_address(address) for address in providers}
    inflows: dict[AssetIdentity, int] = defaultdict(int)
    parseable = True
    for action in actions:
        if action.action_kind is not ActionKind.TRANSFER:
            continue
        endpoints = _endpoints(action.direction)
        asset = action.asset_in or action.asset_out
        amount = action.amount_in_atoms or action.amount_out_atoms
        if endpoints is None or asset is None or amount is None:
            parseable = False
            continue
        source, destination = endpoints
        if destination == _address(actor) and source not in provider_addresses:
            inflows[_asset_identity(asset)] += amount
    return dict(inflows), parseable


def _economic_status(net: int | None, status: AttributionStatus) -> EconomicStatus:
    """Return a fail-closed economic state for the chosen attribution state."""
    if net is None or status in {
        AttributionStatus.AMBIGUOUS_COMPLEX_TX,
        AttributionStatus.INCONSISTENT,
        AttributionStatus.PARTIALLY_ATTRIBUTED,
        AttributionStatus.UNVERIFIED_MISSING_TRACE,
    }:
        return EconomicStatus.UNKNOWN
    return EconomicStatus.POSITIVE if net > 0 else EconomicStatus.NONPOSITIVE


def attribute_cycle(
    actions: tuple[CycleAction, ...],
    subject_deltas: tuple[SubjectBalanceDelta, ...],
    subjects: TransactionSubjects,
    cost_breakdown: ExecutionCostBreakdown,
    *,
    trace_available: bool,
    strategy_actor: str | None = None,
    flash_loan_providers: frozenset[str] = frozenset(),
    price_lookup: Mapping[str, Decimal] | None = None,
) -> AttributionResult:
    """Attribute a settled transaction to a strategy subject.

    Missing traces, inconsistent vectors, ambiguous complex actions, and open
    loan debts fail closed.  Explicit external capital is separated from cycle
    profit, and only independent fees borne by the strategy are subtracted.
    """
    del subjects  # Explicitly injected strategy_actor is authoritative when present.
    if not trace_available:
        return AttributionResult(
            AttributionStatus.UNVERIFIED_MISSING_TRACE,
            EconomicStatus.UNKNOWN,
            None,
            None,
            None,
            ("trace_missing",),
            (),
        )

    successful_actions = _successful(actions)
    reasons: list[str] = []
    delta_basis = "mixed" if len({delta.basis for delta in subject_deltas}) != 1 else (
        subject_deltas[0].basis if subject_deltas else "trace_transfer"
    )
    conservation_ok, conservation_reasons = _validate_conservation(
        subject_deltas, cost_breakdown.components
    )
    fees_ok, fee_reasons = _validate_fees(
        cost_breakdown.components, subject_deltas, delta_basis
    )
    reasons.extend(conservation_reasons)
    reasons.extend(fee_reasons)
    if fees_ok is False or conservation_ok is False:
        return AttributionResult(
            AttributionStatus.INCONSISTENT,
            EconomicStatus.UNKNOWN,
            None,
            None,
            None,
            tuple(dict.fromkeys(reasons)),
            (),
        )

    actor = strategy_actor
    if actor is None:
        return AttributionResult(
            AttributionStatus.PARTIALLY_ATTRIBUTED,
            EconomicStatus.UNKNOWN,
            None,
            None,
            None,
            ("strategy_actor_unknown",),
            (),
        )

    cycle_asset = _cycle_asset(successful_actions)
    if cycle_asset is None:
        reasons.append("cycle_not_detected")
    cycle_identity = None if cycle_asset is None else _asset_identity(cycle_asset)
    if any(action.action_kind in _COMPLEX_ACTIONS for action in successful_actions):
        actor_deltas = _actor_deltas(subject_deltas, actor)
        return AttributionResult(
            AttributionStatus.AMBIGUOUS_COMPLEX_TX,
            EconomicStatus.UNKNOWN,
            None,
            None,
            cycle_asset,
            tuple(dict.fromkeys([*reasons, "unseparable_complex_action"])),
            tuple(actor_deltas.values()),
        )

    if cycle_identity is None or cycle_asset is None:
        return AttributionResult(
            AttributionStatus.PARTIALLY_ATTRIBUTED,
            EconomicStatus.UNKNOWN,
            None,
            None,
            None,
            tuple(dict.fromkeys([*reasons, "principal_asset_unknown"])),
            (),
        )
    reasons.append("cycle_candidate")
    actor_deltas = _actor_deltas(subject_deltas, actor)
    if cycle_identity not in actor_deltas:
        return AttributionResult(
            AttributionStatus.PARTIALLY_ATTRIBUTED,
            EconomicStatus.UNKNOWN,
            None,
            None,
            cycle_asset,
            tuple(dict.fromkeys([*reasons, "strategy_delta_missing"])),
            (),
        )
    other_nonzero = [
        delta
        for identity, delta in actor_deltas.items()
        if identity != cycle_identity and delta.delta_atoms not in (0, None)
    ]
    if other_nonzero:
        return AttributionResult(
            AttributionStatus.AMBIGUOUS_COMPLEX_TX,
            EconomicStatus.UNKNOWN,
            None,
            None,
            cycle_asset,
            tuple(dict.fromkeys([*reasons, "multiple_strategy_assets"])),
            tuple(actor_deltas.values()),
        )

    principal_delta = actor_deltas[cycle_identity]
    raw_delta = principal_delta.delta_atoms
    assert raw_delta is not None
    borrows, repays, loans_identified = _flash_loans(
        successful_actions, flash_loan_providers
    )
    if not loans_identified:
        return AttributionResult(
            AttributionStatus.PARTIALLY_ATTRIBUTED,
            EconomicStatus.UNKNOWN,
            None,
            None,
            cycle_asset,
            tuple(dict.fromkeys([*reasons, "flash_loan_debt_unknown"])),
            tuple(actor_deltas.values()),
        )
    if borrows or repays:
        reasons.append("flash_loan_principal_excluded")
        raw_delta += repays.get(cycle_identity, 0) - borrows.get(cycle_identity, 0)
        outstanding = borrows.get(cycle_identity, 0) - repays.get(cycle_identity, 0)
        if outstanding != 0:
            return AttributionResult(
                AttributionStatus.PARTIALLY_ATTRIBUTED,
                EconomicStatus.UNKNOWN,
                None,
                None,
                cycle_asset,
                tuple(dict.fromkeys([*reasons, f"flash_loan_debt_open:{outstanding}"])),
                tuple(actor_deltas.values()),
            )

    capital, capital_parseable = _capital_inflows(
        successful_actions, actor, flash_loan_providers
    )
    if not capital_parseable:
        return AttributionResult(
            AttributionStatus.AMBIGUOUS_COMPLEX_TX,
            EconomicStatus.UNKNOWN,
            None,
            None,
            cycle_asset,
            tuple(dict.fromkeys([*reasons, "external_flow_unparseable"])),
            tuple(actor_deltas.values()),
        )
    capital_amount = capital.get(cycle_identity, 0)
    unexplained_flows: tuple[SubjectBalanceDelta, ...] = ()
    if capital_amount:
        reasons.append(f"external_capital_separated:{capital_amount}")
        unexplained_flows = (
            SubjectBalanceDelta(
                subject_address=principal_delta.subject_address,
                asset=principal_delta.asset,
                delta_atoms=capital_amount,
                basis=principal_delta.basis,
                completeness=principal_delta.completeness,
                reconciliation_diff_atoms=0,
            ),
        )

    unknown_bearer = any(
        component.economic_bearer is None
        for component in cost_breakdown.components
    )
    other_bearer = any(
        component.economic_bearer is not None
        and _address(component.economic_bearer) != _address(actor)
        for component in cost_breakdown.components
    )
    if other_bearer:
        reasons.append("fee_borne_by_other_subject")
    if unknown_bearer:
        reasons.append("fee_bearer_unknown")

    independent_fees = [
        component
        for component in cost_breakdown.components
        if component.economic_bearer is not None
        and _address(component.economic_bearer) == _address(actor)
        and _asset_identity(component.asset) == cycle_identity
        and component.counted_in.lower() in _INDEPENDENT_FEE_TOKENS
    ]
    net = raw_delta - capital_amount - sum(component.amount_atoms for component in independent_fees)
    if independent_fees:
        reasons.append("independent_fees_subtracted")
    status = (
        AttributionStatus.PARTIALLY_ATTRIBUTED
        if unknown_bearer
        else AttributionStatus.FULLY_ATTRIBUTED
    )

    price: Decimal | None = None
    if price_lookup is not None:
        raw_price = price_lookup.get(_principal_asset_key(cycle_asset))
        if isinstance(raw_price, Decimal):
            price = Decimal(net) * raw_price
        else:
            reasons.append("usd_price_missing")
    return AttributionResult(
        status,
        _economic_status(net, status),
        net,
        price,
        cycle_asset,
        tuple(dict.fromkeys(reasons)),
        unexplained_flows,
    )
