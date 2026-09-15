"""Permit2 offline read-only readiness and allowance verification for Arc.

Enforces pre-flight amount, expiration, and spender binding checks:
- Strict read-only JSON-RPC abstraction (no mutating calls, no network client creation)
- Explicit owner, token, router/spender, permit2, and block context (no Robinhood defaults)
- Exact EVM ABI encoding/decoding: allowance(address,address,address) -> (uint160, uint48, uint48)
- Rigorous fail-closed validation on return length (96 bytes), address format, and value ranges
- Strict block-anchored timestamp evaluation: amount >= requested and expiration > timestamp
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from eth_abi import decode, encode
from web3 import Web3

from arc_readiness.errors import ArcValidationError
from arc_readiness.fixed_block import BlockAnchor
from arc_readiness.models import (
    validate_address,
    validate_bool,
    validate_non_negative_int,
)
from arc_readiness.rpc_readonly import ReadOnlyRpcTransport

# Canonical Permit2 deployment on EVM networks
CANONICAL_PERMIT2: str = "0x000000000022d473030f116ddee9f6b43ac78ba3"

# Selectors (4 bytes hex without 0x)
PERMIT2_ALLOWANCE_SELECTOR: str = "927da105"
ERC20_ALLOWANCE_SELECTOR: str = "dd62ed3e"

# EVM ABI integer bounds
UINT48_MAX: int = (1 << 48) - 1
UINT160_MAX: int = (1 << 160) - 1
UINT256_MAX: int = (1 << 256) - 1


@dataclass(frozen=True, slots=True)
class Permit2ReadinessResult:
    """Immutable result structure for Permit2 readiness verification."""

    allowed: bool
    amount: int
    expiration: int
    nonce: int
    block_number: int
    reason: str | None = None
    owner: str | None = None
    token: str | None = None
    spender: str | None = None
    permit2_address: str | None = None
    block_timestamp: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed", validate_bool(self.allowed, "allowed"))
        object.__setattr__(self, "amount", validate_non_negative_int(self.amount, "amount"))
        object.__setattr__(
            self, "expiration", validate_non_negative_int(self.expiration, "expiration")
        )
        object.__setattr__(self, "nonce", validate_non_negative_int(self.nonce, "nonce"))
        object.__setattr__(
            self, "block_number", validate_non_negative_int(self.block_number, "block_number")
        )
        if self.reason is not None and not isinstance(self.reason, str):
            raise ArcValidationError(
                f"reason must be str or None, got {type(self.reason).__name__}"
            )
        if self.amount > UINT160_MAX:
            raise ArcValidationError(f"amount exceeds uint160 max: {self.amount}")
        if self.expiration > UINT48_MAX:
            raise ArcValidationError(f"expiration exceeds uint48 max: {self.expiration}")
        if self.nonce > UINT48_MAX:
            raise ArcValidationError(f"nonce exceeds uint48 max: {self.nonce}")

    @property
    def has_allowance(self) -> bool:
        """Compatibility property for legacy verification interfaces."""
        return self.allowed

    def __getitem__(self, key: str) -> Any:
        """Support mapping / dict subscript access."""
        if key == "has_allowance":
            return self.allowed
        if hasattr(self, key):
            return getattr(self, key)
        raise KeyError(key)

    def to_dict(self) -> dict[str, Any]:
        """Convert readiness result to a standard dictionary representation."""
        return {
            "allowed": self.allowed,
            "has_allowance": self.allowed,
            "amount": self.amount,
            "expiration": self.expiration,
            "nonce": self.nonce,
            "block_number": self.block_number,
            "reason": self.reason,
            "owner": self.owner,
            "token": self.token,
            "spender": self.spender,
            "permit2_address": self.permit2_address,
            "block_timestamp": self.block_timestamp,
        }


# Type alias aligning with Arc permission status domain models
Permit2AllowanceStatus = Permit2ReadinessResult


def _normalize_block_context(
    block_context: BlockAnchor | Mapping[str, Any] | int,
    explicit_timestamp: int | None = None,
) -> tuple[int, int, str]:
    """Extract and validate (block_number, block_timestamp, hex_block) strictly.

    Disallows silent fallbacks to 'latest', unanchored host clocks, or negative numbers.
    """
    if isinstance(block_context, BlockAnchor):
        b_num = block_context.block_number
        b_ts = explicit_timestamp if explicit_timestamp is not None else block_context.timestamp
    elif isinstance(block_context, Mapping):
        raw_num = block_context.get("number")
        if raw_num is None:
            raw_num = block_context.get("block_number")
        if raw_num is None:
            raise ArcValidationError("block_context mapping missing 'number' or 'block_number'")
        if isinstance(raw_num, str):
            if raw_num.strip().lower() == "latest":
                raise ArcValidationError(
                    "Silent fallback or 'latest' block tag is strictly forbidden in Permit2 verification"
                )
            b_num = int(raw_num, 16) if raw_num.startswith(("0x", "0X")) else int(raw_num)
        else:
            b_num = int(raw_num)

        raw_ts = (
            explicit_timestamp
            if explicit_timestamp is not None
            else block_context.get("timestamp", block_context.get("block_timestamp"))
        )
        if raw_ts is None:
            raise ArcValidationError("block_context mapping missing 'timestamp'")
        if isinstance(raw_ts, str):
            b_ts = int(raw_ts, 16) if raw_ts.startswith(("0x", "0X")) else int(raw_ts)
        else:
            b_ts = int(raw_ts)
    elif isinstance(block_context, int):
        b_num = block_context
        if explicit_timestamp is None:
            raise ArcValidationError(
                "Explicit block_timestamp required when block_context is an integer"
            )
        b_ts = explicit_timestamp
    else:
        raise ArcValidationError(
            f"Unsupported block_context type: {type(block_context).__name__}"
        )

    if b_num < 0:
        raise ArcValidationError(f"block_number cannot be negative: {b_num}")
    if b_ts < 0:
        raise ArcValidationError(f"block_timestamp cannot be negative: {b_ts}")

    hex_block = hex(b_num)
    return b_num, b_ts, hex_block


def encode_permit2_allowance_calldata(owner: str, token: str, spender: str) -> str:
    """Encode calldata for Permit2 allowance(address,address,address)."""
    norm_owner = validate_address(owner, "owner")
    norm_token = validate_address(token, "token")
    norm_spender = validate_address(spender, "spender")

    encoded_params = encode(
        ["address", "address", "address"],
        [norm_owner, norm_token, norm_spender],
    ).hex()
    return "0x" + PERMIT2_ALLOWANCE_SELECTOR + encoded_params


def decode_permit2_allowance_response(data: str | bytes) -> tuple[int, int, int]:
    """Decode raw return bytes from Permit2 allowance call.

    Expects exactly 96 bytes (uint160 amount, uint48 expiration, uint48 nonce).
    Fails closed on malformed length, invalid hex, empty value, or out-of-range values.
    """
    if data is None:
        raise ArcValidationError("Permit2 allowance response is None; fail-closed")
    if isinstance(data, str):
        raw_hex = data.strip()
        if raw_hex.startswith(("0x", "0X")):
            raw_hex = raw_hex[2:]
        if not raw_hex:
            raise ArcValidationError(
                "Empty return data from Permit2 allowance call; fail-closed"
            )
        try:
            raw_bytes = bytes.fromhex(raw_hex)
        except ValueError as e:
            raise ArcValidationError(f"Invalid hex string in Permit2 response: {e}") from e
    elif isinstance(data, (bytes, bytearray)):
        raw_bytes = bytes(data)
        if not raw_bytes:
            raise ArcValidationError(
                "Empty return bytes from Permit2 allowance call; fail-closed"
            )
    else:
        raise ArcValidationError(
            f"Unexpected response type for Permit2 allowance: {type(data).__name__}"
        )

    if len(raw_bytes) != 96:
        raise ArcValidationError(
            f"Malformed Permit2 allowance return length: expected 96 bytes, got {len(raw_bytes)}"
        )

    try:
        amount, expiration, nonce = decode(["uint160", "uint48", "uint48"], raw_bytes)
    except Exception as e:
        raise ArcValidationError(f"Failed to decode Permit2 allowance ABI return: {e}") from e

    if not (0 <= amount <= UINT160_MAX):
        raise ArcValidationError(f"Permit2 amount out of uint160 bounds: {amount}")
    if not (0 <= expiration <= UINT48_MAX):
        raise ArcValidationError(f"Permit2 expiration out of uint48 bounds: {expiration}")
    if not (0 <= nonce <= UINT48_MAX):
        raise ArcValidationError(f"Permit2 nonce out of uint48 bounds: {nonce}")

    return int(amount), int(expiration), int(nonce)


def encode_erc20_allowance_calldata(owner: str, spender: str) -> str:
    """Encode calldata for ERC20 allowance(address,address)."""
    norm_owner = validate_address(owner, "owner")
    norm_spender = validate_address(spender, "spender")

    encoded_params = encode(["address", "address"], [norm_owner, norm_spender]).hex()
    return "0x" + ERC20_ALLOWANCE_SELECTOR + encoded_params


def decode_erc20_allowance_response(data: str | bytes) -> int:
    """Decode raw return bytes from ERC20 allowance call."""
    if data is None:
        raise ArcValidationError("ERC20 allowance response is None; fail-closed")
    if isinstance(data, str):
        raw_hex = data.strip()
        if raw_hex.startswith(("0x", "0X")):
            raw_hex = raw_hex[2:]
        if not raw_hex:
            raise ArcValidationError("Empty return data from ERC20 allowance call; fail-closed")
        try:
            raw_bytes = bytes.fromhex(raw_hex)
        except ValueError as e:
            raise ArcValidationError(f"Invalid hex string in ERC20 response: {e}") from e
    elif isinstance(data, (bytes, bytearray)):
        raw_bytes = bytes(data)
        if not raw_bytes:
            raise ArcValidationError("Empty return bytes from ERC20 allowance call; fail-closed")
    else:
        raise ArcValidationError(
            f"Unexpected response type for ERC20 allowance: {type(data).__name__}"
        )

    if len(raw_bytes) != 32:
        raise ArcValidationError(
            f"Malformed ERC20 allowance return length: expected 32 bytes, got {len(raw_bytes)}"
        )

    try:
        allowance = decode(["uint256"], raw_bytes)[0]
    except Exception as e:
        raise ArcValidationError(f"Failed to decode ERC20 allowance ABI return: {e}") from e

    if not (0 <= allowance <= UINT256_MAX):
        raise ArcValidationError(f"ERC20 allowance out of uint256 bounds: {allowance}")

    return int(allowance)


def check_permit2_allowance(
    transport: ReadOnlyRpcTransport | Callable[[str, Sequence[Any]], Any],
    owner: str,
    token: str,
    spender: str,
    permit2_address: str,
    requested_amount: int,
    block_context: BlockAnchor | Mapping[str, Any] | int,
    block_timestamp: int | None = None,
    *,
    check_erc20: bool = False,
    check_code: bool = False,
    expected_digests: Mapping[str, str] | None = None,
) -> Permit2ReadinessResult:
    """Verify Permit2 allowance offline with explicit binding and fail-closed validation.

    Invariants:
    1. owner, token, spender, and permit2_address must be explicitly passed (no Robinhood defaults).
    2. requested_amount must be non-negative integer.
    3. Block context must provide deterministic block number and timestamp (no latest, no host clock).
    4. Execution is performed strictly via ReadOnlyRpcTransport allowlisted methods.
    5. Evaluation condition: amount >= requested_amount and expiration > block_timestamp.
    """
    # 1. Input parameter validation
    norm_owner = validate_address(owner, "owner")
    norm_token = validate_address(token, "token")
    norm_spender = validate_address(spender, "spender")
    norm_permit2 = validate_address(permit2_address, "permit2_address")
    req_amount = validate_non_negative_int(requested_amount, "requested_amount")

    # An explicitly supplied digest policy must never be silently bypassed.
    normalized_digests: dict[str, str] | None = None
    if expected_digests is not None:
        if not isinstance(expected_digests, Mapping):
            raise ArcValidationError("expected_digests must be an address-to-hash mapping")
        if not check_code:
            raise ArcValidationError("expected_digests requires check_code=True")
        normalized_digests = {}
        for address, digest in expected_digests.items():
            normalized_address = validate_address(address, "expected_digests address")
            if (
                not isinstance(digest, str)
                or len(digest) != 66
                or not digest.startswith(("0x", "0X"))
                or any(char not in "0123456789abcdefABCDEF" for char in digest[2:])
            ):
                raise ArcValidationError("Expected bytecode digest must be a 32-byte hex string")
            normalized_digest = "0x" + digest[2:].lower()
            previous = normalized_digests.get(normalized_address)
            if previous is not None and previous != normalized_digest:
                raise ArcValidationError("Conflicting bytecode digests for the same address")
            normalized_digests[normalized_address] = normalized_digest
        if not {norm_spender, norm_permit2}.issubset(normalized_digests):
            raise ArcValidationError("Expected bytecode digests must cover spender and permit2")

    # 2. Block context normalization
    block_num, block_ts, hex_block = _normalize_block_context(block_context, block_timestamp)

    # 3. Transport wrapping ensuring read-only RPC guards
    if isinstance(transport, ReadOnlyRpcTransport):
        rpc_client = transport
    elif callable(transport):
        rpc_client = ReadOnlyRpcTransport("offline://mock-permit2", handler=transport)
    else:
        raise ArcValidationError(
            f"Invalid transport: expected ReadOnlyRpcTransport or callable, got {type(transport).__name__}"
        )

    # 4. Optional contract bytecode deployment check
    if check_code:
        for addr, name in [(norm_spender, "spender"), (norm_permit2, "permit2")]:
            code = rpc_client.request("eth_getCode", [addr, hex_block])
            if not code or code in ("0x", "0x0"):
                raise ArcValidationError(
                    f"No deployed bytecode found at {name} address {addr} at block {hex_block}"
                )
            if normalized_digests is not None:
                raw_code = code[2:] if isinstance(code, str) and code.startswith(("0x", "0X")) else code
                code_bytes = bytes.fromhex(raw_code) if isinstance(raw_code, str) else bytes(raw_code)
                code_hash = "0x" + Web3.keccak(code_bytes).hex()
                expected_hash = normalized_digests[addr]
                if code_hash.lower() != expected_hash:
                    raise ArcValidationError(
                        f"Bytecode hash mismatch for {name} ({addr}): expected {expected_hash}, got {code_hash}"
                    )

    # 5. Optional ERC-20 underlying allowance check
    erc20_allowance: int | None = None
    if check_erc20:
        erc20_calldata = encode_erc20_allowance_calldata(norm_owner, norm_permit2)
        erc20_resp = rpc_client.request(
            "eth_call",
            [{"to": norm_token, "data": erc20_calldata}, hex_block],
        )
        erc20_allowance = decode_erc20_allowance_response(erc20_resp)

    # 6. Permit2 allowance call
    permit2_calldata = encode_permit2_allowance_calldata(norm_owner, norm_token, norm_spender)
    permit2_resp = rpc_client.request(
        "eth_call",
        [{"to": norm_permit2, "data": permit2_calldata}, hex_block],
    )
    p2_amount, p2_exp, p2_nonce = decode_permit2_allowance_response(permit2_resp)

    # 7. Semantic condition evaluation
    # Required: amount >= requested_amount and expiration > block_timestamp
    has_amount = p2_amount >= req_amount
    is_not_expired = p2_exp > block_ts
    erc20_ok = (erc20_allowance is None) or (erc20_allowance >= req_amount)

    reason: str | None = None
    if not erc20_ok:
        reason = "INSUFFICIENT_ERC20_ALLOWANCE"
        allowed = False
    elif not has_amount and not is_not_expired:
        reason = "INSUFFICIENT_ALLOWANCE_AND_EXPIRED"
        allowed = False
    elif not has_amount:
        reason = "INSUFFICIENT_ALLOWANCE"
        allowed = False
    elif not is_not_expired:
        reason = "EXPIRED_ALLOWANCE"
        allowed = False
    else:
        allowed = True

    return Permit2ReadinessResult(
        allowed=allowed,
        amount=p2_amount,
        expiration=p2_exp,
        nonce=p2_nonce,
        block_number=block_num,
        reason=reason,
        owner=norm_owner,
        token=norm_token,
        spender=norm_spender,
        permit2_address=norm_permit2,
        block_timestamp=block_ts,
    )


def verify_permit2_readiness(
    transport: ReadOnlyRpcTransport | Callable[[str, Sequence[Any]], Any],
    owner: str,
    token: str,
    spender: str,
    permit2_address: str,
    requested_amount: int,
    block_context: BlockAnchor | Mapping[str, Any] | int,
    block_timestamp: int | None = None,
    *,
    check_erc20: bool = True,
    check_code: bool = True,
    expected_digests: Mapping[str, str] | None = None,
) -> Permit2ReadinessResult:
    """Comprehensive Permit2 readiness verification verifying code, ERC20 allowance, and Permit2 allowance."""
    return check_permit2_allowance(
        transport=transport,
        owner=owner,
        token=token,
        spender=spender,
        permit2_address=permit2_address,
        requested_amount=requested_amount,
        block_context=block_context,
        block_timestamp=block_timestamp,
        check_erc20=check_erc20,
        check_code=check_code,
        expected_digests=expected_digests,
    )
