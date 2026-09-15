"""Offline execution slot coordinator with explicit valuation and floor checks.

RESEARCH ONLY:
- Offline reservation coordinator for strategy and cost modeling.
- Valuation evidence is caller-supplied and strictly offline; not connected to live RPC.
- Does not construct PoolDescriptor or sign/broadcast live transactions.
- Zero float: all financial scaling and floor gates use exact Decimal, Fraction, and integer atoms.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Any

from research.reservation_ledger import (
    ExecutionLatched,
    FundsLedger,
    IdentityMismatchError,
    IntentIdentity,
    LedgerError,
    _validate_hex,
)

__all__ = [
    "AmountLimitExceededError",
    "CoordinatorError",
    "ExecutionCoordinator",
    "ExecutionLatched",
    "FloorViolationError",
    "IdentityMismatchError",
    "ReservationRequest",
    "ReservedExecution",
    "SlotVerificationError",
    "ValuationError",
    "ValuationEvidence",
]


class CoordinatorError(LedgerError):
    """Base error for execution coordinator validation and runtime failures."""


class ValuationError(CoordinatorError):
    """Invalid or unverified valuation evidence."""


class FloorViolationError(CoordinatorError):
    """Output floor does not cover principal plus gas and 1 atom profit."""


class AmountLimitExceededError(CoordinatorError):
    """Trade notional exceeds hard maximum (500 USD)."""


class SlotVerificationError(CoordinatorError, ExecutionLatched):
    """Slot does not match persisted intent state or atomic expected identity."""


def _validate_val_decimal(val: Any, name: str, *, positive: bool = False) -> Decimal:
    """Validate valuation decimal rejecting bools, non-finite values, and non-positive numbers."""
    if isinstance(val, bool) or type(val) is not Decimal:
        raise ValuationError(f"{name} must be a Decimal, got {type(val).__name__}")
    if not val.is_finite():
        raise ValuationError(f"{name} must be finite")
    if positive and val <= Decimal(0):
        raise ValuationError(f"{name} must be strictly positive")
    return val


def _validate_coord_decimal(
    val: Any,
    name: str,
    *,
    positive: bool = False,
    non_negative: bool = False,
) -> Decimal:
    """Validate coordinator decimal rejecting bools, non-finite values, and invalid signs."""
    if isinstance(val, bool) or type(val) is not Decimal:
        raise CoordinatorError(f"{name} must be a Decimal, got {type(val).__name__}")
    if not val.is_finite():
        raise CoordinatorError(f"{name} must be finite")
    if positive and val <= Decimal(0):
        raise CoordinatorError(f"{name} must be strictly positive")
    if non_negative and val < Decimal(0):
        raise CoordinatorError(f"{name} must be non-negative and finite")
    return val


@dataclass(frozen=True)
class ValuationEvidence:
    """Caller-supplied offline valuation evidence.

    Explicitly models price, gas cost, and slippage bounds.
    Does not authenticate on-chain pool state or route validity.
    """

    base_price_usd: Decimal
    gas_usd: Decimal
    slippage_bps: int

    def __post_init__(self) -> None:
        _validate_val_decimal(self.base_price_usd, "base_price_usd", positive=True)
        _validate_val_decimal(self.gas_usd, "gas_usd", positive=True)

        if type(self.slippage_bps) is not int or isinstance(self.slippage_bps, bool):
            raise ValuationError(
                f"slippage_bps must be an integer, got {type(self.slippage_bps).__name__}"
            )
        if self.slippage_bps < 1 or self.slippage_bps > 500:
            raise ValuationError(
                f"slippage_bps must be between 1 and 500 bps, got {self.slippage_bps}"
            )


@dataclass(frozen=True)
class ReservationRequest:
    """Explicit reservation request parameters.

    Strictly typed fields with uint256 bounds and token decimal limits.
    """

    plan_id: str
    chain_id: int
    wallet_address: str
    base_symbol: str
    decimals: int
    amount_in_atoms: int
    minimum_output_atoms: int
    nonce: int

    def __post_init__(self) -> None:
        if not isinstance(self.plan_id, str) or not self.plan_id.strip():
            raise CoordinatorError("plan_id must be a non-empty string")

        if type(self.chain_id) is not int or isinstance(self.chain_id, bool):
            raise CoordinatorError(f"chain_id must be an integer, got {type(self.chain_id).__name__}")
        if self.chain_id < 1 or self.chain_id >= 2**256:
            raise CoordinatorError(f"chain_id out of bounds: {self.chain_id}")

        if not isinstance(self.wallet_address, (str, bytes)):
            raise CoordinatorError(
                f"wallet_address must be str or bytes, got {type(self.wallet_address).__name__}"
            )
        norm_wallet = _validate_hex(self.wallet_address, 20, "wallet_address")
        object.__setattr__(self, "wallet_address", norm_wallet)

        if (
            not isinstance(self.base_symbol, str)
            or not self.base_symbol.strip()
            or self.base_symbol != self.base_symbol.strip()
        ):
            raise CoordinatorError(f"Invalid base_symbol: {self.base_symbol!r}")

        if type(self.decimals) is not int or isinstance(self.decimals, bool):
            raise CoordinatorError(f"decimals must be an integer, got {type(self.decimals).__name__}")
        # The 0..36 decimals bound is an offline research coordinator guard rail to prevent
        # excessive atom scaling and rational arithmetic memory overflow in Fraction operations,
        # not a universal ERC-20 standard limit (ERC-20 uint8 supports 0..255) or token corruption.
        if self.decimals < 0 or self.decimals > 36:
            raise CoordinatorError(f"decimals out of bounds (0..36): {self.decimals}")

        if type(self.amount_in_atoms) is not int or isinstance(self.amount_in_atoms, bool):
            raise CoordinatorError(
                f"amount_in_atoms must be an integer, got {type(self.amount_in_atoms).__name__}"
            )
        if self.amount_in_atoms <= 0 or self.amount_in_atoms >= 2**256:
            raise CoordinatorError(
                f"amount_in_atoms must be strictly positive and < 2**256, got {self.amount_in_atoms}"
            )

        if type(self.minimum_output_atoms) is not int or isinstance(self.minimum_output_atoms, bool):
            raise CoordinatorError(
                f"minimum_output_atoms must be an integer, got {type(self.minimum_output_atoms).__name__}"
            )
        if self.minimum_output_atoms <= 0 or self.minimum_output_atoms >= 2**256:
            raise CoordinatorError(
                f"minimum_output_atoms must be strictly positive and < 2**256, got {self.minimum_output_atoms}"
            )

        if type(self.nonce) is not int or isinstance(self.nonce, bool):
            raise CoordinatorError(f"nonce must be an integer, got {type(self.nonce).__name__}")
        if self.nonce < 0 or self.nonce >= 2**256:
            raise CoordinatorError(f"nonce must be non-negative and < 2**256, got {self.nonce}")


def _verify_slot_payload_consistency(slot: ReservedExecution) -> None:
    """Validate that slot.payload strictly matches slot request fields, generation, and one_shot."""
    if slot.payload is None:
        return
    p = slot.payload
    if not isinstance(p, dict):
        raise SlotVerificationError(f"slot.payload must be a dict, got {type(p).__name__}")
    req = slot.request
    if "plan_id" in p and p["plan_id"] != req.plan_id:
        raise SlotVerificationError(
            f"Slot payload plan_id mismatch: slot has {req.plan_id!r}, payload has {p['plan_id']!r}"
        )
    if "chain_id" in p and p["chain_id"] != req.chain_id:
        raise SlotVerificationError(
            f"Slot payload chain_id mismatch: slot has {req.chain_id}, payload has {p['chain_id']}"
        )
    norm_w = _validate_hex(req.wallet_address, 20, "wallet_address")
    if "wallet_address" in p and p["wallet_address"] != norm_w:
        raise SlotVerificationError(
            f"Slot payload wallet_address mismatch: slot has {norm_w!r}, payload has {p['wallet_address']!r}"
        )
    if "base_symbol" in p and p["base_symbol"] != req.base_symbol:
        raise SlotVerificationError(
            f"Slot payload base_symbol mismatch: slot has {req.base_symbol!r}, payload has {p['base_symbol']!r}"
        )
    if "decimals" in p and p["decimals"] != req.decimals:
        raise SlotVerificationError(
            f"Slot payload decimals mismatch: slot has {req.decimals}, payload has {p['decimals']}"
        )
    if "amount_in_atoms" in p and p["amount_in_atoms"] != req.amount_in_atoms:
        raise SlotVerificationError(
            f"Slot payload amount_in_atoms mismatch: slot has {req.amount_in_atoms}, payload has {p['amount_in_atoms']}"
        )
    if "minimum_output_atoms" in p and p["minimum_output_atoms"] != req.minimum_output_atoms:
        raise SlotVerificationError(
            f"Slot payload minimum_output_atoms mismatch: slot has {req.minimum_output_atoms}, payload has {p['minimum_output_atoms']}"
        )
    if "nonce" in p and p["nonce"] != req.nonce:
        raise SlotVerificationError(
            f"Slot payload nonce mismatch: slot has {req.nonce}, payload has {p['nonce']}"
        )
    if "generation" in p and p["generation"] != slot.generation:
        raise SlotVerificationError(
            f"Slot payload generation mismatch: slot has {slot.generation!r}, payload has {p['generation']!r}"
        )
    if "one_shot" in p and p["one_shot"] != slot.one_shot:
        raise SlotVerificationError(
            f"Slot payload one_shot mismatch: slot has {slot.one_shot}, payload has {p['one_shot']}"
        )
    if "reserved_loss_usd" in p:
        try:
            if Fraction(Decimal(str(p["reserved_loss_usd"]))) != Fraction(slot.reserved_loss_usd):
                raise SlotVerificationError(
                    f"Slot payload reserved_loss_usd mismatch: slot has {slot.reserved_loss_usd}, payload has {p['reserved_loss_usd']}"
                )
        except (ArithmeticError, ValueError) as exc:
            raise SlotVerificationError(f"Invalid reserved_loss_usd in payload: {exc}") from exc


@dataclass(frozen=True)
class ReservedExecution:
    """Frozen execution slot holding the reservation request and reserved worst loss."""

    request: ReservationRequest
    reserved_loss_usd: Decimal
    generation: str = ""
    one_shot: bool = True
    payload: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.request, ReservationRequest):
            raise CoordinatorError(
                f"request must be a ReservationRequest, got {type(self.request).__name__}"
            )
        _validate_coord_decimal(self.reserved_loss_usd, "reserved_loss_usd", positive=True)
        if not isinstance(self.generation, str):
            raise CoordinatorError(
                f"generation must be a string, got {type(self.generation).__name__}"
            )
        if type(self.one_shot) is not bool:
            raise CoordinatorError(
                f"one_shot must be a boolean, got {type(self.one_shot).__name__}"
            )
        if self.payload is not None:
            if not isinstance(self.payload, dict):
                raise CoordinatorError("payload must be a dict or None")
            object.__setattr__(self, "payload", dict(self.payload))
            _verify_slot_payload_consistency(self)

    @property
    def plan_id(self) -> str:
        return self.request.plan_id

    @property
    def intent_id(self) -> str:
        return self.request.plan_id


class ExecutionCoordinator:
    """Offline execution slot coordinator with explicit valuation and rigid floor gates.

    RESEARCH ONLY:
    - Pure offline reservation coordinator for strategy and cost modeling.
    - Caller valuation evidence is purely offline modeling; not connected to live RPC.
    - Does not construct PoolDescriptor or sign/broadcast live transactions.
    - All concurrency and latching guarantees are delegated to the underlying FundsLedger.
    """

    def __init__(
        self,
        ledger: FundsLedger,
        *,
        chain_id: int,
        valuation_provider: Callable[[ReservationRequest], ValuationEvidence],
    ) -> None:
        if not isinstance(ledger, FundsLedger):
            raise CoordinatorError(
                f"ledger must be an instance of FundsLedger, got {type(ledger).__name__}"
            )
        if type(chain_id) is not int or isinstance(chain_id, bool):
            raise CoordinatorError(f"chain_id must be an integer, got {type(chain_id).__name__}")
        if chain_id < 1 or chain_id >= 2**256:
            raise CoordinatorError(f"chain_id out of bounds: {chain_id}")
        if not callable(valuation_provider):
            raise CoordinatorError("valuation_provider must be callable")

        self._ledger = ledger
        self._chain_id = chain_id
        self._valuation_provider = valuation_provider

    @property
    def ledger(self) -> FundsLedger:
        return self._ledger

    @property
    def chain_id(self) -> int:
        return self._chain_id

    @property
    def valuation_provider(self) -> Callable[[ReservationRequest], ValuationEvidence]:
        return self._valuation_provider

    def is_in_flight(self, wallet_address: str, base_symbol: str | None = None) -> bool:
        """Query persistent database state to check if wallet has an active pending slot."""
        wallet_norm = _validate_hex(wallet_address, 20, "wallet_address")
        if base_symbol is not None:
            if base_symbol not in self._ledger.allowed_bases:
                raise CoordinatorError(f"Unsupported base: {base_symbol}")
            st = self._ledger.status(self._chain_id, wallet_norm, base_symbol)
            return st["pending"] is not None
        return self._ledger.pending_intent(self._chain_id, wallet_norm) is not None

    def acquire_execution_slot(
        self,
        request: ReservationRequest,
        *,
        one_shot: bool,
    ) -> ReservedExecution:
        """Validate request and valuation, check floors, and reserve slot in durable ledger."""
        if not isinstance(request, ReservationRequest):
            raise CoordinatorError(
                f"request must be a ReservationRequest, got {type(request).__name__}"
            )
        if type(one_shot) is not bool:
            raise CoordinatorError(f"one_shot must be a boolean, got {type(one_shot).__name__}")
        if request.chain_id != self._chain_id:
            raise CoordinatorError(
                f"Chain ID mismatch: request specifies {request.chain_id}, coordinator is {self._chain_id}"
            )
        if request.base_symbol not in self._ledger.allowed_bases:
            raise CoordinatorError(
                f"Base symbol {request.base_symbol!r} not in ledger allowed bases: {sorted(self._ledger.allowed_bases)}"
            )

        # 1. Obtain valuation evidence
        evidence = self._valuation_provider(request)
        if not isinstance(evidence, ValuationEvidence):
            raise ValuationError(
                f"valuation_provider must return ValuationEvidence, got {type(evidence).__name__}"
            )

        # 2. Hard notional cap check: notional <= 500 USD via Fraction (AST Zero-Float)
        amount_in_frac = Fraction(request.amount_in_atoms, 10**request.decimals)
        price_frac = Fraction(evidence.base_price_usd)
        notional_usd = amount_in_frac * price_frac
        if notional_usd > Fraction(500):
            raise AmountLimitExceededError(
                f"Trade notional {notional_usd} USD exceeds 500 USD hard limit"
            )

        # 3. Gas atoms calculation via Fraction ceiling:
        # gas_atoms = ceil(gas_usd / price * 10**decimals)
        gas_usd_frac = Fraction(evidence.gas_usd)
        gas_token_frac = gas_usd_frac / price_frac
        gas_atoms_frac = gas_token_frac * (10**request.decimals)
        # Exact integer ceiling for positive rational: (num + den - 1) // den
        gas_atoms = (gas_atoms_frac.numerator + gas_atoms_frac.denominator - 1) // gas_atoms_frac.denominator

        # 4. Floor guarantee check:
        # min_output >= amount_in + gas_atoms + 1 atom
        required_floor = request.amount_in_atoms + gas_atoms + 1
        if request.minimum_output_atoms < required_floor:
            raise FloorViolationError(
                f"minimum_output_atoms ({request.minimum_output_atoms}) is below "
                f"required floor ({required_floor} = amount_in:{request.amount_in_atoms} + "
                f"gas_atoms:{gas_atoms} + 1)"
            )

        # 5. Worst-case loss reservation: explicit gas cost (token loss cannot occur under legal floor)
        worst_loss_usd = evidence.gas_usd

        # 6. Generate fresh non-reusable generation identifier (ABA defense)
        generation = str(uuid.uuid4())

        # 7. Payload assembly with explicit primitives and generation binding
        payload = {
            "plan_id": request.plan_id,
            "chain_id": request.chain_id,
            "wallet_address": request.wallet_address,
            "base_symbol": request.base_symbol,
            "decimals": request.decimals,
            "amount_in_atoms": request.amount_in_atoms,
            "minimum_output_atoms": request.minimum_output_atoms,
            "nonce": request.nonce,
            "gas_atoms": gas_atoms,
            "slippage_bps": evidence.slippage_bps,
            "base_price_usd": str(evidence.base_price_usd),
            "gas_usd": str(evidence.gas_usd),
            "generation": generation,
            "one_shot": one_shot,
            "reserved_loss_usd": str(worst_loss_usd),
        }

        # 8. Reserve in FundsLedger
        # NOTE: running=True and auto_execute=True are passed purely to satisfy
        # FundsLedger's existing API contract for offline accounting. They do NOT
        # represent real live execution authorization or on-chain transaction dispatch.
        self._ledger.reserve(
            intent_id=request.plan_id,
            chain=request.chain_id,
            wallet=request.wallet_address,
            base=request.base_symbol,
            nonce=request.nonce,
            payload=payload,
            worst_loss_usd=worst_loss_usd,
            running=True,
            auto_execute=True,
            one_shot=one_shot,
        )

        return ReservedExecution(
            request=request,
            reserved_loss_usd=worst_loss_usd,
            generation=generation,
            one_shot=one_shot,
            payload=payload,
        )

    def _build_expected_identity(self, slot: ReservedExecution) -> IntentIdentity:
        wallet_norm = _validate_hex(slot.request.wallet_address, 20, "wallet_address")
        if slot.payload is not None:
            if not isinstance(slot.payload, dict):
                raise SlotVerificationError(f"slot.payload must be a dict, got {type(slot.payload).__name__}")
            _verify_slot_payload_consistency(slot)
            payload_dict = dict(slot.payload)
        else:
            payload_dict = {
                "plan_id": slot.request.plan_id,
                "chain_id": slot.request.chain_id,
                "wallet_address": wallet_norm,
                "base_symbol": slot.request.base_symbol,
                "decimals": slot.request.decimals,
                "amount_in_atoms": slot.request.amount_in_atoms,
                "minimum_output_atoms": slot.request.minimum_output_atoms,
                "nonce": slot.request.nonce,
                "generation": slot.generation,
                "one_shot": slot.one_shot,
                "reserved_loss_usd": str(slot.reserved_loss_usd),
            }
        return IntentIdentity(
            id=slot.request.plan_id,
            chain=slot.request.chain_id,
            wallet=wallet_norm,
            base=slot.request.base_symbol,
            nonce=slot.request.nonce,
            payload=payload_dict,
            reserve=str(slot.reserved_loss_usd),
            one_shot=slot.one_shot,
        )

    def release_execution_slot(self, slot: ReservedExecution) -> None:
        """Cancel a reserved slot before signing. Cannot release SIGNED or UNKNOWN intents."""
        self._verify_slot(slot)
        expected_identity = self._build_expected_identity(slot)
        try:
            self._ledger.cancel_before_signing(
                slot.request.plan_id, expected_identity=expected_identity
            )
        except IdentityMismatchError as exc:
            raise SlotVerificationError(str(exc)) from exc

    def record_signed(self, slot: ReservedExecution, tx_hash: str) -> None:
        """Record signed transaction hash before any simulated broadcast."""
        self._verify_slot(slot)
        if not isinstance(tx_hash, str) or not tx_hash:
            raise CoordinatorError("tx_hash must be a non-empty string")
        expected_identity = self._build_expected_identity(slot)
        try:
            self._ledger.mark_signed(
                slot.request.plan_id, tx_hash, expected_identity=expected_identity
            )
        except IdentityMismatchError as exc:
            raise SlotVerificationError(str(exc)) from exc

    def mark_unknown(self, slot: ReservedExecution) -> None:
        """Mark broadcast outcome unknown, retaining the persistent latch across restarts."""
        self._verify_slot(slot)
        expected_identity = self._build_expected_identity(slot)
        try:
            self._ledger.mark_unknown(
                slot.request.plan_id, expected_identity=expected_identity
            )
        except IdentityMismatchError as exc:
            raise SlotVerificationError(str(exc)) from exc

    def reconcile(
        self,
        slot: ReservedExecution,
        *,
        tx_hash: str,
        block_hash: str,
        net_profit_usd: Decimal,
        actual_gas_usd: Decimal,
        receipt_status: int,
    ) -> None:
        """Reconcile terminal receipt. All arguments are mandatory; no defaults or auto-promotion."""
        if not isinstance(slot, ReservedExecution):
            raise SlotVerificationError(
                f"slot must be a ReservedExecution, got {type(slot).__name__}"
            )
        if not isinstance(slot.request, ReservationRequest):
            raise SlotVerificationError(
                f"slot.request must be a ReservationRequest, got {type(slot.request).__name__}"
            )
        if slot.request.chain_id != self._chain_id:
            raise SlotVerificationError(
                f"Slot chain_id mismatch: slot has {slot.request.chain_id}, coordinator has {self._chain_id}"
            )
        if not isinstance(tx_hash, str) or not tx_hash:
            raise CoordinatorError("tx_hash must be a non-empty string")
        if not isinstance(block_hash, str) or not block_hash:
            raise CoordinatorError("block_hash must be a non-empty string")
        _validate_coord_decimal(net_profit_usd, "net_profit_usd")
        _validate_coord_decimal(actual_gas_usd, "actual_gas_usd", non_negative=True)
        if type(receipt_status) is not int or isinstance(receipt_status, bool):
            raise CoordinatorError(
                f"receipt_status must be an integer, got {type(receipt_status).__name__}"
            )
        if receipt_status not in (0, 1):
            raise CoordinatorError(f"Unknown receipt_status: {receipt_status} (must be 0 or 1)")

        expected_identity = self._build_expected_identity(slot)
        try:
            self._ledger.reconcile(
                slot.request.plan_id,
                tx_hash=tx_hash,
                block_hash=block_hash,
                net_profit_usd=net_profit_usd,
                actual_gas_usd=actual_gas_usd,
                receipt_status=receipt_status,
                expected_identity=expected_identity,
            )
        except IdentityMismatchError as exc:
            raise SlotVerificationError(str(exc)) from exc

    def _verify_slot(self, slot: ReservedExecution) -> dict[str, Any]:
        """Verify that the provided slot matches the persistent pending intent in the ledger."""
        if not isinstance(slot, ReservedExecution):
            raise SlotVerificationError(
                f"slot must be a ReservedExecution, got {type(slot).__name__}"
            )
        if not isinstance(slot.request, ReservationRequest):
            raise SlotVerificationError(
                f"slot.request must be a ReservationRequest, got {type(slot.request).__name__}"
            )
        if slot.request.chain_id != self._chain_id:
            raise SlotVerificationError(
                f"Slot chain_id mismatch: slot has {slot.request.chain_id}, coordinator has {self._chain_id}"
            )

        wallet_norm = _validate_hex(slot.request.wallet_address, 20, "wallet_address")
        pending = self._ledger.pending_intent(self._chain_id, wallet_norm)
        if pending is None:
            raise SlotVerificationError(f"No pending intent found for wallet {wallet_norm}")

        if pending["id"] != slot.request.plan_id:
            raise SlotVerificationError(
                f"Slot plan_id mismatch: slot has {slot.request.plan_id!r}, "
                f"persisted intent has {pending['id']!r}"
            )
        if pending["chain"] != slot.request.chain_id:
            raise SlotVerificationError(
                f"Slot chain mismatch: slot has {slot.request.chain_id}, "
                f"persisted intent has {pending['chain']}"
            )
        if pending["wallet"] != wallet_norm:
            raise SlotVerificationError(
                f"Slot wallet mismatch: slot has {wallet_norm!r}, "
                f"persisted intent has {pending['wallet']!r}"
            )
        if pending["base"] != slot.request.base_symbol:
            raise SlotVerificationError(
                f"Slot base mismatch: slot has {slot.request.base_symbol!r}, "
                f"persisted intent has {pending['base']!r}"
            )
        if pending["nonce"] != slot.request.nonce:
            raise SlotVerificationError(
                f"Slot nonce mismatch: slot has {slot.request.nonce}, "
                f"persisted intent has {pending['nonce']}"
            )
        if Fraction(Decimal(pending["reserve"])) != Fraction(slot.reserved_loss_usd):
            raise SlotVerificationError(
                f"Slot reserve mismatch: slot has {slot.reserved_loss_usd}, "
                f"persisted intent has {pending['reserve']}"
            )

        # Invariant payload checks
        p_payload = pending.get("payload")
        if isinstance(p_payload, dict):
            if "amount_in_atoms" in p_payload and p_payload["amount_in_atoms"] != slot.request.amount_in_atoms:
                raise SlotVerificationError(
                    f"Slot payload amount_in_atoms mismatch: slot has {slot.request.amount_in_atoms}, "
                    f"persisted has {p_payload['amount_in_atoms']}"
                )
            if "minimum_output_atoms" in p_payload and p_payload["minimum_output_atoms"] != slot.request.minimum_output_atoms:
                raise SlotVerificationError(
                    f"Slot payload minimum_output_atoms mismatch: slot has {slot.request.minimum_output_atoms}, "
                    f"persisted has {p_payload['minimum_output_atoms']}"
                )
            if "decimals" in p_payload and p_payload["decimals"] != slot.request.decimals:
                raise SlotVerificationError(
                    f"Slot payload decimals mismatch: slot has {slot.request.decimals}, "
                    f"persisted has {p_payload['decimals']}"
                )
            if "generation" in p_payload and slot.generation and p_payload["generation"] != slot.generation:
                raise SlotVerificationError(
                    f"Slot generation mismatch: slot has {slot.generation!r}, "
                    f"persisted has {p_payload['generation']!r}"
                )
            if "generation" in p_payload and not slot.generation:
                raise SlotVerificationError("Slot generation missing on slot for intent requiring generation")

        return pending

    def unpause_wallet(self, wallet_address: str, *, reason: str) -> None:
        """Thin unpause wrapper delegating strictly to the persistent ledger."""
        if not isinstance(reason, str) or not reason.strip():
            raise CoordinatorError("Resume reason must be non-empty")
        wallet_norm = _validate_hex(wallet_address, 20, "wallet_address")
        self._ledger.resume_wallet(self._chain_id, wallet_norm, reason=reason.strip())
