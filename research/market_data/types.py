"""Pure domain data contracts for research market data.

Side-effect-free data structures and value objects for token identity, pool identity,
market snapshots, routing candidates, quotes, and execution plans.

Constraints:
- Pure Python standard library only (dataclasses, decimal, enum, typing).
- Zero network IO, zero external RPC/subprocess dependencies.
- Offline data structures for modeling and contract validation.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any


def _validate_eth_address(address: str, field_name: str = "address") -> str:
    """Validate standard 20-byte hex Ethereum address (42 chars starting with 0x)."""
    if not isinstance(address, str):
        raise TypeError(f"{field_name} must be a string, got {type(address).__name__}")
    if not address.startswith("0x") or len(address) != 42:
        raise ValueError(f"{field_name} must start with '0x' and be 42 characters, got '{address}'")
    try:
        int(address, 16)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid hex string, got '{address}'") from exc
    return address


def _validate_hex_string(val: str, field_name: str) -> str:
    """Validate arbitrary hex string starting with 0x."""
    if not isinstance(val, str):
        raise TypeError(f"{field_name} must be a string, got {type(val).__name__}")
    if not val.startswith("0x"):
        raise ValueError(f"{field_name} must start with '0x', got '{val}'")
    try:
        int(val, 16)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid hex string, got '{val}'") from exc
    return val


@dataclass(frozen=True)
class TokenIdentity:
    """Immutable representation of a token on a specific chain.

    Address comparison is case-insensitive for equivalence logic, while retaining
    the provided format. Decimals are constrained to the standard EVM range [0, 18].
    """

    chain_id: int
    address: str
    decimals: int
    symbol: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.chain_id, int)
            or isinstance(self.chain_id, bool)
            or self.chain_id <= 0
        ):
            raise ValueError(f"chain_id must be a positive integer, got {self.chain_id}")

        _validate_eth_address(self.address, "TokenIdentity.address")

        if not isinstance(self.decimals, int) or isinstance(self.decimals, bool):
            raise TypeError(f"decimals must be an integer, got {type(self.decimals).__name__}")
        if not (0 <= self.decimals <= 18):
            raise ValueError(f"decimals must be between 0 and 18, got {self.decimals}")

        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError(f"symbol must be a non-empty string, got '{self.symbol}'")


    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "address": self.address,
            "decimals": self.decimals,
            "symbol": self.symbol,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TokenIdentity":
        return cls(
            chain_id=int(data["chain_id"]),
            address=str(data["address"]),
            decimals=int(data["decimals"]),
            symbol=str(data["symbol"]),
        )


@dataclass(frozen=True)
class TokenAmount:
    """Strongly-typed token asset quantity denominated in raw integer atoms (wei / satoshis).

    Prevents floating-point precision loss when handling financial amounts.
    """

    token: TokenIdentity
    atoms: int

    def __post_init__(self) -> None:
        if not isinstance(self.token, TokenIdentity):
            raise TypeError(f"token must be TokenIdentity, got {type(self.token).__name__}")
        if not isinstance(self.atoms, int) or isinstance(self.atoms, bool):
            raise TypeError(f"atoms must be an integer, got {type(self.atoms).__name__}")
        if self.atoms < 0:
            raise ValueError(f"atoms cannot be negative, got {self.atoms}")

    def to_decimal(self) -> Decimal:
        """Convert integer atoms to human-readable Decimal using token decimals."""
        return Decimal(self.atoms) / (Decimal(10) ** self.token.decimals)

    @classmethod
    def from_decimal(cls, token: TokenIdentity, val: Decimal | str | int | float) -> "TokenAmount":
        """Construct TokenAmount from decimal or numeric representation without float pollution."""
        if isinstance(val, (int, str)):
            d = Decimal(str(val))
        elif isinstance(val, Decimal):
            d = val
        elif isinstance(val, float):
            d = Decimal(str(val))
        else:
            raise TypeError(f"val must be Decimal, str, int, or float, got {type(val).__name__}")

        atoms = int(d * (Decimal(10) ** token.decimals))
        return cls(token=token, atoms=atoms)

    def __lt__(self, other: "TokenAmount") -> bool:
        if not isinstance(other, TokenAmount) or self.token != other.token:
            raise TypeError("Cannot compare TokenAmount with different tokens")
        return self.atoms < other.atoms

    def __le__(self, other: "TokenAmount") -> bool:
        return self < other or self == other

    def __gt__(self, other: "TokenAmount") -> bool:
        if not isinstance(other, TokenAmount) or self.token != other.token:
            raise TypeError("Cannot compare TokenAmount with different tokens")
        return self.atoms > other.atoms

    def __ge__(self, other: "TokenAmount") -> bool:
        return self > other or self == other

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token.to_dict(),
            "atoms": self.atoms,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TokenAmount":
        return cls(
            token=TokenIdentity.from_dict(data["token"]),
            atoms=int(data["atoms"]),
        )


@dataclass(frozen=True)
class PoolIdentity:
    """Unique identity and configuration of a DEX liquidity pool.

    Token addresses and pool_id are normalized to lowercase hex strings.
    """

    chain_id: int
    protocol: str  # "uniswap_v3" | "uniswap_v4" | "pancakeswap_v3"
    pool_id: str  # V3: 0x address (42 chars); V4: bytes32 hex string (66 chars)
    token0: str  # normalized lowercase 0x address
    token1: str  # normalized lowercase 0x address
    fee_bps: float  # Basis points (e.g. 5.0 for 0.05%)
    tick_spacing: int
    hooks: str | None = None
    factory: str | None = None
    verified_source: str = "on_chain"  # "on_chain" | "config" | "unverified" | "cache_metadata"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.chain_id, int)
            or isinstance(self.chain_id, bool)
            or self.chain_id <= 0
        ):
            raise ValueError(f"chain_id must be a positive integer, got {self.chain_id}")

        if not isinstance(self.protocol, str) or not self.protocol.strip():
            raise ValueError("protocol must be a non-empty string")

        _validate_hex_string(self.pool_id, "PoolIdentity.pool_id")
        object.__setattr__(self, "pool_id", self.pool_id.lower())

        _validate_eth_address(self.token0, "PoolIdentity.token0")
        _validate_eth_address(self.token1, "PoolIdentity.token1")
        norm_t0 = self.token0.lower()
        norm_t1 = self.token1.lower()
        if norm_t0 == norm_t1:
            raise ValueError(f"token0 and token1 cannot be the same address: '{self.token0}'")
        object.__setattr__(self, "token0", norm_t0)
        object.__setattr__(self, "token1", norm_t1)

        if not isinstance(self.fee_bps, (int, float)) or self.fee_bps < 0:
            raise ValueError(f"fee_bps must be non-negative number, got {self.fee_bps}")
        object.__setattr__(self, "fee_bps", float(self.fee_bps))

        if (
            not isinstance(self.tick_spacing, int)
            or isinstance(self.tick_spacing, bool)
            or self.tick_spacing == 0
        ):
            raise ValueError(f"tick_spacing must be non-zero integer, got {self.tick_spacing}")

        if self.hooks is not None:
            _validate_hex_string(self.hooks, "PoolIdentity.hooks")
            object.__setattr__(self, "hooks", self.hooks.lower())

        if self.factory is not None:
            _validate_hex_string(self.factory, "PoolIdentity.factory")
            object.__setattr__(self, "factory", self.factory.lower())

        valid_sources = ("on_chain", "config", "unverified", "cache_metadata")
        if self.verified_source not in valid_sources:
            raise ValueError(
                f"verified_source must be one of {valid_sources}, got '{self.verified_source}'"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "protocol": self.protocol,
            "pool_id": self.pool_id,
            "token0": self.token0,
            "token1": self.token1,
            "fee_bps": self.fee_bps,
            "tick_spacing": self.tick_spacing,
            "hooks": self.hooks,
            "factory": self.factory,
            "verified_source": self.verified_source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PoolIdentity":
        return cls(
            chain_id=int(data["chain_id"]),
            protocol=str(data["protocol"]),
            pool_id=str(data["pool_id"]),
            token0=str(data["token0"]),
            token1=str(data["token1"]),
            fee_bps=float(data["fee_bps"]),
            tick_spacing=int(data["tick_spacing"]),
            hooks=data.get("hooks"),
            factory=data.get("factory"),
            verified_source=str(data.get("verified_source", "on_chain")),
        )


@dataclass(frozen=True)
class PoolStateSnapshot:
    """State of an individual pool captured at a specific block."""

    pool: PoolIdentity
    block_number: int
    block_timestamp: int
    sqrt_price_x96: int | None
    liquidity: int | None
    tick: int | None
    raw_response: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.pool, PoolIdentity):
            raise TypeError("pool must be PoolIdentity")
        if (
            not isinstance(self.block_number, int)
            or isinstance(self.block_number, bool)
            or self.block_number < 0
        ):
            raise ValueError(f"block_number must be non-negative integer, got {self.block_number}")
        if (
            not isinstance(self.block_timestamp, int)
            or isinstance(self.block_timestamp, bool)
            or self.block_timestamp < 0
        ):
            raise ValueError(
                f"block_timestamp must be non-negative integer, got {self.block_timestamp}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "pool": self.pool.to_dict(),
            "block_number": self.block_number,
            "block_timestamp": self.block_timestamp,
            "sqrt_price_x96": self.sqrt_price_x96,
            "liquidity": self.liquidity,
            "tick": self.tick,
            "raw_response": self.raw_response,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PoolStateSnapshot":
        return cls(
            pool=PoolIdentity.from_dict(data["pool"]),
            block_number=int(data["block_number"]),
            block_timestamp=int(data["block_timestamp"]),
            sqrt_price_x96=int(data["sqrt_price_x96"]) if data.get("sqrt_price_x96") is not None else None,
            liquidity=int(data["liquidity"]) if data.get("liquidity") is not None else None,
            tick=int(data["tick"]) if data.get("tick") is not None else None,
            raw_response=data.get("raw_response"),
        )


@dataclass(frozen=True)
class MarketSnapshot:
    """Market snapshot containing states of multiple pools at a specific block."""

    chain_id: int
    block_number: int
    captured_at: float
    pools: dict[str, PoolStateSnapshot]  # key: pool_id

    def __post_init__(self) -> None:
        if (
            not isinstance(self.chain_id, int)
            or isinstance(self.chain_id, bool)
            or self.chain_id <= 0
        ):
            raise ValueError("chain_id must be a positive integer")
        if (
            not isinstance(self.block_number, int)
            or isinstance(self.block_number, bool)
            or self.block_number < 0
        ):
            raise ValueError("block_number must be non-negative integer")
        if not isinstance(self.pools, dict):
            raise TypeError("pools must be a dict")
        for k, v in self.pools.items():
            if not isinstance(v, PoolStateSnapshot):
                raise TypeError(f"pools['{k}'] must be PoolStateSnapshot")

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "block_number": self.block_number,
            "captured_at": self.captured_at,
            "pools": {k: v.to_dict() for k, v in self.pools.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MarketSnapshot":
        return cls(
            chain_id=int(data["chain_id"]),
            block_number=int(data["block_number"]),
            captured_at=float(data["captured_at"]),
            pools={k: PoolStateSnapshot.from_dict(v) for k, v in data["pools"].items()},
        )


@dataclass(frozen=True)
class RouteHop:
    """Single swap hop through a specific DEX pool."""

    pool: PoolIdentity
    token_in: TokenIdentity
    token_out: TokenIdentity

    def __post_init__(self) -> None:
        if not isinstance(self.pool, PoolIdentity):
            raise TypeError("pool must be PoolIdentity")
        if not isinstance(self.token_in, TokenIdentity):
            raise TypeError("token_in must be TokenIdentity")
        if not isinstance(self.token_out, TokenIdentity):
            raise TypeError("token_out must be TokenIdentity")

        in_addr = self.token_in.address.lower()
        out_addr = self.token_out.address.lower()
        if in_addr == out_addr:
            raise ValueError(
                f"token_in and token_out cannot be the same address: {self.token_in.address}"
            )

        pool_tokens = {self.pool.token0.lower(), self.pool.token1.lower()}
        hop_tokens = {in_addr, out_addr}
        if hop_tokens != pool_tokens:
            raise ValueError(f"Hop tokens {hop_tokens} do not match pool tokens {pool_tokens}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "pool": self.pool.to_dict(),
            "token_in": self.token_in.to_dict(),
            "token_out": self.token_out.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RouteHop":
        return cls(
            pool=PoolIdentity.from_dict(data["pool"]),
            token_in=TokenIdentity.from_dict(data["token_in"]),
            token_out=TokenIdentity.from_dict(data["token_out"]),
        )


@dataclass(frozen=True)
class CandidateRoute:
    """Arbitrage candidate route composed of a sequence of hops.

    Invariance rule:
    Route must be a strictly closed cycle starting and ending at base_token,
    with intermediate hops seamlessly chained (hop[i].token_out == hop[i+1].token_in).
    """

    candidate_id: str
    route_type: str  # "two_hop_spread" | "triangular"
    base_token: TokenIdentity
    hops: tuple[RouteHop, ...]
    observed_gross_bps: float  # observed gross spread (basis points), != net profit
    snapshot_block: int
    created_at: float

    def __post_init__(self) -> None:
        if not isinstance(self.base_token, TokenIdentity):
            raise TypeError("base_token must be TokenIdentity")

        if isinstance(self.hops, (list, tuple)):
            object.__setattr__(self, "hops", tuple(self.hops))
        else:
            raise TypeError("hops must be a sequence of RouteHop")

        if len(self.hops) == 0:
            raise ValueError("CandidateRoute hops cannot be empty")

        for idx, h in enumerate(self.hops):
            if not isinstance(h, RouteHop):
                raise TypeError(f"hop at index {idx} must be RouteHop, got {type(h).__name__}")

        base_addr = self.base_token.address.lower()
        if self.hops[0].token_in.address.lower() != base_addr:
            raise ValueError(
                f"Route must start with base_token {self.base_token.symbol} ({base_addr}), "
                f"got {self.hops[0].token_in.address.lower()}"
            )

        if self.hops[-1].token_out.address.lower() != base_addr:
            raise ValueError(
                f"Route must end with base_token {self.base_token.symbol} ({base_addr}), "
                f"got {self.hops[-1].token_out.address.lower()}"
            )

        for i in range(len(self.hops) - 1):
            curr_out = self.hops[i].token_out.address.lower()
            next_in = self.hops[i + 1].token_in.address.lower()
            if curr_out != next_in:
                raise ValueError(
                    f"Discontinuous hops at step {i}->{i + 1}: "
                    f"token_out '{curr_out}' != next token_in '{next_in}'"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "route_type": self.route_type,
            "base_token": self.base_token.to_dict(),
            "hops": [h.to_dict() for h in self.hops],
            "observed_gross_bps": self.observed_gross_bps,
            "snapshot_block": self.snapshot_block,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CandidateRoute":
        return cls(
            candidate_id=str(data["candidate_id"]),
            route_type=str(data["route_type"]),
            base_token=TokenIdentity.from_dict(data["base_token"]),
            hops=tuple(RouteHop.from_dict(h) for h in data["hops"]),
            observed_gross_bps=float(data["observed_gross_bps"]),
            snapshot_block=int(data["snapshot_block"]),
            created_at=float(data["created_at"]),
        )


class QuoteStatus(StrEnum):
    """Categorized status for quote queries."""

    QUOTED = "QUOTED"
    CONTRACT_REVERT = "CONTRACT_REVERT"
    RPC_ERROR = "RPC_ERROR"
    NODE_LIMITATION = "NODE_LIMITATION"
    INVALID_RESPONSE = "INVALID_RESPONSE"


@dataclass(frozen=True)
class QuoteResult:
    """Structured quote result with error classification.

    Invariance rule:
    When status != QuoteStatus.QUOTED, amount_out and delta_atoms MUST strictly be None.
    When status == QuoteStatus.QUOTED, amount_out must not be None.
    """

    status: QuoteStatus
    amount_in: TokenAmount
    amount_out: TokenAmount | None
    delta_atoms: int | None
    gas_estimate: int | None = None
    error_code: int | None = None
    error_message: str | None = None
    raw_revert_data: str | None = None
    block_number: int | None = None
    quote_mode: str = "live"  # "live" | "historical_replay" | "synthetic"

    def __post_init__(self) -> None:
        status_val: Any = self.status
        if isinstance(status_val, str):
            object.__setattr__(self, "status", QuoteStatus(status_val))
        elif not isinstance(status_val, QuoteStatus):
            raise TypeError(f"status must be QuoteStatus, got {type(status_val).__name__}")

        if not isinstance(self.amount_in, TokenAmount):
            raise TypeError("amount_in must be TokenAmount")

        if self.status != QuoteStatus.QUOTED:
            if self.amount_out is not None or self.delta_atoms is not None:
                raise ValueError(
                    f"Failed quote result (status={self.status.value}) must have amount_out=None "
                    f"and delta_atoms=None, got amount_out={self.amount_out}, delta_atoms={self.delta_atoms}"
                )
        else:
            if self.amount_out is None:
                raise ValueError("Successful quote (status=QUOTED) must have amount_out")
            amount_out_val: Any = self.amount_out
            if not isinstance(amount_out_val, TokenAmount):
                raise TypeError("amount_out must be TokenAmount")

            if self.delta_atoms is None:
                if self.amount_out.token.address.lower() == self.amount_in.token.address.lower():
                    object.__setattr__(
                        self, "delta_atoms", self.amount_out.atoms - self.amount_in.atoms
                    )
            else:
                if not isinstance(self.delta_atoms, int) or isinstance(self.delta_atoms, bool):
                    raise TypeError("delta_atoms must be an integer")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "amount_in": self.amount_in.to_dict(),
            "amount_out": self.amount_out.to_dict() if self.amount_out is not None else None,
            "delta_atoms": self.delta_atoms,
            "gas_estimate": self.gas_estimate,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "raw_revert_data": self.raw_revert_data,
            "block_number": self.block_number,
            "quote_mode": self.quote_mode,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QuoteResult":
        return cls(
            status=QuoteStatus(data["status"]),
            amount_in=TokenAmount.from_dict(data["amount_in"]),
            amount_out=TokenAmount.from_dict(data["amount_out"])
            if data.get("amount_out") is not None
            else None,
            delta_atoms=int(data["delta_atoms"]) if data.get("delta_atoms") is not None else None,
            gas_estimate=int(data["gas_estimate"])
            if data.get("gas_estimate") is not None
            else None,
            error_code=int(data["error_code"]) if data.get("error_code") is not None else None,
            error_message=data.get("error_message"),
            raw_revert_data=data.get("raw_revert_data"),
            block_number=int(data["block_number"])
            if data.get("block_number") is not None
            else None,
            quote_mode=str(data.get("quote_mode", "live")),
        )


@dataclass(frozen=True)
class ExecutionPlan:
    """Immutable execution plan ready for safety checks and transaction dispatch.

    Slippage Safety Red Line:
    min_amount_out.atoms MUST strictly be > 0. Never allow 0 (sandwich/MEV vulnerability).
    """

    plan_id: str
    candidate_id: str
    route_type: str
    base_token: TokenIdentity
    amount_in: TokenAmount
    min_amount_out: TokenAmount  # STRICT: must have atoms > 0
    hops: tuple[RouteHop, ...]
    quoter_block: int
    deadline: int
    estimated_gas_usd: Decimal
    target_router: str

    def __post_init__(self) -> None:
        if not isinstance(self.base_token, TokenIdentity):
            raise TypeError("base_token must be TokenIdentity")
        if not isinstance(self.amount_in, TokenAmount):
            raise TypeError("amount_in must be TokenAmount")
        if not isinstance(self.min_amount_out, TokenAmount):
            raise TypeError("min_amount_out must be TokenAmount")

        if self.min_amount_out.atoms <= 0:
            raise ValueError(
                f"Slippage safety violation: min_amount_out.atoms must be > 0, got {self.min_amount_out.atoms}"
            )
        if self.amount_in.atoms <= 0:
            raise ValueError(f"amount_in.atoms must be > 0, got {self.amount_in.atoms}")

        base_addr = self.base_token.address.lower()
        if self.amount_in.token.address.lower() != base_addr:
            raise ValueError(
                f"amount_in token ({self.amount_in.token.address}) does not match base_token ({self.base_token.address})"
            )
        if self.min_amount_out.token.address.lower() != base_addr:
            raise ValueError(
                f"min_amount_out token ({self.min_amount_out.token.address}) does not match base_token ({self.base_token.address})"
            )

        if isinstance(self.hops, (list, tuple)):
            object.__setattr__(self, "hops", tuple(self.hops))
        else:
            raise TypeError("hops must be a sequence of RouteHop")

        if len(self.hops) == 0:
            raise ValueError("ExecutionPlan hops cannot be empty")

        _validate_eth_address(self.target_router, "ExecutionPlan.target_router")

        gas_val: Any = self.estimated_gas_usd
        if isinstance(gas_val, (int, str, float)):
            object.__setattr__(self, "estimated_gas_usd", Decimal(str(gas_val)))
        elif not isinstance(gas_val, Decimal):
            raise TypeError("estimated_gas_usd must be Decimal")

        if self.estimated_gas_usd < Decimal("0"):
            raise ValueError(f"estimated_gas_usd cannot be negative, got {self.estimated_gas_usd}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "candidate_id": self.candidate_id,
            "route_type": self.route_type,
            "base_token": self.base_token.to_dict(),
            "amount_in": self.amount_in.to_dict(),
            "min_amount_out": self.min_amount_out.to_dict(),
            "hops": [h.to_dict() for h in self.hops],
            "quoter_block": self.quoter_block,
            "deadline": self.deadline,
            "estimated_gas_usd": str(self.estimated_gas_usd),
            "target_router": self.target_router,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecutionPlan":
        return cls(
            plan_id=str(data["plan_id"]),
            candidate_id=str(data["candidate_id"]),
            route_type=str(data["route_type"]),
            base_token=TokenIdentity.from_dict(data["base_token"]),
            amount_in=TokenAmount.from_dict(data["amount_in"]),
            min_amount_out=TokenAmount.from_dict(data["min_amount_out"]),
            hops=tuple(RouteHop.from_dict(h) for h in data["hops"]),
            quoter_block=int(data["quoter_block"]),
            deadline=int(data["deadline"]),
            estimated_gas_usd=Decimal(str(data["estimated_gas_usd"])),
            target_router=str(data["target_router"]),
        )


def to_dict(obj: Any) -> Any:
    """Recursively convert domain objects to json-serializable dicts."""
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        return obj.to_dict()
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, StrEnum):
        return obj.value
    if isinstance(obj, (list, tuple)):
        return [to_dict(x) for x in obj]
    if isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    return obj


def from_dict[T](cls: type[T], data: dict[str, Any]) -> T:
    """Instantiate domain class from dict."""
    factory = getattr(cls, "from_dict", None)
    if callable(factory):
        res: T = factory(data)
        return res
    raise TypeError(f"Class {cls.__name__} does not implement from_dict")
