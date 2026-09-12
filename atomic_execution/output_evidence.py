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

from arbitrage_contracts.identity import validate_bytes32, validate_evm_address
from arc_opportunities.cost_units import rescale_cost_atoms

ARC_USDC_ADDRESS = "0x3600000000000000000000000000000000000000"


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
            if (
                d.account.lower() not in known_accounts
                and d.token_address.lower() == base_token_norm
                and d.delta > 0
            ):
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

        # Absence of costs or complete controlled-account balances is not zero cost.
        seen: set[tuple[str, str]] = set()
        for d in diffs:
            key = (d.account.lower(), d.token_address.lower())
            if key in seen:
                raise OutputVerificationError("Duplicate account/token balance diff")
            seen.add(key)
            validate_evm_address(d.account)
            validate_evm_address(d.token_address)
            if any(type(v) is not int or v < 0 for v in (d.balance_before, d.balance_after)):
                raise OutputVerificationError("Balances must be nonnegative integer atoms")
        validate_bytes32(state_hash)
        missing = any((a, base_token_norm) not in seen for a in {caller_norm, recipient_norm})
        if gas_attribution is None or missing or base_token_norm != ARC_USDC_ADDRESS:
            return OutputEvidence(
                evidence_id,
                plan_id,
                caller_norm,
                router_norm,
                recipient_norm,
                base_token_norm,
                block_number,
                state_hash,
                True,
                False,
                tuple(diffs),
                gas_attribution,
                False,
                OutputVerificationStatus.INSUFFICIENT_EVIDENCE,
                None,
                "Missing gas/base-account coverage or unsupported base currency conversion",
            )
        if (
            gas_attribution.gas_payer.lower() not in {caller_norm, recipient_norm}
            or type(gas_attribution.gas_included_in_diff) is not bool
        ):
            raise OutputVerificationError("Unbound gas payer or ambiguous gas deduction")
        # Arc receipt fee is native 18-decimal USDC; canonical ERC20 view is 6 decimals.
        for value in (gas_attribution.gas_used_atoms, gas_attribution.effective_gas_price_atoms):
            if type(value) is not int or value < 0:
                raise OutputVerificationError("Gas fields must be nonnegative integer atoms")
        fee_base_atoms = rescale_cost_atoms(gas_attribution.gas_fee_atoms, 18, 6)

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
                final_net_atoms -= fee_base_atoms

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
