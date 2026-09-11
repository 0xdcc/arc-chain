"""Arc Output Verification Adapter (T27)

Binds raw simulation results and trace-based OutputEvidence to produce certified SimulationEvidenceBridge:
- Strictly decouples expected net profit from verified output
- Rejects unverified simulation outcomes from asserting positive profit
- Elevates output_verified ONLY when verified OutputEvidence confirms state transition
"""

from __future__ import annotations

from typing import Any

from arbitrage_contracts.arc_extensions import SimulationEvidenceBridge, SimulationStatus
from atomic_execution.arc_planning import ArcExecutionPlan
from atomic_execution.output_evidence import (
    OutputEvidence,
    OutputVerificationError,
    OutputVerificationStatus,
)


class ArcOutputAdapter:
    """Adapts simulation output and trace evidence into canonical bridge representations."""

    @staticmethod
    def adapt_with_evidence(
        call_succeeded: bool,
        plan: ArcExecutionPlan,
        evidence: OutputEvidence | None,
        backend_name: str = "arc_rpc_trace",
        raw_gas_used: int | None = None,
        revert_reason: str | None = None,
    ) -> SimulationEvidenceBridge:
        """Construct a certified SimulationEvidenceBridge.

        Invariants:
        1. If call_succeeded is False -> status is CONTRACT_REVERT or UNVERIFIED, output_verified=False.
        2. If evidence is None or evidence.is_verified is False:
           - output_verified MUST be False.
           - status MUST be OUTPUT_UNVERIFIED (or CONTRACT_REVERT if call failed).
           - net_output_atoms MUST be None (never defaults to plan.expected profit!).
        3. Only when evidence is valid and verified:
           - output_verified is True.
           - status is CALL_SUCCEEDED.
           - net_output_atoms is set to evidence.verified_net_atoms.
        """
        if not call_succeeded:
            return SimulationEvidenceBridge(
                call_succeeded=False,
                output_verified=False,
                status=SimulationStatus.CONTRACT_REVERT,
                net_output_atoms=None,
                gas_used_atoms=raw_gas_used,
                backend=backend_name,
                execution_revert_reason=revert_reason or "SIMULATION_CALL_REVERTED",
            )

        if evidence is None or not evidence.is_verified:
            # Call succeeded on EVM level, but trace / state-diff output could not be proven
            reason = evidence.rejection_reason if evidence else "NO_TRACE_EVIDENCE_PROVIDED"
            return SimulationEvidenceBridge(
                call_succeeded=True,
                output_verified=False,
                status=SimulationStatus.OUTPUT_UNVERIFIED,
                net_output_atoms=None,
                gas_used_atoms=raw_gas_used,
                backend=backend_name,
                execution_revert_reason=reason,
            )

        # Output is verified by real state-diff evidence
        return SimulationEvidenceBridge(
            call_succeeded=True,
            output_verified=True,
            status=SimulationStatus.CALL_SUCCEEDED,
            net_output_atoms=evidence.verified_net_atoms,
            gas_used_atoms=evidence.gas_attribution.gas_used_atoms if evidence.gas_attribution else raw_gas_used,
            backend=backend_name,
            execution_revert_reason=None,
        )

    @staticmethod
    def enforce_no_plan_substitution(
        plan: ArcExecutionPlan,
        claimed_verified_atoms: int | None,
        evidence: OutputEvidence | None,
    ) -> None:
        """Assert that plan expected_out is never substituted for verified evidence."""
        expected_profit = plan.expected_out.atoms - plan.amount_in.atoms
        if claimed_verified_atoms is not None and (evidence is None or not evidence.is_verified):
            raise OutputVerificationError(
                f"Cannot assert verified profit ({claimed_verified_atoms}) without verified OutputEvidence! "
                f"Substituting plan expected profit ({expected_profit}) is strictly prohibited."
            )
