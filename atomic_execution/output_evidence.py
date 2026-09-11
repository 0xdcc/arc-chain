"""Arc Output Evidence and State-Diff Verification (T27)

Enforces:
- output_verified is elevated ONLY when verified trace/state-diff evidence exists
- Plan expected_out or net_profit is NEVER substituted for verified balance change
- Secondary independent balanceOf calls are rejected as invalid post-state proof
- Explicit gas attribution (payer, gas used, fee, whether deducted in state diff)
- Anti-pollution: Unrelated external balance injections are detected and rejected
- Fail-closed: If trace capability is unavailable, output remains UNVERIFIED
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class OutputVerificationStatus(StrEnum):
    VERIFIED = "verified"
    TRACE_UNAVAILABLE = "trace_unavailable"
    INDEPENDENT_POLL_REJECTED = "independent_poll_rejected"
    STATE_POLLUTION_DETECTED = "state_pollution_detected"
    DISCREPANCY_EXCEEDED = "discrepancy_exceeded"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class OutputVerificationError(ValueError):
    """Raised when output evidence violates structural or economic invariants."""


@dataclass(frozen=True, slots=True)
class StateDiffEntry:
    """Atomic token balance diff recorded from transaction trace/state-diff."""

    account: str
    token_address: str
    balance_before: int
    balance_after: int

    @property
    def delta(self) -> int:
        return self.balance_after - self.balance_before


@dataclass(frozen=True, slots=True)
class TraceGasAttribution:
    """Gas accounting bound to trace execution."""

    gas_payer: str
    gas_used_atoms: int
    effective_gas_price_atoms: int
    gas_included_in_diff: bool  # True if balance_after of native token already subtracted gas

    @property
    def gas_fee_atoms(self) -> int:
        return self.gas_used_atoms * self.effective_gas_price_atoms


@dataclass(frozen=True, slots=True)
class OutputEvidence:
    """Structured evidence of verified state transition and output change."""

    evidence_id: str
    plan_id: str
    caller_address: str
    router_address: str
    recipient_address: str
    base_token_address: str
    block_number: int
    state_hash: str
    trace_available: bool
    is_independent_poll: bool  # True if obtained via separate post-call balanceOf
    diffs: tuple[StateDiffEntry, ...]
    gas_attribution: TraceGasAttribution | None
    is_verified: bool
    status: OutputVerificationStatus
    verified_net_atoms: int | None
    rejection_reason: str | None = None


class OutputEvidenceVerifier:
    """Validates trace and state-diff artifacts to certify output changes."""

    @classmethod
    def verify_from_trace(
        cls,
        evidence_id: str,
        plan_id: str,
        caller_address: str,
        router_address: str,
        recipient_address: str,
        base_token_address: str,
        block_number: int,
        state_hash: str,
        trace_available: bool,
        is_independent_poll: bool,
        diffs: list[StateDiffEntry],
        gas_attribution: TraceGasAttribution | None = None,
        allowed_pollution_accounts: set[str] | None = None,
    ) -> OutputEvidence:
        """Verify state diff and trace data against strict verification invariants."""
        caller_norm = caller_address.lower()
        router_norm = router_address.lower()
        recipient_norm = recipient_address.lower()
        base_token_norm = base_token_address.lower()

        # Invariant 1: Independent balanceOf polling is NEVER accepted as post-state
        if is_independent_poll:
            return OutputEvidence(
                evidence_id=evidence_id,
                plan_id=plan_id,
                caller_address=caller_norm,
                router_address=router_norm,
                recipient_address=recipient_norm,
                base_token_address=base_token_norm,
                block_number=block_number,
                state_hash=state_hash,
                trace_available=trace_available,
                is_independent_poll=True,
                diffs=tuple(diffs),
                gas_attribution=gas_attribution,
                is_verified=False,
                status=OutputVerificationStatus.INDEPENDENT_POLL_REJECTED,
                verified_net_atoms=None,
                rejection_reason="Independent balanceOf poll rejected; requires atomic trace or state-diff",
            )

        # Invariant 2: If trace capability is unavailable, fail closed
        if not trace_available:
            return OutputEvidence(
                evidence_id=evidence_id,
                plan_id=plan_id,
                caller_address=caller_norm,
                router_address=router_norm,
                recipient_address=recipient_norm,
                base_token_address=base_token_norm,
                block_number=block_number,
                state_hash=state_hash,
                trace_available=False,
                is_independent_poll=False,
                diffs=tuple(diffs),
                gas_attribution=gas_attribution,
                is_verified=False,
                status=OutputVerificationStatus.TRACE_UNAVAILABLE,
                verified_net_atoms=None,
                rejection_reason="Trace backend capability unavailable; preserving UNKNOWN output",
            )

        if not diffs:
            return OutputEvidence(
                evidence_id=evidence_id,
                plan_id=plan_id,
                caller_address=caller_norm,
                router_address=router_norm,
                recipient_address=recipient_norm,
                base_token_address=base_token_norm,
                block_number=block_number,
                state_hash=state_hash,
                trace_available=True,
                is_independent_poll=False,
                diffs=(),
                gas_attribution=gas_attribution,
                is_verified=False,
                status=OutputVerificationStatus.INSUFFICIENT_EVIDENCE,
                verified_net_atoms=None,
                rejection_reason="Empty state diffs cannot substantiate output change",
            )

        # Invariant 3: Detect state pollution from unrelated third-party accounts
        known_accounts = {caller_norm, router_norm, recipient_norm}
        if allowed_pollution_accounts:
            known_accounts.update(a.lower() for a in allowed_pollution_accounts)

        for d in diffs:
            if d.account.lower() not in known_accounts and d.token_address.lower() == base_token_norm and d.delta > 0:
                return OutputEvidence(
                    evidence_id=evidence_id,
                    plan_id=plan_id,
                    caller_address=caller_norm,
                    router_address=router_norm,
                    recipient_address=recipient_norm,
                    base_token_address=base_token_norm,
                    block_number=block_number,
                    state_hash=state_hash,
                    trace_available=True,
                    is_independent_poll=False,
                    diffs=tuple(diffs),
                    gas_attribution=gas_attribution,
                    is_verified=False,
                    status=OutputVerificationStatus.STATE_POLLUTION_DETECTED,
                    verified_net_atoms=None,
                    rejection_reason=f"State pollution detected from untracked account: {d.account}",
                )

        # Invariant 4: Reconcile recipient/caller base token net change
        # If caller and recipient are distinct, the net economic change across the strategy is caller_delta + recipient_delta.
        # If caller is recipient, it is simply caller's delta.
        total_balance_delta = 0
        for d in diffs:
            if d.token_address.lower() == base_token_norm:
                if d.account.lower() == recipient_norm:
                    total_balance_delta += d.delta
                elif d.account.lower() == caller_norm and caller_norm != recipient_norm:
                    total_balance_delta += d.delta

        # Invariant 5: Explicit gas accounting
        final_net_atoms = total_balance_delta
        if gas_attribution is not None:
            if not gas_attribution.gas_included_in_diff:
                final_net_atoms -= gas_attribution.gas_fee_atoms

        return OutputEvidence(
            evidence_id=evidence_id,
            plan_id=plan_id,
            caller_address=caller_norm,
            router_address=router_norm,
            recipient_address=recipient_norm,
            base_token_address=base_token_norm,
            block_number=block_number,
            state_hash=state_hash,
            trace_available=True,
            is_independent_poll=False,
            diffs=tuple(diffs),
            gas_attribution=gas_attribution,
            is_verified=True,
            status=OutputVerificationStatus.VERIFIED,
            verified_net_atoms=final_net_atoms,
            rejection_reason=None,
        )
