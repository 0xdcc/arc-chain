"""No-future-time lifecycle reduction for W2 observations."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from arbitrage_contracts.identity import Amount, AssetRef, TokenKey
from opportunities.identity import ObservationInput, derive_observation_id

_ATOMS_REGEX = re.compile(r"^(0|[1-9][0-9]*)$")
_FAILURE_RESULTS = frozenset(
    {"quote_failed", "rate_limited", "missing_gas", "unprofitable", "zero_or_negative"}
)
_NON_CANDIDATE_RESULTS = frozenset({"no_opportunity"})
_NOT_MARKET_RESULTS = frozenset({"quote_failed", "rate_limited", "missing_gas"})
_POSITIVE_RESULTS = frozenset({"profitable", "positive"})


class LifecycleError(ValueError):
    """Raised when lifecycle input cannot be reduced safely."""


def _text(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise LifecycleError(f"{field_name} must be a non-empty string")
    return value


def _integer(value: object, field_name: str) -> int:
    if type(value) is not int:
        raise LifecycleError(f"{field_name} must be an integer")
    return value


@dataclass(frozen=True, slots=True)
class LifecyclePolicy:
    """Explicit lifecycle boundaries required by episode-policy-v1."""

    gap_limit_ms: int
    target_delay_ms: int
    policy_version: str = "episode-policy-v1"

    def __post_init__(self) -> None:
        if type(self.gap_limit_ms) is not int or self.gap_limit_ms <= 0:
            raise ValueError("gap_limit_ms must be a positive integer")
        if type(self.target_delay_ms) is not int or self.target_delay_ms < 0:
            raise ValueError("target_delay_ms must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    """A fully timestamped upstream fact with explicit availability."""

    event_id: str
    route_id: str
    source_record_id: str
    base_asset: dict[str, Any]
    amount_atoms: str
    decimals: int
    decimals_evidence_ref: str
    registry_semantic_revision: str
    observed_at_ms: int
    available_at_ms: int
    block_time_s: int | None
    monotonic_ns: int | None
    result: str
    candidate: bool
    complete_scan: bool
    revoked: bool
    truncated: bool
    stream_id: str = "synthetic-stream"
    data_mode: str = "synthetic"
    ledger_sequence: int = 0

    @classmethod
    def from_mapping(cls, raw: dict[str, Any], ledger_sequence: int) -> LifecycleEvent:
        """Build and validate a lifecycle event without accepting missing clocks silently."""
        if not isinstance(raw, dict):
            raise LifecycleError("event must be a JSON object")
        event_id = _text(raw.get("event_id"), "event_id")
        route_id = _text(raw.get("route_id"), "route_id")
        source_record_id = _text(raw.get("source_record_id"), "source_record_id")
        stream_id = _text(raw.get("stream_id", "synthetic-stream"), "stream_id")
        base_asset = raw.get("base_asset")
        if not isinstance(base_asset, dict):
            raise LifecycleError("base_asset must be an object")
        amount_atoms = raw.get("amount_atoms")
        if type(amount_atoms) is not str or not _ATOMS_REGEX.fullmatch(amount_atoms):
            raise LifecycleError("amount_atoms must be a strict decimal string")
        decimals = _integer(raw.get("decimals"), "decimals")
        if decimals < 0 or decimals > 255:
            raise LifecycleError("decimals must be in range 0..255")
        observed_at_ms = _integer(raw.get("observed_at_ms"), "observed_at_ms")
        available_at_ms = _integer(raw.get("available_at_ms"), "available_at_ms")
        if observed_at_ms < 0 or available_at_ms < 0:
            raise LifecycleError("UTC timestamps must be non-negative")
        block_time_s = raw.get("block_time_s")
        if block_time_s is not None:
            block_time_s = _integer(block_time_s, "block_time_s")
            if block_time_s < 0:
                raise LifecycleError("block_time_s must be non-negative")
        monotonic_ns = raw.get("monotonic_ns")
        if monotonic_ns is not None:
            monotonic_ns = _integer(monotonic_ns, "monotonic_ns")
            if monotonic_ns < 0:
                raise LifecycleError("monotonic_ns must be non-negative")
        result = _text(raw.get("result", "neutral"), "result")
        truncated = raw.get("is_truncated", False)
        if type(truncated) is not bool:
            raise LifecycleError("is_truncated must be boolean")
        if truncated and result in {"no_opportunity", "unprofitable"}:
            raise LifecycleError("truncated search cannot be reported as market absent")
        candidate = raw.get("candidate", result not in _NON_CANDIDATE_RESULTS)
        if type(candidate) is not bool:
            raise LifecycleError("candidate must be boolean")
        complete_scan = raw.get("complete_scan", False)
        if type(complete_scan) is not bool:
            raise LifecycleError("complete_scan must be boolean")
        revoked = raw.get("revoked", False)
        if type(revoked) is not bool:
            raise LifecycleError("revoked must be boolean")
        return cls(
            event_id=event_id,
            route_id=route_id,
            source_record_id=source_record_id,
            base_asset=base_asset,
            amount_atoms=amount_atoms,
            decimals=decimals,
            decimals_evidence_ref=_text(
                raw.get("decimals_evidence_ref", "test:decimals"), "decimals_evidence_ref"
            ),
            registry_semantic_revision=_text(
                raw.get("registry_semantic_revision", "v1"), "registry_semantic_revision"
            ),
            observed_at_ms=observed_at_ms,
            available_at_ms=available_at_ms,
            block_time_s=block_time_s,
            monotonic_ns=monotonic_ns,
            result=result,
            candidate=candidate,
            complete_scan=complete_scan,
            revoked=revoked,
            truncated=truncated,
            stream_id=stream_id,
            data_mode=_text(raw.get("data_mode", "synthetic"), "data_mode"),
            ledger_sequence=ledger_sequence,
        )


@dataclass
class EpisodeState:
    """Mutable internal state for one route/amount/registry episode."""

    opportunity_id: str
    route_id: str
    amount_atoms: str
    registry_revision: str
    first_seen_at_ms: int
    last_seen_at_ms: int
    last_rechecked_at_ms: int
    phase: str = "observed"
    observations: list[dict[str, Any]] = field(default_factory=list)
    rejection_reasons: list[str] = field(default_factory=list)
    positive_intervals: list[list[int]] = field(default_factory=list)
    continuity_unknown: bool = False
    right_censored: bool = False
    disappeared_at_ms: int | None = None
    parent_opportunity_id: str | None = None
    positive_open: bool = False
    positive_start: int | None = None
    recheck_due_at_ms: int | None = None


@dataclass(frozen=True, slots=True)
class LifecycleResult:
    """Canonical business output independent of process timing."""

    episodes: tuple[EpisodeState, ...]
    duplicate_event_count: int
    late_revision_count: int
    missed_due_count: int
    limitations: tuple[str, ...]
    decision_watermark_ms: int | None


def _asset_ref(raw: dict[str, Any]) -> AssetRef:
    interface_kind = raw.get("interface_kind")
    chain_id = raw.get("chain_id")
    if type(chain_id) is not int or chain_id <= 0:
        raise LifecycleError("base_asset.chain_id must be a positive integer")
    if interface_kind == "erc20":
        token_key = raw.get("token_key")
        if not isinstance(token_key, dict) or type(token_key.get("address")) is not str:
            raise LifecycleError("erc20 base_asset requires token_key.address")
        return AssetRef.erc20(TokenKey(chain_id, token_key["address"]))
    if interface_kind == "native":
        native_identifier = raw.get("native_identifier")
        if type(native_identifier) is not str or not native_identifier:
            raise LifecycleError("native base_asset requires native_identifier")
        return AssetRef.native(chain_id, native_identifier)
    raise LifecycleError("base_asset.interface_kind is unsupported")


def _observation(event: LifecycleEvent, policy: LifecyclePolicy) -> tuple[str, Amount]:
    asset_ref = _asset_ref(event.base_asset)
    amount_in = Amount.from_atoms_str(
        asset_ref,
        event.amount_atoms,
        event.decimals,
        event.decimals_evidence_ref,
    )
    observation_input = ObservationInput(
        route_id=event.route_id,
        base_asset=asset_ref,
        amount_atoms=event.amount_atoms,
        decimals_evidence_ref=event.decimals_evidence_ref,
        stream_id=event.stream_id,
        source_record_id=event.source_record_id,
        registry_semantic_revision=event.registry_semantic_revision,
        policy_version=policy.policy_version,
    )
    return (
        derive_observation_id(
            observation_input,
            run_id=f"w2:{event.stream_id}",
            state_version_ref=event.registry_semantic_revision,
            amount_in=amount_in,
        ),
        amount_in,
    )


class ObservationMerger:
    """Merge events under an availability barrier without rewriting committed decisions."""

    def __init__(self, policy: LifecyclePolicy) -> None:
        self.policy = policy
        self._states: dict[tuple[str, str, str], EpisodeState] = {}
        self._closed: list[EpisodeState] = []
        self._identity_payloads: dict[str, dict[str, Any]] = {}
        self._identity_content: dict[str, tuple[Any, ...]] = {}
        self._duplicates = 0
        self._late = 0
        self._missed_due = 0
        self._watermark: int | None = None

    @staticmethod
    def _key(event: LifecycleEvent) -> tuple[str, str, str]:
        return event.route_id, event.amount_atoms, event.registry_semantic_revision

    def _close(self, state: EpisodeState, at_ms: int, *, revoked: bool = False) -> None:
        if state.positive_open and state.positive_start is not None:
            state.positive_intervals.append([state.positive_start, state.last_seen_at_ms])
            state.positive_open = False
            state.positive_start = None
        state.disappeared_at_ms = at_ms
        state.phase = "rejected" if revoked else "disappeared"
        self._closed.append(state)
        key = (state.route_id, state.amount_atoms, state.registry_revision)
        self._states.pop(key, None)

    def _record_recheck(self, event: LifecycleEvent, state: EpisodeState) -> None:
        due = state.recheck_due_at_ms
        if due is None or event.available_at_ms < due:
            return
        actual_elapsed = event.observed_at_ms - state.last_seen_at_ms
        if actual_elapsed < 0:
            raise LifecycleError("recheck elapsed time cannot be negative")
        if event.available_at_ms > due:
            self._missed_due += 1
        state.observations.append(
            {
                "observation_id": event.event_id,
                "observed_at_ms": event.observed_at_ms,
                "target_delay_ms": self.policy.target_delay_ms,
                "actual_elapsed_ms": actual_elapsed,
                "missed_due": event.available_at_ms > due,
            }
        )
        state.recheck_due_at_ms = None

    def _append_observation(
        self, event: LifecycleEvent, state: EpisodeState, observation_id: str
    ) -> None:
        state.last_seen_at_ms = max(state.last_seen_at_ms, event.observed_at_ms)
        state.observations.append(
            {
                "observation_id": observation_id,
                "observed_at_ms": event.observed_at_ms,
                "available_at_ms": event.available_at_ms,
                "result": event.result,
                "candidate": event.candidate,
                "is_truncated": event.truncated,
            }
        )
        if event.result in _FAILURE_RESULTS:
            reason = event.result
            if reason not in state.rejection_reasons:
                state.rejection_reasons.append(reason)
            if state.positive_open and state.positive_start is not None:
                state.positive_intervals.append([state.positive_start, state.last_seen_at_ms])
                state.positive_open = False
                state.positive_start = None
            state.recheck_due_at_ms = None
        elif event.result in _POSITIVE_RESULTS:
            if not state.positive_open:
                state.positive_open = True
                state.positive_start = event.observed_at_ms
            state.recheck_due_at_ms = event.available_at_ms + self.policy.target_delay_ms
        if event.truncated:
            state.right_censored = True

    def process(self, event: LifecycleEvent) -> None:
        """Apply one already-availability-ordered event."""
        if event.available_at_ms < event.observed_at_ms:
            raise LifecycleError("availability cannot precede observation time")
        if self._watermark is not None and event.available_at_ms < self._watermark:
            raise LifecycleError("events must be sorted by availability before processing")
        self._watermark = max(self._watermark or 0, event.available_at_ms)
        observation_id, _ = _observation(event, self.policy)
        content = (
            event.route_id,
            event.source_record_id,
            event.observed_at_ms,
            event.available_at_ms,
            event.result,
            event.candidate,
        )
        if observation_id in self._identity_content:
            if self._identity_content[observation_id] != content:
                raise LifecycleError(f"observation identity conflict: {observation_id}")
            self._duplicates += 1
            return
        self._identity_content[observation_id] = content
        self._identity_payloads[observation_id] = {"event_id": event.event_id}
        key = self._key(event)
        state = self._states.get(key)
        if state is not None and event.observed_at_ms < state.last_seen_at_ms:
            self._late += 1
            return
        if state is not None:
            if event.available_at_ms - state.last_seen_at_ms > self.policy.gap_limit_ms:
                state.continuity_unknown = True
                state.right_censored = True
                self._close(state, state.last_seen_at_ms)
                new_state = EpisodeState(
                    opportunity_id=observation_id,
                    route_id=event.route_id,
                    amount_atoms=event.amount_atoms,
                    registry_revision=event.registry_semantic_revision,
                    first_seen_at_ms=event.observed_at_ms,
                    last_seen_at_ms=event.observed_at_ms,
                    last_rechecked_at_ms=event.observed_at_ms,
                    continuity_unknown=True,
                    parent_opportunity_id=state.opportunity_id,
                )
                self._states[key] = new_state
                self._record_recheck(event, new_state)
                self._append_observation(event, new_state, observation_id)
                return
        if state is None:
            state = EpisodeState(
                opportunity_id=observation_id,
                route_id=event.route_id,
                amount_atoms=event.amount_atoms,
                registry_revision=event.registry_semantic_revision,
                first_seen_at_ms=event.observed_at_ms,
                last_seen_at_ms=event.observed_at_ms,
                last_rechecked_at_ms=event.observed_at_ms,
            )
            self._states[key] = state
        self._record_recheck(event, state)
        if event.revoked or not event.candidate:
            self._close(
                state,
                event.observed_at_ms,
                revoked=event.revoked,
            )
            return
        if event.complete_scan and event.result == "no_opportunity":
            self._close(state, event.observed_at_ms)
            return
        self._append_observation(event, state, observation_id)

    def finalize(self) -> LifecycleResult:
        """Close uncensored open episodes and return the canonical result."""
        for state in list(self._states.values()):
            if state.positive_open and state.positive_start is not None:
                state.positive_intervals.append([state.positive_start, state.last_seen_at_ms])
                state.positive_open = False
                state.positive_start = None
            state.right_censored = True
            self._closed.append(state)
            self._states.clear()
        limitations: list[str] = []
        if not self._closed:
            limitations.append("data_insufficient")
        elif any(state.right_censored for state in self._closed):
            limitations.append("right_censored")
        return LifecycleResult(
            episodes=tuple(self._closed),
            duplicate_event_count=self._duplicates,
            late_revision_count=self._late,
            missed_due_count=self._missed_due,
            limitations=tuple(limitations),
            decision_watermark_ms=self._watermark,
        )
