"""Fixed-block quote adapter for contract-typed routes."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Protocol

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode

from arbitrage_contracts.identity import Amount, FeeModel
from arbitrage_contracts.quote import (
    ActorScope,
    DataMode,
    EvidenceLevel,
    GasEvidence,
    GasEvidenceKind,
    HopQuote,
    HopRef,
    QuoteEvidence,
    QuoteStatus,
    RouteRef,
    TriState,
)
from arbitrage_contracts.state import StateVersion

_V3_SELECTOR = bytes.fromhex("c6a5026a")
_V4_SELECTOR = bytes.fromhex("aa9d21cb")
_V3_OUTPUT_TYPES = ["uint256", "uint160", "uint32", "uint256"]
_V4_OUTPUT_TYPES = ["uint256", "uint256"]
_UINT256_MAX = (1 << 256) - 1


class QuoteAdapterInputError(ValueError):
    """Raised when route, amount, state, or pool identity cannot be quoted safely."""


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
class QuoteAdapterRequest:
    """Explicit metadata and quoter bindings for a quote adapter."""

    quoter_v3: str
    quoter_v4: str
    data_mode: str = DataMode.LIVE_READONLY
    actor_scope: str = ActorScope.OWN_AUTHORIZED
    source_refs: tuple[str, ...] = ()
    run_baseline_ref: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("quoter_v3", "quoter_v4"):
            value = getattr(self, field_name)
            if not _is_address(value):
                raise QuoteAdapterInputError(f"{field_name} must be an EVM address")


@dataclass(frozen=True, slots=True)
class QuoteResult:
    """Adapter result containing evidence and the cache key that produced it."""

    evidence: QuoteEvidence
    cache_key: str
    cache_hit: bool
    call_count: int = 0


def _is_hex_character(value: str) -> bool:
    return value in "0123456789abcdefABCDEF"


def _is_address(value: str) -> bool:
    return (
        type(value) is str
        and value.startswith("0x")
        and len(value) == 42
        and all(_is_hex_character(character) for character in value[2:])
    )


def _is_bytes32(value: str) -> bool:
    return (
        type(value) is str
        and value.startswith("0x")
        and len(value) == 66
        and all(_is_hex_character(character) for character in value[2:])
    )


def _fee_raw(hop: HopRef) -> int:
    descriptor = hop.pool_descriptor
    if descriptor is None or not isinstance(descriptor.fee_model, FeeModel):
        raise QuoteAdapterInputError(f"hop {hop.pool_key.pool_id} lacks static fee evidence")
    fee_model = descriptor.fee_model
    if fee_model.kind == "dynamic":
        raise QuoteAdapterInputError(f"hop {hop.pool_key.pool_id} uses unsupported dynamic fee")
    if fee_model.kind != "static" or type(fee_model.raw_value) is not int:
        raise QuoteAdapterInputError(f"hop {hop.pool_key.pool_id} lacks static fee value")
    if fee_model.raw_value < 0 or fee_model.raw_value > _UINT256_MAX:
        raise QuoteAdapterInputError(f"hop {hop.pool_key.pool_id} has invalid fee units")
    return fee_model.raw_value


def _validate_pool_mapping(hop: HopRef) -> None:
    pool = hop.pool_key
    if not _is_address(pool.venue_address):
        raise QuoteAdapterInputError("pool venue address is invalid")
    if pool.protocol_id == "uniswap_v3":
        if pool.venue_kind != "factory" or pool.pool_id_kind != "address":
            raise QuoteAdapterInputError("uniswap_v3 pool identity is incomplete")
        if not _is_address(pool.pool_id):
            raise QuoteAdapterInputError("uniswap_v3 pool address is invalid")
    elif pool.protocol_id == "uniswap_v4":
        if pool.venue_kind != "manager" or pool.pool_id_kind != "bytes32":
            raise QuoteAdapterInputError("uniswap_v4 pool identity is incomplete")
        if not _is_bytes32(pool.pool_id):
            raise QuoteAdapterInputError("uniswap_v4 pool id is invalid")
    else:
        raise QuoteAdapterInputError(f"unsupported protocol: {pool.protocol_id!r}")

    descriptor = hop.pool_descriptor
    if descriptor is None:
        raise QuoteAdapterInputError(f"hop {pool.pool_id} pool mapping incomplete")
    if descriptor.key != pool:
        raise QuoteAdapterInputError("pool descriptor key does not match HopRef")
    expected_in, expected_out = (
        (descriptor.currency0, descriptor.currency1)
        if hop.direction == "zero_for_one"
        else (descriptor.currency1, descriptor.currency0)
    )
    if hop.asset_in != expected_in or hop.asset_out != expected_out:
        raise QuoteAdapterInputError(f"hop {pool.pool_id} direction does not match descriptor")
    if pool.protocol_id == "uniswap_v4":
        if descriptor.hooks is None:
            raise QuoteAdapterInputError(f"V4 hop {pool.pool_id} lacks hook evidence")
        if not _is_address(descriptor.hooks):
            raise QuoteAdapterInputError(f"V4 hop {pool.pool_id} has invalid hooks")
        if descriptor.tick_spacing is None:
            raise QuoteAdapterInputError(f"V4 hop {pool.pool_id} lacks tick spacing")
    if descriptor.deployment_status != "deployed":
        raise QuoteAdapterInputError(f"pool {pool.pool_id} is not deployed")
    _fee_raw(hop)


def _encode_calldata(hop: HopRef, amount_in: int) -> str:
    descriptor = hop.pool_descriptor
    if descriptor is None:
        raise QuoteAdapterInputError("pool descriptor is required")
    token_in = hop.asset_in.token_key
    token_out = hop.asset_out.token_key
    if token_in is None or token_out is None:
        raise QuoteAdapterInputError("native currency is unsupported by this adapter")
    fee_raw = _fee_raw(hop)
    if hop.pool_key.protocol_id == "uniswap_v3":
        encoded = abi_encode(
            ["(address,address,uint256,uint24,uint160)"],
            [(token_in.address, token_out.address, amount_in, fee_raw, 0)],
        )
        return "0x" + (_V3_SELECTOR + encoded).hex()
    tick_spacing = descriptor.tick_spacing
    hooks = descriptor.hooks
    if type(tick_spacing) is not int or hooks is None:
        raise QuoteAdapterInputError("V4 pool identity is incomplete")
    encoded = abi_encode(
        ["((address,address,uint24,int24,address),bool,uint128,bytes)"],
        [
            (
                (token_in.address, token_out.address, fee_raw, tick_spacing, hooks),
                hop.direction == "zero_for_one",
                amount_in,
                b"",
            )
        ],
    )
    return "0x" + (_V4_SELECTOR + encoded).hex()


def _decode_output(hop: HopRef, raw_hex: Any) -> tuple[int, int]:
    if type(raw_hex) is not str or not raw_hex.startswith("0x"):
        raise QuoteAdapterInputError("quote result is not a hex string")
    output_types = (
        _V3_OUTPUT_TYPES if hop.pool_key.protocol_id == "uniswap_v3" else _V4_OUTPUT_TYPES
    )
    try:
        decoded = abi_decode(output_types, bytes.fromhex(raw_hex[2:]))
    except Exception as exc:
        raise QuoteAdapterInputError(f"malformed quote ABI response: {exc}") from exc
    amount_out = int(decoded[0])
    gas_estimate = int(decoded[-1])
    if amount_out > _UINT256_MAX or gas_estimate > _UINT256_MAX:
        raise QuoteAdapterInputError("quote result exceeds uint256")
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


class _RecordingTransport:
    """Transport decorator that records exact RPC request parameters."""

    def __init__(self, rpc: _RpcTransport) -> None:
        self._rpc = rpc
        self.calls: list[dict[str, Any]] = []

    def call(
        self,
        method: str,
        params: list[Any] | tuple[Any, ...] | None = None,
        block_identifier: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"method": method, "params": params, "block": block_identifier})
        return self._rpc.call(method, params, block_identifier)


def _quote_incomplete_pool_mapping(hop: HopRef) -> bool:
    pool = hop.pool_key
    descriptor = hop.pool_descriptor
    if descriptor is None or descriptor.key != pool:
        return True
    if pool.protocol_id == "uniswap_v4" and (
        descriptor.hooks is None or descriptor.tick_spacing is None
    ):
        return True
    return False


class FixedBlockQuoteAdapter:
    """Quote exact contract routes at one verified state version."""

    def __init__(self, rpc: _RpcTransport, request: QuoteAdapterRequest) -> None:
        self._rpc = rpc
        self._request = request
        self._cache: dict[str, QuoteEvidence] = {}
        self._last_calls: list[dict[str, Any]] = []

    def quote_route(
        self,
        route: RouteRef,
        amount_in: Amount,
        state: StateVersion,
    ) -> QuoteResult:
        """Quote a closed route at the exact supplied state and classify failures."""
        if amount_in.asset_ref != route.base_asset:
            raise QuoteAdapterInputError("amount asset does not match route base asset")
        if (
            type(amount_in.atoms) is not int
            or isinstance(amount_in.atoms, bool)
            or amount_in.atoms <= 0
        ):
            raise QuoteAdapterInputError("amount_in must be a positive integer")
        if not isinstance(state, StateVersion):
            raise QuoteAdapterInputError("state must be StateVersion")
        if state.completeness != "ready" or state.block_number <= 0:
            raise QuoteAdapterInputError("state must be ready with a positive block number")
        if state.chain_id != route.chain_id:
            raise QuoteAdapterInputError("state chain does not match route")

        cache_key_payload = {
            "adapter": "fixed_block_v1",
            "amount_in": amount_in.to_atoms_str(),
            "fee_scope": "pool_included",
            "route_id": route.route_id,
            "schema_id": "arbitrage-evidence",
            "state_version_ref": _state_version_ref(state),
        }
        canonical = json.dumps(cache_key_payload, sort_keys=True, separators=(",", ":"))
        cache_key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        cached = self._cache.get(cache_key)
        if cached is not None:
            return QuoteResult(evidence=cached, cache_key=cache_key, cache_hit=True, call_count=0)

        transport = _RecordingTransport(self._rpc)
        block_identifier = hex(state.block_number)
        started_at_ms = int(time.time() * 1000)
        hop_quotes: list[HopQuote] = []
        current_amount = amount_in
        status = QuoteStatus.QUOTED
        error: str | None = None
        total_gas = 0

        for hop_index, hop in enumerate(route.hops):
            try:
                _validate_pool_mapping(hop)
                if _quote_incomplete_pool_mapping(hop):
                    raise QuoteAdapterInputError(
                        f"hop {hop.pool_key.pool_id} pool mapping incomplete"
                    )
                if hop.asset_in != current_amount.asset_ref:
                    raise QuoteAdapterInputError("hop input does not match previous output")
                if hop.pool_key.protocol_id == "uniswap_v3":
                    target = self._request.quoter_v3
                else:
                    target = self._request.quoter_v4
                response = transport.call(
                    "eth_call",
                    [
                        {"to": target, "data": _encode_calldata(hop, current_amount.atoms)},
                        block_identifier,
                    ],
                    block_identifier,
                )
                if "error" in response:
                    status = _classify_error(response)
                    error = json.dumps(response["error"], sort_keys=True, separators=(",", ":"))
                    break
                amount_out_atoms, gas_estimate = _decode_output(hop, response.get("result"))
                previous_amount = current_amount
                current_amount = Amount(hop.asset_out, amount_out_atoms, previous_amount.decimals)
                total_gas += gas_estimate
                hop_quotes.append(
                    HopQuote(
                        hop_index,
                        hop.pool_key,
                        hop.asset_in,
                        hop.asset_out,
                        previous_amount,
                        current_amount,
                        fee_model=hop.pool_descriptor.fee_model if hop.pool_descriptor else None,
                        gas_estimate=gas_estimate,
                    )
                )
            except QuoteAdapterInputError as exc:
                status = (
                    QuoteStatus.UNSUPPORTED
                    if "unsupported" in str(exc) or "incomplete" in str(exc)
                    else QuoteStatus.INVALID_INPUT
                )
                error = str(exc)
                break
            except Exception as exc:
                status = QuoteStatus.RPC_ERROR
                error = str(exc)
                break

        amount_out = current_amount if status == QuoteStatus.QUOTED else None
        delta_atoms = amount_out.atoms - amount_in.atoms if amount_out is not None else None
        state_ref = _state_version_ref(state)
        evidence = QuoteEvidence(
            quote_id=f"fixed-block:{cache_key}",
            route_ref=route,
            amount_in=amount_in,
            amount_out=amount_out,
            delta_atoms=delta_atoms,
            hop_quotes=tuple(hop_quotes),
            state_version_ref=state_ref,
            started_at_ms=started_at_ms,
            finished_at_ms=int(time.time() * 1000),
            status=status,
            evidence_level=EvidenceLevel.RPC_QUOTE,
            data_mode=self._request.data_mode,
            actor_scope=self._request.actor_scope,
            fee_included=TriState.YES,
            impact_included=TriState.YES,
            gas_evidence=GasEvidence(
                gas_kind=GasEvidenceKind.QUOTER_ESTIMATE,
                gas_units=total_gas,
                source_refs=self._request.source_refs or ("quote_adapter:quoter",),
            ),
            error=error,
            source_refs=self._request.source_refs,
            run_baseline_ref=self._request.run_baseline_ref,
        )
        self._cache[cache_key] = evidence
        self._last_calls = transport.calls
        return QuoteResult(
            evidence=evidence, cache_key=cache_key, cache_hit=False, call_count=len(transport.calls)
        )

    def last_calls(self) -> list[dict[str, Any]]:
        """Return call metadata from the most recent uncached quote attempt."""
        return list(self._last_calls)
