"""Arc Chain Extension Contracts (Contract-v1)

Provides immutable data contracts for Arc Chain (5042 L1) extensions:
- NetworkProfile
- RawEnvelope
- CoverageManifest
- TickCoverage
- CostEvidence
- SimulationEvidenceBridge
- MarketStructureEvent
- OtcQuote

Strictly decoupled from business logic and calculators. Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any


class BlockDomain(StrEnum):
    L1 = "l1"
    L2 = "l2"


class SimulationStatus(StrEnum):
    CALL_SUCCEEDED = "CALL_SUCCEEDED"
    OUTPUT_UNVERIFIED = "OUTPUT_UNVERIFIED"
    CONTRACT_REVERT = "CONTRACT_REVERT"
    RPC_ERROR = "RPC_ERROR"
    NODE_LIMITATION = "NODE_LIMITATION"


@dataclass(frozen=True)
class NetworkProfile:
    """Arc network identity and runtime profile."""

    chain_id: int
    name: str
    block_domain: BlockDomain
    rpc_endpoints: tuple[str, ...]
    native_asset_domain: str
    max_trade_usd: float = 500.0
    is_testnet: bool = False

    def __post_init__(self) -> None:
        if self.chain_id not in (5042, 5042002):
            raise ValueError(f"Invalid Arc chain_id: {self.chain_id}. Must be 5042 (mainnet) or 5042002 (testnet).")
        if self.chain_id == 5042 and self.is_testnet:
            raise ValueError("Mainnet chain_id 5042 cannot be flagged as is_testnet=True.")
        if self.chain_id == 5042002 and not self.is_testnet:
            raise ValueError("Testnet chain_id 5042002 must have is_testnet=True.")
        if self.block_domain != BlockDomain.L1:
            raise ValueError(f"Arc network must use L1 block domain, got: {self.block_domain}")
        if self.max_trade_usd <= 0 or self.max_trade_usd > 500.0:
            raise ValueError(f"max_trade_usd must be in (0, 500.0], got {self.max_trade_usd}")


@dataclass(frozen=True)
class RawEnvelope:
    """Raw RPC envelope with durable cursor and source metadata."""

    chain_id: int
    block_domain: BlockDomain
    block_number: int
    block_hash: str
    cursor: str
    received_at: float
    payload_type: str
    raw_payload: str
    schema_version: str = "1.0"

    def __post_init__(self) -> None:
        if self.chain_id != 5042 and self.chain_id != 5042002:
            raise ValueError(f"Envelope chain_id must be 5042 or 5042002, got {self.chain_id}")
        if self.block_number < 0:
            raise ValueError(f"block_number cannot be negative: {self.block_number}")
        if not self.block_hash.startswith("0x") or len(self.block_hash) != 66:
            raise ValueError(f"Invalid block_hash format: {self.block_hash}")
        if not self.cursor:
            raise ValueError("cursor cannot be empty")


@dataclass(frozen=True)
class CoverageManifest:
    """Proof of block data coverage over a designated range."""

    chain_id: int
    from_block: int
    to_block: int
    expected_blocks: int
    covered_blocks: int
    missing_blocks: tuple[int, ...] = field(default_factory=tuple)
    coverage_ratio: float = 1.0
    verified_at: float = 0.0

    def __post_init__(self) -> None:
        if self.from_block > self.to_block:
            raise ValueError(f"from_block ({self.from_block}) cannot exceed to_block ({self.to_block})")
        calc_expected = self.to_block - self.from_block + 1
        if self.expected_blocks != calc_expected:
            raise ValueError(f"expected_blocks mismatch: {self.expected_blocks} vs {calc_expected}")
        if self.covered_blocks + len(self.missing_blocks) != self.expected_blocks:
            raise ValueError("covered_blocks + missing_blocks must equal expected_blocks")


@dataclass(frozen=True)
class TickCoverage:
    """Liquidity tick bitmap coverage evidence."""

    pool_id: str
    current_tick: int
    initialized_ticks_count: int
    min_tick: int
    max_tick: int
    is_complete: bool
    as_of_block: int

    def __post_init__(self) -> None:
        if self.min_tick > self.max_tick:
            raise ValueError(f"min_tick ({self.min_tick}) cannot exceed max_tick ({self.max_tick})")
        if self.as_of_block < 0:
            raise ValueError("as_of_block cannot be negative")


@dataclass(frozen=True)
class CostEvidence:
    """Financial cost evidence in atoms and currency."""

    currency: str
    cost_atoms: int
    source: str
    kind: str  # e.g., "gas", "dex_fee", "l1_base_fee"
    estimated_or_observed: str  # "estimated" or "observed"
    gas_payer: str | None = None
    beneficiary: str | None = None
    as_of_timestamp: float = 0.0

    def __post_init__(self) -> None:
        if self.cost_atoms < 0:
            raise ValueError("cost_atoms cannot be negative")
        if self.estimated_or_observed not in ("estimated", "observed"):
            raise ValueError(f"Invalid estimated_or_observed: {self.estimated_or_observed}")


@dataclass(frozen=True)
class SimulationEvidenceBridge:
    """Normalized readonly contract simulation outcome."""

    call_succeeded: bool
    output_verified: bool
    status: SimulationStatus
    net_output_atoms: int | None
    gas_used_atoms: int | None
    backend: str
    execution_revert_reason: str | None = None

    def __post_init__(self) -> None:
        # Invariant: If output_verified is True, call_succeeded must be True
        if self.output_verified and not self.call_succeeded:
            raise ValueError("output_verified cannot be True when call_succeeded is False")
        # Invariant: If status is CONTRACT_REVERT, call_succeeded must be False
        if self.status == SimulationStatus.CONTRACT_REVERT and self.call_succeeded:
            raise ValueError("CONTRACT_REVERT status requires call_succeeded=False")
        # Invariant: If status != CALL_SUCCEEDED, net_output_atoms cannot be asserted as positive profit
        if self.status == SimulationStatus.OUTPUT_UNVERIFIED and self.output_verified:
            raise ValueError("OUTPUT_UNVERIFIED status cannot have output_verified=True")


@dataclass(frozen=True)
class MarketStructureEvent:
    """Canonical event describing liquidity or protocol changes."""

    event_id: str
    chain_id: int
    block_number: int
    timestamp: float
    event_type: str
    pool_id: str | None
    affected_assets: tuple[str, ...]
    details: tuple[tuple[str, Any], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.block_number < 0:
            raise ValueError("block_number cannot be negative")
        if not self.event_id:
            raise ValueError("event_id cannot be empty")


@dataclass(frozen=True)
class OtcQuote:
    """Read-only off-chain OTC channel quote."""

    quote_id: str
    venue: str
    base_asset: str
    quote_asset: str
    side: str  # "buy" | "sell"
    amount_in_atoms: int
    amount_out_atoms: int
    expiry_timestamp: float
    state_ref: str | None = None

    def __post_init__(self) -> None:
        if self.side not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell', got {self.side}")
        if self.amount_in_atoms <= 0:
            raise ValueError("amount_in_atoms must be positive")
        if self.amount_out_atoms <= 0:
            raise ValueError("amount_out_atoms must be positive")
        if self.expiry_timestamp <= 0:
            raise ValueError("expiry_timestamp must be positive")
