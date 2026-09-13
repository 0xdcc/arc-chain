"""Unified asset and liquidity pool catalog for research and offline testing.

Provides a pure, strongly-typed asset and pool registry for Robinhood (chain 4663),
preventing dangerous decimal guesswork from token symbols.

Pure library constraints: zero network IO, zero subprocess calls, zero external side-effects.
"""

from __future__ import annotations

from research.market_data.types import PoolIdentity, TokenIdentity

ROBINHOOD_CHAIN_ID: int = 4663

# Audited tokens on Robinhood (4663)
# Decimals are strictly audited to 18 or 6 - guesswork is strictly forbidden.
_CORE_TOKEN_SPECS: list[tuple[str, str, int]] = [
    ("WETH", "0x4200000000000000000000000000000000000006", 18),
    ("USDG", "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168", 6),
    ("USDC", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6),
    ("USDT", "0xdAC17F958D2ee523a2206206994597C13D831ec7", 6),
    ("MOO", "0xd9db30bb0d2b8d2eae3826a1372117e058791e18", 18),
    ("PONS", "0x39dbed3a2bd333467115de45665cc57f813c4571", 18),
    ("CASHCAT", "0x020bfc650a365f8bb26819deaabf3e21291018b4", 18),
    ("MEME", "0x385f4f8ae47651ce5f58f5265395a669f8281e18", 18),
    ("TENDIES", "0x45242320dbb855eea8fd36804c6487e10e97fcf9", 18),
    ("SPY", "0xd8bc240f1eb252d6a5c101c5bdf57ee925232712", 18),
    ("NVDA", "0x43869911fdc5625ff17b9b0fa69634e443422026", 18),
    ("GLD", "0xa05e4fa4418a09bc30678d4924a68ebcc187d7dc", 18),
    ("SGOV", "0x0f4b3602fc5f096230bf9f9640ce1c4c16ca66bf", 18),
    ("TSLA", "0x88fbe6c4f0fd0c497406a4b13a35ff86cb3ca74c", 18),
    ("GME", "0x34d58849eb2f8c5c7db6ef80d9931bdfa3754983", 18),
    ("AI", "0x411aa6d2b389ba24fcba97d8b52c00fa88924b1a", 18),
    ("SLV", "0x6f9ea223395b83965b262a40fb649e1a81283c74", 18),
    ("NET", "0xCA9c78Dd337A67F6e0077F65F5E9218719d30eDf", 18),
    ("FATCOIN", "0x12d5ee7917ca430073c3a638ee1e6f0648a98a01", 18),
    ("NASDUCK", "0x4444444444444444444444444444444444444441", 18),
]

VERIFIED_TOKENS: dict[str, TokenIdentity] = {
    symbol: TokenIdentity(
        chain_id=ROBINHOOD_CHAIN_ID,
        address=address,
        decimals=decimals,
        symbol=symbol,
    )
    for symbol, address, decimals in _CORE_TOKEN_SPECS
}

_TOKENS_BY_SYMBOL: dict[str, TokenIdentity] = {
    token.symbol.upper(): token for token in VERIFIED_TOKENS.values()
}
_TOKENS_BY_ADDRESS: dict[str, TokenIdentity] = {
    token.address.lower(): token for token in VERIFIED_TOKENS.values()
}

# DEX factory registry whitelist
VERIFIED_DEX_FACTORIES: dict[str, str] = {
    "uniswap-v3": "0x1f7d7550b1b028f7571e69a784071f0205fd2efa",
    "up-v3": "0x1ac9db4a2608ba45d6127b1737949b51bb54b7f3",
    "giga-v3": "0xece6ecd61177336ea6fb9b17937ac439d85ee20b",
    "ramses-v3": "0xe0c4ceb92d08ca985bb70fe0a22feb121a9854a8",
    "uniswap-v2": "0x8bceaa40b9acdfaedf85adf4ff01f5ad6517937f",
}

# Pool registry state
_REGISTERED_POOLS: dict[str, PoolIdentity] = {}


def get_verified_token(address_or_symbol: str) -> TokenIdentity:
    """Retrieve verified TokenIdentity by symbol or address.

    Args:
        address_or_symbol: Token symbol (e.g. 'WETH') or EVM address string.

    Returns:
        The registered TokenIdentity instance.

    Raises:
        KeyError: If the token is not present in the verified catalog.
        TypeError: If the input is not a string.
    """
    if not isinstance(address_or_symbol, str):
        raise TypeError(
            f"address_or_symbol must be a string, got {type(address_or_symbol).__name__}"
        )

    query = address_or_symbol.strip()
    if not query:
        raise KeyError("Empty token identifier query")

    # Match by address (lowercase)
    if query.startswith(("0x", "0X")) and len(query) == 42:
        token = _TOKENS_BY_ADDRESS.get(query.lower())
        if token is not None:
            return token

    # Match by uppercase symbol
    token = _TOKENS_BY_SYMBOL.get(query.upper())
    if token is not None:
        return token

    # Match by fallback address lookup
    token = _TOKENS_BY_ADDRESS.get(query.lower())
    if token is not None:
        return token

    raise KeyError(f"Token '{address_or_symbol}' not found in verified token catalog")


def register_pool(pool: PoolIdentity) -> None:
    """Register a PoolIdentity into the global catalog.

    Normalizes pool_id to lowercase for reliable identity indexing.
    """
    if not isinstance(pool, PoolIdentity):
        raise TypeError(f"pool must be a PoolIdentity instance, got {type(pool).__name__}")
    norm_id = pool.pool_id.strip().lower()
    _REGISTERED_POOLS[norm_id] = pool


def get_pool(pool_id: str) -> PoolIdentity:
    """Retrieve a registered PoolIdentity by pool_id.

    Args:
        pool_id: Pool address or bytes32 identifier.

    Returns:
        The registered PoolIdentity instance.

    Raises:
        KeyError: If pool_id is not registered in the catalog.
        TypeError: If pool_id is not a string.
    """
    if not isinstance(pool_id, str):
        raise TypeError(f"pool_id must be a string, got {type(pool_id).__name__}")
    norm_id = pool_id.strip().lower()
    if norm_id not in _REGISTERED_POOLS:
        raise KeyError(f"Pool '{pool_id}' not found in registered pool catalog")
    return _REGISTERED_POOLS[norm_id]


def list_pools(protocol: str | None = None) -> list[PoolIdentity]:
    """List registered pools, optionally filtered by protocol."""
    if protocol is None:
        return list(_REGISTERED_POOLS.values())
    norm_proto = protocol.strip().lower().replace("-", "_")
    return [
        p for p in _REGISTERED_POOLS.values() if p.protocol.lower().replace("-", "_") == norm_proto
    ]


def clear_pools() -> None:
    """Clear all registered pools in the catalog."""
    _REGISTERED_POOLS.clear()


def create_cached_pool(
    *,
    chain_id: int = ROBINHOOD_CHAIN_ID,
    protocol: str,
    pool_id: str,
    token0: str,
    token1: str,
    fee_bps: float,
    tick_spacing: int,
    hooks: str | None = None,
    factory: str | None = None,
) -> PoolIdentity:
    """Create a PoolIdentity tagged with verified_source='cache_metadata'.

    Ensures cached TVL metadata is strictly distinct from live capacity.
    """
    return PoolIdentity(
        chain_id=chain_id,
        protocol=protocol,
        pool_id=pool_id,
        token0=token0,
        token1=token1,
        fee_bps=fee_bps,
        tick_spacing=tick_spacing,
        hooks=hooks,
        factory=factory,
        verified_source="cache_metadata",
    )
