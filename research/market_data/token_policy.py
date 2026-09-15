"""Historical token tax metadata for offline research."""

KNOWN_TAX_TOKENS: dict[str, float] = {
    "0x56910d4409f3a0c78c64dd8d0545ff0705389870".lower(): 3.0,  # Index (3% transfer tax)
    "0x0d257ca40d40090be60c2d2ed5bb3535392838cc".lower(): 5.0,  # Hood10 (5% transfer tax)
}

KNOWN_TAX_SYMBOLS: set[str] = {"INDEX", "HOOD10"}

def is_tax_token(token_address_or_symbol: str) -> bool:
    """Check whether a token address or symbol has known transfer tax.

    Args:
        token_address_or_symbol: Token EVM contract address or symbol.

    Returns:
        True if token is known to impose transfer fee / tax.
    """
    if not token_address_or_symbol:
        return False
    clean = token_address_or_symbol.strip()
    if clean.upper() in KNOWN_TAX_SYMBOLS:
        return True
    return clean.lower() in KNOWN_TAX_TOKENS
