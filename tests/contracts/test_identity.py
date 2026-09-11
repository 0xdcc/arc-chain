"""Unit tests for domain identity contracts (TokenKey, AssetRef, Amount, PoolKey, FeeModel, PoolDescriptor)."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from arbitrage_contracts.identity import (
    UINT256_MAX,
    Amount,
    AssetInterfaceKind,
    AssetRef,
    FeeModel,
    FeeModelKind,
    PoolDescriptor,
    PoolIdKind,
    PoolKey,
    TokenKey,
    VenueKind,
    validate_bytes32,
    validate_evm_address,
)


class TestTokenKey(unittest.TestCase):
    """Test suite verifying TokenKey format, canonicalization, equality, and safety."""

    def test_canonical_address_normalization_and_raw_preservation(self) -> None:
        """Verify canonical address is lowercased while raw_address is preserved exactly."""
        raw_address_input = "0x55d398326f99059fF775485246999027B3197955"
        token_key = TokenKey(chain_id=56, address=raw_address_input)
        self.assertEqual(token_key.chain_id, 56)
        self.assertEqual(token_key.raw_address, raw_address_input)
        self.assertEqual(token_key.address, raw_address_input.lower())

    def test_case_insensitive_equality_and_hashing(self) -> None:
        """Verify TokenKeys with differing casing are equal and produce identical hash values."""
        address_uppercase = "0x55D398326F99059FF775485246999027B3197955"
        address_lowercase = "0x55d398326f99059ff775485246999027b3197955"
        key_one = TokenKey(56, address_uppercase)
        key_two = TokenKey(56, address_lowercase)
        self.assertEqual(key_one, key_two)
        self.assertEqual(hash(key_one), hash(key_two))

    def test_chain_separation(self) -> None:
        """Verify identical addresses on distinct chains produce distinct TokenKeys."""
        shared_address = "0x55d398326f99059ff775485246999027b3197955"
        key_bsc = TokenKey(56, shared_address)
        key_eth = TokenKey(1, shared_address)
        self.assertNotEqual(key_bsc, key_eth)
        self.assertNotEqual(hash(key_bsc), hash(key_eth))

    def test_rejection_of_invalid_address_formats(self) -> None:
        """Verify malformed, truncated, or invalid prefix EVM addresses are rejected fail-closed."""
        valid_sample = "0x55d398326f99059ff775485246999027b3197955"
        invalid_addresses = [
            valid_sample[2:],
            "0X" + valid_sample[2:],
            valid_sample[:-1],
            valid_sample + "0",
            " " + valid_sample,
            valid_sample + " ",
            "0x" + "g" * 40,
            "0x" + "z" * 40,
            "",
        ]
        for invalid_address in invalid_addresses:
            with self.assertRaises(ValueError, msg=f"Should reject {invalid_address!r}"):
                TokenKey(1, invalid_address)

        invalid_types = [123, None, 1.5, b"0x55d398326f99059ff775485246999027b3197955"]
        for invalid_type in invalid_types:
            with self.assertRaises(TypeError, msg=f"Should reject type {type(invalid_type)}"):
                TokenKey(1, invalid_type)  # type: ignore[arg-type]

    def test_rejection_of_invalid_chain_id(self) -> None:
        """Verify boolean, float, string, zero, and negative chain_ids are rejected."""
        valid_address = "0x55d398326f99059ff775485246999027b3197955"
        for invalid_type in [True, False, 1.0, 56.0, "56", None, []]:
            with self.assertRaises(
                TypeError, msg=f"Should reject chain_id type {type(invalid_type)}"
            ):
                TokenKey(invalid_type, valid_address)  # type: ignore[arg-type]

        for invalid_value in [0, -1, -56]:
            with self.assertRaises(ValueError, msg=f"Should reject chain_id value {invalid_value}"):
                TokenKey(invalid_value, valid_address)

    def test_deep_immutability(self) -> None:
        """Verify TokenKey attributes cannot be mutated after instantiation."""
        token_key = TokenKey(1, "0x55d398326f99059ff775485246999027b3197955")
        with self.assertRaises((FrozenInstanceError, AttributeError)):
            token_key.chain_id = 56  # type: ignore[misc]
        with self.assertRaises((FrozenInstanceError, AttributeError)):
            token_key.raw_address = "0x0000000000000000000000000000000000000000"  # type: ignore[misc]


class TestAssetRef(unittest.TestCase):
    """Test suite verifying AssetRef separation of ERC20 and Native assets."""

    def setUp(self) -> None:
        self.token_key = TokenKey(1, "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48")
        self.zero_address_key = TokenKey(1, "0x0000000000000000000000000000000000000000")

    def test_erc20_and_native_creation(self) -> None:
        """Verify construction of ERC20 and Native AssetRef instances."""
        erc20_asset = AssetRef.erc20(self.token_key)
        self.assertEqual(erc20_asset.interface_kind, AssetInterfaceKind.ERC20)
        self.assertEqual(erc20_asset.chain_id, 1)
        self.assertEqual(erc20_asset.token_key, self.token_key)
        self.assertIsNone(erc20_asset.native_identifier)

        native_asset = AssetRef.native(chain_id=1, native_identifier="ETH")
        self.assertEqual(native_asset.interface_kind, AssetInterfaceKind.NATIVE)
        self.assertEqual(native_asset.chain_id, 1)
        self.assertEqual(native_asset.native_identifier, "ETH")
        self.assertIsNone(native_asset.token_key)

    def test_native_never_equals_zero_address_erc20(self) -> None:
        """Verify Native AssetRef is never equal to an ERC20 AssetRef targeting address(0)."""
        erc20_zero = AssetRef.erc20(self.zero_address_key)
        native_eth = AssetRef.native(1, "ETH")
        self.assertNotEqual(erc20_zero, native_eth)
        self.assertNotEqual(hash(erc20_zero), hash(native_eth))

    def test_cross_interface_balance_domain_retained_without_confusion(self) -> None:
        """Verify shared balance_domain_id retains structural relationship without topological equality."""
        shared_domain = "domain-arc-usdc"
        erc20_part = AssetRef.erc20(self.token_key, balance_domain_id=shared_domain)
        native_part = AssetRef.native(1, "USDC_NATIVE", balance_domain_id=shared_domain)
        self.assertNotEqual(erc20_part, native_part)
        self.assertEqual(erc20_part.balance_domain_id, shared_domain)
        self.assertEqual(native_part.balance_domain_id, shared_domain)

    def test_invalid_asset_ref_combinations(self) -> None:
        """Verify invalid AssetRef combinations are rejected fail-closed."""
        with self.assertRaises(ValueError):
            AssetRef(interface_kind="erc20", chain_id=1, token_key=None)
        with self.assertRaises(ValueError):
            AssetRef(
                interface_kind="erc20",
                chain_id=1,
                token_key=self.token_key,
                native_identifier="ETH",
            )
        with self.assertRaises(ValueError):
            AssetRef(interface_kind="erc20", chain_id=56, token_key=self.token_key)
        with self.assertRaises(ValueError):
            AssetRef(
                interface_kind="native",
                chain_id=1,
                token_key=self.token_key,
                native_identifier="ETH",
            )
        with self.assertRaises(ValueError):
            AssetRef(interface_kind="native", chain_id=1, native_identifier="")
        with self.assertRaises(ValueError):
            AssetRef(interface_kind="unknown", chain_id=1)


class TestAmount(unittest.TestCase):
    """Test suite verifying Amount uint256 bounds, decimals validation, and decimal serialization."""

    def setUp(self) -> None:
        self.token_key = TokenKey(1, "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48")
        self.asset_ref = AssetRef.erc20(self.token_key)

    def test_valid_amounts_and_boundaries(self) -> None:
        """Verify minimum (0) and maximum (2**256 - 1) uint256 amounts are accepted."""
        zero_amount = Amount(asset_ref=self.asset_ref, atoms=0, decimals=18)
        self.assertEqual(zero_amount.atoms, 0)
        self.assertEqual(zero_amount.decimals, 18)
        self.assertEqual(zero_amount.to_atoms_str(), "0")

        max_amount = Amount(asset_ref=self.asset_ref, atoms=UINT256_MAX, decimals=18)
        self.assertEqual(max_amount.atoms, UINT256_MAX)
        self.assertEqual(max_amount.to_atoms_str(), str(UINT256_MAX))

    def test_rejection_of_out_of_bounds_atoms(self) -> None:
        """Verify negative atoms, overflows, floats, and booleans are rejected."""
        with self.assertRaises(ValueError):
            Amount(self.asset_ref, atoms=-1, decimals=18)
        with self.assertRaises(ValueError):
            Amount(self.asset_ref, atoms=UINT256_MAX + 1, decimals=18)
        with self.assertRaises(TypeError):
            Amount(self.asset_ref, atoms=1.0, decimals=18)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            Amount(self.asset_ref, atoms=True, decimals=18)

    def test_rejection_of_invalid_decimals(self) -> None:
        """Verify decimals outside 0..255 or non-integers are rejected."""
        valid_zero_dec = Amount(self.asset_ref, atoms=100, decimals=0)
        self.assertEqual(valid_zero_dec.decimals, 0)

        valid_max_dec = Amount(self.asset_ref, atoms=100, decimals=255)
        self.assertEqual(valid_max_dec.decimals, 255)

        with self.assertRaises(ValueError):
            Amount(self.asset_ref, atoms=100, decimals=-1)
        with self.assertRaises(ValueError):
            Amount(self.asset_ref, atoms=100, decimals=256)
        with self.assertRaises(TypeError):
            Amount(self.asset_ref, atoms=100, decimals=18.0)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            Amount(self.asset_ref, atoms=100, decimals=False)

    def test_from_atoms_str_strict_parsing(self) -> None:
        """Verify strict decimal string parsing without leading zeros, exponents, or whitespace."""
        parsed_zero = Amount.from_atoms_str(self.asset_ref, "0", decimals=6)
        self.assertEqual(parsed_zero.atoms, 0)

        parsed_one = Amount.from_atoms_str(self.asset_ref, "1000000", decimals=6)
        self.assertEqual(parsed_one.atoms, 1_000_000)

        invalid_strings = [
            "01",
            "007",
            " 1000",
            "1000 ",
            "+1000",
            "-1000",
            "1e18",
            "1.0",
            "",
            "0x10",
        ]
        for invalid_string in invalid_strings:
            with self.assertRaises(
                ValueError, msg=f"Should reject atoms string {invalid_string!r}"
            ):
                Amount.from_atoms_str(self.asset_ref, invalid_string, decimals=6)


class TestPoolKey(unittest.TestCase):
    """Test suite verifying PoolKey architecture separation across V2/V3 and V4."""

    def test_v2_v3_pool_key_with_evm_address(self) -> None:
        """Verify V2/V3 pool key enforces 20-byte address format for pool_id."""
        factory_address = "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"
        pool_address = "0x58F876857a02D6762E0101bb5C46A8c1ED44Dc16"
        pool_key = PoolKey(
            chain_id=56,
            protocol_id="pancakeswap_v2",
            venue_kind=VenueKind.FACTORY,
            venue_address=factory_address,
            pool_id_kind=PoolIdKind.ADDRESS,
            pool_id=pool_address,
        )
        self.assertEqual(pool_key.canonical_venue_address, factory_address.lower())
        self.assertEqual(pool_key.canonical_pool_id, pool_address.lower())

    def test_v4_pool_key_with_bytes32(self) -> None:
        """Verify V4 pool key enforces 66-character bytes32 format for pool_id."""
        manager_address = "0x000000000004444c5dc75cB358380D2e3dE08A90"
        pool_id_bytes32 = "0x" + "1234567890abcdef" * 4
        pool_key = PoolKey(
            chain_id=1,
            protocol_id="uniswap_v4",
            venue_kind=VenueKind.MANAGER,
            venue_address=manager_address,
            pool_id_kind=PoolIdKind.BYTES32,
            pool_id=pool_id_bytes32,
        )
        self.assertEqual(pool_key.pool_id_kind, PoolIdKind.BYTES32)
        self.assertEqual(pool_key.canonical_pool_id, pool_id_bytes32.lower())

    def test_rejection_of_v4_masquerading_address(self) -> None:
        """Verify 42-character address masquerading as V4 bytes32 poolId is rejected."""
        manager_address = "0x000000000004444c5dc75cB358380D2e3dE08A90"
        address_value = "0x58F876857a02D6762E0101bb5C46A8c1ED44Dc16"
        with self.assertRaises(ValueError):
            PoolKey(
                chain_id=1,
                protocol_id="uniswap_v4",
                venue_kind=VenueKind.MANAGER,
                venue_address=manager_address,
                pool_id_kind=PoolIdKind.BYTES32,
                pool_id=address_value,
            )

    def test_shared_manager_different_pool_ids_are_distinct(self) -> None:
        """Verify pools sharing a manager but with distinct pool_ids produce distinct keys."""
        manager_address = "0x000000000004444c5dc75cB358380D2e3dE08A90"
        pool_id_one = "0x" + "11" * 32
        pool_id_two = "0x" + "22" * 32
        key_one = PoolKey(1, "uniswap_v4", "manager", manager_address, "bytes32", pool_id_one)
        key_two = PoolKey(1, "uniswap_v4", "manager", manager_address, "bytes32", pool_id_two)
        self.assertNotEqual(key_one, key_two)
        self.assertNotEqual(hash(key_one), hash(key_two))


class TestFeeModelAndPoolDescriptor(unittest.TestCase):
    """Test suite verifying FeeModel static/dynamic/unknown classification and PoolDescriptor invariants."""

    def setUp(self) -> None:
        self.token_one = TokenKey(1, "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48")
        self.token_two = TokenKey(1, "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2")
        self.asset_one = AssetRef.erc20(self.token_one)
        self.asset_two = AssetRef.erc20(self.token_two)
        self.pool_key = PoolKey(
            chain_id=1,
            protocol_id="uniswap_v3",
            venue_kind=VenueKind.FACTORY,
            venue_address="0x1F98431c8aD98523631AE4a59f267346ea31F984",
            pool_id_kind=PoolIdKind.ADDRESS,
            pool_id="0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640",
        )

    def test_fee_model_zero_static_fee_allowed(self) -> None:
        """Verify zero fee is a valid static fee, distinct from unknown fee."""
        zero_static_fee = FeeModel.static(raw_value=0)
        self.assertEqual(zero_static_fee.kind, FeeModelKind.STATIC)
        self.assertEqual(zero_static_fee.raw_value, 0)

        unknown_fee = FeeModel.unknown()
        self.assertEqual(unknown_fee.kind, FeeModelKind.UNKNOWN)
        self.assertIsNone(unknown_fee.raw_value)
        self.assertNotEqual(zero_static_fee, unknown_fee)

    def test_fee_model_dynamic_classification(self) -> None:
        """Verify dynamic fee remains dynamic even if raw_value is currently 0."""
        dynamic_fee = FeeModel.dynamic(hook_ref="0x" + "33" * 20, raw_value=0)
        self.assertEqual(dynamic_fee.kind, FeeModelKind.DYNAMIC)
        self.assertEqual(dynamic_fee.raw_value, 0)

    def test_pool_descriptor_distinct_currencies_required(self) -> None:
        """Verify PoolDescriptor rejects identical currencies."""
        fee_model = FeeModel.static(raw_value=500)
        with self.assertRaises(ValueError):
            PoolDescriptor(
                key=self.pool_key,
                currency0=self.asset_one,
                currency1=self.asset_one,
                fee_model=fee_model,
            )

    def test_pool_descriptor_currency_chain_match_required(self) -> None:
        """Verify PoolDescriptor rejects currency on a different chain from pool."""
        bsc_token = TokenKey(56, "0x55d398326f99059ff775485246999027b3197955")
        bsc_asset = AssetRef.erc20(bsc_token)
        fee_model = FeeModel.static(raw_value=500)
        with self.assertRaises(ValueError):
            PoolDescriptor(
                key=self.pool_key,
                currency0=self.asset_one,
                currency1=bsc_asset,
                fee_model=fee_model,
            )

    def test_pool_descriptor_deep_immutability_defensive_copy(self) -> None:
        """Verify modifying external collection passed to PoolDescriptor does not mutate object."""
        mutable_evidence = ["evidence_alpha", "evidence_beta"]
        descriptor = PoolDescriptor(
            key=self.pool_key,
            currency0=self.asset_one,
            currency1=self.asset_two,
            fee_model=FeeModel.static(500),
            identity_evidence_refs=mutable_evidence,
        )
        self.assertEqual(descriptor.identity_evidence_refs, ("evidence_alpha", "evidence_beta"))
        mutable_evidence.append("evidence_gamma")
        self.assertEqual(descriptor.identity_evidence_refs, ("evidence_alpha", "evidence_beta"))


if __name__ == "__main__":
    unittest.main()
