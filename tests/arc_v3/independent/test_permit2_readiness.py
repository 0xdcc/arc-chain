"""Independent verification test suite for Permit2 offline read-only readiness.

Validates:
1. Exact correspondence to tests/test_remaining_protocols.py 3-case allowance logic:
   - Case 1: delegated=0, expiration=1000 -> allowed=False (amount < requested)
   - Case 2: delegated=100, expiration=99 -> allowed=False (expiration <= timestamp)
   - Case 3: delegated=100, expiration=101 -> allowed=True (amount >= requested and expiration > timestamp)
2. Rigorous boundary verification on expiration == timestamp (must be rejected as expired).
3. Exact ABI encoding/decoding & fail-closed 96-byte length validation.
4. Integer range validation (uint160 amount, uint48 expiration, uint48 nonce).
5. Address format validation (42-char hex format with 0x prefix).
6. Block context validation (forbidding "latest", rejecting negative block numbers / timestamps).
7. Read-only RPC boundaries (preventing mutating RPC methods).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from eth_abi import decode, encode
from web3 import Web3

from arc_readiness.errors import ArcValidationError
from arc_readiness.fixed_block import BlockAnchor
from arc_readiness.permit2 import (
    CANONICAL_PERMIT2,
    ERC20_ALLOWANCE_SELECTOR,
    PERMIT2_ALLOWANCE_SELECTOR,
    Permit2ReadinessResult,
    check_permit2_allowance,
    decode_erc20_allowance_response,
    decode_permit2_allowance_response,
    encode_erc20_allowance_calldata,
    encode_permit2_allowance_calldata,
    verify_permit2_readiness,
)
from arc_readiness.rpc_readonly import ReadOnlyRpcTransport

# Standard test fixtures
OWNER: str = "0x" + "11" * 20
TOKEN: str = "0x" + "22" * 20
ROUTER: str = "0x" + "33" * 20
PERMIT2_ADDR: str = CANONICAL_PERMIT2


def _make_permit2_return_data(amount: int, expiration: int, nonce: int = 0) -> str:
    """Helper to encode a 96-byte Permit2 allowance return value."""
    return "0x" + encode(["uint160", "uint48", "uint48"], [amount, expiration, nonce]).hex()


def _make_erc20_return_data(amount: int) -> str:
    """Helper to encode a 32-byte ERC20 allowance return value."""
    return "0x" + encode(["uint256"], [amount]).hex()


class TestPermit2OriginalThreeCases:
    """Validate strict compatibility and semantics with test_remaining_protocols.py."""

    @pytest.mark.parametrize(
        "delegated,expiration,expected_allowed,expected_reason",
        [
            (0, 1000, False, "INSUFFICIENT_ALLOWANCE"),
            (100, 99, False, "EXPIRED_ALLOWANCE"),
            (100, 101, True, None),
        ],
    )
    def test_actual_router_permit2_amount_and_expiration_compatibility(
        self,
        delegated: int,
        expiration: int,
        expected_allowed: bool,
        expected_reason: str | None,
    ) -> None:
        """Faithfully preserve the exact 3-case behavior from test_remaining_protocols.py."""
        requests: list[tuple[str, Sequence[Any]]] = []

        def mock_rpc(method: str, params: Sequence[Any]) -> Any:
            requests.append((method, params))
            if method == "eth_getCode":
                return "0x01"
            assert method == "eth_call"
            assert params[1] == "0xa"
            data = params[0]["data"]
            if data[2:10] == ERC20_ALLOWANCE_SELECTOR:
                return _make_erc20_return_data(1000)
            assert data[2:10] == PERMIT2_ALLOWANCE_SELECTOR
            owner, asset, spender = decode(["address"] * 3, bytes.fromhex(data[10:]))
            assert (owner, asset, spender) == (OWNER.lower(), TOKEN.lower(), ROUTER.lower())
            return _make_permit2_return_data(delegated, expiration, 0)

        result = check_permit2_allowance(
            transport=mock_rpc,
            owner=OWNER,
            token=TOKEN,
            spender=ROUTER,
            permit2_address=PERMIT2_ADDR,
            requested_amount=100,
            block_context={"number": "0xa", "hash": "0x" + "44" * 32, "timestamp": "0x64"},
            check_erc20=True,
            check_code=True,
        )

        assert result.allowed is expected_allowed
        assert result.has_allowance is expected_allowed
        assert result["has_allowance"] is expected_allowed
        assert result["allowed"] is expected_allowed
        assert result.amount == delegated
        assert result.expiration == expiration
        assert result.reason == expected_reason
        assert {m for m, _ in requests} == {"eth_call", "eth_getCode"}

    def test_verify_permit2_readiness_wrapper_compatibility(self) -> None:
        """Ensure verify_permit2_readiness functions identically as high-level entrypoint."""
        def mock_rpc(method: str, params: Sequence[Any]) -> Any:
            if method == "eth_getCode":
                return "0x01"
            if method == "eth_call":
                data = params[0]["data"]
                if data[2:10] == ERC20_ALLOWANCE_SELECTOR:
                    return _make_erc20_return_data(1000)
                return _make_permit2_return_data(100, 101, 7)
            raise AssertionError(f"Unexpected method: {method}")

        res = verify_permit2_readiness(
            transport=mock_rpc,
            owner=OWNER,
            token=TOKEN,
            spender=ROUTER,
            permit2_address=PERMIT2_ADDR,
            requested_amount=100,
            block_context={"number": "0xa", "timestamp": "0x64"},
        )
        assert res.allowed is True
        assert res.amount == 100
        assert res.expiration == 101
        assert res.nonce == 7
        assert res.owner == OWNER.lower()
        assert res.token == TOKEN.lower()
        assert res.spender == ROUTER.lower()
        assert res.permit2_address == PERMIT2_ADDR.lower()


class TestPermit2ExpirationBoundary:
    """Rigorous boundary verification on expiration timestamps."""

    def test_expiry_strictly_greater_than_timestamp_is_allowed(self) -> None:
        """Expiration exactly 1 second after timestamp is valid."""
        def mock_rpc(method: str, params: Sequence[Any]) -> Any:
            return _make_permit2_return_data(100, 101, 0)

        res = check_permit2_allowance(
            transport=mock_rpc,
            owner=OWNER,
            token=TOKEN,
            spender=ROUTER,
            permit2_address=PERMIT2_ADDR,
            requested_amount=100,
            block_context={"number": 10, "timestamp": 100},
        )
        assert res.allowed is True
        assert res.reason is None

    def test_expiry_equals_timestamp_is_strictly_rejected(self) -> None:
        """Expiration exactly equal to timestamp must be rejected as EXPIRED_ALLOWANCE."""
        def mock_rpc(method: str, params: Sequence[Any]) -> Any:
            return _make_permit2_return_data(100, 100, 0)

        res = check_permit2_allowance(
            transport=mock_rpc,
            owner=OWNER,
            token=TOKEN,
            spender=ROUTER,
            permit2_address=PERMIT2_ADDR,
            requested_amount=100,
            block_context={"number": 10, "timestamp": 100},
        )
        assert res.allowed is False
        assert res.reason == "EXPIRED_ALLOWANCE"

    def test_expiry_less_than_timestamp_is_rejected(self) -> None:
        """Expiration 1 second before timestamp is rejected as EXPIRED_ALLOWANCE."""
        def mock_rpc(method: str, params: Sequence[Any]) -> Any:
            return _make_permit2_return_data(100, 99, 0)

        res = check_permit2_allowance(
            transport=mock_rpc,
            owner=OWNER,
            token=TOKEN,
            spender=ROUTER,
            permit2_address=PERMIT2_ADDR,
            requested_amount=100,
            block_context={"number": 10, "timestamp": 100},
        )
        assert res.allowed is False
        assert res.reason == "EXPIRED_ALLOWANCE"


class TestPermit2AmountBoundary:
    """Boundary verification on amount comparisons."""

    def test_amount_equals_requested_is_allowed(self) -> None:
        """Amount exactly equal to requested amount is permitted."""
        def mock_rpc(method: str, params: Sequence[Any]) -> Any:
            return _make_permit2_return_data(100, 200, 0)

        res = check_permit2_allowance(
            transport=mock_rpc,
            owner=OWNER,
            token=TOKEN,
            spender=ROUTER,
            permit2_address=PERMIT2_ADDR,
            requested_amount=100,
            block_context={"number": 10, "timestamp": 100},
        )
        assert res.allowed is True

    def test_amount_one_atom_below_requested_is_rejected(self) -> None:
        """Amount 1 atom less than requested is rejected as INSUFFICIENT_ALLOWANCE."""
        def mock_rpc(method: str, params: Sequence[Any]) -> Any:
            return _make_permit2_return_data(99, 200, 0)

        res = check_permit2_allowance(
            transport=mock_rpc,
            owner=OWNER,
            token=TOKEN,
            spender=ROUTER,
            permit2_address=PERMIT2_ADDR,
            requested_amount=100,
            block_context={"number": 10, "timestamp": 100},
        )
        assert res.allowed is False
        assert res.reason == "INSUFFICIENT_ALLOWANCE"

    def test_insufficient_amount_and_expired_sets_combined_reason(self) -> None:
        """Both amount insufficient and expired sets combined failure reason."""
        def mock_rpc(method: str, params: Sequence[Any]) -> Any:
            return _make_permit2_return_data(50, 50, 0)

        res = check_permit2_allowance(
            transport=mock_rpc,
            owner=OWNER,
            token=TOKEN,
            spender=ROUTER,
            permit2_address=PERMIT2_ADDR,
            requested_amount=100,
            block_context={"number": 10, "timestamp": 100},
        )
        assert res.allowed is False
        assert res.reason == "INSUFFICIENT_ALLOWANCE_AND_EXPIRED"


class TestPermit2ReturnLengthAndDecoding:
    """Fail-closed validation on return length (96 bytes) and ABI encoding."""

    @pytest.mark.parametrize(
        ("invalid_length_bytes", "expected_match"),
        [
            (b"", "Empty return bytes from Permit2 allowance call; fail-closed"),
            (b"\x00" * 32, "Malformed Permit2 allowance return length"),
            (b"\x00" * 64, "Malformed Permit2 allowance return length"),
            (b"\x00" * 95, "Malformed Permit2 allowance return length"),
            (b"\x00" * 97, "Malformed Permit2 allowance return length"),
            (b"\x00" * 128, "Malformed Permit2 allowance return length"),
        ],
    )
    def test_malformed_return_length_raises_arc_validation_error(
        self, invalid_length_bytes: bytes, expected_match: str
    ) -> None:
        """Permit2 decoding strictly requires exactly 96 bytes, while empty bytes fails closed."""
        with pytest.raises(ArcValidationError, match=expected_match):
            decode_permit2_allowance_response(invalid_length_bytes)

    def test_none_or_empty_response_raises_arc_validation_error(self) -> None:
        """None or empty hex string raises ArcValidationError."""
        with pytest.raises(ArcValidationError, match="Permit2 allowance response is None"):
            decode_permit2_allowance_response(None)  # type: ignore[arg-type]

        with pytest.raises(ArcValidationError, match="Empty return data"):
            decode_permit2_allowance_response("")

    def test_invalid_hex_string_raises_arc_validation_error(self) -> None:
        """Invalid hex payload raises ArcValidationError."""
        with pytest.raises(ArcValidationError, match="Invalid hex string"):
            decode_permit2_allowance_response("0xZZZZ")

    def test_encode_calldata_structure(self) -> None:
        """Permit2 calldata must match selector 0x927da105 and 3 encoded addresses."""
        calldata = encode_permit2_allowance_calldata(OWNER, TOKEN, ROUTER)
        assert calldata.startswith("0x" + PERMIT2_ALLOWANCE_SELECTOR)
        assert len(calldata) == 2 + 8 + 64 * 3
        decoded_owner, decoded_token, decoded_spender = decode(
            ["address", "address", "address"], bytes.fromhex(calldata[10:])
        )
        assert (decoded_owner, decoded_token, decoded_spender) == (
            OWNER.lower(),
            TOKEN.lower(),
            ROUTER.lower(),
        )


class TestPermit2IntegerRangeValidation:
    """Boundary enforcement on integer bounds: uint160 amount, uint48 expiration, uint48 nonce."""

    def test_requested_amount_negative_is_rejected(self) -> None:
        """Negative requested_amount must raise ArcValidationError."""
        def mock_rpc(method: str, params: Sequence[Any]) -> Any:
            return _make_permit2_return_data(100, 200, 0)

        with pytest.raises(ArcValidationError, match="must be a non-negative integer"):
            check_permit2_allowance(
                transport=mock_rpc,
                owner=OWNER,
                token=TOKEN,
                spender=ROUTER,
                permit2_address=PERMIT2_ADDR,
                requested_amount=-1,
                block_context={"number": 10, "timestamp": 100},
            )

    def test_out_of_bounds_uint_in_readiness_result_raises(self) -> None:
        """Permit2ReadinessResult rejects values beyond uint160/uint48 bounds."""
        uint160_overflow = (1 << 160)
        with pytest.raises(ArcValidationError, match="amount exceeds uint160 max"):
            Permit2ReadinessResult(
                allowed=True,
                amount=uint160_overflow,
                expiration=100,
                nonce=0,
                block_number=1,
            )

        uint48_overflow = (1 << 48)
        with pytest.raises(ArcValidationError, match="expiration exceeds uint48 max"):
            Permit2ReadinessResult(
                allowed=True,
                amount=100,
                expiration=uint48_overflow,
                nonce=0,
                block_number=1,
            )

        with pytest.raises(ArcValidationError, match="nonce exceeds uint48 max"):
            Permit2ReadinessResult(
                allowed=True,
                amount=100,
                expiration=100,
                nonce=uint48_overflow,
                block_number=1,
            )


class TestPermit2AddressFormatValidation:
    """Format and syntax validation for 42-character hex addresses."""

    @pytest.mark.parametrize(
        "bad_addr",
        [
            "not_an_address",
            "0x1234",
            "0x" + "11" * 19,  # 40 chars total (20-byte missing 1 byte)
            "0x" + "11" * 21,  # 44 chars total
            12345,
            None,
        ],
    )
    def test_invalid_address_format_raises_arc_validation_error(self, bad_addr: Any) -> None:
        """Non-42 char or non-string address must raise ArcValidationError."""
        def mock_rpc(method: str, params: Sequence[Any]) -> Any:
            return _make_permit2_return_data(100, 200, 0)

        with pytest.raises(ArcValidationError):
            check_permit2_allowance(
                transport=mock_rpc,
                owner=bad_addr,
                token=TOKEN,
                spender=ROUTER,
                permit2_address=PERMIT2_ADDR,
                requested_amount=100,
                block_context={"number": 10, "timestamp": 100},
            )


class TestPermit2BlockContextValidation:
    """Verification of block context handling and rejection of unanchored 'latest'."""

    def test_latest_block_tag_is_strictly_forbidden(self) -> None:
        """Silent fallback to 'latest' is strictly forbidden."""
        def mock_rpc(method: str, params: Sequence[Any]) -> Any:
            return _make_permit2_return_data(100, 200, 0)

        with pytest.raises(ArcValidationError, match="Silent fallback or 'latest' block tag is strictly forbidden"):
            check_permit2_allowance(
                transport=mock_rpc,
                owner=OWNER,
                token=TOKEN,
                spender=ROUTER,
                permit2_address=PERMIT2_ADDR,
                requested_amount=100,
                block_context={"number": "latest", "timestamp": 100},
            )

    def test_missing_timestamp_raises_arc_validation_error(self) -> None:
        """Block context missing timestamp raises ArcValidationError."""
        def mock_rpc(method: str, params: Sequence[Any]) -> Any:
            return _make_permit2_return_data(100, 200, 0)

        with pytest.raises(ArcValidationError, match="missing 'timestamp'"):
            check_permit2_allowance(
                transport=mock_rpc,
                owner=OWNER,
                token=TOKEN,
                spender=ROUTER,
                permit2_address=PERMIT2_ADDR,
                requested_amount=100,
                block_context={"number": 10},
            )

    def test_negative_block_number_or_timestamp_raises_arc_validation_error(self) -> None:
        """Negative block numbers or timestamps must be rejected."""
        def mock_rpc(method: str, params: Sequence[Any]) -> Any:
            return _make_permit2_return_data(100, 200, 0)

        with pytest.raises(ArcValidationError, match="block_number cannot be negative"):
            check_permit2_allowance(
                transport=mock_rpc,
                owner=OWNER,
                token=TOKEN,
                spender=ROUTER,
                permit2_address=PERMIT2_ADDR,
                requested_amount=100,
                block_context={"number": -1, "timestamp": 100},
            )

        with pytest.raises(ArcValidationError, match="block_timestamp cannot be negative"):
            check_permit2_allowance(
                transport=mock_rpc,
                owner=OWNER,
                token=TOKEN,
                spender=ROUTER,
                permit2_address=PERMIT2_ADDR,
                requested_amount=100,
                block_context={"number": 10, "timestamp": -5},
            )

    def test_block_anchor_object_support(self) -> None:
        """BlockAnchor dataclass is directly supported as block_context."""
        # Non-Arc chain_id rejection contrast check
        with pytest.raises(ArcValidationError, match="Invalid Arc chain_id: 1"):
            BlockAnchor(
                block_number=42,
                block_hash="0x" + "aa" * 32,
                parent_hash="0x" + "bb" * 32,
                timestamp=500,
                chain_id=1,
            )

        anchor = BlockAnchor(
            block_number=42,
            block_hash="0x" + "aa" * 32,
            parent_hash="0x" + "bb" * 32,
            timestamp=500,
            chain_id=5042,
        )

        def mock_rpc(method: str, params: Sequence[Any]) -> Any:
            assert params[1] == "0x2a"  # hex(42)
            return _make_permit2_return_data(100, 600, 0)

        res = check_permit2_allowance(
            transport=mock_rpc,
            owner=OWNER,
            token=TOKEN,
            spender=ROUTER,
            permit2_address=PERMIT2_ADDR,
            requested_amount=100,
            block_context=anchor,
        )
        assert res.allowed is True
        assert res.block_number == 42
        assert res.block_timestamp == 500
