"""Independent single-hop quote evidence and fixed-block RPC adapter."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Protocol

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode

from arbitrage_contracts.identity import Amount, FeeModel, PoolDescriptor
from arbitrage_contracts.quote import (
    ActorScope,
    DataMode,
    EvidenceLevel,
    GasEvidence,
    GasEvidenceKind,
    QuoteStatus,
)
from arbitrage_contracts.state import StateVersion

_V3_SELECTOR = bytes.fromhex("c6a5026a")
_V4_SELECTOR = bytes.fromhex("aa9d21cb")
_V3_OUTPUT_TYPES = ["uint256", "uint160", "uint32", "uint256"]
_V4_OUTPUT_TYPES = ["uint256", "uint256"]
_UINT256_MAX = (1 << 256) - 1


class SingleHopQuoteInputError(ValueError):
    """Raised when a single-hop quote request cannot be encoded or classified safely."""


class _RpcTransport(Protocol):
    """Minimal read-only JSON-RPC transport contract."""

    def call(
        self,
        method: str,
        params: list[Any] | tuple[Any, ...] | None = None,
        block_identifier: str | None = None,
    ) -> dict[str, Any]:
        """Execute one read-only JSON-RPC call."""


@dataclass(frozen=True, slots=True)
class SingleHopQuoteRequest:
    """Explicit quoter bindings and evidence scope for one adapter."""

    quoter_v3: str
    quoter_v4: str
    data_mode: str = DataMode.LIVE_READONLY
    actor_scope: str = ActorScope.OWN_AUTHORIZED
    source_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("quoter_v3", "quoter_v4"):
            if not _is_address(getattr(self, field_name)):
                raise SingleHopQuoteInputError(f"{field_name} must be an EVM address")


@dataclass(frozen=True, slots=True)
class SingleHopQuoteEvidence:
    """One-direction quote evidence with no route closure and no net profit semantics."""

    quote_id: str
    pool_descriptor: PoolDescriptor
    direction: str
    asset_in: Any
    asset_out: Any
    amount_in: Amount
    amount_out: Amount | None = None
    state_version_ref: str | None = None
    started_at_ms: int | None = None
    finished_at_ms: int | None = None
    status: str = QuoteStatus.QUOTED
    evidence_level: str = EvidenceLevel.RPC_QUOTE
    data_mode: str = DataMode.SYNTHETIC
    actor_scope: str = ActorScope.SYNTHETIC
    fee_included: str = "yes"
    impact_included: str = "yes"
    gas_evidence: GasEvidence | None = None
    error: str | None = None
    source_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.quote_id.strip():
            raise ValueError("quote_id must be a non-empty string")
        if not isinstance(self.pool_descriptor, PoolDescriptor):
            raise TypeError("pool_descriptor must be a PoolDescriptor")
        if self.direction not in ("zero_for_one", "one_for_zero"):
            raise ValueError("direction must be zero_for_one or one_for_zero")
        expected_in = (
            self.pool_descriptor.currency0
            if self.direction == "zero_for_one"
            else self.pool_descriptor.currency1
        )
        expected_out = (
            self.pool_descriptor.currency1
            if self.direction == "zero_for_one"
            else self.pool_descriptor.currency0
        )
        if self.asset_in != expected_in or self.asset_out != expected_out:
            raise ValueError("direction does not match pool descriptor currencies")
        if not isinstance(self.amount_in, Amount):
            raise TypeError("amount_in must be an Amount")
        if self.amount_in.asset_ref != self.asset_in:
            raise ValueError("amount_in does not match asset_in")
        if self.status != QuoteStatus.QUOTED:
            if self.amount_out is not None:
                raise ValueError("amount_out must be None unless status is quoted")
        else:
            if self.amount_out is None:
                raise ValueError("amount_out is required when status is quoted")
            if self.amount_out.asset_ref != self.asset_out:
                raise ValueError("amount_out does not match asset_out")
        if self.evidence_level not in ("rpc_quote", "local_quote"):
            raise ValueError("single-hop evidence cannot claim atomic or confirmed execution")
        if self.data_mode not in (
            "synthetic",
            "historical_replay",
            "live_readonly",
            "confirmed_chain_history",
        ):
            raise ValueError("invalid data_mode")
        if self.data_mode == "synthetic" and self.evidence_level == "confirmed_execution":
            raise ValueError("synthetic evidence cannot claim confirmed execution")
        if self.gas_evidence is not None and self.gas_evidence.gas_kind != (
            GasEvidenceKind.QUOTER_ESTIMATE
        ):
            raise ValueError("single-hop gas evidence must be a quoter estimate")


@dataclass(frozen=True, slots=True)
class SingleHopQuoteResult:
    """A quote result plus its cache key and call count."""

    evidence: SingleHopQuoteEvidence
    cache_key: str
    cache_hit: bool
    call_count: int = 0


def _is_address(value: str) -> bool:
    return (
        type(value) is str
        and value.startswith("0x")
        and len(value) == 42
        and all(character in "0123456789abcdefABCDEF" for character in value[2:])
    )


def _is_bytes32(value: str) -> bool:
    return (
        type(value) is str
        and value.startswith("0x")
        and len(value) == 66
        and all(character in "0123456789abcdefABCDEF" for character in value[2:])
    )


def _validate_pool(descriptor: PoolDescriptor) -> None:
    key = descriptor.key
    if not _is_address(key.venue_address):
        raise SingleHopQuoteInputError("pool venue address is invalid")
    if key.protocol_id == "uniswap_v3":
        if key.venue_kind != "factory" or key.pool_id_kind != "address":
            raise SingleHopQuoteInputError("uniswap_v3 pool identity is incomplete")
        if not _is_address(key.pool_id):
            raise SingleHopQuoteInputError("uniswap_v3 pool address is invalid")
    elif key.protocol_id == "uniswap_v4":
        if key.venue_kind != "manager" or key.pool_id_kind != "bytes32":
            raise SingleHopQuoteInputError("uniswap_v4 pool identity is incomplete")
        if not _is_bytes32(key.pool_id):
            raise SingleHopQuoteInputError("uniswap_v4 pool id is invalid")
        if descriptor.hooks is None or descriptor.tick_spacing is None:
            raise SingleHopQuoteInputError(f"V4 pool {key.pool_id} lacks hooks or tick spacing")
    else:
        raise SingleHopQuoteInputError(f"unsupported protocol: {key.protocol_id!r}")
    if descriptor.deployment_status != "deployed":
        raise SingleHopQuoteInputError(f"pool {key.pool_id} is not deployed")
    fee_model = descriptor.fee_model
    if not isinstance(fee_model, FeeModel) or fee_model.kind != "static":
        raise SingleHopQuoteInputError(f"pool {key.pool_id} lacks static fee evidence")
    fee_raw = fee_model.raw_value
    if type(fee_raw) is not int or isinstance(fee_raw, bool) or not 0 <= fee_raw <= _UINT256_MAX:
        raise SingleHopQuoteInputError(f"pool {key.pool_id} has invalid fee units")


def _asset_address(descriptor: PoolDescriptor, direction: str) -> tuple[str, str]:
    asset_in, asset_out = (
        (descriptor.currency0, descriptor.currency1)
        if direction == "zero_for_one"
        else (descriptor.currency1, descriptor.currency0)
    )
    token_in = asset_in.token_key
    token_out = asset_out.token_key
    if token_in is None or token_out is None:
        raise SingleHopQuoteInputError("native currency is unsupported by this adapter")
    return token_in.address, token_out.address


def encode_single_hop_calldata(
    descriptor: PoolDescriptor,
    direction: str,
    amount_in: int,
) -> str:
    """Encode one direction without changing the canonical V4 PoolKey ordering."""
    _validate_pool(descriptor)
    if type(amount_in) is not int or isinstance(amount_in, bool) or amount_in <= 0:
        raise SingleHopQuoteInputError("amount_in must be a positive integer")
    if direction not in ("zero_for_one", "one_for_zero"):
        raise SingleHopQuoteInputError("invalid single-hop direction")
    token_in_address, token_out_address = _asset_address(descriptor, direction)
    fee_raw = descriptor.fee_model.raw_value
    assert fee_raw is not None
    if descriptor.key.protocol_id == "uniswap_v3":
        encoded = abi_encode(
            ["(address,address,uint256,uint24,uint160)"],
            [(token_in_address, token_out_address, amount_in, fee_raw, 0)],
        )
        return "0x" + (_V3_SELECTOR + encoded).hex()
    tick_spacing = descriptor.tick_spacing
    hooks = descriptor.hooks
    if tick_spacing is None or hooks is None:
        raise SingleHopQuoteInputError("V4 pool identity is incomplete")
    pool_key = (token_in_address, token_out_address, fee_raw, tick_spacing, hooks)
    encoded = abi_encode(
        ["((address,address,uint24,int24,address),bool,uint128,bytes)"],
        [(pool_key, direction == "zero_for_one", amount_in, b"")],
    )
    return "0x" + (_V4_SELECTOR + encoded).hex()


def _decode_output(protocol_id: str, raw_hex: Any) -> tuple[int, int]:
    if type(raw_hex) is not str or not raw_hex.startswith("0x"):
        raise SingleHopQuoteInputError("quote result is not a hex string")
    output_types = _V3_OUTPUT_TYPES if protocol_id == "uniswap_v3" else _V4_OUTPUT_TYPES
    try:
        decoded = abi_decode(output_types, bytes.fromhex(raw_hex[2:]))
    except Exception as exc:
        raise SingleHopQuoteInputError(f"malformed quote ABI response: {exc}") from exc
    amount_out = int(decoded[0])
    gas_estimate = int(decoded[-1])
    if amount_out > _UINT256_MAX or gas_estimate > _UINT256_MAX:
        raise SingleHopQuoteInputError("quote result exceeds uint256")
    return amount_out, gas_estimate


def _classify_error(response: dict[str, Any]) -> QuoteStatus:
    error = response.get("error")
    if not isinstance(error, dict):
        return QuoteStatus.INVALID_RESPONSE
    code = error.get("code")
    data = error.get("data")
    if code == -32602:
        return QuoteStatus.NODE_LIMITATION
    if code == 3 or (type(data) is str and data.startswith("0x") and len(data) >= 10):
        return QuoteStatus.CONTRACT_REVERT
    return QuoteStatus.RPC_ERROR


def _state_version_ref(state: StateVersion) -> str:
    """Bind this observed quote to its exact chain, block hash and cursor."""
    from arbitrage_contracts.state import canonical_state_ref

    return canonical_state_ref(state)


class SingleHopQuoteAdapter:
    """Quote each pool direction independently at one fixed state version."""

    def __init__(self, rpc: _RpcTransport, request: SingleHopQuoteRequest) -> None:
        self._rpc = rpc
        self._request = request
        self._cache: dict[str, SingleHopQuoteEvidence] = {}
        self._last_calls: list[dict[str, Any]] = []

    def quote(
        self,
        descriptor: PoolDescriptor,
        direction: str,
        amount_in: Amount,
        state: StateVersion,
    ) -> SingleHopQuoteResult:
        """Issue one RPC for one direction; never derive the opposite direction."""
        _validate_pool(descriptor)
        if direction not in ("zero_for_one", "one_for_zero"):
            raise SingleHopQuoteInputError("invalid single-hop direction")
        asset_in, asset_out = (
            (descriptor.currency0, descriptor.currency1)
            if direction == "zero_for_one"
            else (descriptor.currency1, descriptor.currency0)
        )
        if amount_in.asset_ref != asset_in:
            raise SingleHopQuoteInputError("amount asset does not match requested direction")
        if (
            type(amount_in.atoms) is not int
            or isinstance(amount_in.atoms, bool)
            or amount_in.atoms <= 0
        ):
            raise SingleHopQuoteInputError("amount_in must be a positive integer")
        if not isinstance(state, StateVersion):
            raise SingleHopQuoteInputError("state must be StateVersion")
        if state.completeness != "ready" or state.block_number <= 0:
            raise SingleHopQuoteInputError("state must be ready with a positive block number")
        if state.chain_id != descriptor.key.chain_id:
            raise SingleHopQuoteInputError("state chain does not match pool")

        cache_key_payload = {
            "adapter": "single_hop_fixed_block_v1",
            "amount_in": amount_in.to_atoms_str(),
            "direction": direction,
            "fee_scope": "pool_included",
            "pool_id": descriptor.key.canonical_pool_id,
            "schema_id": "arbitrage-evidence",
            "state_version_ref": _state_version_ref(state),
        }
        canonical = json.dumps(cache_key_payload, sort_keys=True, separators=(",", ":"))
        cache_key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        cached = self._cache.get(cache_key)
        if cached is not None:
            return SingleHopQuoteResult(evidence=cached, cache_key=cache_key, cache_hit=True)

        block_identifier = hex(state.block_number)
        target = (
            self._request.quoter_v3
            if descriptor.key.protocol_id == "uniswap_v3"
            else self._request.quoter_v4
        )
        calldata = encode_single_hop_calldata(descriptor, direction, amount_in.atoms)
        started_at_ms = int(time.time() * 1000)
        try:
            response = self._rpc.call(
                "eth_call",
                [{"to": target, "data": calldata}, block_identifier],
                block_identifier,
            )
            if "error" in response:
                status = _classify_error(response)
                error = json.dumps(response["error"], sort_keys=True, separators=(",", ":"))
                amount_out = None
                gas_units = None
            else:
                amount_out_atoms, gas_units = _decode_output(
                    descriptor.key.protocol_id,
                    response.get("result"),
                )
                amount_out = Amount(asset_out, amount_out_atoms, amount_in.decimals)
                status = QuoteStatus.QUOTED
                error = None
        except SingleHopQuoteInputError as exc:
            status = (
                QuoteStatus.UNSUPPORTED
                if "unsupported" in str(exc) or "lacks" in str(exc) or "incomplete" in str(exc)
                else QuoteStatus.INVALID_INPUT
            )
            error = str(exc)
            amount_out = None
            gas_units = None
        except Exception as exc:
            status = QuoteStatus.RPC_ERROR
            error = str(exc)
            amount_out = None
            gas_units = None

        evidence = SingleHopQuoteEvidence(
            quote_id=f"single-hop:{cache_key}",
            pool_descriptor=descriptor,
            direction=direction,
            asset_in=asset_in,
            asset_out=asset_out,
            amount_in=amount_in,
            amount_out=amount_out,
            state_version_ref=_state_version_ref(state),
            started_at_ms=started_at_ms,
            finished_at_ms=int(time.time() * 1000),
            status=status,
            evidence_level=EvidenceLevel.RPC_QUOTE,
            data_mode=self._request.data_mode,
            actor_scope=self._request.actor_scope,
            fee_included="yes",
            impact_included="yes",
            gas_evidence=GasEvidence(
                gas_kind=GasEvidenceKind.QUOTER_ESTIMATE,
                gas_units=gas_units,
                source_refs=self._request.source_refs or ("single_hop_adapter:quoter",),
            ),
            error=error,
            source_refs=self._request.source_refs,
        )
        self._cache[cache_key] = evidence
        self._last_calls.append(
            {
                "method": "eth_call",
                "params": [{"to": target, "data": calldata}, block_identifier],
                "block_identifier": block_identifier,
            }
        )
        return SingleHopQuoteResult(
            evidence=evidence,
            cache_key=cache_key,
            cache_hit=False,
            call_count=1,
        )

    def last_calls(self) -> list[dict[str, Any]]:
        """Return metadata from calls issued by this adapter instance."""
        return list(self._last_calls)
