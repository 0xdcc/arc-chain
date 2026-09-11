"""Arc Execution Simulation Service (T26)

Coordinates read-only simulation of ArcExecutionPlan over ArcSimulationTransport:
- Validates pre-execution token inventory without assumptions
- Executes full-path routed calldata via single eth_call
- Enforces strict SimulationEvidenceBridge invariants
- CALL_SUCCEEDED with unverified return data yields status=OUTPUT_UNVERIFIED, output_verified=False
"""

from __future__ import annotations

from eth_abi.abi import decode as abi_decode
from eth_abi.exceptions import DecodingError

from arbitrage_contracts.arc_extensions import SimulationEvidenceBridge, SimulationStatus
from atomic_execution.arc_encoding import EncodedArcCalldata, encode_arc_execution_plan
from atomic_execution.arc_planning import ArcExecutionPlan
from atomic_execution.arc_transport import (
    ArcSimulationTransport,
    InventoryUnknownError,
    SimulationTransportError,
)


class ArcSimulationService:
    """Read-only contract simulation service for Arc opportunities."""

    def __init__(
        self, transport: ArcSimulationTransport, backend_name: str = "arc_rpc_readonly"
    ) -> None:
        self.transport = transport
        self.backend_name = backend_name

    def simulate_plan(
        self,
        plan: ArcExecutionPlan,
        encoded: EncodedArcCalldata,
        caller_address: str,
        block_number: int,
        require_inventory_check: bool = True,
    ) -> SimulationEvidenceBridge:
        """Execute read-only simulation for an ArcExecutionPlan.

        Guarantees:
        1. Inventory check: Caller must hold >= amount_in; RPC timeout raises InventoryUnknownError.
        2. Contract revert: Returns call_succeeded=False, status=CONTRACT_REVERT.
        3. Output verification: If return data is empty or cannot be parsed, output_verified=False.
        """
        if type(block_number) is not int or block_number < 0:
            raise SimulationTransportError("A fixed nonnegative block number is required")
        try:
            raw = bytes.fromhex(encoded.calldata_hex[2:])
            _, _, deadline = abi_decode(["bytes", "bytes[]", "uint256"], raw[4:])
            expected = encode_arc_execution_plan(plan, deadline_s=deadline)
        except (ValueError, TypeError, DecodingError) as exc:
            raise SimulationTransportError("Malformed or unsupported encoded plan") from exc
        if encoded != expected:
            raise SimulationTransportError(
                "Encoded calldata/metadata do not match originating plan"
            )
        if require_inventory_check is not True:
            raise SimulationTransportError("Inventory checking cannot be disabled")
        base_token_addr = plan.base_asset.token_key.address if plan.base_asset.token_key else ""

        # Step 1: Pre-flight inventory verification
        if require_inventory_check and base_token_addr and self.transport.rpc_client is not None:
            caller_bal = self.transport.check_token_balance(
                token_address=base_token_addr,
                account_address=caller_address,
                block_number=block_number,
            )
            if caller_bal < plan.amount_in.atoms:
                return SimulationEvidenceBridge(
                    call_succeeded=False,
                    output_verified=False,
                    status=SimulationStatus.CONTRACT_REVERT,
                    net_output_atoms=None,
                    gas_used_atoms=None,
                    backend=self.backend_name,
                    execution_revert_reason=f"INSUFFICIENT_CALLER_BALANCE: {caller_bal} < {plan.amount_in.atoms}",
                )

        if self.transport.rpc_client is not None:
            tokens = {
                h.asset_in.token_key.address for h in plan.route_ref.hops if h.asset_in.token_key
            }
            tokens.update(
                h.asset_out.token_key.address for h in plan.route_ref.hops if h.asset_out.token_key
            )
            for token in tokens:
                if (
                    self.transport.check_token_balance(token, encoded.target_router, block_number)
                    != 0
                ):
                    raise InventoryUnknownError("Router inventory may subsidize simulated route")

        # Step 2: Execute routed eth_call simulation
        call_res = self.transport.execute_simulation_call(
            to_address=encoded.target_router,
            calldata_hex=encoded.calldata_hex,
            block_number=block_number,
            from_address=caller_address,
        )

        if not call_res.call_succeeded:
            return SimulationEvidenceBridge(
                call_succeeded=False,
                output_verified=False,
                status=call_res.status,
                net_output_atoms=None,
                gas_used_atoms=call_res.gas_used_atoms,
                backend=self.backend_name,
                execution_revert_reason=call_res.revert_reason or call_res.rpc_error_message,
            )

        # Universal Router return bytes do not prove post-state balances.
        # Only the independently bound OutputEvidence adapter may elevate verification.
        status = SimulationStatus.OUTPUT_UNVERIFIED
        output_verified = False
        net_atoms: int | None = None

        return SimulationEvidenceBridge(
            call_succeeded=True,
            output_verified=output_verified,
            status=status,
            net_output_atoms=net_atoms,
            gas_used_atoms=call_res.gas_used_atoms,
            backend=self.backend_name,
            execution_revert_reason=None,
        )
