"""Universal Router Calldata Encoder and Dual-Decoded Verification for Atomic Execution.

Implements pure, deterministic calldata assembly for Uniswap V3, Uniswap V4, and
mixed-hop swap cycles on Robinhood (4663) targeting the Universal Router.
Enforces C10~C12 safety invariants:
- Bit 7 (allow_revert / 0x80) strictly unset across all commands
- Intermediate hop outputs settle in Router (ROUTER / address(this))
- Final hop output directed to sender wallet (SENDER / msg.sender)
- minOut rigidly bound to plan.min_amount_out on final hop, 1 on intermediate hops
- Uniswap V4 PoolKey strictly follows currency0 < currency1 lexicographical ordering
- Dual-decoded verification field-by-field equality check against ExecutionPlan
- Zero promotion: status is strictly ENCODED and can_atomic_execute is strictly False
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from eth_abi import decode as abi_decode
from eth_typing import ChecksumAddress, HexStr
from uniswap_universal_router_decoder import FunctionRecipient, PathKey, RouterCodec
from web3 import Web3
from web3.types import Wei

from arbitrage_contracts.identity import (
    AssetRef,
    validate_evm_address,
    validate_positive_integer,
)
from arbitrage_contracts.quote import HopRef
from atomic_execution.inputs import (
    ROBINHOOD_CHAIN_ID,
    ZERO_ADDRESS,
)
from atomic_execution.models import DraftSimulationEvidence
from atomic_execution.planning import ExecutionPlan

FLAG_ALLOW_REVERT: int = 0x80
COMMAND_TYPE_MASK: int = 0x3F
COMMAND_V3_SWAP_EXACT_IN: int = 0x00
COMMAND_V4_SWAP: int = 0x10

MSG_SENDER: str = "0x0000000000000000000000000000000000000001"
ADDRESS_THIS: str = "0x0000000000000000000000000000000000000002"
ROUTER_BALANCE: int = 1 << 255

EXECUTE_SELECTOR_WITH_DEADLINE: str = "0x3593564c"
SUPPORTED_COMMANDS: frozenset[int] = frozenset({COMMAND_V3_SWAP_EXACT_IN, COMMAND_V4_SWAP})


class EncodingMode(StrEnum):
    """Routing calldata assembly structure."""

    PURE_PATH = "pure_path"
    SEQUENTIAL = "sequential"


class EncodingError(Exception):
    """Base exception for Universal Router encoding and verification failures."""


class UnsupportedProtocolError(EncodingError):
    """Raised when an unwhitelisted or un-audited protocol is encountered."""


class UnsupportedChainError(EncodingError):
    """Raised when the execution plan target chain is not supported."""


class CommandSecurityError(EncodingError):
    """Raised when a command bit, recipient, or funds safety invariant is breached."""


class DualVerificationError(EncodingError):
    """Raised when dual-decoded verification does not match the ExecutionPlan."""


def _get_asset_address(asset: AssetRef) -> str:
    """Extract EVM address from AssetRef, raising EncodingError if missing."""
    if asset.token_key is None:
        raise EncodingError(f"AssetRef {asset} has no associated token_key address")
    return asset.token_key.address


def compute_calldata_sha256(calldata: str | bytes) -> str:
    """Compute the lowercase 64-hex-character SHA-256 digest of raw calldata bytes."""
    if isinstance(calldata, str):
        normalized = calldata.strip()
        if normalized.startswith("0x") or normalized.startswith("0X"):
            normalized = normalized[2:]
        raw_bytes = bytes.fromhex(normalized)
    else:
        raw_bytes = calldata
    return hashlib.sha256(raw_bytes).hexdigest()


def validate_commands_security(commands: bytes | str) -> None:
    """Validate Universal Router command bytes against C11 fail-closed security invariants.

    Asserts:
    1. Bit 7 (FLAG_ALLOW_REVERT) is strictly 0 for every command.
    2. Command IDs belong strictly to the supported whitelist (V3 or V4).
    """
    if isinstance(commands, str):
        normalized = commands.strip()
        if normalized.startswith("0x") or normalized.startswith("0X"):
            normalized = normalized[2:]
        command_bytes = bytes.fromhex(normalized)
    else:
        command_bytes = commands

    if not command_bytes:
        raise CommandSecurityError("Commands sequence cannot be empty")

    for command_index, command_byte in enumerate(command_bytes):
        if (command_byte & FLAG_ALLOW_REVERT) != 0:
            raise CommandSecurityError(
                f"Command byte at index {command_index} (0x{command_byte:02x}) has bit 7 "
                "(FLAG_ALLOW_REVERT) set: allow_revert is strictly prohibited per C11"
            )
        command_id = command_byte & COMMAND_TYPE_MASK
        if command_id not in SUPPORTED_COMMANDS:
            raise UnsupportedProtocolError(
                f"Command ID 0x{command_id:02x} at index {command_index} is unsupported or un-audited. "
                "Only Uniswap V3 (0x00) and Uniswap V4 (0x10) are permitted per C11/C12"
            )


def resolve_v4_pool_key(hop: HopRef) -> tuple[dict[str, Any], bool]:
    """Resolve canonical Uniswap V4 PoolKey ensuring currency0 < currency1 ordering.

    Returns a tuple of (pool_key_dict, zero_for_one).
    """
    desc = hop.pool_descriptor
    if desc is None:
        raise EncodingError(f"V4 hop {hop.pool_key.pool_id} is missing required pool_descriptor")

    if desc.hooks and desc.hooks != ZERO_ADDRESS:
        raise CommandSecurityError(
            f"V4 pool has non-zero hook {desc.hooks}: only zero-hook V4 pools are permitted per C12"
        )
    if desc.fee_model.kind != "static" or desc.fee_model.raw_value is None:
        raise CommandSecurityError(
            f"V4 pool has non-static fee model {desc.fee_model.kind}: dynamic fees forbidden per C12"
        )
    if desc.tick_spacing is None or desc.tick_spacing <= 0:
        raise EncodingError(f"V4 pool requires positive tick_spacing, got {desc.tick_spacing}")

    in_address = Web3.to_checksum_address(_get_asset_address(hop.asset_in))
    out_address = Web3.to_checksum_address(_get_asset_address(hop.asset_out))
    in_int = int(in_address, 16)
    out_int = int(out_address, 16)
    if in_int == out_int:
        raise EncodingError(f"Hop input and output assets cannot be identical: {in_address}")

    if in_int < out_int:
        currency0 = in_address
        currency1 = out_address
        zero_for_one = True
    else:
        currency0 = out_address
        currency1 = in_address
        zero_for_one = False

    pool_key_dict = {
        "currency0": currency0,
        "currency1": currency1,
        "fee": int(desc.fee_model.raw_value),
        "tick_spacing": int(desc.tick_spacing),
        "hooks": Web3.to_checksum_address(desc.hooks or ZERO_ADDRESS),
    }
    return pool_key_dict, zero_for_one


@dataclass(frozen=True, slots=True)
class DecodedVerificationResult:
    """Audit verification record confirming dual-decoded calldata matches ExecutionPlan."""

    is_valid: bool
    function_name: str
    commands_count: int
    deadline: int
    amount_in: int
    min_amount_out: int
    allow_revert_detected: bool
    intermediate_recipients: tuple[str, ...]
    final_recipient: str
    details: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize verification result to dictionary."""
        return {
            "is_valid": self.is_valid,
            "function_name": self.function_name,
            "commands_count": self.commands_count,
            "deadline": self.deadline,
            "amount_in": self.amount_in,
            "min_amount_out": self.min_amount_out,
            "allow_revert_detected": self.allow_revert_detected,
            "intermediate_recipients": list(self.intermediate_recipients),
            "final_recipient": self.final_recipient,
            "details": self.details,
        }

    def to_json(self) -> str:
        """Serialize verification result to compact canonical JSON."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True, slots=True)
class EncodedCalldata:
    """Immutable calldata container and metadata for Universal Router atomic execution."""

    plan_id: str
    route_id: str
    chain_id: int
    router_address: str
    calldata_hex: str
    calldata_sha256: str
    commands_hex: str
    commands_count: int
    deadline: int
    amount_in: int
    min_amount_out: int
    status: str = "ENCODED"
    can_atomic_execute: bool = False
    encoding_mode: str = "pure_path"
    dual_verified: bool = True
    verification_result: DecodedVerificationResult | None = None

    def __init__(
        self,
        plan_id: str,
        route_id: str,
        chain_id: int,
        router_address: str,
        calldata_hex: str,
        calldata_sha256: str,
        commands_hex: str,
        commands_count: int,
        deadline: int,
        amount_in: int,
        min_amount_out: int,
        status: str = "ENCODED",
        can_atomic_execute: bool = False,
        encoding_mode: str = "pure_path",
        dual_verified: bool = True,
        verification_result: DecodedVerificationResult | None = None,
    ) -> None:
        if type(plan_id) is not str or not plan_id.strip():
            raise EncodingError("plan_id must be a non-empty string")
        if type(route_id) is not str or not route_id.strip():
            raise EncodingError("route_id must be a non-empty string")
        val_chain_id = validate_positive_integer(chain_id, "chain_id")
        val_router = validate_evm_address(router_address)
        if type(calldata_hex) is not str or not calldata_hex.startswith("0x"):
            raise EncodingError("calldata_hex must be a 0x-prefixed hex string")
        if len(calldata_hex) % 2 != 0:
            raise EncodingError("calldata_hex must have even hex characters")
        if type(calldata_sha256) is not str or len(calldata_sha256) != 64:
            raise EncodingError("calldata_sha256 must be a 64-character hex string")
        val_deadline = validate_positive_integer(deadline, "deadline")
        val_amount_in = validate_positive_integer(amount_in, "amount_in")
        val_min_out = validate_positive_integer(min_amount_out, "min_amount_out")

        if can_atomic_execute:
            raise CommandSecurityError(
                "Offline encoding cannot promote capability to can_atomic_execute per C12"
            )

        object.__setattr__(self, "plan_id", plan_id.strip())
        object.__setattr__(self, "route_id", route_id.strip())
        object.__setattr__(self, "chain_id", val_chain_id)
        object.__setattr__(self, "router_address", val_router)
        object.__setattr__(self, "calldata_hex", calldata_hex)
        object.__setattr__(self, "calldata_sha256", calldata_sha256)
        object.__setattr__(self, "commands_hex", commands_hex)
        object.__setattr__(self, "commands_count", int(commands_count))
        object.__setattr__(self, "deadline", val_deadline)
        object.__setattr__(self, "amount_in", val_amount_in)
        object.__setattr__(self, "min_amount_out", val_min_out)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "can_atomic_execute", False)
        object.__setattr__(self, "encoding_mode", encoding_mode)
        object.__setattr__(self, "dual_verified", dual_verified)
        object.__setattr__(self, "verification_result", verification_result)

    def to_dict(self) -> dict[str, Any]:
        """Serialize encoded calldata package to nested dictionary."""
        return {
            "plan_id": self.plan_id,
            "route_id": self.route_id,
            "chain_id": self.chain_id,
            "router_address": self.router_address,
            "calldata_hex": self.calldata_hex,
            "calldata_sha256": self.calldata_sha256,
            "commands_hex": self.commands_hex,
            "commands_count": self.commands_count,
            "deadline": self.deadline,
            "amount_in": self.amount_in,
            "min_amount_out": self.min_amount_out,
            "status": self.status,
            "can_atomic_execute": self.can_atomic_execute,
            "encoding_mode": self.encoding_mode,
            "dual_verified": self.dual_verified,
            "verification_result": (
                self.verification_result.to_dict() if self.verification_result is not None else None
            ),
        }

    def to_json(self) -> str:
        """Serialize encoded calldata package to compact canonical JSON."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EncodedCalldata:
        """Reconstruct EncodedCalldata from mapping."""
        ver_data = data.get("verification_result")
        verification_result: DecodedVerificationResult | None = None
        if isinstance(ver_data, Mapping):
            verification_result = DecodedVerificationResult(
                is_valid=ver_data["is_valid"],
                function_name=ver_data["function_name"],
                commands_count=ver_data["commands_count"],
                deadline=ver_data["deadline"],
                amount_in=ver_data["amount_in"],
                min_amount_out=ver_data["min_amount_out"],
                allow_revert_detected=ver_data["allow_revert_detected"],
                intermediate_recipients=tuple(ver_data.get("intermediate_recipients", ())),
                final_recipient=ver_data["final_recipient"],
                details=ver_data.get("details", ""),
            )

        return cls(
            plan_id=data["plan_id"],
            route_id=data["route_id"],
            chain_id=data["chain_id"],
            router_address=data["router_address"],
            calldata_hex=data["calldata_hex"],
            calldata_sha256=data["calldata_sha256"],
            commands_hex=data["commands_hex"],
            commands_count=data["commands_count"],
            deadline=data["deadline"],
            amount_in=data["amount_in"],
            min_amount_out=data["min_amount_out"],
            status=data.get("status", "ENCODED"),
            can_atomic_execute=data.get("can_atomic_execute", False),
            encoding_mode=data.get("encoding_mode", "pure_path"),
            dual_verified=data.get("dual_verified", True),
            verification_result=verification_result,
        )

    @classmethod
    def from_json(cls, text: str) -> EncodedCalldata:
        """Deserialize EncodedCalldata from JSON string."""
        return cls.from_dict(json.loads(text))

    def to_simulation_evidence(
        self,
        block_number: int,
        block_hash: str,
        from_address: str,
        *,
        value_wei: int = 0,
        status: str = "CALL_SUCCEEDED",
        gas_used: int | None = None,
        return_data_hex: str = "0x",
        error_message: str | None = None,
    ) -> DraftSimulationEvidence:
        """Convert encoded calldata package into internal DraftSimulationEvidence model."""
        return DraftSimulationEvidence(
            chain_id=self.chain_id,
            router_address=self.router_address,
            calldata_hex=self.calldata_hex,
            calldata_sha256=self.calldata_sha256,
            block_number=block_number,
            block_hash=block_hash,
            from_address=from_address,
            value_wei=value_wei,
            status=status,
            gas_used=gas_used,
            return_data_hex=return_data_hex,
            error_message=error_message,
            is_draft=True,
        )


def _inspect_hop_protocols(hops: Sequence[HopRef]) -> tuple[bool, bool]:
    """Inspect hops and verify protocol whitelist, returning (all_v3, all_v4)."""
    v3_count = 0
    v4_count = 0
    for hop_index, hop in enumerate(hops):
        protocol = hop.pool_key.protocol_id.lower()
        if "v2" in protocol:
            raise UnsupportedProtocolError(
                f"Hop {hop_index} specifies unsupported protocol {hop.pool_key.protocol_id}. "
                "Uniswap V2 is strictly unsupported on Robinhood 4663 per C12"
            )
        if "v3" in protocol:
            v3_count += 1
            if hop.pool_descriptor is None or hop.pool_descriptor.fee_model.raw_value is None:
                raise EncodingError(
                    f"Hop {hop_index} (V3) requires pool_descriptor with raw_value fee"
                )
        elif "v4" in protocol:
            v4_count += 1
            resolve_v4_pool_key(hop)
        else:
            raise UnsupportedProtocolError(
                f"Hop {hop_index} specifies unknown protocol {hop.pool_key.protocol_id}. "
                "Only Uniswap V3 and V4 are permitted per C12"
            )
    return (v3_count == len(hops)), (v4_count == len(hops))


def verify_dual_decoded_calldata(
    calldata_hex: str,
    plan: ExecutionPlan,
    *,
    recipient: str | None = None,
    expected_deadline: int | None = None,
    codec: RouterCodec | None = None,
) -> DecodedVerificationResult:
    """Perform independent dual-decoded verification against original ExecutionPlan per C10.

    Reverses raw calldata into ABI typed structures and checks field-by-field equality:
    - execute selector and deadline
    - allow_revert bit 7 strictly unset on all commands
    - intermediate hop outputs directed to ROUTER (address(this))
    - final hop output directed to SENDER (msg.sender) or authorized recipient
    - intermediate hop minOut is 1
    - final hop minOut is strictly plan.min_amount_out.atoms
    - token path, pool fees, tick spacings, and currency ordering match plan
    """
    if type(calldata_hex) is not str or not calldata_hex.startswith("0x"):
        raise DualVerificationError("calldata_hex must be a 0x-prefixed hex string")
    if len(calldata_hex) < 10:
        raise DualVerificationError("calldata_hex is too short to contain function selector")

    selector = calldata_hex[:10].lower()
    if selector != EXECUTE_SELECTOR_WITH_DEADLINE.lower():
        raise DualVerificationError(
            f"Calldata selector {selector} does not match execute(bytes,bytes[],uint256) selector {EXECUTE_SELECTOR_WITH_DEADLINE}"
        )

    resolved_codec = codec if codec is not None else RouterCodec()
    try:
        fct_instance, decoded_input = resolved_codec.decode.function_input(
            cast(HexStr, calldata_hex)
        )
    except Exception as decode_exc:
        raise DualVerificationError(
            f"Failed to decode Universal Router calldata: {decode_exc}"
        ) from decode_exc

    if fct_instance.fn_name != "execute":
        raise DualVerificationError(
            f"Decoded function name is {fct_instance.fn_name}, expected execute"
        )

    target_deadline = expected_deadline if expected_deadline is not None else plan.deadline
    if decoded_input.get("deadline") != target_deadline:
        dec_dl = decoded_input.get("deadline")
        raise DualVerificationError(
            f"Decoded deadline {dec_dl} does not match target deadline {target_deadline}"
        )

    commands = cast(bytes, decoded_input.get("commands", b""))
    validate_commands_security(commands)

    inputs = decoded_input.get("inputs", [])
    if len(inputs) != len(commands):
        raise DualVerificationError(
            f"Mismatch between commands length ({len(commands)}) and decoded inputs length ({len(inputs)})"
        )

    expected_final_recipient = (
        Web3.to_checksum_address(recipient)
        if recipient is not None
        else Web3.to_checksum_address(MSG_SENDER)
    )

    intermediate_recipients: list[str] = []
    allow_revert_detected = False

    # Case 1: Pure V3 path (single command 0x00)
    if commands == bytes([COMMAND_V3_SWAP_EXACT_IN]):
        cmd_fn, cmd_args, flags = inputs[0]
        if not flags.get("revert_on_fail", False):
            allow_revert_detected = True
            raise CommandSecurityError("Bit 7 (allow_revert) detected in decoded V3 command flags")

        dec_amt_in = cmd_args.get("amountIn")
        if dec_amt_in != plan.amount_in.atoms:
            raise DualVerificationError(
                f"Decoded amountIn ({dec_amt_in}) != plan amount_in ({plan.amount_in.atoms})"
            )
        dec_amt_out_min = cmd_args.get("amountOutMin")
        if dec_amt_out_min != plan.min_amount_out.atoms:
            raise DualVerificationError(
                f"Decoded amountOutMin ({dec_amt_out_min}) != plan min_amount_out ({plan.min_amount_out.atoms})"
            )
        if Web3.to_checksum_address(cmd_args.get("recipient", "")) != expected_final_recipient:
            dec_recip = cmd_args.get("recipient")
            raise DualVerificationError(
                f"Decoded recipient ({dec_recip}) != expected final recipient ({expected_final_recipient})"
            )
        if not cmd_args.get("payerIsSender", False):
            raise DualVerificationError("V3 pure path payerIsSender must be True")

        path_bytes = cmd_args.get("path", b"")
        decoded_path = resolved_codec.decode.v3_path("V3_SWAP_EXACT_IN", path_bytes)
        expected_start = Web3.to_checksum_address(_get_asset_address(plan.hops[0].asset_in))
        expected_end = Web3.to_checksum_address(_get_asset_address(plan.min_amount_out.asset_ref))

        if Web3.to_checksum_address(str(decoded_path[0])) != expected_start:
            raise DualVerificationError(
                f"Path start asset {decoded_path[0]} != expected {expected_start}"
            )
        if Web3.to_checksum_address(str(decoded_path[-1])) != expected_end:
            raise DualVerificationError(
                f"Path end asset {decoded_path[-1]} != expected {expected_end}"
            )

        for hop_index, hop in enumerate(plan.hops):
            expected_fee = hop.pool_descriptor.fee_model.raw_value if hop.pool_descriptor else None
            decoded_fee = decoded_path[2 * hop_index + 1]
            if decoded_fee != expected_fee:
                raise DualVerificationError(
                    f"Hop {hop_index} fee mismatch: decoded {decoded_fee} != expected {expected_fee}"
                )
            expected_hop_out = Web3.to_checksum_address(_get_asset_address(hop.asset_out))
            decoded_hop_out = Web3.to_checksum_address(str(decoded_path[2 * hop_index + 2]))
            if decoded_hop_out != expected_hop_out:
                raise DualVerificationError(
                    f"Hop {hop_index} asset_out mismatch: decoded {decoded_hop_out} != expected {expected_hop_out}"
                )

    # Case 2: Pure V4 path (single command 0x10)
    elif commands == bytes([COMMAND_V4_SWAP]):
        cmd_fn, cmd_args, flags = inputs[0]
        if not flags.get("revert_on_fail", False):
            allow_revert_detected = True
            raise CommandSecurityError("Bit 7 (allow_revert) detected in decoded V4 command flags")

        actions_params = cmd_args.get("params", [])
        action_names = [item[0].fn_name for item in actions_params]

        if "SETTLE" not in action_names or "SWAP_EXACT_IN" not in action_names:
            raise DualVerificationError(
                f"V4 pure path missing required SETTLE or SWAP_EXACT_IN actions: found {action_names}"
            )

        settle_dict = next(item[1] for item in actions_params if item[0].fn_name == "SETTLE")
        swap_dict = next(item[1] for item in actions_params if item[0].fn_name == "SWAP_EXACT_IN")
        take_item = next(
            (item for item in actions_params if item[0].fn_name in ("TAKE_ALL", "TAKE")), None
        )

        if take_item is None:
            raise DualVerificationError("V4 pure path missing TAKE_ALL or TAKE action")

        expected_start = Web3.to_checksum_address(_get_asset_address(plan.hops[0].asset_in))
        expected_end = Web3.to_checksum_address(_get_asset_address(plan.min_amount_out.asset_ref))

        if Web3.to_checksum_address(settle_dict.get("currency", "")) != expected_start:
            dec_cur = settle_dict.get("currency")
            raise DualVerificationError(
                f"V4 SETTLE currency {dec_cur} != expected {expected_start}"
            )
        if settle_dict.get("amount") != plan.amount_in.atoms:
            dec_amt = settle_dict.get("amount")
            raise DualVerificationError(
                f"V4 SETTLE amount {dec_amt} != expected {plan.amount_in.atoms}"
            )
        if not settle_dict.get("payerIsUser", False):
            raise DualVerificationError("V4 SETTLE payerIsUser must be True for pure path")

        swap_params = swap_dict.get("params", {})
        if Web3.to_checksum_address(swap_params.get("currencyIn", "")) != expected_start:
            dec_in = swap_params.get("currencyIn")
            raise DualVerificationError(f"V4 SWAP currencyIn {dec_in} != expected {expected_start}")
        if swap_params.get("amountIn") != plan.amount_in.atoms:
            dec_sw_amt = swap_params.get("amountIn")
            raise DualVerificationError(
                f"V4 SWAP amountIn {dec_sw_amt} != expected {plan.amount_in.atoms}"
            )
        if swap_params.get("amountOutMinimum") != plan.min_amount_out.atoms:
            dec_min = swap_params.get("amountOutMinimum")
            raise DualVerificationError(
                f"V4 SWAP amountOutMinimum {dec_min} != expected {plan.min_amount_out.atoms}"
            )

        path_keys = swap_params.get("PathKeys", [])
        if len(path_keys) != len(plan.hops):
            raise DualVerificationError(
                f"V4 PathKeys count ({len(path_keys)}) != hops count ({len(plan.hops)})"
            )

        for hop_index, hop in enumerate(plan.hops):
            pk = path_keys[hop_index]
            expected_hop_out = Web3.to_checksum_address(_get_asset_address(hop.asset_out))
            if Web3.to_checksum_address(pk.get("intermediateCurrency", "")) != expected_hop_out:
                dec_inter = pk.get("intermediateCurrency")
                raise DualVerificationError(
                    f"PathKey {hop_index} intermediateCurrency {dec_inter} != {expected_hop_out}"
                )
            desc = hop.pool_descriptor
            if desc and pk.get("fee") != desc.fee_model.raw_value:
                dec_f = pk.get("fee")
                raise DualVerificationError(
                    f"PathKey {hop_index} fee {dec_f} != {desc.fee_model.raw_value}"
                )
            if desc and pk.get("tickSpacing") != desc.tick_spacing:
                dec_ts = pk.get("tickSpacing")
                raise DualVerificationError(
                    f"PathKey {hop_index} tickSpacing {dec_ts} != {desc.tick_spacing}"
                )

        take_fn, take_dict = take_item
        if Web3.to_checksum_address(take_dict.get("currency", "")) != expected_end:
            dec_tk_cur = take_dict.get("currency")
            raise DualVerificationError(f"V4 TAKE currency {dec_tk_cur} != expected {expected_end}")
        if take_fn.fn_name == "TAKE_ALL":
            if take_dict.get("minAmount") != plan.min_amount_out.atoms:
                dec_min_amt = take_dict.get("minAmount")
                raise DualVerificationError(
                    f"V4 TAKE_ALL minAmount {dec_min_amt} != expected {plan.min_amount_out.atoms}"
                )
        elif take_fn.fn_name == "TAKE":
            if Web3.to_checksum_address(take_dict.get("recipient", "")) != expected_final_recipient:
                dec_rec = take_dict.get("recipient")
                raise DualVerificationError(
                    f"V4 TAKE recipient {dec_rec} != expected {expected_final_recipient}"
                )

    # Case 3: Sequential assembly (one command per hop)
    else:
        if len(inputs) != len(plan.hops):
            raise DualVerificationError(
                f"Sequential commands count ({len(inputs)}) != route hops count ({len(plan.hops)})"
            )

        total_hops = len(plan.hops)
        for hop_index, hop in enumerate(plan.hops):
            is_first = hop_index == 0
            is_last = hop_index == total_hops - 1
            target_recipient = (
                expected_final_recipient if is_last else Web3.to_checksum_address(ADDRESS_THIS)
            )
            target_min_out = plan.min_amount_out.atoms if is_last else 1

            if not is_last:
                intermediate_recipients.append(target_recipient)

            cmd_fn, cmd_args, flags = inputs[hop_index]
            if not flags.get("revert_on_fail", False):
                allow_revert_detected = True
                raise CommandSecurityError(
                    f"Bit 7 (allow_revert) detected on command index {hop_index}"
                )

            protocol = hop.pool_key.protocol_id.lower()
            if "v3" in protocol:
                if cmd_fn.fn_name != "V3_SWAP_EXACT_IN":
                    raise DualVerificationError(
                        f"Hop {hop_index} expected V3_SWAP_EXACT_IN, got {cmd_fn.fn_name}"
                    )
                if Web3.to_checksum_address(cmd_args.get("recipient", "")) != target_recipient:
                    raise DualVerificationError(
                        f"Hop {hop_index} recipient ({cmd_args.get(recipient)}) != {target_recipient}"
                    )
                if cmd_args.get("amountOutMin") != target_min_out:
                    dec_hop_min = cmd_args.get("amountOutMin")
                    raise DualVerificationError(
                        f"Hop {hop_index} amountOutMin ({dec_hop_min}) != {target_min_out}"
                    )
                if is_first:
                    if cmd_args.get("amountIn") != plan.amount_in.atoms or not cmd_args.get(
                        "payerIsSender", False
                    ):
                        raise DualVerificationError(
                            "First hop V3 amountIn or payerIsSender mismatch"
                        )
                else:
                    if cmd_args.get("amountIn") != ROUTER_BALANCE or cmd_args.get(
                        "payerIsSender", False
                    ):
                        raise DualVerificationError(f"Hop {hop_index} V3 balance chaining mismatch")

                path_bytes = cmd_args.get("path", b"")
                decoded_path = resolved_codec.decode.v3_path("V3_SWAP_EXACT_IN", path_bytes)
                hop_in = Web3.to_checksum_address(_get_asset_address(hop.asset_in))
                hop_out = Web3.to_checksum_address(_get_asset_address(hop.asset_out))
                if Web3.to_checksum_address(str(decoded_path[0])) != hop_in:
                    raise DualVerificationError(
                        f"Hop {hop_index} input {decoded_path[0]} != {hop_in}"
                    )
                if Web3.to_checksum_address(str(decoded_path[2])) != hop_out:
                    raise DualVerificationError(
                        f"Hop {hop_index} output {decoded_path[2]} != {hop_out}"
                    )

            elif "v4" in protocol:
                if cmd_fn.fn_name != "V4_SWAP":
                    raise DualVerificationError(
                        f"Hop {hop_index} expected V4_SWAP, got {cmd_fn.fn_name}"
                    )
                actions_params = cmd_args.get("params", [])
                action_names = [item[0].fn_name for item in actions_params]

                if (
                    "SETTLE" not in action_names
                    or "SWAP_EXACT_IN_SINGLE" not in action_names
                    or "TAKE" not in action_names
                ):
                    raise DualVerificationError(
                        f"Hop {hop_index} V4 missing required actions: found {action_names}"
                    )

                settle_dict = next(
                    item[1] for item in actions_params if item[0].fn_name == "SETTLE"
                )
                swap_dict = next(
                    item[1] for item in actions_params if item[0].fn_name == "SWAP_EXACT_IN_SINGLE"
                )
                take_dict = next(item[1] for item in actions_params if item[0].fn_name == "TAKE")

                hop_in = Web3.to_checksum_address(_get_asset_address(hop.asset_in))
                hop_out = Web3.to_checksum_address(_get_asset_address(hop.asset_out))

                if Web3.to_checksum_address(settle_dict.get("currency", "")) != hop_in:
                    raise DualVerificationError(f"Hop {hop_index} V4 SETTLE currency mismatch")
                expected_settle_amt = plan.amount_in.atoms if is_first else ROUTER_BALANCE
                if (
                    settle_dict.get("amount") != expected_settle_amt
                    or settle_dict.get("payerIsUser") != is_first
                ):
                    raise DualVerificationError(f"Hop {hop_index} V4 SETTLE amount/payer mismatch")

                single_params = swap_dict.get("exact_in_single_params", {})
                decoded_pk = single_params.get("PoolKey", {})
                pk_dict, expected_zero_for_one = resolve_v4_pool_key(hop)

                if (
                    Web3.to_checksum_address(decoded_pk.get("currency0", ""))
                    != pk_dict["currency0"]
                ):
                    raise DualVerificationError(f"Hop {hop_index} PoolKey currency0 mismatch")
                if (
                    Web3.to_checksum_address(decoded_pk.get("currency1", ""))
                    != pk_dict["currency1"]
                ):
                    raise DualVerificationError(f"Hop {hop_index} PoolKey currency1 mismatch")
                if decoded_pk.get("fee") != pk_dict["fee"]:
                    raise DualVerificationError(f"Hop {hop_index} PoolKey fee mismatch")
                if decoded_pk.get("tickSpacing") != pk_dict["tick_spacing"]:
                    raise DualVerificationError(f"Hop {hop_index} PoolKey tickSpacing mismatch")
                if single_params.get("zeroForOne") != expected_zero_for_one:
                    raise DualVerificationError(f"Hop {hop_index} zeroForOne mismatch")
                if single_params.get("amountOutMinimum") != target_min_out:
                    dec_sing_min = single_params.get("amountOutMinimum")
                    raise DualVerificationError(
                        f"Hop {hop_index} amountOutMinimum ({dec_sing_min}) != {target_min_out}"
                    )

                if Web3.to_checksum_address(take_dict.get("currency", "")) != hop_out:
                    raise DualVerificationError(f"Hop {hop_index} TAKE currency mismatch")
                if Web3.to_checksum_address(take_dict.get("recipient", "")) != target_recipient:
                    raise DualVerificationError(
                        f"Hop {hop_index} TAKE recipient ({take_dict.get(recipient)}) != {target_recipient}"
                    )

    return DecodedVerificationResult(
        is_valid=True,
        function_name=fct_instance.fn_name,
        commands_count=len(commands),
        deadline=target_deadline,
        amount_in=plan.amount_in.atoms,
        min_amount_out=plan.min_amount_out.atoms,
        allow_revert_detected=allow_revert_detected,
        intermediate_recipients=tuple(intermediate_recipients),
        final_recipient=expected_final_recipient,
        details="Dual-decoded verification succeeded: calldata strictly matches ExecutionPlan 100%",
    )


def encode_execution_plan(
    plan: ExecutionPlan,
    *,
    recipient: str | None = None,
    deadline: int | None = None,
    force_sequential: bool = False,
    allowed_recipients: Sequence[str] | set[str] | None = None,
    verify: bool = True,
    codec: RouterCodec | None = None,
) -> EncodedCalldata:
    """Assemble deterministic Universal Router calldata for an ExecutionPlan per C10~C12.

    Supports:
    - Pure Uniswap V3 multi-hop path (single V3_SWAP_EXACT_IN command)
    - Pure Uniswap V4 multi-hop path (single V4_SWAP command with PathKeys)
    - Mixed V3 and V4 sequential assembly (chained hop commands with Router intermediate balance)

    Invariants strictly enforced:
    - Target chain must be Robinhood (4663)
    - Intermediate hops output to ROUTER (address(this)), minOut = 1
    - Final hop output to SENDER (msg.sender) or authorized recipient, minOut = plan.min_amount_out.atoms
    - Bit 7 (allow_revert / 0x80) strictly 0 on every command
    - V4 currency0 < currency1 lexicographical ordering
    - Dual-decoded verification against original ExecutionPlan
    - Output status strictly ENCODED, can_atomic_execute strictly False
    """
    if not isinstance(plan, ExecutionPlan):
        raise TypeError(f"plan must be ExecutionPlan, got {type(plan).__name__}")

    if plan.chain_id != ROBINHOOD_CHAIN_ID:
        raise UnsupportedChainError(
            f"Chain ID {plan.chain_id} is unsupported. Only Robinhood ({ROBINHOOD_CHAIN_ID}) is supported per C12"
        )

    if len(plan.hops) < 2 or len(plan.hops) > 3:
        raise EncodingError(
            f"Unsupported hop count {len(plan.hops)}: only 2-hop or 3-hop cycles supported"
        )

    if plan.min_amount_out.atoms <= 0:
        raise CommandSecurityError(
            f"Slippage safety violation: min_amount_out must be > 0, got {plan.min_amount_out.atoms}"
        )

    resolved_codec = codec if codec is not None else RouterCodec()
    builder = resolved_codec.encode.chain()

    # Resolve deadline
    resolved_deadline: int
    if deadline is not None:
        resolved_deadline = validate_positive_integer(deadline, "deadline")
    else:
        resolved_deadline = plan.deadline

    # Resolve recipient
    final_recipient: str
    function_recipient: FunctionRecipient
    custom_recipient: ChecksumAddress | None

    if recipient is None:
        final_recipient = Web3.to_checksum_address(MSG_SENDER)
        function_recipient = FunctionRecipient.SENDER
        custom_recipient = None
    else:
        validated_recipient = validate_evm_address(recipient)
        if validated_recipient.lower() == ADDRESS_THIS.lower():
            raise CommandSecurityError(
                "ROUTER cannot be the final recipient: funds would remain trapped in router"
            )
        if validated_recipient.lower() == MSG_SENDER.lower():
            final_recipient = Web3.to_checksum_address(MSG_SENDER)
            function_recipient = FunctionRecipient.SENDER
            custom_recipient = None
        else:
            if allowed_recipients is None:
                raise CommandSecurityError(
                    f"Recipient {validated_recipient} is not authorized: allowed_recipients not provided"
                )
            allowed_set = {validate_evm_address(a).lower() for a in allowed_recipients}
            if validated_recipient.lower() not in allowed_set:
                raise CommandSecurityError(
                    f"Recipient {validated_recipient} is not in authorized recipients list"
                )
            final_recipient = Web3.to_checksum_address(validated_recipient)
            function_recipient = FunctionRecipient.CUSTOM
            custom_recipient = Web3.to_checksum_address(validated_recipient)

    all_v3, all_v4 = _inspect_hop_protocols(plan.hops)

    encoding_mode = (
        EncodingMode.SEQUENTIAL
        if (force_sequential or (not all_v3 and not all_v4))
        else EncodingMode.PURE_PATH
    )

    if all_v3 and not force_sequential:
        # Optimal V3 multi-hop pure path
        v3_path: list[int | ChecksumAddress] = [
            Web3.to_checksum_address(_get_asset_address(plan.hops[0].asset_in))
        ]
        for hop in plan.hops:
            desc = hop.pool_descriptor
            if desc is None or desc.fee_model.raw_value is None:
                raise EncodingError("V3 hop missing pool_descriptor fee")
            v3_path.append(int(desc.fee_model.raw_value))
            v3_path.append(Web3.to_checksum_address(_get_asset_address(hop.asset_out)))

        builder.v3_swap_exact_in(
            function_recipient=function_recipient,
            amount_in=cast(Wei, plan.amount_in.atoms),
            amount_out_min=cast(Wei, plan.min_amount_out.atoms),
            path=cast(Sequence[int | ChecksumAddress], v3_path),
            custom_recipient=custom_recipient,
            payer_is_sender=True,
        )

    elif all_v4 and not force_sequential:
        # Optimal V4 multi-hop pure path with PathKeys
        path_keys: list[PathKey] = []
        for hop in plan.hops:
            desc = hop.pool_descriptor
            if desc is None or desc.fee_model.raw_value is None or desc.tick_spacing is None:
                raise EncodingError("V4 hop missing fee or tick_spacing")
            path_keys.append(
                PathKey(
                    intermediate_currency=Web3.to_checksum_address(
                        _get_asset_address(hop.asset_out)
                    ),
                    fee=int(desc.fee_model.raw_value),
                    tick_spacing=int(desc.tick_spacing),
                    hooks=Web3.to_checksum_address(desc.hooks or ZERO_ADDRESS),
                    hook_data=b"",
                )
            )

        first_token = Web3.to_checksum_address(_get_asset_address(plan.hops[0].asset_in))
        last_token = Web3.to_checksum_address(_get_asset_address(plan.min_amount_out.asset_ref))

        v4_builder = builder.v4_swap()
        v4_builder.settle(
            currency=first_token,
            amount=plan.amount_in.atoms,
            payer_is_user=True,
        )
        v4_builder.swap_exact_in(
            currency_in=first_token,
            path_keys=path_keys,
            amount_in=plan.amount_in.atoms,
            amount_out_min=plan.min_amount_out.atoms,
        )
        if custom_recipient is not None:
            v4_builder.take(
                currency=last_token,
                recipient=custom_recipient,
                amount=0,
            )
        else:
            v4_builder.take_all(
                currency=last_token,
                min_amount=cast(Wei, plan.min_amount_out.atoms),
            )
        v4_builder.build_v4_swap()

    else:
        # Mixed or forced sequential assembly
        total_hops = len(plan.hops)
        for hop_index, hop in enumerate(plan.hops):
            is_first = hop_index == 0
            is_last = hop_index == total_hops - 1

            hop_recipient_func = function_recipient if is_last else FunctionRecipient.ROUTER
            hop_custom_recip = custom_recipient if is_last else None
            hop_recipient_addr = final_recipient if is_last else ADDRESS_THIS
            hop_min_out = plan.min_amount_out.atoms if is_last else 1

            protocol = hop.pool_key.protocol_id.lower()
            desc = hop.pool_descriptor

            if "v3" in protocol:
                if desc is None or desc.fee_model.raw_value is None:
                    raise EncodingError("V3 hop missing fee")
                v3_hop_path: list[int | ChecksumAddress] = [
                    Web3.to_checksum_address(_get_asset_address(hop.asset_in)),
                    int(desc.fee_model.raw_value),
                    Web3.to_checksum_address(_get_asset_address(hop.asset_out)),
                ]
                if is_first:
                    builder.v3_swap_exact_in(
                        function_recipient=hop_recipient_func,
                        amount_in=cast(Wei, plan.amount_in.atoms),
                        amount_out_min=cast(Wei, hop_min_out),
                        path=cast(Sequence[int | ChecksumAddress], v3_hop_path),
                        custom_recipient=hop_custom_recip,
                        payer_is_sender=True,
                    )
                else:
                    builder.v3_swap_exact_in_from_balance(
                        function_recipient=hop_recipient_func,
                        amount_out_min=cast(Wei, hop_min_out),
                        path=cast(Sequence[int | ChecksumAddress], v3_hop_path),
                        custom_recipient=hop_custom_recip,
                    )

            elif "v4" in protocol:
                pool_key_dict, zero_for_one = resolve_v4_pool_key(hop)
                v4_pk = resolved_codec.encode.v4_pool_key(
                    pool_key_dict["currency0"],
                    pool_key_dict["currency1"],
                    pool_key_dict["fee"],
                    pool_key_dict["tick_spacing"],
                    pool_key_dict["hooks"],
                )
                in_curr = Web3.to_checksum_address(_get_asset_address(hop.asset_in))
                out_curr = Web3.to_checksum_address(_get_asset_address(hop.asset_out))

                v4_builder = builder.v4_swap()
                v4_builder.settle(
                    currency=in_curr,
                    amount=plan.amount_in.atoms if is_first else ROUTER_BALANCE,
                    payer_is_user=is_first,
                )
                v4_builder.swap_exact_in_single(
                    pool_key=v4_pk,
                    zero_for_one=zero_for_one,
                    amount_in=cast(Wei, plan.amount_in.atoms if is_first else 0),
                    amount_out_min=cast(Wei, hop_min_out),
                )
                v4_builder.take(
                    currency=out_curr,
                    recipient=Web3.to_checksum_address(hop_recipient_addr),
                    amount=0,
                )
                v4_builder.build_v4_swap()

    raw_calldata = builder.build(deadline=resolved_deadline)
    calldata_hex: str = raw_calldata if raw_calldata.startswith("0x") else f"0x{raw_calldata}"

    # Extract commands bytes and validate security invariants
    types = ["bytes", "bytes[]", "uint256"]
    decoded_head = abi_decode(types, bytes.fromhex(calldata_hex[10:]))
    commands_bytes = cast(bytes, decoded_head[0])
    validate_commands_security(commands_bytes)

    calldata_sha256 = compute_calldata_sha256(calldata_hex)

    verification_result: DecodedVerificationResult | None = None
    if verify:
        verification_result = verify_dual_decoded_calldata(
            calldata_hex=calldata_hex,
            plan=plan,
            recipient=final_recipient,
            expected_deadline=resolved_deadline,
            codec=resolved_codec,
        )

    return EncodedCalldata(
        plan_id=plan.plan_id,
        route_id=plan.route_id,
        chain_id=plan.chain_id,
        router_address=plan.target_router,
        calldata_hex=calldata_hex,
        calldata_sha256=calldata_sha256,
        commands_hex="0x" + commands_bytes.hex(),
        commands_count=len(commands_bytes),
        deadline=resolved_deadline,
        amount_in=plan.amount_in.atoms,
        min_amount_out=plan.min_amount_out.atoms,
        status="ENCODED",
        can_atomic_execute=False,
        encoding_mode=str(encoding_mode),
        dual_verified=verification_result.is_valid if verification_result else False,
        verification_result=verification_result,
    )
