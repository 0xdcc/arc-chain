"""Strict decoder regression tests for W3-C."""

from __future__ import annotations

import unittest

from research.settled_cycles.decoders import (
    DEPOSIT_WETH_TOPIC,
    SWAP_V3_TOPIC,
    SWAP_V4_TOPIC,
    TRANSFER_TOPIC,
    WITHDRAWAL_WETH_TOPIC,
    DecodedSwapV3,
    DecodedSwapV4,
    DecodedTransfer,
    DecodedUnwrap,
    DecodedWrap,
    UnsupportedLog,
    decode_log,
    parse_topic_address,
    parse_uint256,
    to_signed_int24,
    to_signed_int128,
    to_signed_int256,
)

POOL = "0x" + "a1" * 20
TOKEN = "0x" + "b2" * 20
MANAGER = "0x" + "c3" * 20
SENDER = "0x" + "d4" * 20
RECIPIENT = "0x" + "e5" * 20
POOL_ID = "0x" + "66" * 32


def address_topic(address: str) -> str:
    return "0x" + "0" * 24 + address[2:]


def word(value: int, bits: int = 256) -> str:
    mask256 = (1 << 256) - 1
    val = (1 << 256) + value if value < 0 else value
    return f"{val & mask256:064x}"


unsigned_word = word


class DecoderMathTests(unittest.TestCase):
    def test_signed_and_unsigned_bounds(self) -> None:
        self.assertEqual(-1, to_signed_int256((1 << 256) - 1))
        self.assertEqual(-(1 << 255), to_signed_int256(1 << 255))
        self.assertEqual((1 << 127) - 1, to_signed_int128((1 << 127) - 1))
        self.assertEqual(-(1 << 127), to_signed_int128(1 << 127))
        self.assertEqual(-1, to_signed_int24((1 << 24) - 1))
        with self.assertRaises(ValueError):
            to_signed_int128(1 << 128)
        with self.assertRaises(ValueError):
            to_signed_int24((1 << 24) + 1)
        with self.assertRaises(ValueError):
            parse_uint256(1 << 256)
        with self.assertRaises(ValueError):
            parse_uint256("0xzz")

    def test_topic_address_requires_exact_padding(self) -> None:
        self.assertEqual(SENDER, parse_topic_address(address_topic(SENDER)))
        with self.assertRaises(ValueError):
            parse_topic_address("0x1" + "0" * 23 + SENDER[2:])
        with self.assertRaises(ValueError):
            parse_topic_address("0x" + "0" * 63)


class DecoderTests(unittest.TestCase):
    def test_decode_uniswap_v3_swap_success(self) -> None:
        log = {
            "address": POOL,
            "topics": [SWAP_V3_TOPIC, address_topic(SENDER), address_topic(RECIPIENT)],
            "data": "0x"
            + word(-100)
            + word(700)
            + word(1)
            + word(2)
            + word(-5),
            "logIndex": 3,
        }
        decoded = decode_log(log)
        self.assertIsInstance(decoded, DecodedSwapV3)
        assert isinstance(decoded, DecodedSwapV3)
        self.assertEqual(-100, decoded.amount0_atoms)
        self.assertEqual(700, decoded.amount1_atoms)
        self.assertEqual(-5, decoded.tick)
        self.assertEqual(3, decoded.log_index)

    def test_decode_uniswap_v3_bad_length_or_topics(self) -> None:
        valid_data = "0x" + "0" * 320
        base = {"address": POOL, "topics": [SWAP_V3_TOPIC, address_topic(SENDER), address_topic(RECIPIENT)]}
        for data, reason in (
            ("0x" + "0" * 319, "v3_bad_data_length"),
            ("0x" + "0" * 384, "v3_bad_data_length"),
            ("0x" + "0" * 321, "v3_bad_data_length"),
        ):
            decoded = decode_log({**base, "data": data})
            self.assertIsInstance(decoded, UnsupportedLog)
            assert isinstance(decoded, UnsupportedLog)
            self.assertEqual(reason, decoded.reason)
        decoded = decode_log({**base, "topics": base["topics"][:2], "data": valid_data})
        self.assertIsInstance(decoded, UnsupportedLog)
        assert isinstance(decoded, UnsupportedLog)
        self.assertEqual("v3_bad_data_length", decoded.reason)
        decoded = decode_log({**base, "topics": [*base["topics"], address_topic(POOL)], "data": valid_data})
        self.assertIsInstance(decoded, UnsupportedLog)
        assert isinstance(decoded, UnsupportedLog)
        self.assertEqual("v3_bad_data_length", decoded.reason)

    def test_decode_uniswap_v4_swap_success(self) -> None:
        data = (
            "0x"
            + word((1 << 127) - 1, 128)
            + word(-(1 << 127), 128)
            + word(4)
            + word(5)
            + word(-12)
            + word(777)
        )
        log = {
            "address": MANAGER,
            "topics": [SWAP_V4_TOPIC, POOL_ID, address_topic(SENDER)],
            "data": data,
            "logIndex": 1,
        }
        decoded = decode_log(log)
        self.assertIsInstance(decoded, DecodedSwapV4)
        assert isinstance(decoded, DecodedSwapV4)
        self.assertEqual(POOL_ID, decoded.pool_id)
        self.assertEqual((1 << 127) - 1, decoded.amount0_atoms)
        self.assertEqual(-(1 << 127), decoded.amount1_atoms)
        self.assertEqual(777, decoded.fee)
        self.assertEqual(-12, decoded.tick)

    def test_decode_uniswap_v4_out_of_bounds(self) -> None:
        data = (
            "0x"
            + "00" * 16 + "80" + "00" * 15
            + word(0, 128)
            + word(4)
            + word(5)
            + word(0)
            + word(777)
        )
        log = {
            "address": MANAGER,
            "topics": [SWAP_V4_TOPIC, POOL_ID, address_topic(SENDER)],
            "data": data,
        }
        decoded = decode_log(log)
        self.assertIsInstance(decoded, UnsupportedLog)
        assert isinstance(decoded, UnsupportedLog)
        self.assertEqual("v4_out_of_bounds", decoded.reason)

    def test_decode_transfer_success_and_malformed(self) -> None:
        log = {
            "address": TOKEN,
            "topics": [TRANSFER_TOPIC, address_topic(SENDER), address_topic(RECIPIENT)],
            "data": "0x" + unsigned_word(123),
            "logIndex": 2,
        }
        decoded = decode_log(log)
        self.assertIsInstance(decoded, DecodedTransfer)
        assert isinstance(decoded, DecodedTransfer)
        self.assertEqual((TOKEN, SENDER, RECIPIENT, 123, 2), (
            decoded.token_address,
            decoded.from_address,
            decoded.to_address,
            decoded.value_atoms,
            decoded.log_index,
        ))
        malformed = {
            **log,
            "topics": [TRANSFER_TOPIC, "0x" + "1" + "0" * 23 + SENDER[2:], address_topic(RECIPIENT)],
        }
        decoded = decode_log(malformed)
        self.assertIsInstance(decoded, UnsupportedLog)
        assert isinstance(decoded, UnsupportedLog)
        self.assertEqual("malformed_log", decoded.reason)
        decoded = decode_log({**log, "data": "0x" + "0" * 63})
        self.assertIsInstance(decoded, UnsupportedLog)
        assert isinstance(decoded, UnsupportedLog)
        self.assertEqual("transfer_bad_data_length", decoded.reason)

    def test_decode_wrap_unwrap(self) -> None:
        deposit = decode_log(
            {
                "address": TOKEN,
                "topics": [DEPOSIT_WETH_TOPIC, address_topic(RECIPIENT)],
                "data": "0x" + unsigned_word(9),
                "logIndex": 0,
            }
        )
        self.assertIsInstance(deposit, DecodedWrap)
        assert isinstance(deposit, DecodedWrap)
        self.assertEqual((TOKEN, RECIPIENT, 9, 0), (deposit.token_address, deposit.dst, deposit.wad_atoms, deposit.log_index))
        withdrawal = decode_log(
            {
                "address": TOKEN,
                "topics": [WITHDRAWAL_WETH_TOPIC, address_topic(SENDER)],
                "data": "0x" + unsigned_word(8),
                "logIndex": 1,
            }
        )
        self.assertIsInstance(withdrawal, DecodedUnwrap)
        assert isinstance(withdrawal, DecodedUnwrap)
        self.assertEqual((TOKEN, SENDER, 8, 1), (withdrawal.token_address, withdrawal.src, withdrawal.wad_atoms, withdrawal.log_index))

    def test_decode_unknown_and_missing_topics_preserve_original(self) -> None:
        unknown = {"address": TOKEN, "topics": ["0x" + "f" * 64], "data": "0x01", "logIndex": 4}
        decoded = decode_log(unknown)
        self.assertIsInstance(decoded, UnsupportedLog)
        assert isinstance(decoded, UnsupportedLog)
        self.assertEqual(("unsupported_topic", TOKEN, ("0x" + "f" * 64,), "0x01", 4), (
            decoded.reason,
            decoded.address,
            decoded.topics,
            decoded.data,
            decoded.log_index,
        ))
        decoded = decode_log({"address": TOKEN, "topics": [], "data": "0x"})
        self.assertIsInstance(decoded, UnsupportedLog)
        assert isinstance(decoded, UnsupportedLog)
        self.assertEqual("missing_topics", decoded.reason)


if __name__ == "__main__":
    unittest.main()
