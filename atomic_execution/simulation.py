"""Pure read-only atomic execution simulation adapter and error hierarchy.

Conforms to W5-E specifications (C13 ~ C17):
- C13 (Caller & Permissions Consistency): from == caller_wallet, value_wei == 0, state overrides prohibited;
- C14 (Fixed Block & Reorg Defense): strictly pinned block height, reorg mutation check before/after call;
- C15 (Five-State Outcome Hierarchy): CALL_SUCCEEDED, OUTPUT_UNVERIFIED (0x return), CONTRACT_REVERT,
  RPC_ERROR, NODE_LIMITATION. Zero-assumed output defense prevents copying quoter expected_out;
- C16 (Simulation Independence): strictly prohibits multi-hop quoter calldata or quoter addresses;
- C17 (Inventory Subsidy Defense): checks router balance of all path tokens == 0;
- Models: output DraftSimulationEvidence with can_atomic_execute strictly False.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import FrozenInstanceError
from enum import StrEnum
from typing import Any

from eth_abi import decode as abi_decode

from arbitrage_contracts.identity import (
    validate_bytes32,
    validate_evm_address,
)
from atomic_execution.encoding import (
    EncodedCalldata,
    encode_execution_plan,
)
from atomic_execution.inputs import ZERO_ADDRESS
from atomic_execution.models import DraftSimulationEvidence
from atomic_execution.planning import CANONICAL_UNIVERSAL_ROUTER, ExecutionPlan
from atomic_execution.transport import (
    BaseSimulationTransport,
    SimulationCallRequest,
    SimulationCallResponse,
    TransportContractRevertError,
    TransportNodeLimitationError,
    TransportRpcError,
)
from atomic_execution.transport import (
    NativeValueProhibitedError as TransportNativeValueError,
)
from atomic_execution.transport import (
    StateOverrideProhibitedError as TransportStateOverrideError,
)

# Alias per TASK-W5-E specification
EncodedCall = EncodedCalldata


class SimulationStatus(StrEnum):
    """Rigid five-state simulation outcome taxonomy plus reorg status per C14/C15."""

    CALL_SUCCEEDED = "CALL_SUCCEEDED"
    OUTPUT_UNVERIFIED = "OUTPUT_UNVERIFIED"
    CONTRACT_REVERT = "CONTRACT_REVERT"
    RPC_ERROR = "RPC_ERROR"
    NODE_LIMITATION = "NODE_LIMITATION"
    BLOCK_REORGANIZED = "BLOCK_REORGANIZED"


class SimulationError(Exception):
    """Base exception for all simulation-level failures."""


class SimulationSecurityError(SimulationError):
    """Base exception for C13/C14/C16/C17 security guard violations."""


class CallerSecurityError(SimulationSecurityError):
    """Raised on caller mismatch, zero-address caller, or unauthorized caller per C13."""


# Transport security errors re-exported as simulation security errors under C13
NativeValueProhibitedError = TransportNativeValueError
StateOverrideProhibitedError = TransportStateOverrideError


class InvalidBlockError(SimulationSecurityError):
    """Raised when block height is unpinned, non-positive, or uses 'latest' tags per C14."""


class BlockReorganizedError(SimulationSecurityError):
    """Raised when block hash mutates before or after simulation per C14."""


class QuoterSimulationProhibitedError(SimulationSecurityError):
    """Raised when a Quoter contract or quoter selector is used per C16."""


class InventorySubsidyError(SimulationSecurityError):
    """Raised when router holds non-zero balance of route path tokens per C17."""


class SimulationExecutionError(SimulationError):
    """Base exception for simulated execution and communication errors."""


class ContractRevertError(SimulationExecutionError):
    """Raised when on-chain contract execution reverts."""

    def __init__(self, message: str, revert_data_hex: str = "0x") -> None:
        super().__init__(message)
        self.revert_data_hex = revert_data_hex


class SimulationRpcError(SimulationExecutionError):
    """Raised on RPC timeout, rate limit, or communication failure."""


class NodeLimitationError(SimulationExecutionError):
    """Raised when node lacks archive state or fixed-block query capabilities."""


# Standard ABI revert selectors
ERROR_SELECTOR = "0x08c379a0"  # Error(string)
PANIC_SELECTOR = "0x4e487b71"  # Panic(uint256)
EXECUTION_FAILED_SELECTOR = "0x2c4029e9"  # ExecutionFailed(uint256,bytes)

PANIC_DESCRIPTIONS: dict[int, str] = {
    0x00: "Generic compiler panic",
    0x01: "Assert evaluated to false",
    0x11: "Arithmetic overflow or underflow",
    0x12: "Division or modulo by zero",
    0x21: "Invalid enum value conversion",
    0x22: "Access out-of-bounds storage array element",
    0x31: "Pop on empty array",
    0x32: "Array index out of bounds",
    0x41: "Resource allocation error (out of memory)",
    0x51: "Zero-initialized variable of internal function type",
}

KNOWN_QUOTER_ADDRESSES: frozenset[str] = frozenset(
    [
        "0x8dc178efb8111bb0973dd9d722ebeff267c98f94".lower(),  # V4 Quoter 4663
        "0x7466548773e35183efec4466b025d57b28dbdb3a".lower(),  # V3 QuoterV2 4663
        "0x88f28cc20014792ecfbe53e34b971a2e7c37b4e9".lower(),  # V3 Quoter 4663
        "0x61ffe014ba17989e743c5f6cb21bf9697540b217".lower(),  # V3 Quoter mainnet
        "0xb27308f9f90d607463bb33ea1bebb41c27ce5ab6".lower(),  # V3 QuoterV2 mainnet
    ]
)

KNOWN_QUOTER_SELECTORS: frozenset[str] = frozenset(
    [
        "0xcdca1753",  # quoteExactInput(bytes,uint256)
        "0x0424d622",  # quoteExactInputSingle(address,address,uint24,uint256,uint160)
        "0xc6a5026a",  # quoteExactInputSingle((address,address,uint256,uint24,uint160))
        "0xf7729d43",  # V4 quoteExactInputSingle
        "0xbd2ab012",  # quoteExactOutput
    ]
)


def parse_revert_data(revert_hex: str) -> str | None:
    """Safely decode raw hex revert data into readable error messages."""
    if not isinstance(revert_hex, str) or not revert_hex.startswith("0x"):
        return None

    clean_hex = revert_hex.strip()
    if len(clean_hex) < 10:
        return None

    selector = clean_hex[:10].lower()
    raw_payload = clean_hex[10:]

    try:
        payload_bytes = bytes.fromhex(raw_payload)
    except ValueError:
        return None

    if selector == ERROR_SELECTOR:
        try:
            decoded = abi_decode(["string"], payload_bytes)
            return f"Reverted: {decoded[0]}"
        except Exception:
            return f"Reverted with Error(string) (undecodable payload: {clean_hex[:34]}...)"

    if selector == PANIC_SELECTOR:
        try:
            decoded_panic = abi_decode(["uint256"], payload_bytes)
            panic_code = int(decoded_panic[0])
            desc = PANIC_DESCRIPTIONS.get(panic_code, "Unknown panic code")
            return f"Panic: {desc} (0x{panic_code:02x})"
        except Exception:
            return f"Reverted with Panic(uint256) (undecodable payload: {clean_hex[:34]}...)"

    if selector == EXECUTION_FAILED_SELECTOR:
        try:
            decoded_exec = abi_decode(["uint256", "bytes"], payload_bytes)
            command_index = int(decoded_exec[0])
            inner_bytes: bytes = decoded_exec[1]
            inner_hex = "0x" + inner_bytes.hex()
            inner_parsed = parse_revert_data(inner_hex)
            inner_detail = inner_parsed or inner_hex
            return f"ExecutionFailed at command {command_index}: {inner_detail}"
        except Exception:
            return f"ExecutionFailed (undecodable payload: {clean_hex[:34]}...)"

    return f"Custom error ({selector})"


def extract_path_token_addresses(plan: ExecutionPlan) -> tuple[str, ...]:
    """Extract sorted, deduplicated tuple of all token addresses involved in an ExecutionPlan."""
    tokens: set[str] = set()
    if plan.base_asset.token_key is not None:
        tokens.add(validate_evm_address(plan.base_asset.token_key.address))

    for hop in plan.route_ref.hops:
        if hop.asset_in.token_key is not None:
            tokens.add(validate_evm_address(hop.asset_in.token_key.address))
        if hop.asset_out.token_key is not None:
            tokens.add(validate_evm_address(hop.asset_out.token_key.address))

    return tuple(sorted(tokens))


# Invariant guard: ensure DraftSimulationEvidence.can_atomic_execute is strictly False and immutable
if not hasattr(DraftSimulationEvidence, "can_atomic_execute"):
    type.__setattr__(DraftSimulationEvidence, "can_atomic_execute", property(lambda self: False))
    type.__setattr__(
        DraftSimulationEvidence,
        "__setattr__",
        lambda self, name, value: (_ for _ in ()).throw(
            AttributeError("can_atomic_execute is immutable and strictly False")
            if name == "can_atomic_execute"
            else FrozenInstanceError(f"cannot assign to field {name!r}")
        ),
    )


class SimulationAdapter:
    """Pure read-only atomic execution simulation adapter conforming to C13~C17."""

    def __init__(
        self,
        transport: BaseSimulationTransport,
        authorized_callers: Sequence[str] | None = None,
    ) -> None:
        self._transport = transport
        self._authorized_callers = (
            tuple(validate_evm_address(caller).lower() for caller in authorized_callers)
            if authorized_callers is not None
            else None
        )

    @property
    def transport(self) -> BaseSimulationTransport:
        """Return underlying read-only simulation transport."""
        return self._transport

    @property
    def authorized_callers(self) -> tuple[str, ...] | None:
        """Return sequence of authorized caller addresses, if configured."""
        return self._authorized_callers

    def simulate(
        self,
        target: EncodedCalldata | ExecutionPlan,
        caller_wallet: str,
        block_number: int,
        block_hash: str,
        *,
        value_wei: int = 0,
        state_override: Mapping[str, Any] | None = None,
        path_tokens: Sequence[str] | None = None,
        execution_plan: ExecutionPlan | None = None,
        raise_on_revert: bool = False,
        raise_on_error: bool = False,
        raise_on_reorg: bool = True,
    ) -> DraftSimulationEvidence:
        """Execute single deterministic atomic eth_call simulation under C13~C17."""
        # ----------------------------------------------------------------------
        # Target Unpacking & Calldata Resolution
        # ----------------------------------------------------------------------
        # Metadata used here only for early structural/security rejection, never execution.
        if isinstance(target, ExecutionPlan):
            chain_id = target.chain_id
            router_address = target.target_router
            calldata_hex = "0x"
        elif isinstance(target, EncodedCalldata):
            chain_id = target.chain_id
            router_address = target.router_address
            calldata_hex = target.calldata_hex
        else:
            raise TypeError("target must be ExecutionPlan or EncodedCalldata")

        # ----------------------------------------------------------------------
        # C13 Guard: Caller, Permission & Value Consistency
        # ----------------------------------------------------------------------
        if not isinstance(caller_wallet, str) or not caller_wallet.strip():
            raise CallerSecurityError("caller_wallet must be a non-empty string under C13")

        validated_caller = validate_evm_address(caller_wallet)
        if validated_caller.lower() == ZERO_ADDRESS.lower():
            raise CallerSecurityError("Zero address (0x0) caller is strictly prohibited under C13")

        if (
            self._authorized_callers is not None
            and validated_caller.lower() not in self._authorized_callers
        ):
            raise CallerSecurityError(
                f"Caller {validated_caller} is not in authorized callers whitelist under C13"
            )

        if not isinstance(value_wei, int) or isinstance(value_wei, bool):
            raise NativeValueProhibitedError(
                f"value_wei must be integer 0, got {type(value_wei).__name__}"
            )
        if value_wei != 0:
            raise NativeValueProhibitedError(
                f"value_wei must be 0 under C13; Native ETH is strictly forbidden, got {value_wei}"
            )

        if state_override is not None and len(state_override) > 0:
            raise StateOverrideProhibitedError(
                "State overrides are strictly prohibited under C13 to prevent counterfeit solvency"
            )

        # ----------------------------------------------------------------------
        # C14 Guard: Fixed Block Height & Hash Validation
        # ----------------------------------------------------------------------
        if not isinstance(block_number, int) or isinstance(block_number, bool):
            raise InvalidBlockError(
                f"block_number must be an integer, got {type(block_number).__name__}"
            )
        if block_number <= 0:
            raise InvalidBlockError(
                f"block_number must be a fixed positive integer under C14; got {block_number}"
            )

        if not isinstance(block_hash, str) or not block_hash.strip():
            raise InvalidBlockError("block_hash must be a non-empty string under C14")
        if block_hash.strip().lower() in ("latest", "pending", "earliest"):
            raise InvalidBlockError(
                f"Block tag {block_hash!r} is strictly forbidden under C14; pinned block hash required"
            )

        validated_block_hash = validate_bytes32(block_hash).lower()

        # ----------------------------------------------------------------------
        # C16 Guard: Simulation Independence & Quoter Masquerade Defense
        # ----------------------------------------------------------------------
        normalized_router = validate_evm_address(router_address)
        if normalized_router.lower() in KNOWN_QUOTER_ADDRESSES:
            raise QuoterSimulationProhibitedError(
                f"Target address {normalized_router} is a known Quoter contract; "
                "atomic simulation must target Universal Router under C16"
            )

        calldata_selector = calldata_hex[:10].lower()
        if calldata_selector in KNOWN_QUOTER_SELECTORS:
            raise QuoterSimulationProhibitedError(
                f"Calldata selector {calldata_selector} matches Quoter method; "
                "multi-hop quoter calldata cannot masquerade as atomic simulation under C16"
            )

        if path_tokens is not None and len(path_tokens) == 0:
            raise InventorySubsidyError("Empty path_tokens is strictly prohibited")
        if isinstance(target, ExecutionPlan):
            plan = target
            if execution_plan is not None and execution_plan != plan:
                raise SimulationSecurityError("Conflicting execution plans")
        elif isinstance(target, EncodedCalldata):
            if not isinstance(execution_plan, ExecutionPlan):
                raise InventorySubsidyError(
                    "EncodedCalldata requires its originating execution_plan"
                )
            plan = execution_plan
        else:
            raise TypeError("target must be ExecutionPlan or EncodedCalldata")
        if (
            plan.chain_id != 4663
            or plan.target_router.lower() != CANONICAL_UNIVERSAL_ROUTER.lower()
        ):
            raise SimulationSecurityError("Unverified chain or router")
        encoded = encode_execution_plan(plan)
        if isinstance(target, EncodedCalldata):
            fields = (
                "plan_id",
                "route_id",
                "chain_id",
                "deadline",
                "amount_in",
                "min_amount_out",
                "commands_count",
            )
            if any(getattr(target, field) != getattr(encoded, field) for field in fields):
                raise SimulationSecurityError("Encoded metadata does not match plan")
            for field in ("router_address", "calldata_hex", "calldata_sha256", "commands_hex"):
                if getattr(target, field).lower() != getattr(encoded, field).lower():
                    raise SimulationSecurityError("Encoded calldata does not match plan")
        chain_id = plan.chain_id
        router_address = plan.target_router
        calldata_hex = encoded.calldata_hex
        calldata_sha256 = encoded.calldata_sha256
        resolved_path_tokens = tuple(token.lower() for token in extract_path_token_addresses(plan))
        if not resolved_path_tokens:
            raise InventorySubsidyError("No verified path tokens")
        if path_tokens is not None:
            supplied = {validate_evm_address(token).lower() for token in path_tokens}
            if supplied != set(resolved_path_tokens):
                raise InventorySubsidyError(
                    "Explicit path_tokens does not match execution plan tokens"
                )
        if plan.quoter_block != block_number:
            raise InvalidBlockError("Simulation block does not match execution plan")

        # ----------------------------------------------------------------------
        # C14 Pre-Call Block Hash Consistency Check
        # ----------------------------------------------------------------------
        hash_before = self._transport.get_block_hash(chain_id, block_number).lower()
        if hash_before != validated_block_hash:
            reorg_message = (
                f"Block hash mismatch before simulation at block {block_number}: "
                f"expected {validated_block_hash}, observed {hash_before}"
            )
            if raise_on_reorg:
                raise BlockReorganizedError(reorg_message)
            return DraftSimulationEvidence(
                chain_id=chain_id,
                router_address=normalized_router,
                calldata_hex=calldata_hex,
                calldata_sha256=calldata_sha256,
                block_number=block_number,
                block_hash=validated_block_hash,
                from_address=validated_caller,
                value_wei=0,
                status=str(SimulationStatus.BLOCK_REORGANIZED),
                gas_used=None,
                return_data_hex="0x",
                error_message=reorg_message,
                is_draft=True,
            )

        # ----------------------------------------------------------------------
        # C17 Guard: Inventory Subsidy Defense (Router zero balance check)
        # ----------------------------------------------------------------------
        for path_token in resolved_path_tokens:
            try:
                router_balance = self._transport.get_router_token_balance(
                    chain_id=chain_id,
                    router_address=normalized_router,
                    token_address=path_token,
                    block_number=block_number,
                )
            except TransportNodeLimitationError as exc:
                raise InventorySubsidyError("Missing verified router inventory evidence") from exc
            if type(router_balance) is not int or router_balance != 0:
                raise InventorySubsidyError(
                    f"Router holds non-zero balance ({router_balance}) of path token {path_token} "
                    f"at block {block_number}; simulation rejected under C17 inventory subsidy defense"
                )

        # ----------------------------------------------------------------------
        # Execution of eth_call via Transport
        # ----------------------------------------------------------------------
        request = SimulationCallRequest(
            chain_id=chain_id,
            to_address=normalized_router,
            from_address=validated_caller,
            calldata_hex=calldata_hex,
            calldata_sha256=calldata_sha256,
            block_number=block_number,
            block_hash=validated_block_hash,
            value_wei=0,
            state_override=None,
        )

        try:
            call_response = self._transport.simulate_call(request)
        except TransportContractRevertError as revert_exc:
            call_response = SimulationCallResponse(
                status=SimulationStatus.CONTRACT_REVERT,
                revert_data_hex=revert_exc.revert_data_hex,
                error_message=revert_exc.revert_reason or str(revert_exc),
            )
        except TransportRpcError as rpc_exc:
            call_response = SimulationCallResponse(
                status=SimulationStatus.RPC_ERROR,
                rpc_error_type=rpc_exc.error_type,
                error_message=str(rpc_exc),
            )
        except TransportNodeLimitationError as node_exc:
            call_response = SimulationCallResponse(
                status=SimulationStatus.NODE_LIMITATION,
                node_limitation_type=node_exc.limitation_type,
                error_message=str(node_exc),
            )
        except TransportStateOverrideError as override_exc:
            raise StateOverrideProhibitedError(str(override_exc)) from override_exc
        except TransportNativeValueError as native_exc:
            raise NativeValueProhibitedError(str(native_exc)) from native_exc

        # ----------------------------------------------------------------------
        # C14 Post-Call Block Hash Consistency (Reorg Detection)
        # ----------------------------------------------------------------------
        hash_after = self._transport.get_block_hash(chain_id, block_number).lower()
        if hash_after != validated_block_hash or hash_after != hash_before:
            reorg_message = (
                f"Block reorganization detected during simulation at block {block_number}: "
                f"block hash mutated from {validated_block_hash} to {hash_after}"
            )
            if raise_on_reorg:
                raise BlockReorganizedError(reorg_message)
            return DraftSimulationEvidence(
                chain_id=chain_id,
                router_address=normalized_router,
                calldata_hex=calldata_hex,
                calldata_sha256=calldata_sha256,
                block_number=block_number,
                block_hash=validated_block_hash,
                from_address=validated_caller,
                value_wei=0,
                status=str(SimulationStatus.BLOCK_REORGANIZED),
                gas_used=None,
                return_data_hex="0x",
                error_message=reorg_message,
                is_draft=True,
            )

        # ----------------------------------------------------------------------
        # C15 Outcome Classification & Zero-Assumed Output Defense
        # ----------------------------------------------------------------------
        status_str = call_response.status.upper()
        gas_used = call_response.gas_used
        return_data_hex = call_response.return_data_hex
        error_message: str | None = None

        if status_str == SimulationStatus.CALL_SUCCEEDED:
            if return_data_hex in ("0x", ""):
                # C15: eth_call returning 0x must be intercepted as OUTPUT_UNVERIFIED
                final_status = SimulationStatus.OUTPUT_UNVERIFIED
                return_data_hex = "0x"
                error_message = (
                    "eth_call returned empty data (0x); output unverified per C15. "
                    "Expected quoter output is strictly not assumed."
                )
            else:
                final_status = SimulationStatus.CALL_SUCCEEDED
                error_message = None

        elif status_str == SimulationStatus.OUTPUT_UNVERIFIED:
            final_status = SimulationStatus.OUTPUT_UNVERIFIED
            return_data_hex = "0x"
            error_message = (
                call_response.error_message or "eth_call returned 0x; output unverified per C15"
            )

        elif status_str == SimulationStatus.CONTRACT_REVERT:
            final_status = SimulationStatus.CONTRACT_REVERT
            revert_hex = call_response.revert_data_hex or return_data_hex
            return_data_hex = revert_hex if revert_hex.startswith("0x") else "0x"
            parsed_reason = parse_revert_data(return_data_hex)
            error_message = (
                parsed_reason or call_response.error_message or "Contract execution reverted"
            )
            if raise_on_revert:
                raise ContractRevertError(error_message, revert_data_hex=return_data_hex)

        elif status_str == SimulationStatus.RPC_ERROR:
            final_status = SimulationStatus.RPC_ERROR
            return_data_hex = "0x"
            error_message = (
                call_response.error_message
                or f"RPC communication failure ({call_response.rpc_error_type or 'UNKNOWN'})"
            )
            if raise_on_error:
                raise SimulationRpcError(error_message)

        elif status_str == SimulationStatus.NODE_LIMITATION:
            final_status = SimulationStatus.NODE_LIMITATION
            return_data_hex = "0x"
            error_message = (
                call_response.error_message
                or f"Node limitation ({call_response.node_limitation_type or 'UNSUPPORTED'})"
            )
            if raise_on_error:
                raise NodeLimitationError(error_message)

        else:
            final_status = SimulationStatus(status_str)
            error_message = call_response.error_message

        # ----------------------------------------------------------------------
        # Construct DraftSimulationEvidence
        # ----------------------------------------------------------------------
        evidence = DraftSimulationEvidence(
            chain_id=chain_id,
            router_address=normalized_router,
            calldata_hex=calldata_hex,
            calldata_sha256=calldata_sha256,
            block_number=block_number,
            block_hash=validated_block_hash,
            from_address=validated_caller,
            value_wei=0,
            status=str(final_status),
            gas_used=gas_used,
            return_data_hex=return_data_hex,
            error_message=error_message,
            is_draft=True,
        )

        assert evidence.can_atomic_execute is False  # type: ignore[attr-defined]
        return evidence


def simulate_execution(
    target: EncodedCalldata | ExecutionPlan,
    transport: BaseSimulationTransport,
    caller_wallet: str,
    block_number: int,
    block_hash: str,
    *,
    authorized_callers: Sequence[str] | None = None,
    value_wei: int = 0,
    state_override: Mapping[str, Any] | None = None,
    path_tokens: Sequence[str] | None = None,
    execution_plan: ExecutionPlan | None = None,
    raise_on_revert: bool = False,
    raise_on_error: bool = False,
    raise_on_reorg: bool = True,
) -> DraftSimulationEvidence:
    """Convenience functional wrapper for SimulationAdapter.simulate."""
    adapter = SimulationAdapter(transport=transport, authorized_callers=authorized_callers)
    return adapter.simulate(
        target=target,
        caller_wallet=caller_wallet,
        block_number=block_number,
        block_hash=block_hash,
        value_wei=value_wei,
        state_override=state_override,
        path_tokens=path_tokens,
        execution_plan=execution_plan,
        raise_on_revert=raise_on_revert,
        raise_on_error=raise_on_error,
        raise_on_reorg=raise_on_reorg,
    )
