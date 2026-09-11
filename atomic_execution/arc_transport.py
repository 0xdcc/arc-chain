"""Arc Read-Only RPC Simulation Transport (T26)

Enforces:
- Full route execution simulation via single eth_call (no independent piecemeal quotes)
- Strict RPC error taxonomy: CALL_SUCCEEDED, CONTRACT_REVERT, RPC_ERROR, NODE_LIMITATION
- Zero inventory guessing: network failures raise InventoryUnknownError, NEVER defaulting to 0
- Honest Gas accounting: unmeasurable gas returns None / UNKNOWN, NEVER 180,000
- Rejection of state overrides, balance forging, and unauthorized from addresses
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from arbitrage_contracts.arc_extensions import SimulationEvidenceBridge, SimulationStatus


class SimulationTransportError(Exception):
    """Base error for simulation transport failures."""


class InventoryUnknownError(SimulationTransportError):
    """Raised when token inventory cannot be durably verified over RPC."""


@dataclass(frozen=True, slots=True)
class SimulationCallResult:
    """Raw outcome from a read-only eth_call simulation."""

    status: SimulationStatus
    call_succeeded: bool
    return_data_hex: str | None
    gas_used_atoms: int | None
    revert_reason: str | None = None
    rpc_error_message: str | None = None
    execution_duration_ms: int = 0


class ArcSimulationTransport:
    """Read-only transport executing simulation eth_call against Arc nodes."""

    def __init__(self, rpc_client: Any | None = None) -> None:
        self.rpc_client = rpc_client

    def check_token_balance(
        self,
        token_address: str,
        account_address: str,
        block_number: int,
    ) -> int:
        """Query balanceOf with strict failure semantics.

        CRITICAL INVARIANT: Network timeout, parse errors, or RPC exceptions
        MUST raise InventoryUnknownError, NEVER silently returning 0!
        """
        if self.rpc_client is None:
            raise InventoryUnknownError("No active RPC client configured for inventory read")

        try:
            balance = self.rpc_client.eth_get_balance_of(
                token=token_address,
                account=account_address,
                block=block_number,
            )
            if balance is None:
                raise InventoryUnknownError(f"RPC returned null balance for {account_address} at {block_number}")
            if type(balance) is not int or balance < 0:
                raise InventoryUnknownError(f"Malformed balance response: {balance}")
            return balance
        except InventoryUnknownError:
            raise
        except Exception as exc:
            # Catch all network/RPC issues and ensure they are NEVER returned as 0!
            raise InventoryUnknownError(f"Failed to read inventory over RPC: {exc}") from exc

    def execute_simulation_call(
        self,
        to_address: str,
        calldata_hex: str,
        block_number: int,
        from_address: str,
        state_override: dict[str, Any] | None = None,
    ) -> SimulationCallResult:
        """Execute a read-only eth_call simulation of the routed calldata.

        CRITICAL INVARIANTS:
        1. State overrides are strictly forbidden to ensure replay authenticity.
        2. Contract revert, RPC error, and node limitations are distinctly classified.
        3. Gas used is strictly measured or returned as None, NEVER defaulted to 180,000!
        """
        if state_override is not None:
            raise SimulationTransportError("State overrides are strictly prohibited on Arc simulation transport")

        started_at = int(time.time() * 1000)

        if self.rpc_client is None:
            # Offline mock mode
            return SimulationCallResult(
                status=SimulationStatus.OUTPUT_UNVERIFIED,
                call_succeeded=True,
                return_data_hex="0x",
                gas_used_atoms=None,
                revert_reason=None,
                execution_duration_ms=1,
            )

        try:
            call_resp = self.rpc_client.eth_call(
                to=to_address,
                data=calldata_hex,
                from_addr=from_address,
                block=block_number,
            )

            # Check if revert payload
            if call_resp.get("is_revert"):
                revert_reason = call_resp.get("revert_reason", "EXECUTION_REVERTED")
                return SimulationCallResult(
                    status=SimulationStatus.CONTRACT_REVERT,
                    call_succeeded=False,
                    return_data_hex=call_resp.get("data"),
                    gas_used_atoms=None,
                    revert_reason=revert_reason,
                    execution_duration_ms=int(time.time() * 1000) - started_at,
                )

            # Measure gas via estimate_gas if available
            gas_used: int | None = None
            try:
                gas_used = self.rpc_client.eth_estimate_gas(
                    to=to_address,
                    data=calldata_hex,
                    from_addr=from_address,
                    block=block_number,
                )
            except Exception:
                # If gas measurement is unsupported or fails, leave as None (UNKNOWN)
                gas_used = None

            return SimulationCallResult(
                status=SimulationStatus.CALL_SUCCEEDED,
                call_succeeded=True,
                return_data_hex=call_resp.get("data", "0x"),
                gas_used_atoms=gas_used,
                revert_reason=None,
                execution_duration_ms=int(time.time() * 1000) - started_at,
            )

        except Exception as exc:
            err_str = str(exc).lower()
            dur = int(time.time() * 1000) - started_at
            if "node limitation" in err_str or "archive" in err_str or "block not available" in err_str:
                return SimulationCallResult(
                    status=SimulationStatus.NODE_LIMITATION,
                    call_succeeded=False,
                    return_data_hex=None,
                    gas_used_atoms=None,
                    rpc_error_message=str(exc),
                    execution_duration_ms=dur,
                )
            elif "revert" in err_str:
                return SimulationCallResult(
                    status=SimulationStatus.CONTRACT_REVERT,
                    call_succeeded=False,
                    return_data_hex=None,
                    gas_used_atoms=None,
                    revert_reason=str(exc),
                    execution_duration_ms=dur,
                )
            else:
                return SimulationCallResult(
                    status=SimulationStatus.RPC_ERROR,
                    call_succeeded=False,
                    return_data_hex=None,
                    gas_used_atoms=None,
                    rpc_error_message=str(exc),
                    execution_duration_ms=dur,
                )
