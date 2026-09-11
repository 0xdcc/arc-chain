"""Pure functional deterministic on-chain execution reconciler and four-state verdict.

Evaluates on-chain transaction receipts, Transfer logs, and simulation traces against
immutable execution plans and intent records to produce an immutable ReconciliationAssessment.

Enforces acceptance criteria C18 ~ C19:
- VERIFIED: status=1, tx_hash & nonce match, logs prove net base token increase, counterparties whitelisted,
  exact gas cost deducted to determine net profit.
- REVERTED: status=0, exact gas loss accounted for, no zero-gas spoofing, anomalous logs intercepted.
- UNKNOWN: missing receipt, timeout, unfinalized/pending block, insufficient confirmations, missing logs,
  or missing price evidence; yields persistent latch decision (LATCH_REQUIRED) to prohibit retry/concurrency.
- FAKE_EVENT_REJECTED: forged hash, nonce mismatch, dry-run/test masquerade, non-target token transfers,
  external subsidy/donation, unauthorized debit, malformed log topics, or non-positive token delta.

Prohibits crude balance subtraction (balance_after - balance_before); strictly performs fine-grained
Transfer event log netting and gas accounting.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from arbitrage_contracts.identity import (
    UINT256_MAX,
    validate_bytes32,
    validate_evm_address,
    validate_non_negative_integer,
)
from atomic_execution.encoding import EncodedCalldata
from atomic_execution.inputs import (
    ROBINHOOD_CHAIN_ID,
    USDG_ADDRESS_4663,
    WETH_ADDRESS_4663,
    ZERO_ADDRESS,
)
from atomic_execution.planning import CANONICAL_UNIVERSAL_ROUTER, ExecutionPlan
from atomic_execution.policy import (
    validate_non_negative_decimal,
    validate_positive_decimal,
)

CANONICAL_PERMIT2: str = "0x000000000022d473030f116ddee9f6b43ac78ba3"
TRANSFER_EVENT_TOPIC: str = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

DEFAULT_COUNTERPARTIES: frozenset[str] = frozenset(
    {
        CANONICAL_UNIVERSAL_ROUTER.lower(),
        CANONICAL_PERMIT2.lower(),
    }
)


class ReconciliationStatus(StrEnum):
    """Four definitive reconciliation verdict states."""

    VERIFIED = "VERIFIED"
    REVERTED = "REVERTED"
    UNKNOWN = "UNKNOWN"
    FAKE_EVENT_REJECTED = "FAKE_EVENT_REJECTED"


class LatchDecision(StrEnum):
    """Concurrency latching decision for wallet and nonce state."""

    LATCH_REQUIRED = "LATCH_REQUIRED"
    NONE = "NONE"


@dataclass(frozen=True, slots=True)
class ReconciliationAssessment:
    """Immutable four-state reconciliation assessment and financial accounting."""

    status: ReconciliationStatus
    reason: str
    latch_decision: LatchDecision
    plan_id: str | None = None
    tx_hash: str | None = None
    nonce: int | None = None
    block_number: int | None = None
    block_hash: str | None = None
    wallet_address: str | None = None
    base_asset_address: str | None = None
    token_delta_atoms: int = 0
    token_delta_usd: Decimal = Decimal("0")
    gas_used: int = 0
    effective_gas_price_wei: int = 0
    actual_gas_cost_wei: int = 0
    actual_gas_cost_native: Decimal = Decimal("0")
    actual_gas_cost_usd: Decimal = Decimal("0")
    net_profit_usd: Decimal = Decimal("0")
    transfers_count: int = 0
    confirmations: int = 0
    details: dict[str, Any] | None = None
    raw_receipt: dict[str, Any] | None = None

    def __init__(
        self,
        status: ReconciliationStatus | str,
        reason: str,
        latch_decision: LatchDecision | str = LatchDecision.NONE,
        plan_id: str | None = None,
        tx_hash: str | None = None,
        nonce: int | None = None,
        block_number: int | None = None,
        block_hash: str | None = None,
        wallet_address: str | None = None,
        base_asset_address: str | None = None,
        token_delta_atoms: int = 0,
        token_delta_usd: Decimal | str | int = Decimal("0"),
        gas_used: int = 0,
        effective_gas_price_wei: int = 0,
        actual_gas_cost_wei: int = 0,
        actual_gas_cost_native: Decimal | str | int = Decimal("0"),
        actual_gas_cost_usd: Decimal | str | int = Decimal("0"),
        net_profit_usd: Decimal | str | int = Decimal("0"),
        transfers_count: int = 0,
        confirmations: int = 0,
        details: dict[str, Any] | None = None,
        raw_receipt: dict[str, Any] | None = None,
    ) -> None:
        resolved_status = (
            status
            if isinstance(status, ReconciliationStatus)
            else ReconciliationStatus(str(status))
        )
        resolved_latch = (
            latch_decision
            if isinstance(latch_decision, LatchDecision)
            else LatchDecision(str(latch_decision))
        )

        val_tx_hash: str | None = None
        if tx_hash is not None:
            try:
                val_tx_hash = validate_bytes32(tx_hash)
            except Exception:
                val_tx_hash = str(tx_hash).strip()

        val_block_hash: str | None = None
        if block_hash is not None:
            try:
                val_block_hash = validate_bytes32(block_hash)
            except Exception:
                val_block_hash = str(block_hash).strip()

        val_wallet: str | None = None
        if wallet_address is not None:
            try:
                val_wallet = validate_evm_address(wallet_address)
            except Exception:
                val_wallet = str(wallet_address).strip().lower()

        val_base_asset: str | None = None
        if base_asset_address is not None:
            try:
                val_base_asset = validate_evm_address(base_asset_address)
            except Exception:
                val_base_asset = str(base_asset_address).strip().lower()

        val_nonce = None if nonce is None else validate_non_negative_integer(nonce, "nonce")
        val_block_number = (
            None
            if block_number is None
            else validate_non_negative_integer(block_number, "block_number")
        )

        dec_token_delta_usd = (
            Decimal(str(token_delta_usd))
            if not isinstance(token_delta_usd, Decimal)
            else token_delta_usd
        )
        dec_gas_cost_native = (
            Decimal(str(actual_gas_cost_native))
            if not isinstance(actual_gas_cost_native, Decimal)
            else actual_gas_cost_native
        )
        dec_gas_cost_usd = (
            Decimal(str(actual_gas_cost_usd))
            if not isinstance(actual_gas_cost_usd, Decimal)
            else actual_gas_cost_usd
        )
        dec_net_profit_usd = (
            Decimal(str(net_profit_usd))
            if not isinstance(net_profit_usd, Decimal)
            else net_profit_usd
        )

        object.__setattr__(self, "status", resolved_status)
        object.__setattr__(self, "reason", str(reason))
        object.__setattr__(self, "latch_decision", resolved_latch)
        object.__setattr__(self, "plan_id", plan_id)
        object.__setattr__(self, "tx_hash", val_tx_hash)
        object.__setattr__(self, "nonce", val_nonce)
        object.__setattr__(self, "block_number", val_block_number)
        object.__setattr__(self, "block_hash", val_block_hash)
        object.__setattr__(self, "wallet_address", val_wallet)
        object.__setattr__(self, "base_asset_address", val_base_asset)
        object.__setattr__(self, "token_delta_atoms", int(token_delta_atoms))
        object.__setattr__(self, "token_delta_usd", dec_token_delta_usd)
        object.__setattr__(self, "gas_used", validate_non_negative_integer(gas_used, "gas_used"))
        object.__setattr__(
            self,
            "effective_gas_price_wei",
            validate_non_negative_integer(effective_gas_price_wei, "effective_gas_price_wei"),
        )
        object.__setattr__(
            self,
            "actual_gas_cost_wei",
            validate_non_negative_integer(actual_gas_cost_wei, "actual_gas_cost_wei"),
        )
        object.__setattr__(self, "actual_gas_cost_native", dec_gas_cost_native)
        object.__setattr__(self, "actual_gas_cost_usd", dec_gas_cost_usd)
        object.__setattr__(self, "net_profit_usd", dec_net_profit_usd)
        object.__setattr__(
            self,
            "transfers_count",
            validate_non_negative_integer(transfers_count, "transfers_count"),
        )
        object.__setattr__(
            self, "confirmations", validate_non_negative_integer(confirmations, "confirmations")
        )
        object.__setattr__(self, "details", dict(details) if details is not None else None)
        object.__setattr__(
            self, "raw_receipt", dict(raw_receipt) if raw_receipt is not None else None
        )

    @property
    def is_verified(self) -> bool:
        """Return True if transaction and settlement are fully audited and verified."""
        return self.status == ReconciliationStatus.VERIFIED

    @property
    def is_reverted(self) -> bool:
        """Return True if transaction rolled back on-chain with real gas accounting."""
        return self.status == ReconciliationStatus.REVERTED

    @property
    def is_unknown(self) -> bool:
        """Return True if transaction state is pending, timed out, or unconfirmed."""
        return self.status == ReconciliationStatus.UNKNOWN

    @property
    def is_rejected(self) -> bool:
        """Return True if event was forged, non-target token, or external subsidy."""
        return self.status == ReconciliationStatus.FAKE_EVENT_REJECTED

    @property
    def is_latch_required(self) -> bool:
        """Return True if wallet and nonce must be locked against concurrent execution."""
        return self.latch_decision == LatchDecision.LATCH_REQUIRED

    @property
    def can_retry(self) -> bool:
        """Return True if blind retry is permitted (strictly False under fail-closed rules)."""
        return False

    @property
    def can_concurrent(self) -> bool:
        """Return True if subsequent orders may proceed without awaiting latch clearance."""
        return not self.is_latch_required

    @property
    def token_delta(self) -> int:
        """Alias for token_delta_atoms."""
        return self.token_delta_atoms

    @property
    def actual_gas_used(self) -> int:
        """Alias for gas_used."""
        return self.gas_used

    @property
    def actual_gas_native(self) -> Decimal:
        """Alias for actual_gas_cost_native."""
        return self.actual_gas_cost_native

    @property
    def actual_gas_usd(self) -> Decimal:
        """Alias for actual_gas_cost_usd."""
        return self.actual_gas_cost_usd

    @property
    def accounting(self) -> dict[str, Any] | None:
        """Alias for details dictionary."""
        return self.details

    def to_dict(self) -> dict[str, Any]:
        """Serialize assessment to JSON-serializable dictionary."""
        return {
            "status": self.status.value,
            "reason": self.reason,
            "latch_decision": self.latch_decision.value,
            "plan_id": self.plan_id,
            "tx_hash": self.tx_hash,
            "nonce": self.nonce,
            "block_number": self.block_number,
            "block_hash": self.block_hash,
            "wallet_address": self.wallet_address,
            "base_asset_address": self.base_asset_address,
            "token_delta_atoms": self.token_delta_atoms,
            "token_delta_usd": str(self.token_delta_usd),
            "gas_used": self.gas_used,
            "effective_gas_price_wei": self.effective_gas_price_wei,
            "actual_gas_cost_wei": self.actual_gas_cost_wei,
            "actual_gas_cost_native": str(self.actual_gas_cost_native),
            "actual_gas_cost_usd": str(self.actual_gas_cost_usd),
            "net_profit_usd": str(self.net_profit_usd),
            "transfers_count": self.transfers_count,
            "confirmations": self.confirmations,
            "details": self.details,
            "raw_receipt": self.raw_receipt,
        }

    def to_json(self) -> str:
        """Serialize assessment to compact canonical JSON string."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReconciliationAssessment:
        """Reconstruct assessment from mapping."""
        return cls(
            status=data["status"],
            reason=data["reason"],
            latch_decision=data.get("latch_decision", LatchDecision.NONE),
            plan_id=data.get("plan_id"),
            tx_hash=data.get("tx_hash"),
            nonce=data.get("nonce"),
            block_number=data.get("block_number"),
            block_hash=data.get("block_hash"),
            wallet_address=data.get("wallet_address"),
            base_asset_address=data.get("base_asset_address"),
            token_delta_atoms=int(data.get("token_delta_atoms", 0)),
            token_delta_usd=Decimal(str(data.get("token_delta_usd", "0"))),
            gas_used=int(data.get("gas_used", 0)),
            effective_gas_price_wei=int(data.get("effective_gas_price_wei", 0)),
            actual_gas_cost_wei=int(data.get("actual_gas_cost_wei", 0)),
            actual_gas_cost_native=Decimal(str(data.get("actual_gas_cost_native", "0"))),
            actual_gas_cost_usd=Decimal(str(data.get("actual_gas_cost_usd", "0"))),
            net_profit_usd=Decimal(str(data.get("net_profit_usd", "0"))),
            transfers_count=int(data.get("transfers_count", 0)),
            confirmations=int(data.get("confirmations", 0)),
            details=data.get("details"),
            raw_receipt=data.get("raw_receipt"),
        )

    @classmethod
    def from_json(cls, text: str) -> ReconciliationAssessment:
        """Deserialize assessment from JSON string."""
        return cls.from_dict(json.loads(text))


# Backward compatibility alias
ReconciliationResult = ReconciliationAssessment


def _parse_hex_or_int(value: Any) -> int:
    """Safely convert hex string, byte sequence, or integer into non-boolean Python int."""
    if isinstance(value, bool):
        raise TypeError("Boolean value cannot be parsed as integer quantity")
    if isinstance(value, int):
        return value
    if isinstance(value, bytes):
        return int.from_bytes(value, "big")
    if isinstance(value, str):
        val_str = value.strip()
        if val_str.startswith("0x") or val_str.startswith("0X"):
            return int(val_str, 16)
        return int(val_str)
    raise TypeError(f"Cannot parse value of type {type(value).__name__} as integer")


def _parse_topic_address(topic: str) -> str:
    """Decode and validate a 32-byte indexed topic containing a 20-byte EVM address.

    Ensures that the leading 12 bytes are strictly zero-padded (0x00...00) to prevent
    address aliasing and topic spoofing attacks.
    """
    if type(topic) is not str:
        raise ValueError("Topic must be a hex string")
    clean = topic.strip()
    if clean.startswith("0x") or clean.startswith("0X"):
        clean = clean[2:]
    if len(clean) != 64:
        raise ValueError(
            f"Indexed address topic must be 32 bytes (64 hex characters), got length {len(clean)}"
        )
    padding = clean[:24]
    if padding != "0" * 24:
        raise ValueError(f"Indexed address topic contains non-zero high bytes: 0x{padding}")
    addr_hex = clean[24:]
    return validate_evm_address(f"0x{addr_hex}")


def _parse_log_data_uint256(data: Any) -> int:
    """Parse uint256 token transfer amount from event log data field."""
    amount = _parse_hex_or_int(data)
    if amount < 0 or amount > UINT256_MAX:
        raise ValueError(f"Transfer value out of bounds [0, 2^256-1]: {amount}")
    return amount


def reconcile_execution(
    *,
    receipt: Mapping[str, Any] | None = None,
    plan: ExecutionPlan | None = None,
    encoded_calldata: EncodedCalldata | None = None,
    logs: Sequence[Mapping[str, Any]] | None = None,
    trace: Mapping[str, Any] | None = None,
    expected_tx_hash: str | None = None,
    expected_nonce: int | None = None,
    wallet_address: str | None = None,
    base_asset_address: str | None = None,
    base_decimals: int | None = None,
    base_asset_usd_price: Decimal | str | int | None = None,
    native_token_usd_price: Decimal | str | int | None = None,
    counterparties: Iterable[str] | None = None,
    current_block_number: int | None = None,
    min_confirmations: int = 1,
    timeout: bool = False,
    is_dry_run_or_test: bool = False,
    event: Mapping[str, Any] | None = None,
    require_positive_net_profit: bool = False,
    coordinator: Any | None = None,
    reserved: Any | None = None,
    hold_flight_lock_on_unknown: bool = False,
    **kwargs: Any,
) -> ReconciliationAssessment:
    """Pure functional deterministic reconciliation evaluating on-chain receipts and Transfer logs.

    Categorizes the execution outcome into VERIFIED, REVERTED, UNKNOWN, or FAKE_EVENT_REJECTED.
    """
    # 1. Detect fake / dry-run / simulation event masquerade
    if is_dry_run_or_test:
        return ReconciliationAssessment(
            status=ReconciliationStatus.FAKE_EVENT_REJECTED,
            reason="Dry-run or test execution flagged; cannot be reconciled as live on-chain execution",
            tx_hash=expected_tx_hash,
            wallet_address=wallet_address,
        )

    if event is not None:
        if (
            event.get("dry_run") is True
            or event.get("test") is True
            or event.get("is_dry_run") is True
            or event.get("is_test") is True
            or event.get("simulation") is True
        ):
            return ReconciliationAssessment(
                status=ReconciliationStatus.FAKE_EVENT_REJECTED,
                reason="Event payload contains test/dry_run flag; cannot be reconciled as live execution",
                tx_hash=expected_tx_hash or event.get("tx_hash"),
                wallet_address=wallet_address or event.get("wallet"),
            )

    if receipt is not None:
        if (
            receipt.get("dry_run") is True
            or receipt.get("test") is True
            or receipt.get("is_dry_run") is True
            or receipt.get("is_draft") is True
        ):
            return ReconciliationAssessment(
                status=ReconciliationStatus.FAKE_EVENT_REJECTED,
                reason="Receipt payload contains dry_run/draft flag; cannot be reconciled as live receipt",
                tx_hash=expected_tx_hash or receipt.get("transactionHash"),
                wallet_address=wallet_address,
                raw_receipt=dict(receipt),
            )

    if trace is not None and receipt is None:
        if (
            trace.get("is_draft") is True
            or trace.get("is_simulation") is True
            or trace.get("dry_run") is True
        ):
            return ReconciliationAssessment(
                status=ReconciliationStatus.FAKE_EVENT_REJECTED,
                reason="Simulation trace payload cannot be reconciled as authoritative live receipt",
                tx_hash=expected_tx_hash,
                wallet_address=wallet_address,
            )

    # 2. Timeout defense -> UNKNOWN with LATCH_REQUIRED
    if timeout:
        return ReconciliationAssessment(
            status=ReconciliationStatus.UNKNOWN,
            reason="Execution receipt retrieval timed out; latching wallet and nonce against retry",
            latch_decision=LatchDecision.LATCH_REQUIRED,
            tx_hash=expected_tx_hash,
            nonce=expected_nonce,
            wallet_address=wallet_address,
        )

    # 3. Missing receipt defense -> UNKNOWN with LATCH_REQUIRED
    effective_receipt = receipt if receipt is not None else trace
    if effective_receipt is None:
        return ReconciliationAssessment(
            status=ReconciliationStatus.UNKNOWN,
            reason="Execution receipt missing or null from RPC provider; latching wallet and nonce",
            latch_decision=LatchDecision.LATCH_REQUIRED,
            tx_hash=expected_tx_hash,
            nonce=expected_nonce,
            wallet_address=wallet_address,
        )

    # Validate chain_id if present on receipt
    raw_chain_id = effective_receipt.get("chainId")
    if raw_chain_id is not None:
        try:
            val_chain = _parse_hex_or_int(raw_chain_id)
            if val_chain != ROBINHOOD_CHAIN_ID:
                return ReconciliationAssessment(
                    status=ReconciliationStatus.FAKE_EVENT_REJECTED,
                    reason=f"Receipt chainId ({val_chain}) does not match Robinhood ({ROBINHOOD_CHAIN_ID})",
                    tx_hash=expected_tx_hash or effective_receipt.get("transactionHash"),
                    wallet_address=wallet_address,
                    raw_receipt=dict(effective_receipt),
                )
        except Exception:
            return ReconciliationAssessment(
                status=ReconciliationStatus.FAKE_EVENT_REJECTED,
                reason=f"Receipt chainId is unparseable: {raw_chain_id}",
                tx_hash=expected_tx_hash or effective_receipt.get("transactionHash"),
                wallet_address=wallet_address,
                raw_receipt=dict(effective_receipt),
            )

    # 4. Resolve core intents from plan, encoded calldata, event, or parameters
    resolved_plan_id = (
        plan.plan_id
        if plan is not None
        else (encoded_calldata.plan_id if encoded_calldata is not None else None)
    )

    resolved_wallet = (
        wallet_address
        or (event.get("wallet") if event else None)
        or (getattr(reserved, "wallet_address", None) if reserved else None)
    )

    if resolved_wallet is not None and resolved_wallet.lower() == ZERO_ADDRESS.lower():
        return ReconciliationAssessment(
            status=ReconciliationStatus.FAKE_EVENT_REJECTED,
            reason="Wallet address cannot be zero address",
            tx_hash=expected_tx_hash or effective_receipt.get("transactionHash"),
            wallet_address=ZERO_ADDRESS,
            raw_receipt=dict(effective_receipt),
        )

    resolved_base_asset: str | None = base_asset_address
    if resolved_base_asset is None and plan is not None:
        if plan.base_asset.token_key is not None:
            resolved_base_asset = plan.base_asset.token_key.raw_address

    if resolved_base_asset is None and event is not None:
        resolved_base_asset = event.get("base_asset_address")

    # Fallback to symbol mapping if address not provided directly
    if resolved_base_asset is None:
        symbol = (
            (event.get("base_symbol") if event else None)
            or (
                getattr(plan.base_asset, "symbol", None)
                if plan and hasattr(plan, "base_asset")
                else None
            )
            or (getattr(reserved, "base_symbol", None) if reserved else None)
        )
        if symbol:
            sym_upper = symbol.strip().upper()
            if sym_upper == "WETH":
                resolved_base_asset = WETH_ADDRESS_4663
            elif sym_upper == "USDG":
                resolved_base_asset = USDG_ADDRESS_4663
            else:
                return ReconciliationAssessment(
                    status=ReconciliationStatus.FAKE_EVENT_REJECTED,
                    reason=f"Unsupported or unreviewed base symbol: {symbol}",
                    tx_hash=expected_tx_hash,
                    raw_receipt=dict(effective_receipt),
                )

    # Resolve decimals
    resolved_base_decimals = base_decimals
    if resolved_base_decimals is None and plan is not None:
        resolved_base_decimals = plan.amount_in.decimals

    if resolved_base_decimals is None and resolved_base_asset is not None:
        norm_base = resolved_base_asset.lower()
        if norm_base == WETH_ADDRESS_4663.lower():
            resolved_base_decimals = 18
        elif norm_base == USDG_ADDRESS_4663.lower():
            resolved_base_decimals = 6

    # Resolve base asset price
    resolved_base_price: Decimal | None = None
    raw_base_price = (
        base_asset_usd_price
        or (plan.base_asset_usd_price if plan else None)
        or (event.get("base_price_usd") if event else None)
    )
    if raw_base_price is not None:
        try:
            resolved_base_price = validate_positive_decimal(raw_base_price, "base_asset_usd_price")
        except Exception as exc:
            return ReconciliationAssessment(
                status=ReconciliationStatus.UNKNOWN,
                reason=f"Invalid base_asset_usd_price evidence: {exc}",
                latch_decision=LatchDecision.LATCH_REQUIRED,
                tx_hash=expected_tx_hash,
                raw_receipt=dict(effective_receipt),
            )

    # Resolve native token price
    resolved_native_price: Decimal | None = None
    raw_native_price = (
        native_token_usd_price
        or (kwargs.get("default_native_price_usd"))
        or (event.get("native_price_usd") if event else None)
    )
    if raw_native_price is not None:
        try:
            resolved_native_price = validate_positive_decimal(
                raw_native_price, "native_token_usd_price"
            )
        except Exception as exc:
            return ReconciliationAssessment(
                status=ReconciliationStatus.UNKNOWN,
                reason=f"Invalid native_token_usd_price evidence: {exc}",
                latch_decision=LatchDecision.LATCH_REQUIRED,
                tx_hash=expected_tx_hash,
                raw_receipt=dict(effective_receipt),
            )
    elif (
        resolved_base_asset is not None
        and resolved_base_asset.lower() == WETH_ADDRESS_4663.lower()
        and resolved_base_price is not None
    ):
        resolved_native_price = resolved_base_price

    # 5. Missing intent guard -> UNKNOWN with LATCH_REQUIRED
    if resolved_wallet is None and expected_tx_hash is None and plan is None:
        return ReconciliationAssessment(
            status=ReconciliationStatus.UNKNOWN,
            reason="Missing original execution intent or caller identity; latching execution state",
            latch_decision=LatchDecision.LATCH_REQUIRED,
            raw_receipt=dict(effective_receipt),
        )

    # 6. Extract and audit block number and block hash
    raw_block_number = effective_receipt.get("blockNumber")
    raw_block_hash = effective_receipt.get("blockHash")
    if raw_block_number is None or raw_block_hash is None:
        return ReconciliationAssessment(
            status=ReconciliationStatus.UNKNOWN,
            reason="Receipt belongs to an unfinalized or pending block; latching execution state",
            latch_decision=LatchDecision.LATCH_REQUIRED,
            tx_hash=expected_tx_hash,
            nonce=expected_nonce,
            wallet_address=resolved_wallet,
            raw_receipt=dict(effective_receipt),
        )

    try:
        block_number = _parse_hex_or_int(raw_block_number)
    except Exception:
        return ReconciliationAssessment(
            status=ReconciliationStatus.UNKNOWN,
            reason=f"Receipt blockNumber is unparseable: {raw_block_number}",
            latch_decision=LatchDecision.LATCH_REQUIRED,
            raw_receipt=dict(effective_receipt),
        )

    block_hash_str = str(raw_block_hash).strip()

    # Block confirmation check
    confirmations = 1
    if current_block_number is not None:
        confirmations = current_block_number - block_number + 1
        if confirmations < min_confirmations:
            return ReconciliationAssessment(
                status=ReconciliationStatus.UNKNOWN,
                reason=(
                    f"Insufficient block confirmations ({confirmations} < required {min_confirmations}); "
                    "latching execution state against reorg"
                ),
                latch_decision=LatchDecision.LATCH_REQUIRED,
                tx_hash=expected_tx_hash,
                nonce=expected_nonce,
                block_number=block_number,
                block_hash=block_hash_str,
                wallet_address=resolved_wallet,
                confirmations=confirmations,
                raw_receipt=dict(effective_receipt),
            )

    # 7. Transaction hash and nonce verification
    receipt_tx_hash = effective_receipt.get("transactionHash")
    if not receipt_tx_hash or type(receipt_tx_hash) is not str:
        return ReconciliationAssessment(
            status=ReconciliationStatus.UNKNOWN,
            reason="Receipt is missing valid transactionHash; latching execution state",
            latch_decision=LatchDecision.LATCH_REQUIRED,
            wallet_address=resolved_wallet,
            raw_receipt=dict(effective_receipt),
        )

    clean_receipt_hash = receipt_tx_hash.strip()
    if expected_tx_hash is not None:
        if clean_receipt_hash.lower() != expected_tx_hash.strip().lower():
            return ReconciliationAssessment(
                status=ReconciliationStatus.FAKE_EVENT_REJECTED,
                reason=(
                    f"Receipt transactionHash ({clean_receipt_hash}) does not match expected "
                    f"transaction hash ({expected_tx_hash})"
                ),
                tx_hash=clean_receipt_hash,
                block_number=block_number,
                block_hash=block_hash_str,
                wallet_address=resolved_wallet,
                raw_receipt=dict(effective_receipt),
            )

    receipt_nonce_val = effective_receipt.get("nonce")
    receipt_nonce: int | None = None
    if receipt_nonce_val is not None:
        try:
            receipt_nonce = _parse_hex_or_int(receipt_nonce_val)
        except Exception:
            return ReconciliationAssessment(
                status=ReconciliationStatus.FAKE_EVENT_REJECTED,
                reason=f"Receipt nonce is malformed: {receipt_nonce_val}",
                tx_hash=clean_receipt_hash,
                raw_receipt=dict(effective_receipt),
            )

    if expected_nonce is not None and receipt_nonce is not None:
        if receipt_nonce != expected_nonce:
            return ReconciliationAssessment(
                status=ReconciliationStatus.FAKE_EVENT_REJECTED,
                reason=f"Transaction nonce mismatch: expected {expected_nonce}, got {receipt_nonce}",
                tx_hash=clean_receipt_hash,
                nonce=receipt_nonce,
                block_number=block_number,
                block_hash=block_hash_str,
                wallet_address=resolved_wallet,
                raw_receipt=dict(effective_receipt),
            )

    # 8. Audit on-chain status
    status_raw = effective_receipt.get("status")
    if status_raw is None:
        return ReconciliationAssessment(
            status=ReconciliationStatus.UNKNOWN,
            reason="Receipt status field is missing; latching execution state",
            latch_decision=LatchDecision.LATCH_REQUIRED,
            tx_hash=clean_receipt_hash,
            nonce=receipt_nonce or expected_nonce,
            block_number=block_number,
            block_hash=block_hash_str,
            wallet_address=resolved_wallet,
            raw_receipt=dict(effective_receipt),
        )

    try:
        on_chain_status = _parse_hex_or_int(status_raw)
    except Exception:
        return ReconciliationAssessment(
            status=ReconciliationStatus.UNKNOWN,
            reason=f"Receipt status field is unparseable: {status_raw}",
            latch_decision=LatchDecision.LATCH_REQUIRED,
            tx_hash=clean_receipt_hash,
            raw_receipt=dict(effective_receipt),
        )

    if on_chain_status not in (0, 1):
        return ReconciliationAssessment(
            status=ReconciliationStatus.UNKNOWN,
            reason=f"Receipt status is unmapped: {on_chain_status}",
            latch_decision=LatchDecision.LATCH_REQUIRED,
            tx_hash=clean_receipt_hash,
            raw_receipt=dict(effective_receipt),
        )

    # 9. Extract and audit gas usage and pricing
    raw_gas_used = effective_receipt.get("gasUsed")
    raw_gas_price = effective_receipt.get("effectiveGasPrice") or effective_receipt.get("gasPrice")

    if raw_gas_used is None or raw_gas_price is None:
        return ReconciliationAssessment(
            status=ReconciliationStatus.UNKNOWN,
            reason="Receipt missing gasUsed or effectiveGasPrice; cannot account for gas",
            latch_decision=LatchDecision.LATCH_REQUIRED,
            tx_hash=clean_receipt_hash,
            nonce=receipt_nonce or expected_nonce,
            block_number=block_number,
            block_hash=block_hash_str,
            wallet_address=resolved_wallet,
            raw_receipt=dict(effective_receipt),
        )

    try:
        gas_used = _parse_hex_or_int(raw_gas_used)
        effective_gas_price = _parse_hex_or_int(raw_gas_price)
    except Exception as exc:
        return ReconciliationAssessment(
            status=ReconciliationStatus.UNKNOWN,
            reason=f"Failed to parse gas quantities: {exc}",
            latch_decision=LatchDecision.LATCH_REQUIRED,
            tx_hash=clean_receipt_hash,
            raw_receipt=dict(effective_receipt),
        )

    if gas_used <= 0 or effective_gas_price <= 0:
        return ReconciliationAssessment(
            status=ReconciliationStatus.UNKNOWN,
            reason="Gas used or effective gas price is zero; real gas cost cannot be verified",
            latch_decision=LatchDecision.LATCH_REQUIRED,
            tx_hash=clean_receipt_hash,
            nonce=receipt_nonce or expected_nonce,
            block_number=block_number,
            block_hash=block_hash_str,
            wallet_address=resolved_wallet,
            raw_receipt=dict(effective_receipt),
        )

    if resolved_native_price is None:
        return ReconciliationAssessment(
            status=ReconciliationStatus.UNKNOWN,
            reason="Missing native token USD price evidence for gas cost accounting",
            latch_decision=LatchDecision.LATCH_REQUIRED,
            tx_hash=clean_receipt_hash,
            nonce=receipt_nonce or expected_nonce,
            block_number=block_number,
            block_hash=block_hash_str,
            wallet_address=resolved_wallet,
            raw_receipt=dict(effective_receipt),
        )

    actual_gas_cost_wei = gas_used * effective_gas_price
    actual_gas_cost_native = Decimal(actual_gas_cost_wei) / Decimal(10**18)
    actual_gas_cost_usd = actual_gas_cost_native * resolved_native_price

    # 10. EVM Reverted (status=0) branch
    if on_chain_status == 0:
        # Check for anomalous Transfer event logs present on EVM revert
        candidate_logs = logs if logs is not None else effective_receipt.get("logs", [])
        if candidate_logs:
            for item in candidate_logs:
                topics = item.get("topics")
                if topics and isinstance(topics, (list, tuple)):
                    if str(topics[0]).lower() == TRANSFER_EVENT_TOPIC.lower():
                        return ReconciliationAssessment(
                            status=ReconciliationStatus.FAKE_EVENT_REJECTED,
                            reason="Reverted receipt contains anomalous event logs",
                            tx_hash=clean_receipt_hash,
                            nonce=receipt_nonce or expected_nonce,
                            block_number=block_number,
                            block_hash=block_hash_str,
                            wallet_address=resolved_wallet,
                            raw_receipt=dict(effective_receipt),
                        )

        revert_details = {
            "tx_hash": clean_receipt_hash,
            "block_hash": block_hash_str,
            "block_number": block_number,
            "receipt_status": 0,
            "token_delta": 0,
            "actual_gas_native": str(actual_gas_cost_native),
            "actual_gas_usd": str(actual_gas_cost_usd),
            "net_profit_usd": str(-actual_gas_cost_usd),
        }

        return ReconciliationAssessment(
            status=ReconciliationStatus.REVERTED,
            reason=f"EVM execution reverted (status=0); verified gas loss ${actual_gas_cost_usd:.4f}",
            latch_decision=LatchDecision.NONE,
            plan_id=resolved_plan_id,
            tx_hash=clean_receipt_hash,
            nonce=receipt_nonce or expected_nonce,
            block_number=block_number,
            block_hash=block_hash_str,
            wallet_address=resolved_wallet,
            base_asset_address=resolved_base_asset,
            token_delta_atoms=0,
            token_delta_usd=Decimal("0"),
            gas_used=gas_used,
            effective_gas_price_wei=effective_gas_price,
            actual_gas_cost_wei=actual_gas_cost_wei,
            actual_gas_cost_native=actual_gas_cost_native,
            actual_gas_cost_usd=actual_gas_cost_usd,
            net_profit_usd=-actual_gas_cost_usd,
            transfers_count=0,
            confirmations=confirmations,
            details=revert_details,
            raw_receipt=dict(effective_receipt),
        )

    # 11. Success (status=1) branch: Fine-grained Transfer log netting
    receipt_logs = logs if logs is not None else effective_receipt.get("logs")
    if receipt_logs is None:
        return ReconciliationAssessment(
            status=ReconciliationStatus.UNKNOWN,
            reason="Transfer logs missing for status=1 receipt; latching execution state",
            latch_decision=LatchDecision.LATCH_REQUIRED,
            tx_hash=clean_receipt_hash,
            nonce=receipt_nonce or expected_nonce,
            block_number=block_number,
            block_hash=block_hash_str,
            wallet_address=resolved_wallet,
            raw_receipt=dict(effective_receipt),
        )

    if len(receipt_logs) == 0:
        return ReconciliationAssessment(
            status=ReconciliationStatus.UNKNOWN,
            reason="Transfer event logs empty for status=1 receipt; latching execution state",
            latch_decision=LatchDecision.LATCH_REQUIRED,
            tx_hash=clean_receipt_hash,
            nonce=receipt_nonce or expected_nonce,
            block_number=block_number,
            block_hash=block_hash_str,
            wallet_address=resolved_wallet,
            raw_receipt=dict(effective_receipt),
        )

    if not resolved_wallet:
        return ReconciliationAssessment(
            status=ReconciliationStatus.UNKNOWN,
            reason="Wallet address required to reconcile Transfer logs; latching execution state",
            latch_decision=LatchDecision.LATCH_REQUIRED,
            tx_hash=clean_receipt_hash,
            raw_receipt=dict(effective_receipt),
        )

    if not resolved_base_asset:
        return ReconciliationAssessment(
            status=ReconciliationStatus.UNKNOWN,
            reason="Target base asset address required to audit Transfer logs; latching execution state",
            latch_decision=LatchDecision.LATCH_REQUIRED,
            tx_hash=clean_receipt_hash,
            raw_receipt=dict(effective_receipt),
        )

    if resolved_base_decimals is None:
        return ReconciliationAssessment(
            status=ReconciliationStatus.UNKNOWN,
            reason="Target base decimals required for financial accounting; latching execution state",
            latch_decision=LatchDecision.LATCH_REQUIRED,
            tx_hash=clean_receipt_hash,
            raw_receipt=dict(effective_receipt),
        )

    if resolved_base_price is None:
        return ReconciliationAssessment(
            status=ReconciliationStatus.UNKNOWN,
            reason="Base asset USD price required for financial accounting; latching execution state",
            latch_decision=LatchDecision.LATCH_REQUIRED,
            tx_hash=clean_receipt_hash,
            raw_receipt=dict(effective_receipt),
        )

    # Build whitelisted counterparties set
    allowed_counterparties: set[str] = set(DEFAULT_COUNTERPARTIES)
    if plan is not None and plan.target_router:
        allowed_counterparties.add(plan.target_router.lower())
    if encoded_calldata is not None and encoded_calldata.router_address:
        allowed_counterparties.add(encoded_calldata.router_address.lower())
    if counterparties is not None:
        for cp in counterparties:
            if cp:
                allowed_counterparties.add(cp.strip().lower())

    norm_wallet = resolved_wallet.lower()
    norm_base_asset = resolved_base_asset.lower()

    seen_log_indices: set[int] = set()
    total_debits: int = 0
    total_credits: int = 0
    transfers_counted: int = 0

    for log in receipt_logs:
        # Check duplicate logIndex
        log_idx_raw = log.get("logIndex")
        if log_idx_raw is not None:
            try:
                log_idx = _parse_hex_or_int(log_idx_raw)
            except Exception:
                return ReconciliationAssessment(
                    status=ReconciliationStatus.FAKE_EVENT_REJECTED,
                    reason=f"Log contains unparseable logIndex: {log_idx_raw}",
                    tx_hash=clean_receipt_hash,
                    raw_receipt=dict(effective_receipt),
                )
            if log_idx in seen_log_indices:
                return ReconciliationAssessment(
                    status=ReconciliationStatus.FAKE_EVENT_REJECTED,
                    reason=f"Duplicate logIndex {log_idx} detected in transaction logs",
                    tx_hash=clean_receipt_hash,
                    raw_receipt=dict(effective_receipt),
                )
            seen_log_indices.add(log_idx)

        # Check reorg removed flag
        if log.get("removed") is True:
            return ReconciliationAssessment(
                status=ReconciliationStatus.FAKE_EVENT_REJECTED,
                reason="Transfer log marked as removed due to chain reorganization",
                tx_hash=clean_receipt_hash,
                raw_receipt=dict(effective_receipt),
            )

        topics = log.get("topics")
        if not topics or not isinstance(topics, (list, tuple)):
            continue

        if str(topics[0]).lower() != TRANSFER_EVENT_TOPIC.lower():
            # Non-transfer log, skip
            continue

        if len(topics) < 3:
            return ReconciliationAssessment(
                status=ReconciliationStatus.FAKE_EVENT_REJECTED,
                reason="Transfer event log has fewer than 3 topics",
                tx_hash=clean_receipt_hash,
                raw_receipt=dict(effective_receipt),
            )

        try:
            sender = _parse_topic_address(str(topics[1]))
            recipient = _parse_topic_address(str(topics[2]))
        except ValueError as exc:
            return ReconciliationAssessment(
                status=ReconciliationStatus.FAKE_EVENT_REJECTED,
                reason=f"Malformed indexed address padding in Transfer log: {exc}",
                tx_hash=clean_receipt_hash,
                raw_receipt=dict(effective_receipt),
            )

        try:
            amount = _parse_log_data_uint256(log.get("data", "0x0"))
        except Exception as exc:
            return ReconciliationAssessment(
                status=ReconciliationStatus.FAKE_EVENT_REJECTED,
                reason=f"Failed to decode Transfer log data: {exc}",
                tx_hash=clean_receipt_hash,
                raw_receipt=dict(effective_receipt),
            )

        log_addr_raw = log.get("address", "")
        if not log_addr_raw:
            return ReconciliationAssessment(
                status=ReconciliationStatus.FAKE_EVENT_REJECTED,
                reason="Transfer log missing emitting contract address",
                tx_hash=clean_receipt_hash,
                raw_receipt=dict(effective_receipt),
            )
        norm_log_addr = str(log_addr_raw).strip().lower()

        sender_lower = sender.lower()
        recipient_lower = recipient.lower()
        touches_wallet = sender_lower == norm_wallet or recipient_lower == norm_wallet

        # Security Red Line: Transfer on non-target token involving wallet is strictly intercepted
        if touches_wallet and norm_log_addr != norm_base_asset:
            return ReconciliationAssessment(
                status=ReconciliationStatus.FAKE_EVENT_REJECTED,
                reason=(
                    f"Non-target token transfer involving wallet intercepted: "
                    f"token={norm_log_addr}, expected_base={norm_base_asset}"
                ),
                tx_hash=clean_receipt_hash,
                raw_receipt=dict(effective_receipt),
            )

        if norm_log_addr != norm_base_asset:
            continue

        if not touches_wallet:
            continue

        # Audit counterparty authenticity and direction
        if sender_lower == norm_wallet and recipient_lower != norm_wallet:
            if recipient_lower not in allowed_counterparties:
                return ReconciliationAssessment(
                    status=ReconciliationStatus.FAKE_EVENT_REJECTED,
                    reason=f"Unattributed token debit to unauthorized address: {recipient}",
                    tx_hash=clean_receipt_hash,
                    raw_receipt=dict(effective_receipt),
                )
            total_debits += amount
            transfers_counted += 1

        if recipient_lower == norm_wallet and sender_lower != norm_wallet:
            if sender_lower not in allowed_counterparties:
                return ReconciliationAssessment(
                    status=ReconciliationStatus.FAKE_EVENT_REJECTED,
                    reason=f"External transfer from unauthorized address: {sender}",
                    tx_hash=clean_receipt_hash,
                    raw_receipt=dict(effective_receipt),
                )
            total_credits += amount
            transfers_counted += 1

    if transfers_counted == 0:
        return ReconciliationAssessment(
            status=ReconciliationStatus.FAKE_EVENT_REJECTED,
            reason="Zero base token transfers involving caller wallet detected in receipt",
            tx_hash=clean_receipt_hash,
            wallet_address=resolved_wallet,
            raw_receipt=dict(effective_receipt),
        )

    token_delta_atoms = total_credits - total_debits
    if token_delta_atoms <= 0:
        return ReconciliationAssessment(
            status=ReconciliationStatus.FAKE_EVENT_REJECTED,
            reason=(
                f"Transfer events do not demonstrate net base token increase: "
                f"token_delta={token_delta_atoms} <= 0"
            ),
            tx_hash=clean_receipt_hash,
            wallet_address=resolved_wallet,
            token_delta_atoms=token_delta_atoms,
            raw_receipt=dict(effective_receipt),
        )

    # Check that debit does not exceed plan amount_in
    if plan is not None and total_debits > plan.amount_in.atoms:
        return ReconciliationAssessment(
            status=ReconciliationStatus.FAKE_EVENT_REJECTED,
            reason=(
                f"Actual token debit ({total_debits}) exceeds planned amount_in "
                f"({plan.amount_in.atoms})"
            ),
            tx_hash=clean_receipt_hash,
            wallet_address=resolved_wallet,
            token_delta_atoms=token_delta_atoms,
            raw_receipt=dict(effective_receipt),
        )

    token_delta_usd = (
        Decimal(token_delta_atoms) / Decimal(10**resolved_base_decimals)
    ) * resolved_base_price
    validate_non_negative_decimal(token_delta_usd, "token_delta_usd")
    net_profit_usd = token_delta_usd - actual_gas_cost_usd

    if require_positive_net_profit and net_profit_usd <= Decimal("0"):
        return ReconciliationAssessment(
            status=ReconciliationStatus.FAKE_EVENT_REJECTED,
            reason=f"Execution failed to produce positive net profit after gas deduction: ${net_profit_usd:.4f}",
            tx_hash=clean_receipt_hash,
            wallet_address=resolved_wallet,
            token_delta_atoms=token_delta_atoms,
            token_delta_usd=token_delta_usd,
            net_profit_usd=net_profit_usd,
            raw_receipt=dict(effective_receipt),
        )

    accounting_details = {
        "tx_hash": clean_receipt_hash,
        "block_hash": block_hash_str,
        "block_number": block_number,
        "receipt_status": 1,
        "token_delta": token_delta_atoms,
        "token_delta_usd": str(token_delta_usd),
        "actual_gas_native": str(actual_gas_cost_native),
        "actual_gas_usd": str(actual_gas_cost_usd),
        "net_profit_usd": str(net_profit_usd),
    }

    # Optional coordinator slot release integration
    if (
        coordinator is not None
        and hasattr(coordinator, "release_execution_slot")
        and reserved is not None
    ):
        try:
            coordinator.release_execution_slot(
                reserved,
                receipt={
                    "status": 1,
                    "actual_gas_usd": actual_gas_cost_usd,
                    "net_profit_usd": net_profit_usd,
                    "tx_hash": clean_receipt_hash,
                    "block_hash": block_hash_str,
                },
            )
        except Exception:
            pass

    return ReconciliationAssessment(
        status=ReconciliationStatus.VERIFIED,
        reason="On-chain receipt and ERC-20 transfer logs audited and verified",
        latch_decision=LatchDecision.NONE,
        plan_id=resolved_plan_id,
        tx_hash=clean_receipt_hash,
        nonce=receipt_nonce or expected_nonce,
        block_number=block_number,
        block_hash=block_hash_str,
        wallet_address=resolved_wallet,
        base_asset_address=resolved_base_asset,
        token_delta_atoms=token_delta_atoms,
        token_delta_usd=token_delta_usd,
        gas_used=gas_used,
        effective_gas_price_wei=effective_gas_price,
        actual_gas_cost_wei=actual_gas_cost_wei,
        actual_gas_cost_native=actual_gas_cost_native,
        actual_gas_cost_usd=actual_gas_cost_usd,
        net_profit_usd=net_profit_usd,
        transfers_count=transfers_counted,
        confirmations=confirmations,
        details=accounting_details,
        raw_receipt=dict(effective_receipt),
    )


# Functional alias
reconcile = reconcile_execution


class PureReconciler:
    """Pure functional deterministic reconciler bound to security guardrails."""

    def __init__(
        self,
        coordinator: Any | None = None,
        *,
        default_counterparties: Iterable[str] | None = None,
        min_confirmations: int = 1,
        default_native_price_usd: Decimal | str | int | None = None,
        default_fee_model: str = "arbitrum_nitro_total",
    ) -> None:
        self.coordinator = coordinator
        self.default_counterparties = (
            frozenset(cp.strip().lower() for cp in default_counterparties)
            if default_counterparties is not None
            else frozenset()
        )
        self.min_confirmations = min_confirmations
        self.default_native_price_usd = (
            Decimal(str(default_native_price_usd)) if default_native_price_usd is not None else None
        )
        self.default_fee_model = default_fee_model

    def reconcile(
        self,
        receipt: Mapping[str, Any] | None = None,
        *,
        expected_tx_hash: str | None = None,
        expected_nonce: int | None = None,
        wallet: str | None = None,
        base_symbol: str | None = None,
        counterparties: Iterable[str] | None = None,
        native_price_usd: Decimal | str | int | None = None,
        base_price_usd: Decimal | str | int | None = None,
        fee_model: str | None = None,
        event: Mapping[str, Any] | None = None,
        plan: ExecutionPlan | None = None,
        encoded_calldata: EncodedCalldata | None = None,
        logs: Sequence[Mapping[str, Any]] | None = None,
        trace: Mapping[str, Any] | None = None,
        timeout: bool = False,
        coordinator: Any | None = None,
        reserved: Any | None = None,
        hold_flight_lock_on_unknown: bool = False,
        current_block_number: int | None = None,
        min_confirmations: int | None = None,
        require_positive_net_profit: bool = False,
        **kwargs: Any,
    ) -> ReconciliationAssessment:
        """Evaluate execution evidence and return a four-state ReconciliationAssessment."""
        eff_coord = coordinator if coordinator is not None else self.coordinator
        eff_native_price = (
            native_price_usd if native_price_usd is not None else self.default_native_price_usd
        )
        eff_min_conf = (
            min_confirmations if min_confirmations is not None else self.min_confirmations
        )

        merged_cps: set[str] = set(self.default_counterparties)
        if counterparties is not None:
            merged_cps.update(cp.strip().lower() for cp in counterparties if cp)

        event_dict: dict[str, Any] = dict(event) if event is not None else {}
        if base_symbol and "base_symbol" not in event_dict:
            event_dict["base_symbol"] = base_symbol
        if wallet and "wallet" not in event_dict:
            event_dict["wallet"] = wallet

        return reconcile_execution(
            receipt=receipt,
            plan=plan,
            encoded_calldata=encoded_calldata,
            logs=logs,
            trace=trace,
            expected_tx_hash=expected_tx_hash,
            expected_nonce=expected_nonce,
            wallet_address=wallet,
            base_asset_usd_price=base_price_usd,
            native_token_usd_price=eff_native_price,
            counterparties=merged_cps,
            current_block_number=current_block_number,
            min_confirmations=eff_min_conf,
            timeout=timeout,
            event=event_dict if event_dict else None,
            require_positive_net_profit=require_positive_net_profit,
            coordinator=eff_coord,
            reserved=reserved,
            hold_flight_lock_on_unknown=hold_flight_lock_on_unknown,
            **kwargs,
        )


# Class alias for backward compatibility
ExecutionReconciler = PureReconciler

__all__ = [
    "CANONICAL_PERMIT2",
    "CANONICAL_UNIVERSAL_ROUTER",
    "DEFAULT_COUNTERPARTIES",
    "ExecutionReconciler",
    "LatchDecision",
    "PureReconciler",
    "ReconciliationAssessment",
    "ReconciliationResult",
    "ReconciliationStatus",
    "TRANSFER_EVENT_TOPIC",
    "reconcile",
    "reconcile_execution",
]
