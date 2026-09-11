"""Restricted read-only simulation transport layer and deterministic offline replay.

Conforms to W5-E specifications (C13, C14, C15, C17):
- Prohibits transactions, signing, and state modifications;
- Enforces exact-match deterministic replay keys without fuzzy or wildcard matching;
- Defends against state overrides and native ETH value leakage.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arbitrage_contracts.identity import (
    validate_bytes32,
    validate_evm_address,
    validate_non_negative_integer,
    validate_positive_integer,
)


class TransportError(Exception):
    """Base exception for all transport-level errors."""


class TransportSecurityError(TransportError):
    """Security invariant violation at the transport boundary."""


class StateOverrideProhibitedError(TransportSecurityError):
    """Raised when a state override is provided under C13."""


class NativeValueProhibitedError(TransportSecurityError):
    """Raised when non-zero native ETH value is provided under C13."""


class TransportKeyNotFoundError(TransportError):
    """Raised when an exact replay key is not found in deterministic transport."""


class TransportWildcardForbiddenError(TransportError):
    """Raised when a wildcard, fuzzy pattern, or missing field is used in replay key."""


class TransportRpcError(TransportError):
    """Simulated or real RPC communication failure."""

    def __init__(
        self,
        message: str,
        error_type: str = "GENERIC_RPC_ERROR",
        code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.code = code


class TransportNodeLimitationError(TransportError):
    """Simulated or real node limitation failure."""

    def __init__(
        self,
        message: str,
        limitation_type: str = "UNSUPPORTED_METHOD",
        code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.limitation_type = limitation_type
        self.code = code


class TransportContractRevertError(TransportError):
    """Raised when an on-chain contract execution reverts."""

    def __init__(
        self,
        message: str,
        revert_data_hex: str = "0x",
        revert_reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.revert_data_hex = revert_data_hex
        self.revert_reason = revert_reason


@dataclass(frozen=True, slots=True)
class ExactReplayKey:
    """Rigid 7-tuple replay key for deterministic simulation lookup per C13/C15."""

    chain_id: int
    block_number: int
    block_hash: str
    from_address: str
    to_address: str
    value_wei: int
    calldata_sha256: str


def make_exact_replay_key(
    chain_id: int,
    block_number: int,
    block_hash: str,
    from_address: str,
    to_address: str,
    value_wei: int,
    calldata_sha256: str,
) -> ExactReplayKey:
    """Construct and strictly validate an ExactReplayKey without wildcard tolerance."""
    string_candidates = [
        ("block_hash", block_hash),
        ("from_address", from_address),
        ("to_address", to_address),
        ("calldata_sha256", calldata_sha256),
    ]
    for param_name, string_value in string_candidates:
        if not isinstance(string_value, str):
            raise TransportWildcardForbiddenError(
                f"{param_name} must be a string, got {type(string_value).__name__}"
            )
        stripped = string_value.strip()
        if not stripped:
            raise TransportWildcardForbiddenError(f"{param_name} must not be empty or blank")
        if any(char in stripped for char in ("*", "?", "%")) or stripped.upper() in (
            "ANY",
            "LATEST",
            "PENDING",
            "EARLIEST",
        ):
            raise TransportWildcardForbiddenError(
                f"Wildcard or fuzzy token forbidden in {param_name}: {stripped}"
            )

    validated_chain_id = validate_positive_integer(chain_id, "chain_id")
    validated_block_number = validate_non_negative_integer(block_number, "block_number")
    validated_block_hash = validate_bytes32(block_hash).lower()
    validated_from = validate_evm_address(from_address)
    validated_to = validate_evm_address(to_address)
    validated_value = validate_non_negative_integer(value_wei, "value_wei")

    if validated_value != 0:
        raise NativeValueProhibitedError(
            f"value_wei must be 0 under C13; Native ETH is strictly forbidden, got {validated_value}"
        )

    clean_sha256 = calldata_sha256.strip().lower()
    if len(clean_sha256) != 64 or any(c not in "0123456789abcdef" for c in clean_sha256):
        raise TransportWildcardForbiddenError(
            f"calldata_sha256 must be a 64-character lowercase hex string, got {calldata_sha256}"
        )

    return ExactReplayKey(
        chain_id=validated_chain_id,
        block_number=validated_block_number,
        block_hash=validated_block_hash,
        from_address=validated_from,
        to_address=validated_to,
        value_wei=validated_value,
        calldata_sha256=clean_sha256,
    )


@dataclass(frozen=True, slots=True)
class SimulationCallRequest:
    """Read-only eth_call request envelope."""

    chain_id: int
    to_address: str
    from_address: str
    calldata_hex: str
    calldata_sha256: str
    block_number: int
    block_hash: str
    value_wei: int = 0
    state_override: Mapping[str, Any] | None = None

    def __init__(
        self,
        chain_id: int,
        to_address: str,
        from_address: str,
        calldata_hex: str,
        calldata_sha256: str,
        block_number: int,
        block_hash: str,
        value_wei: int = 0,
        state_override: Mapping[str, Any] | None = None,
    ) -> None:
        validated_chain_id = validate_positive_integer(chain_id, "chain_id")
        validated_to = validate_evm_address(to_address)
        validated_from = validate_evm_address(from_address)
        validated_block_number = validate_non_negative_integer(block_number, "block_number")
        validated_block_hash = validate_bytes32(block_hash).lower()
        validated_value = validate_non_negative_integer(value_wei, "value_wei")

        if type(calldata_hex) is not str or not calldata_hex.startswith("0x"):
            raise ValueError("calldata_hex must be a 0x-prefixed hex string")
        if len(calldata_hex) % 2 != 0:
            raise ValueError("calldata_hex must have even length")

        clean_sha256 = calldata_sha256.strip().lower()
        if len(clean_sha256) != 64:
            raise ValueError("calldata_sha256 must be 64 hex characters")

        object.__setattr__(self, "chain_id", validated_chain_id)
        object.__setattr__(self, "to_address", validated_to)
        object.__setattr__(self, "from_address", validated_from)
        object.__setattr__(self, "calldata_hex", calldata_hex)
        object.__setattr__(self, "calldata_sha256", clean_sha256)
        object.__setattr__(self, "block_number", validated_block_number)
        object.__setattr__(self, "block_hash", validated_block_hash)
        object.__setattr__(self, "value_wei", validated_value)
        object.__setattr__(self, "state_override", state_override)


@dataclass(frozen=True, slots=True)
class SimulationCallResponse:
    """Read-only eth_call simulation response envelope."""

    status: str
    return_data_hex: str = "0x"
    gas_used: int | None = None
    error_message: str | None = None
    revert_data_hex: str | None = None
    rpc_error_type: str | None = None
    node_limitation_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize response to dictionary."""
        return {
            "status": self.status,
            "return_data_hex": self.return_data_hex,
            "gas_used": self.gas_used,
            "error_message": self.error_message,
            "revert_data_hex": self.revert_data_hex,
            "rpc_error_type": self.rpc_error_type,
            "node_limitation_type": self.node_limitation_type,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SimulationCallResponse:
        """Construct response from dictionary mapping."""
        return cls(
            status=str(data["status"]),
            return_data_hex=str(data.get("return_data_hex", "0x")),
            gas_used=int(data["gas_used"]) if data.get("gas_used") is not None else None,
            error_message=data.get("error_message"),
            revert_data_hex=data.get("revert_data_hex"),
            rpc_error_type=data.get("rpc_error_type"),
            node_limitation_type=data.get("node_limitation_type"),
        )


@dataclass(frozen=True, slots=True)
class SimulationReplayRecord:
    """Deterministic fixture replay entry."""

    key: ExactReplayKey
    response: SimulationCallResponse
    router_balances: Mapping[str, int] | None = None
    post_call_block_hash: str | None = None


class BaseSimulationTransport(ABC):
    """Abstract pure read-only simulation transport interface.

    Strictly devoid of transaction broadcasting, wallet signing, or state mutation methods.
    """

    @abstractmethod
    def simulate_call(self, request: SimulationCallRequest) -> SimulationCallResponse:
        """Execute read-only eth_call simulation."""
        raise NotImplementedError

    @abstractmethod
    def get_block_hash(self, chain_id: int, block_number: int) -> str:
        """Query block hash for fixed block consistency and reorg detection."""
        raise NotImplementedError

    @abstractmethod
    def get_router_token_balance(
        self, chain_id: int, router_address: str, token_address: str, block_number: int
    ) -> int:
        """Query token balance held by the router contract for C17 subsidy verification."""
        raise NotImplementedError

    def __getattr__(self, name: str) -> Any:
        """Guard against any write or signing methods."""
        forbidden_operations = (
            "send_transaction",
            "send_raw_transaction",
            "sign_transaction",
            "sign",
            "broadcast",
            "transact",
        )
        if any(forbidden in name.lower() for forbidden in forbidden_operations):
            raise AttributeError(
                f"BaseSimulationTransport is strictly read-only; {name} is forbidden"
            )
        raise AttributeError(f"{type(self).__name__} has no attribute {name!r}")


class DeterministicSimulationTransport(BaseSimulationTransport):
    """Pure deterministic offline replay transport.

    Matches requests strictly by ExactReplayKey. Prohibits any wildcard or fuzzy matching.
    """

    def __init__(
        self,
        records: Mapping[ExactReplayKey, SimulationReplayRecord] | None = None,
        block_hashes: Mapping[tuple[int, int], str] | None = None,
        router_balances: Mapping[tuple[int, str, str, int], int] | None = None,
        post_call_block_hashes: Mapping[tuple[int, int], str] | None = None,
    ) -> None:
        self._records: dict[ExactReplayKey, SimulationReplayRecord] = {}
        self._block_hashes: dict[tuple[int, int], str] = (
            dict(block_hashes) if block_hashes is not None else {}
        )
        self._router_balances: dict[tuple[int, str, str, int], int] = (
            dict(router_balances) if router_balances is not None else {}
        )
        self._post_call_block_hashes: dict[tuple[int, int], str] = (
            dict(post_call_block_hashes) if post_call_block_hashes is not None else {}
        )
        self._activated_reorg_blocks: set[tuple[int, int]] = set()
        self._call_history: list[SimulationCallRequest] = []

        if records is not None:
            for record in records.values():
                self.register_record(record)

    @property
    def call_history(self) -> tuple[SimulationCallRequest, ...]:
        """Return audit trail of simulated call requests."""
        return tuple(self._call_history)

    def register_record(self, record: SimulationReplayRecord) -> None:
        """Register a single simulation replay record."""
        self._records[record.key] = record
        block_key = (record.key.chain_id, record.key.block_number)
        self._block_hashes[block_key] = record.key.block_hash

        if record.post_call_block_hash is not None:
            self._post_call_block_hashes[block_key] = validate_bytes32(
                record.post_call_block_hash
            ).lower()

        if record.router_balances is not None:
            for token_raw, balance_raw in record.router_balances.items():
                token_norm = validate_evm_address(token_raw)
                bal_key = (
                    record.key.chain_id,
                    record.key.to_address,
                    token_norm,
                    record.key.block_number,
                )
                self._router_balances[bal_key] = validate_non_negative_integer(
                    balance_raw, "balance"
                )

    def register_case(self, case_data: Mapping[str, Any]) -> ExactReplayKey:
        """Register a fixture case mapping into deterministic replay storage."""
        chain_id = int(case_data["chain_id"])
        block_number = int(case_data["block_number"])
        block_hash = str(case_data["block_hash"])
        from_address = str(case_data["from_address"])
        to_address = str(case_data["to_address"])
        value_wei = int(case_data.get("value_wei", 0))
        calldata_sha256 = str(case_data["calldata_sha256"])

        key = make_exact_replay_key(
            chain_id=chain_id,
            block_number=block_number,
            block_hash=block_hash,
            from_address=from_address,
            to_address=to_address,
            value_wei=value_wei,
            calldata_sha256=calldata_sha256,
        )

        resp_raw = case_data.get("response")
        if isinstance(resp_raw, Mapping):
            response = SimulationCallResponse.from_dict(resp_raw)
        else:
            response = SimulationCallResponse(
                status=str(case_data.get("status", "CALL_SUCCEEDED")),
                return_data_hex=str(case_data.get("return_data_hex", "0x")),
                gas_used=case_data.get("gas_used"),
                error_message=case_data.get("error_message"),
                revert_data_hex=case_data.get("revert_data_hex"),
                rpc_error_type=case_data.get("rpc_error_type"),
                node_limitation_type=case_data.get("node_limitation_type"),
            )

        router_balances = case_data.get("router_balances")
        post_call_hash = case_data.get("post_call_block_hash")

        record = SimulationReplayRecord(
            key=key,
            response=response,
            router_balances=router_balances if isinstance(router_balances, Mapping) else None,
            post_call_block_hash=str(post_call_hash) if post_call_hash else None,
        )
        self.register_record(record)
        return key

    def load_fixture_cases(self, cases: Sequence[Mapping[str, Any]]) -> int:
        """Register a sequence of fixture cases into replay storage."""
        count = 0
        for case in cases:
            self.register_case(case)
            count += 1
        return count

    def load_fixture_file(self, file_path: str | Path) -> int:
        """Read a JSONL fixture file and register all replay cases."""
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"Fixture file not found: {path}")

        count = 0
        with path.open("r", encoding="utf-8") as fixture_stream:
            for line_number, raw_line in enumerate(fixture_stream, start=1):
                clean_line = raw_line.strip()
                if not clean_line or clean_line.startswith("#"):
                    continue
                parsed = json.loads(clean_line)
                if not isinstance(parsed, dict):
                    raise ValueError(f"Line {line_number} in {path} must be a JSON object")
                if "chain_id" in parsed and "calldata_sha256" in parsed and "response" in parsed:
                    try:
                        self.register_case(parsed)
                        count += 1
                    except (TransportError, ValueError):
                        continue
        return count

    def set_block_hash(self, chain_id: int, block_number: int, block_hash: str) -> None:
        """Set or update block hash mapping."""
        validated_chain_id = validate_positive_integer(chain_id, "chain_id")
        validated_block = validate_non_negative_integer(block_number, "block_number")
        validated_hash = validate_bytes32(block_hash).lower()
        self._block_hashes[(validated_chain_id, validated_block)] = validated_hash

    def simulate_reorg(self, chain_id: int, block_number: int, mutated_block_hash: str) -> None:
        """Configure post-call block hash mutation to simulate block reorganization under C14."""
        validated_chain_id = validate_positive_integer(chain_id, "chain_id")
        validated_block = validate_non_negative_integer(block_number, "block_number")
        validated_hash = validate_bytes32(mutated_block_hash).lower()
        self._post_call_block_hashes[(validated_chain_id, validated_block)] = validated_hash

    def set_router_token_balance(
        self,
        chain_id: int,
        router_address: str,
        token_address: str,
        block_number: int,
        balance: int,
    ) -> None:
        """Set token balance for router at specified block."""
        validated_chain_id = validate_positive_integer(chain_id, "chain_id")
        validated_router = validate_evm_address(router_address)
        validated_token = validate_evm_address(token_address)
        validated_block = validate_non_negative_integer(block_number, "block_number")
        validated_bal = validate_non_negative_integer(balance, "balance")
        self._router_balances[
            (validated_chain_id, validated_router, validated_token, validated_block)
        ] = validated_bal

    def simulate_call(self, request: SimulationCallRequest) -> SimulationCallResponse:
        """Execute rigid deterministic eth_call simulation.

        Raises StateOverrideProhibitedError if state_override is supplied.
        Raises NativeValueProhibitedError if value_wei != 0.
        Raises TransportKeyNotFoundError if exact key is not registered.
        """
        if request.state_override is not None and len(request.state_override) > 0:
            raise StateOverrideProhibitedError(
                "State overrides are strictly prohibited under C13 to prevent counterfeit solvency"
            )
        if request.value_wei != 0:
            raise NativeValueProhibitedError(
                f"value_wei must be 0 under C13; Native ETH is strictly forbidden, got {request.value_wei}"
            )

        exact_key = make_exact_replay_key(
            chain_id=request.chain_id,
            block_number=request.block_number,
            block_hash=request.block_hash,
            from_address=request.from_address,
            to_address=request.to_address,
            value_wei=request.value_wei,
            calldata_sha256=request.calldata_sha256,
        )

        record = self._records.get(exact_key)
        if record is None:
            raise TransportKeyNotFoundError(
                f"Exact replay record not found for key: {exact_key}. "
                "Fuzzy and wildcard matches are strictly forbidden under C13/C15."
            )

        self._call_history.append(request)

        block_key = (request.chain_id, request.block_number)
        if block_key in self._post_call_block_hashes:
            self._activated_reorg_blocks.add(block_key)

        return record.response

    def get_block_hash(self, chain_id: int, block_number: int) -> str:
        """Return block hash, returning post-call mutated hash if reorg was triggered."""
        validated_chain_id = validate_positive_integer(chain_id, "chain_id")
        validated_block = validate_non_negative_integer(block_number, "block_number")
        block_key = (validated_chain_id, validated_block)

        if block_key in self._activated_reorg_blocks:
            return self._post_call_block_hashes[block_key]

        if block_key in self._block_hashes:
            return self._block_hashes[block_key]

        raise TransportKeyNotFoundError(
            f"No block hash registered for chain {validated_chain_id} at block {validated_block}"
        )

    def get_router_token_balance(
        self, chain_id: int, router_address: str, token_address: str, block_number: int
    ) -> int:
        """Return router token balance atoms for C17 inventory subsidy check."""
        validated_chain_id = validate_positive_integer(chain_id, "chain_id")
        validated_router = validate_evm_address(router_address)
        validated_token = validate_evm_address(token_address)
        validated_block = validate_non_negative_integer(block_number, "block_number")
        matches = [
            value
            for key, value in self._router_balances.items()
            if key[0] == validated_chain_id
            and key[3] == validated_block
            and key[1].lower() == validated_router.lower()
            and key[2].lower() == validated_token.lower()
        ]
        if not matches or any(value != matches[0] for value in matches):
            raise TransportNodeLimitationError("Missing or conflicting router balance evidence")
        return validate_non_negative_integer(matches[0], "balance")
