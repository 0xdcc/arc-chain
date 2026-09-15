"""Deterministic contract tests for PoolReader ABI string decoding.

Maps and preserves the exact 3 legacy test obligations from:
    tests/test_spread_monitor.py :: TestDecodeString
while verifying boundary robustness, failclosed behavior, and regression protection
against accidental stripping of legitimate leading characters.
"""

from __future__ import annotations

import pytest

from research.market_data.pool_reader import PoolReader


class TestPoolStringDecode:
    """Original 3 cases from tests/test_spread_monitor.py TestDecodeString."""

    def test_decodes_bytes32_layout(self) -> None:
        """bytes32 布局: 前补零, 尾部为字符串."""
        raw = b"WETH".hex()
        word = raw.ljust(64, "0")
        assert PoolReader._decode_string("0x" + word) == "WETH"

    def test_decodes_abi_string_layout(self) -> None:
        """标准 ABI string: [offset][length][data]."""
        data = b"USDC"
        payload = (32).to_bytes(32, "big") + len(data).to_bytes(32, "big") + data
        assert PoolReader._decode_string("0x" + payload.hex()) == "USDC"

    def test_strips_null_bytes(self) -> None:
        """历史 fixture 兼容: 32 字节 raw 前导零加单字节长度前缀 (非底层链规则, 仅为历史测试兼容)."""
        raw = b"\x00" * 27 + b"\x04" + b"WETH"
        assert PoolReader._decode_string("0x" + raw.hex()) == "WETH"


class TestPoolStringDecodeBoundaryAndFailClosed:
    """Boundary conditions, dynamic ABI offset/length checks, and failclosed behaviors."""

    def test_decodes_padded_standard_abi_string(self) -> None:
        """Standard 96-byte ABI dynamic string with 32-byte padded data word."""
        data = b"DAI"
        payload = (32).to_bytes(32, "big") + len(data).to_bytes(32, "big") + data.ljust(32, b"\x00")
        assert PoolReader._decode_string("0x" + payload.hex()) == "DAI"

    def test_decodes_right_aligned_bytes32_without_length_prefix(self) -> None:
        """Right-aligned bytes32 token symbol without length prefix byte."""
        raw = b"\x00" * 28 + b"USDT"
        assert PoolReader._decode_string("0x" + raw.hex()) == "USDT"

    def test_empty_and_zero_values_return_empty_string(self) -> None:
        """Empty inputs and all-null byte buffers return empty string per original convention."""
        assert PoolReader._decode_string("") == ""
        assert PoolReader._decode_string("0x") == ""
        assert PoolReader._decode_string("0x" + "00" * 32) == ""
        assert PoolReader._decode_string("0x" + "00" * 64) == ""

    def test_abi_dynamic_string_zero_length(self) -> None:
        """Dynamic ABI string explicitly declared with length 0 returns empty string."""
        payload = (32).to_bytes(32, "big") + (0).to_bytes(32, "big")
        assert PoolReader._decode_string("0x" + payload.hex()) == ""

    def test_rejects_truncated_abi_dynamic_string(self) -> None:
        """Dynamic ABI string where length exceeds total buffer fails closed (ValueError)."""
        payload = (32).to_bytes(32, "big") + (100).to_bytes(32, "big") + b"short"
        with pytest.raises(ValueError, match="truncated or out of bounds"):
            PoolReader._decode_string("0x" + payload.hex())

    def test_rejects_invalid_hex_format(self) -> None:
        """Invalid hex strings (odd length, non-hex chars) fail closed (ValueError)."""
        with pytest.raises(ValueError, match="Invalid hex string"):
            PoolReader._decode_string("0x123")
        with pytest.raises(ValueError, match="Invalid hex string"):
            PoolReader._decode_string("0xZZ")

    def test_rejects_non_string_input(self) -> None:
        """Non-string inputs fail closed (TypeError)."""
        with pytest.raises(TypeError, match="Expected str"):
            PoolReader._decode_string(12345)  # type: ignore[arg-type]

    def test_rejects_invalid_utf8(self) -> None:
        """Payloads containing invalid UTF-8 byte sequences fail closed (ValueError)."""
        with pytest.raises(ValueError, match="invalid utf-8"):
            PoolReader._decode_string("0x" + (b"\xff\xfe\xfd" * 10).hex())

    def test_no_fake_symbol_fallback(self) -> None:
        """Decoding failures must never mask corruption by returning dummy symbols like 'UNKNOWN'."""
        malformed_inputs = [
            "0x123",
            "0x" + ((32).to_bytes(32, "big") + (999).to_bytes(32, "big")).hex(),
            "0x" + (b"\xff\xaa" * 16).hex(),
        ]
        for inp in malformed_inputs:
            with pytest.raises((ValueError, TypeError)):
                result = PoolReader._decode_string(inp)
                assert result != "UNKNOWN"
                assert result != "N/A"

    def test_preserves_ascii_leading_char_when_length_matches_long_raw(self) -> None:
        """Long raw string (66B) starting with 'A' (ASCII 65 == len-1) must not strip 'A'."""
        # 66 bytes total: 'A' (ASCII 65) + 65 characters of payload.
        # Under the old buggy heuristic, candidate[0] == 65 == len - 1 triggered Pascal stripping,
        # erroneously dropping the legal leading 'A'.
        payload = b"A" + (b"X" * 65)
        decoded = PoolReader._decode_string("0x" + payload.hex())
        assert decoded == "A" + ("X" * 65)

    def test_standard_abi_dynamic_string_preserves_leading_control_byte(self) -> None:
        """Standard dynamic ABI string with leading control byte (valid UTF-8) must not enter Pascal strip."""
        # Dynamic ABI string layout: offset=32, length=5, data=b"\x04WETH"
        # Even though data[0] == 4 == len(data) - 1, standard dynamic ABI is already
        # bounded by offset/length and must strictly never enter the historical Pascal stripping branch.
        data = b"\x04WETH"
        payload = (32).to_bytes(32, "big") + len(data).to_bytes(32, "big") + data.ljust(32, b"\x00")
        assert PoolReader._decode_string("0x" + payload.hex()) == "\x04WETH"

        data2 = b"\x01TOKEN"
        payload2 = (32).to_bytes(32, "big") + len(data2).to_bytes(32, "big") + data2.ljust(32, b"\x00")
        assert PoolReader._decode_string("0x" + payload2.hex()) == "\x01TOKEN"
