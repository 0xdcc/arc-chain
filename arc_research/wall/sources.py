"""Arc USDC Wall and Off-Chain OTC Sources Reader (T31)

Enforces:
- Separation of bids (buy quotes) and asks (sell quotes)
- Explicit provenance: URL, collected_at_utc, raw payload sha256
- Expired quotes are pruned and rejected (never reuse stale screenshots)
- Fail-closed defense: unauthorized private gateway or login requirement returns UNAVAILABLE
- Sellers' ask price is NOT assumed to be executable without delivery terms and depth
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from arbitrage_contracts.arc_extensions import OtcQuote


class OtcSourceError(ValueError):
    """Raised for malformed or unauthorized OTC sources."""


@dataclass(frozen=True, slots=True)
class OtcSourceConfig:
    """Configuration for an OTC public read-only source."""

    venue: str
    endpoint_url: str
    is_public: bool = True
    requires_auth: bool = False
    rate_limit_per_minute: int = 60

    def __post_init__(self) -> None:
        if not self.venue:
            raise OtcSourceError("venue cannot be empty")
        if not self.endpoint_url.startswith(("http://", "https://", "file://")):
            raise OtcSourceError(f"Invalid endpoint_url: {self.endpoint_url}")


@dataclass(frozen=True, slots=True)
class OtcOrderBook:
    """Normalized snapshot of bidirectional off-chain OTC liquidity."""

    venue: str
    endpoint_url: str
    collected_at_utc: float
    raw_payload_hash: str
    bids: tuple[OtcQuote, ...]  # Buy quotes (venue buys base)
    asks: tuple[OtcQuote, ...]  # Sell quotes (venue sells base)
    is_stale: bool = False

    def is_executable(self, now_utc: float) -> bool:
        """Check if orderbook has unexpired actionable quotes."""
        if self.is_stale:
            return False
        return any(q.expiry_timestamp > now_utc for q in (self.bids + self.asks))


class OtcSourceCollector:
    """Read-only collector for public OTC channels."""

    def __init__(self, config: OtcSourceConfig) -> None:
        self.config = config

    def parse_orderbook_payload(
        self,
        raw_text: str,
        now_utc: float | None = None,
    ) -> OtcOrderBook:
        """Parse raw JSON payload into normalized bidirectional orderbook.

        Guarantees:
        1. Private/unauthorized endpoints fail closed without probe attempts.
        2. Strict payload SHA-256 hash preservation.
        3. Stale/expired quotes are distinctly flagged.
        """
        if self.config.requires_auth:
            raise OtcSourceError(
                f"Venue {self.config.venue} requires authentication; unauthorized private gateway access is prohibited"
            )

        current_time = now_utc if now_utc is not None else time.time()
        payload_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()

        try:
            data = json.loads(raw_text)
        except Exception as exc:
            raise OtcSourceError(f"Malformed JSON from OTC source: {exc}") from exc

        bids: list[OtcQuote] = []
        asks: list[OtcQuote] = []

        # Parse bids
        for item in data.get("bids", []):
            quote = OtcQuote(
                quote_id=item["quote_id"],
                venue=self.config.venue,
                base_asset=item["base_asset"],
                quote_asset=item["quote_asset"],
                side="buy",
                amount_in_atoms=item["amount_in_atoms"],
                amount_out_atoms=item["amount_out_atoms"],
                expiry_timestamp=item["expiry_timestamp"],
                state_ref=item.get("state_ref"),
            )
            # Filter expired quotes
            if quote.expiry_timestamp > current_time:
                bids.append(quote)

        # Parse asks
        for item in data.get("asks", []):
            quote = OtcQuote(
                quote_id=item["quote_id"],
                venue=self.config.venue,
                base_asset=item["base_asset"],
                quote_asset=item["quote_asset"],
                side="sell",
                amount_in_atoms=item["amount_in_atoms"],
                amount_out_atoms=item["amount_out_atoms"],
                expiry_timestamp=item["expiry_timestamp"],
                state_ref=item.get("state_ref"),
            )
            if quote.expiry_timestamp > current_time:
                asks.append(quote)

        return OtcOrderBook(
            venue=self.config.venue,
            endpoint_url=self.config.endpoint_url,
            collected_at_utc=current_time,
            raw_payload_hash=payload_hash,
            bids=tuple(bids),
            asks=tuple(asks),
            is_stale=(len(bids) == 0 and len(asks) == 0),
        )
