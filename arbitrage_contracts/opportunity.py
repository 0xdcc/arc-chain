"""Domain contracts for arbitrage opportunity records and observation lifecycle snapshots."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from .identity import Amount, validate_non_negative_integer
from .quote import ActorScope, DataMode, QuoteEvidence


class OpportunityPhase(StrEnum):
    """Lifecycle phase of an observed arbitrage opportunity episode."""

    OBSERVED = "observed"
    QUOTED = "quoted"
    SIMULATED = "simulated"
    REJECTED = "rejected"
    STALE = "stale"
    DISAPPEARED = "disappeared"


def compute_opportunity_id(
    route_id: str,
    amount_in: Amount,
    first_seen_event_id: str,
    registry_revision: str = "v1",
) -> str:
    """Compute deterministic SHA-256 identifier for an opportunity episode."""
    payload = {
        "schema_id": "arbitrage-evidence",
        "route_id": route_id,
        "amount_in": {
            "chain_id": amount_in.asset_ref.chain_id,
            "address": amount_in.asset_ref.token_key.address
            if amount_in.asset_ref.token_key
            else amount_in.asset_ref.native_identifier,
            "atoms": str(amount_in.atoms),
        },
        "first_seen_event_id": first_seen_event_id,
        "registry_revision": registry_revision,
    }
    canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def compute_observation_id(
    run_id: str,
    source_seq_or_hash: str,
    state_version_ref: str,
    amount_in: Amount,
) -> str:
    """Compute deterministic observation event identifier."""
    payload = {
        "schema_id": "arbitrage-evidence",
        "run_id": run_id,
        "source_seq_or_hash": source_seq_or_hash,
        "state_version_ref": state_version_ref,
        "amount_in_atoms": str(amount_in.atoms),
    }
    canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Observation:
    """Point-in-time observation of a candidate opportunity."""

    observation_id: str
    run_id: str
    observed_at_ms: int
    phase: str = OpportunityPhase.OBSERVED
    state_version_ref: str | None = None
    quote_evidence: QuoteEvidence | None = None
    result: str = "neutral"
    rejection_reason: str | None = None
    is_truncated: bool = False
    evidence_refs: tuple[str, ...] = ()

    def __init__(
        self,
        observation_id: str,
        run_id: str,
        observed_at_ms: int,
        phase: str = OpportunityPhase.OBSERVED,
        state_version_ref: str | None = None,
        quote_evidence: QuoteEvidence | None = None,
        result: str = "neutral",
        rejection_reason: str | None = None,
        is_truncated: bool = False,
        evidence_refs: Sequence[str] = (),
    ) -> None:
        if type(observation_id) is not str or not observation_id.strip():
            raise ValueError("observation_id must be a non-empty string")
        if type(run_id) is not str or not run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        val_observed = validate_non_negative_integer(observed_at_ms, "observed_at_ms")

        if quote_evidence is not None:
            if not isinstance(quote_evidence, QuoteEvidence):
                raise TypeError("quote_evidence must be an instance of QuoteEvidence")
            if (
                quote_evidence.started_at_ms is not None
                and quote_evidence.started_at_ms > val_observed
            ):
                raise ValueError(
                    f"Temporal violation: quote started_at_ms ({quote_evidence.started_at_ms}) "
                    f"cannot be in the future relative to observation ({val_observed})"
                )

        if is_truncated and result in ("no_opportunity", "unprofitable"):
            raise ValueError(
                "Search truncated due to budget/coverage cannot be reported as 'no_opportunity'"
            )

        object.__setattr__(self, "observation_id", observation_id.strip())
        object.__setattr__(self, "run_id", run_id.strip())
        object.__setattr__(self, "observed_at_ms", val_observed)
        object.__setattr__(self, "phase", phase)
        object.__setattr__(self, "state_version_ref", state_version_ref)
        object.__setattr__(self, "quote_evidence", quote_evidence)
        object.__setattr__(self, "result", result)
        object.__setattr__(self, "rejection_reason", rejection_reason)
        object.__setattr__(self, "is_truncated", bool(is_truncated))
        object.__setattr__(self, "evidence_refs", tuple(evidence_refs))


@dataclass(frozen=True, slots=True)
class OpportunityRecord:
    """Pure record container tracking an opportunity episode across time."""

    opportunity_id: str
    route_id: str
    amount_in: Amount
    first_seen_at_ms: int
    last_seen_at_ms: int
    last_rechecked_at_ms: int
    phase: str = OpportunityPhase.OBSERVED
    observations: tuple[Observation, ...] = ()
    rejection_reasons: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    disappeared_at_ms: int | None = None
    registry_revision: str = "v1"
    run_baseline_ref: str | None = None
    data_mode: str = DataMode.SYNTHETIC
    actor_scope: str = ActorScope.SYNTHETIC

    def __init__(
        self,
        opportunity_id: str,
        route_id: str,
        amount_in: Amount,
        first_seen_at_ms: int,
        last_seen_at_ms: int,
        last_rechecked_at_ms: int,
        phase: str = OpportunityPhase.OBSERVED,
        observations: Sequence[Observation] = (),
        rejection_reasons: Sequence[str] = (),
        evidence_refs: Sequence[str] = (),
        disappeared_at_ms: int | None = None,
        registry_revision: str = "v1",
        run_baseline_ref: str | None = None,
        data_mode: str = DataMode.SYNTHETIC,
        actor_scope: str = ActorScope.SYNTHETIC,
    ) -> None:
        if type(opportunity_id) is not str or not opportunity_id.strip():
            raise ValueError("opportunity_id must be a non-empty string")
        if type(route_id) is not str or not route_id.strip():
            raise ValueError("route_id must be a non-empty string")
        if not isinstance(amount_in, Amount):
            raise TypeError("amount_in must be an instance of Amount")

        val_first = validate_non_negative_integer(first_seen_at_ms, "first_seen_at_ms")
        val_last = validate_non_negative_integer(last_seen_at_ms, "last_seen_at_ms")
        val_rechecked = validate_non_negative_integer(last_rechecked_at_ms, "last_rechecked_at_ms")

        if not (val_first <= val_rechecked <= val_last):
            raise ValueError(
                f"Temporal monotonicity violated: first_seen_at_ms ({val_first}) <= "
                f"last_rechecked_at_ms ({val_rechecked}) <= last_seen_at_ms ({val_last})"
            )

        val_disappeared = None
        if disappeared_at_ms is not None:
            val_disappeared = validate_non_negative_integer(disappeared_at_ms, "disappeared_at_ms")
            if val_disappeared < val_last:
                raise ValueError(
                    f"disappeared_at_ms ({val_disappeared}) cannot precede last_seen_at_ms ({val_last})"
                )

        obs_tuple = tuple(observations)
        for obs in obs_tuple:
            if not isinstance(obs, Observation):
                raise TypeError("observations items must be instances of Observation")
            if not (val_first <= obs.observed_at_ms <= val_last):
                raise ValueError(
                    f"Observation timestamp {obs.observed_at_ms} outside bounds "
                    f"[{val_first}, {val_last}]"
                )

        object.__setattr__(self, "opportunity_id", opportunity_id.strip())
        object.__setattr__(self, "route_id", route_id.strip())
        object.__setattr__(self, "amount_in", amount_in)
        object.__setattr__(self, "first_seen_at_ms", val_first)
        object.__setattr__(self, "last_seen_at_ms", val_last)
        object.__setattr__(self, "last_rechecked_at_ms", val_rechecked)
        object.__setattr__(self, "phase", phase)
        object.__setattr__(self, "observations", obs_tuple)
        object.__setattr__(self, "rejection_reasons", tuple(rejection_reasons))
        object.__setattr__(self, "evidence_refs", tuple(evidence_refs))
        object.__setattr__(self, "disappeared_at_ms", val_disappeared)
        object.__setattr__(self, "registry_revision", registry_revision)
        object.__setattr__(self, "run_baseline_ref", run_baseline_ref)
        object.__setattr__(self, "data_mode", data_mode)
        object.__setattr__(self, "actor_scope", actor_scope)
