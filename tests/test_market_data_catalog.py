"""Contract and unit tests for arbitrage/market_data/catalog.py.

Verifies:
1. WETH (18 decimals), USDG (6 decimals), and USDC (6 decimals) address and precision resolution.
2. Invalid symbol or unregistered address queries strictly raise KeyError (no decimal guessing).
3. Pool registration, lowercase ID normalization, and protocol filtering.
4. Cached TVL pool identity marks verified_source as 'cache_metadata'.
5. DEX factory whitelist presence.
6. Pure library constraints (zero network IO, zero subprocess).
"""

from __future__ import annotations

import ast
from collections.abc import Generator
from pathlib import Path

import pytest

from arbitrage.domain.types import PoolIdentity, TokenIdentity
from arbitrage.market_data.catalog import (
    ROBINHOOD_CHAIN_ID,
    VERIFIED_DEX_FACTORIES,
    VERIFIED_TOKENS,
    clear_pools,
    create_cached_pool,
    get_pool,
    get_verified_token,
    list_pools,
    register_pool,
)


@pytest.fixture(autouse=True)
def clean_pool_catalog() -> Generator[None, None, None]:
    """Ensure a clean pool catalog before and after each test."""
    clear_pools()
    yield
    clear_pools()


class TestTokenCatalogResolution:
    """Test token decimal resolution, address parsing, and error handling."""

    def test_weth_18_decimals_resolution(self) -> None:
        """Verify WETH resolves strictly to 18 decimals and matching address."""
        token_by_sym = get_verified_token("WETH")
        assert isinstance(token_by_sym, TokenIdentity)
        assert token_by_sym.chain_id == ROBINHOOD_CHAIN_ID
        assert token_by_sym.decimals == 18
        assert token_by_sym.symbol == "WETH"
        assert token_by_sym.address.lower() == "0x4200000000000000000000000000000000000006"

        # Case-insensitive symbol lookup
        token_lower_sym = get_verified_token("weth")
        assert token_lower_sym == token_by_sym

        # Address lookup
        token_by_addr = get_verified_token("0x4200000000000000000000000000000000000006")
        assert token_by_addr == token_by_sym

    def test_usdg_6_decimals_resolution(self) -> None:
        """Verify USDG resolves strictly to 6 decimals and matching address."""
        token_by_sym = get_verified_token("USDG")
        assert isinstance(token_by_sym, TokenIdentity)
        assert token_by_sym.chain_id == ROBINHOOD_CHAIN_ID
        assert token_by_sym.decimals == 6
        assert token_by_sym.symbol == "USDG"
        assert token_by_sym.address.lower() == "0x5fc5360d0400a0fd4f2af552add042d716f1d168"

        # Case-insensitive symbol lookup
        assert get_verified_token("usdg") == token_by_sym

        # Address lookup (supports uppercase / checksum)
        assert get_verified_token("0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168") == token_by_sym
        assert get_verified_token("0x5fc5360d0400a0fd4f2af552add042d716f1d168") == token_by_sym

    def test_usdc_6_decimals_resolution(self) -> None:
        """Verify USDC resolves strictly to 6 decimals and matching address."""
        token_by_sym = get_verified_token("USDC")
        assert token_by_sym.decimals == 6
        assert token_by_sym.symbol == "USDC"
        assert token_by_sym.address.lower() == "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"

        # Address lookup
        assert get_verified_token("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48") == token_by_sym

    def test_all_tokens_strict_decimals_no_guessing(self) -> None:
        """All verified tokens must strictly have decimals of 18 or 6."""
        assert len(VERIFIED_TOKENS) > 0
        for symbol, token in VERIFIED_TOKENS.items():
            assert token.chain_id == ROBINHOOD_CHAIN_ID
            assert token.decimals in (6, 18), (
                f"Token {symbol} has {token.decimals} decimals, expected 18 or 6"
            )

    @pytest.mark.parametrize(
        "invalid_query",
        [
            "FAKE_TOKEN",
            "SHIBA_UNVERIFIED",
            "0x0000000000000000000000000000000000000001",
            "0xdeaddeaddeaddeaddeaddeaddeaddeaddeaddead",
            "",
            "   ",
        ],
    )
    def test_unregistered_token_query_raises_key_error(self, invalid_query: str) -> None:
        """Unverified token symbol or address query must raise KeyError (never guess)."""
        with pytest.raises(KeyError):
            get_verified_token(invalid_query)

    def test_invalid_type_raises_type_error(self) -> None:
        """Non-string inputs must raise TypeError."""
        with pytest.raises(TypeError):
            get_verified_token(123)  # type: ignore[arg-type]


class TestPoolCatalogManagement:
    """Test pool registration, lowercase normalization, and protocol querying."""

    def test_pool_registration_and_lowercase_normalization(self) -> None:
        """Register pool with mixed-case ID and verify lowercase normalized retrieval."""
        mixed_id = "0x52E65B17FB6E5BA00ED806F37AFCD2DAA50271CA"
        pool = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v3",
            pool_id=mixed_id,
            token0="0x4200000000000000000000000000000000000006",
            token1="0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168",
            fee_bps=1.0,
            tick_spacing=10,
            verified_source="config",
        )
        register_pool(pool)

        # Retrieval via lowercase ID
        retrieved_lower = get_pool(mixed_id.lower())
        assert retrieved_lower == pool
        assert retrieved_lower.pool_id == mixed_id.lower()

        # Retrieval via mixed-case ID
        retrieved_mixed = get_pool(mixed_id)
        assert retrieved_mixed == pool

    def test_get_unregistered_pool_raises_key_error(self) -> None:
        """Querying unregistered pool_id raises KeyError."""
        with pytest.raises(KeyError):
            get_pool("0x0000000000000000000000000000000000000099")

    def test_list_pools_and_protocol_filtering(self) -> None:
        """Verify listing all pools and filtering by protocol."""
        p_v3 = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v3",
            pool_id="0x" + "11" * 20,
            token0="0x" + "aa" * 20,
            token1="0x" + "bb" * 20,
            fee_bps=5.0,
            tick_spacing=10,
        )
        p_up = PoolIdentity(
            chain_id=4663,
            protocol="up_v3",
            pool_id="0x" + "22" * 20,
            token0="0x" + "aa" * 20,
            token1="0x" + "bb" * 20,
            fee_bps=5.0,
            tick_spacing=10,
        )
        p_v2 = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v2",
            pool_id="0x" + "33" * 20,
            token0="0x" + "aa" * 20,
            token1="0x" + "bb" * 20,
            fee_bps=30.0,
            tick_spacing=60,
        )

        register_pool(p_v3)
        register_pool(p_up)
        register_pool(p_v2)

        all_pools = list_pools()
        assert len(all_pools) == 3

        # Filter by protocol (supports hyphen and underscore)
        v3_pools = list_pools("uniswap_v3")
        assert v3_pools == [p_v3]

        v3_hyphen_pools = list_pools("uniswap-v3")
        assert v3_hyphen_pools == [p_v3]

        up_pools = list_pools("up-v3")
        assert up_pools == [p_up]

        v2_pools = list_pools("uniswap-v2")
        assert v2_pools == [p_v2]

        none_pools = list_pools("unknown_protocol")
        assert none_pools == []


class TestCachedMetadataTVLSource:
    """Verify cache TVL pool source tagging adheres strictly to cache_metadata."""

    def test_pool_identity_supports_cache_metadata(self) -> None:
        """PoolIdentity must accept verified_source='cache_metadata'."""
        pool = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v3",
            pool_id="0x" + "44" * 20,
            token0="0x" + "aa" * 20,
            token1="0x" + "bb" * 20,
            fee_bps=5.0,
            tick_spacing=10,
            verified_source="cache_metadata",
        )
        assert pool.verified_source == "cache_metadata"

    def test_create_cached_pool_helper(self) -> None:
        """Helper create_cached_pool tags pool identity with cache_metadata."""
        pool = create_cached_pool(
            protocol="uniswap-v3",
            pool_id="0x" + "55" * 20,
            token0="0x" + "aa" * 20,
            token1="0x" + "bb" * 20,
            fee_bps=30.0,
            tick_spacing=60,
        )
        assert pool.verified_source == "cache_metadata"
        assert pool.chain_id == ROBINHOOD_CHAIN_ID


class TestDexFactoriesWhitelist:
    """Verify solid DEX factory whitelist."""

    def test_required_dex_factories_present(self) -> None:
        """Verify all 5 required DEX factories are in VERIFIED_DEX_FACTORIES."""
        expected = ["uniswap-v3", "up-v3", "giga-v3", "ramses-v3", "uniswap-v2"]
        for dex in expected:
            assert dex in VERIFIED_DEX_FACTORIES
            factory_addr = VERIFIED_DEX_FACTORIES[dex]
            assert factory_addr.startswith("0x")
            assert len(factory_addr) == 42


class TestArchitecturePurity:
    """Audit catalog.py module purity: zero network, subprocess, or external side effects."""

    def test_catalog_ast_audit(self) -> None:
        """Parse catalog.py AST to enforce zero network/subprocess calls."""
        catalog_path = (
            Path(__file__).resolve().parent.parent / "arbitrage" / "market_data" / "catalog.py"
        )
        tree = ast.parse(catalog_path.read_text(encoding="utf-8"))

        forbidden_imports = {
            "socket",
            "urllib",
            "requests",
            "aiohttp",
            "httpx",
            "subprocess",
            "os.system",
            "web3",
        }

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name not in forbidden_imports, f"Forbidden import: {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                assert node.module not in forbidden_imports, f"Forbidden from-import: {node.module}"
