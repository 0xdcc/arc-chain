"""Universal Router Calldata Encoder for Arc Chain (T25)

Adapts W5 Universal Router calldata assembly for Arc (5042 / 5042002):
- Guarantees Bit 7 (0x80 / allow_revert) is strictly UNSET across all commands
- Binds intermediate outputs to Router and final output to MSG_SENDER
- Binds min_amount_out rigidly to plan.min_amount_out on final hop
- Uses execute(bytes,bytes[],uint256) selector 0x3593564c
- Locks can_atomic_execute strictly to False
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from eth_abi.abi import encode as abi_encode

from atomic_execution.arc_planning import ArcExecutionPlan

COMMAND_V3_SWAP_EXACT_IN: int = 0x00
FLAG_ALLOW_REVERT: int = 0x80
EXECUTE_SELECTOR_WITH_DEADLINE: str = "0x3593564c"

MSG_SENDER: str = "0x0000000000000000000000000000000000000001"
ADDRESS_THIS: str = "0x0000000000000000000000000000000000000002"
ROUTER_BALANCE: int = 1 << 255


class ArcEncodingError(ValueError):
    """Raised for encoding invariant breaches or malformed calldata."""


@dataclass(frozen=True, slots=True)
class EncodedArcCalldata:
    """Audit-ready encoded calldata payload bound to an ArcExecutionPlan."""

    plan_id: str
    target_router: str
    calldata_hex: str
    calldata_hash: str
    commands_count: int
    allow_revert_flags_unset: bool
    can_atomic_execute: bool = False
    status: str = "ENCODED"


def encode_arc_execution_plan(plan: ArcExecutionPlan, deadline_s: int = 180) -> EncodedArcCalldata:
    """Encode an ArcExecutionPlan into deterministic Universal Router calldata.

    Invariants enforced:
    1. Bit 7 (allow_revert) is 0 for every command byte.
    2. Intermediate hops pay ROUTER, final hop pays MSG_SENDER.
    3. Final hop specifies plan.min_amount_out.
    4. can_atomic_execute is False.
    """
    commands_bytearray = bytearray()
    inputs_list: list[bytes] = []

    hops = plan.route_ref.hops
    num_hops = len(hops)

    for i, hop in enumerate(hops):
        is_first = (i == 0)
        is_last = (i == num_hops - 1)

        # Build command: V3 swap exact input without 0x80 flag
        cmd_byte = COMMAND_V3_SWAP_EXACT_IN
        if (cmd_byte & FLAG_ALLOW_REVERT) != 0:
            raise ArcEncodingError("allow_revert flag (0x80) cannot be set")
        commands_bytearray.append(cmd_byte)

        # Recipient: intermediate -> ADDRESS_THIS, final -> MSG_SENDER
        recipient = MSG_SENDER if is_last else ADDRESS_THIS

        # Amount in: first hop uses exact amount_in, intermediate hops use ROUTER_BALANCE flag
        amount_in_flag = plan.amount_in.atoms if is_first else ROUTER_BALANCE

        # Amount out minimum: final hop enforces plan.min_amount_out, intermediate uses 1 atom
        amount_out_min = plan.min_amount_out.atoms if is_last else 1

        # Encode V3 path: (tokenIn, fee_pips, tokenOut)
        token_in = hop.asset_in.token_key.address if hop.asset_in.token_key else hop.asset_in.native_identifier or ""
        token_out = hop.asset_out.token_key.address if hop.asset_out.token_key else hop.asset_out.native_identifier or ""
        fee_pips = hop.pool_descriptor.fee_model.raw_value if (hop.pool_descriptor and hop.pool_descriptor.fee_model.raw_value) else 500

        # Encode V3 packed path: 20 bytes + 3 bytes (fee) + 20 bytes
        in_bytes = bytes.fromhex(token_in[2:])
        out_bytes = bytes.fromhex(token_out[2:])
        fee_bytes = fee_pips.to_bytes(3, byteorder="big")
        path_bytes = in_bytes + fee_bytes + out_bytes

        # Encode V3_SWAP_EXACT_IN param tuple: (address recipient, uint256 amountIn, uint256 amountOutMin, bytes path, bool payerIsUser)
        param_bytes = abi_encode(
            ["address", "uint256", "uint256", "bytes", "bool"],
            [recipient, amount_in_flag, amount_out_min, path_bytes, is_first],
        )
        inputs_list.append(param_bytes)

    # Encode full execute call: execute(bytes commands, bytes[] inputs, uint256 deadline)
    commands_bytes = bytes(commands_bytearray)
    selector_bytes = bytes.fromhex(EXECUTE_SELECTOR_WITH_DEADLINE[2:])
    encoded_args = abi_encode(
        ["bytes", "bytes[]", "uint256"],
        [commands_bytes, inputs_list, deadline_s],
    )
    full_calldata = selector_bytes + encoded_args
    calldata_hex = "0x" + full_calldata.hex()
    calldata_hash = hashlib.sha256(full_calldata).hexdigest()

    return EncodedArcCalldata(
        plan_id=plan.plan_id,
        target_router=plan.target_router,
        calldata_hex=calldata_hex,
        calldata_hash=calldata_hash,
        commands_count=num_hops,
        allow_revert_flags_unset=True,
        can_atomic_execute=False,
        status="ENCODED",
    )
