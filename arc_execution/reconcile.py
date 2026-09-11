"""Arc Execution Settlement Reconciliation Engine (T30)

Enforces:
- Receipt + real balance diff accounting determines realized profit (never assumptions)
- Receipt success (status=1) does NOT automatically equal profit (slippage or high gas can yield loss)
- On-chain revert (status=0) strictly records gas spent as financial loss
- Multi-controlled account consolidation (e.g., EOA pays gas, contract receives payout)
- Untracked third-party balance injections are detected and classified as INDETERMINATE (anti-pollution)
- Non-base asset residual dust is explicitly surfaced and accounted for
- Stop-not-auto-repair: indeterminate or reverting reconciliations trigger defensive hold
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from atomic_execution.arc_planning import ArcExecutionPlan


class ReconciliationCategory(StrEnum):
    SUCCESS = "success"
    REVERTED = "reverted"
    INDETERMINATE = "indeterminate"


class ReconciliationError(ValueError):
    """Raised when reconciliation invariants are violated."""


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    """Normalized on-chain transaction receipt for settlement reconciliation."""

    tx_hash: str
    status: int  # 1 = EVM success, 0 = EVM revert
    block_number: int
    gas_used_atoms: int
    effective_gas_price_atoms: int
    from_address: str
    to_address: str

    @property
    def is_evm_success(self) -> bool:
        return self.status == 1

    @property
    def gas_fee_atoms(self) -> int:
        return self.gas_used_atoms * self.effective_gas_price_atoms


@dataclass(frozen=True, slots=True)
class AccountBalanceSnapshot:
    """Token balance change recorded for an account across execution."""

    account: str
    token_address: str
    balance_before: int
    balance_after: int

    @property
    def delta(self) -> int:
        return self.balance_after - self.balance_before


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    """Comprehensive settlement audit report certifying trade outcome."""

    reconcile_id: str
    tx_hash: str
    category: ReconciliationCategory
    is_profitable: bool
    realized_net_atoms: int
    gas_fee_atoms: int
    base_asset: str
    base_asset_net_delta: int
    controlled_accounts: tuple[str, ...]
    residual_dust: dict[str, int]
    untracked_injections_detected: bool
    verdict_notes: tuple[str, ...]


class ExecutionReconciler:
    """Reconciles transaction receipts with account state diffs to certify financial outcomes."""

    @classmethod
    def reconcile(
        cls,
        reconcile_id: str,
        plan: ArcExecutionPlan,
        receipt: ExecutionReceipt | None,
        balance_snapshots: list[AccountBalanceSnapshot],
        controlled_accounts: list[str],
        gas_deducted_in_base_token: bool = False,
    ) -> ReconciliationReport:
        """Execute atomic financial reconciliation.

        Invariants:
        1. If receipt is None -> INDETERMINATE (unconfirmed or lost receipt).
        2. If receipt.status == 0 -> REVERTED. Gas fee is debited as real loss.
        3. If receipt.status == 1:
           - Sum base asset delta across controlled_accounts.
           - If untracked third party deposited base asset -> INDETERMINATE (anti-pollution).
           - Subtract gas fee if not already deducted in base token balance diff.
           - Surface any non-base intermediate token residues in residual_dust.
           - is_profitable is True ONLY if realized_net_atoms > 0.
        """
        base_asset_addr = (
            plan.base_asset.token_key.address.lower()
            if plan.base_asset.token_key
            else ""
        )
        controlled_set = {a.lower() for a in controlled_accounts}
        notes: list[str] = []

        # 1. Unconfirmed / missing receipt check
        if receipt is None:
            notes.append("Receipt missing or unconfirmed on-chain; cannot certify outcome")
            return ReconciliationReport(
                reconcile_id=reconcile_id,
                tx_hash="0x" + "00" * 32,
                category=ReconciliationCategory.INDETERMINATE,
                is_profitable=False,
                realized_net_atoms=0,
                gas_fee_atoms=0,
                base_asset=base_asset_addr,
                base_asset_net_delta=0,
                controlled_accounts=tuple(sorted(controlled_set)),
                residual_dust={},
                untracked_injections_detected=False,
                verdict_notes=tuple(notes),
            )

        tx_hash_norm = receipt.tx_hash.lower()

        # 2. Transaction reverted on-chain
        if not receipt.is_evm_success:
            gas_loss = receipt.gas_fee_atoms
            notes.append(f"Transaction reverted on-chain; gas fee {gas_loss} atoms debited as net loss")
            return ReconciliationReport(
                reconcile_id=reconcile_id,
                tx_hash=tx_hash_norm,
                category=ReconciliationCategory.REVERTED,
                is_profitable=False,
                realized_net_atoms=-gas_loss,
                gas_fee_atoms=gas_loss,
                base_asset=base_asset_addr,
                base_asset_net_delta=0,
                controlled_accounts=tuple(sorted(controlled_set)),
                residual_dust={},
                untracked_injections_detected=False,
                verdict_notes=tuple(notes),
            )

        # 3. Transaction succeeded on EVM level: verify balance diffs
        untracked_injections = False
        base_net_delta = 0
        residual_dust: dict[str, int] = {}

        for snap in balance_snapshots:
            acc_norm = snap.account.lower()
            token_norm = snap.token_address.lower()

            if token_norm == base_asset_addr:
                if acc_norm in controlled_set:
                    base_net_delta += snap.delta
                elif snap.delta > 0:
                    # Untracked third party injected base asset!
                    untracked_injections = True
                    notes.append(f"State pollution detected: untracked account {snap.account} received {snap.delta} base tokens")
            else:
                # Non-base token delta check (dust residues)
                if acc_norm in controlled_set and snap.delta > 0:
                    residual_dust[token_norm] = residual_dust.get(token_norm, 0) + snap.delta

        if untracked_injections:
            notes.append("Reconciliation failed due to untracked external balance injection; classified INDETERMINATE")
            return ReconciliationReport(
                reconcile_id=reconcile_id,
                tx_hash=tx_hash_norm,
                category=ReconciliationCategory.INDETERMINATE,
                is_profitable=False,
                realized_net_atoms=0,
                gas_fee_atoms=receipt.gas_fee_atoms,
                base_asset=base_asset_addr,
                base_asset_net_delta=base_net_delta,
                controlled_accounts=tuple(sorted(controlled_set)),
                residual_dust=residual_dust,
                untracked_injections_detected=True,
                verdict_notes=tuple(notes),
            )

        # Calculate true realized profit
        gas_fee = receipt.gas_fee_atoms
        realized_net = base_net_delta
        if not gas_deducted_in_base_token:
            realized_net -= gas_fee

        is_prof = realized_net > 0
        if is_prof:
            notes.append(f"Closed-loop arbitrage verified: net profit +{realized_net} base atoms after gas")
        else:
            notes.append(f"Receipt succeeded but trade was unprofitable: net {realized_net} base atoms after gas")

        return ReconciliationReport(
            reconcile_id=reconcile_id,
            tx_hash=tx_hash_norm,
            category=ReconciliationCategory.SUCCESS,
            is_profitable=is_prof,
            realized_net_atoms=realized_net,
            gas_fee_atoms=gas_fee,
            base_asset=base_asset_addr,
            base_asset_net_delta=base_net_delta,
            controlled_accounts=tuple(sorted(controlled_set)),
            residual_dust=residual_dust,
            untracked_injections_detected=False,
            verdict_notes=tuple(notes),
        )
