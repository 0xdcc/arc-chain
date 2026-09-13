"""Explicit token decimals management and registry for Arc assets."""

from __future__ import annotations

import re
from dataclasses import dataclass

from arc_readiness.errors import ArcNetworkMismatchError, ArcValidationError

_HEX_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


@dataclass(frozen=True)
class DecimalsEntry:
    """Immutable verified decimals record for an Arc asset."""

    token_address: str
    decimals: int
    chain_id: int
    source_proof: str

    def __post_init__(self) -> None:
        if not _HEX_ADDR_RE.match(self.token_address):
            raise ArcValidationError(f"Invalid 42-char hex token address: {self.token_address}")

        # 0 is a completely legitimate decimals value (e.g. NFT/fractional/RWA or integer tokens).
        # We enforce 0 <= decimals <= 255 (EVM uint8 standard), never artificially restricting to 0 < decimals <= 18.
        if not isinstance(self.decimals, int) or isinstance(self.decimals, bool) or not (0 <= self.decimals <= 255):
            raise ArcValidationError(
                f"Decimals must be an integer in [0, 255], got {self.decimals!r}. "
                "0 is a valid decimals value; negative numbers or values > 255 are rejected."
            )

        if self.chain_id not in (5042, 5042002):
            raise ArcNetworkMismatchError(f"Unsupported Arc chain_id: {self.chain_id}")

        if not self.source_proof or not self.source_proof.strip():
            raise ArcValidationError("source_proof cannot be empty")


class DecimalsRegistry:
    """Registry maintaining verified token decimals for Arc markets."""

    def __init__(self, chain_id: int = 5042) -> None:
        if chain_id not in (5042, 5042002):
            raise ArcNetworkMismatchError(f"Unsupported chain_id: {chain_id}")
        self.chain_id = chain_id
        self._entries: dict[str, DecimalsEntry] = {}

    def register_decimals(
        self,
        token_address: str,
        decimals: int,
        source_proof: str,
        chain_id: int | None = None,
    ) -> DecimalsEntry:
        """Register token decimals anchored to an explicit source proof."""
        target_chain = chain_id if chain_id is not None else self.chain_id
        if target_chain != self.chain_id:
            raise ArcNetworkMismatchError(
                f"Cannot register token decimals for chain {target_chain} in registry for chain {self.chain_id}"
            )

        norm_addr = token_address.lower()
        if norm_addr in self._entries:
            existing = self._entries[norm_addr]
            if existing.decimals != decimals:
                raise ArcValidationError(
                    f"Decimals conflict for {norm_addr}: existing={existing.decimals}, new={decimals}"
                )
            return existing

        entry = DecimalsEntry(
            token_address=norm_addr,
            decimals=decimals,
            chain_id=self.chain_id,
            source_proof=source_proof,
        )
        self._entries[norm_addr] = entry
        return entry

    def get_decimals(self, token_address: str) -> int | None:
        """Lookup verified decimals for a token address, returning None if unverified."""
        entry = self._entries.get(token_address.lower())
        return entry.decimals if entry is not None else None

    def require_decimals(self, token_address: str) -> int:
        """Lookup verified decimals, failing-closed if unverified."""
        d = self.get_decimals(token_address)
        if d is None:
            raise ArcValidationError(f"Decimals unverified for token {token_address}")
        return d
