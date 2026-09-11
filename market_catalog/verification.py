"""Offline protocol/parameter consistency checks, not live identity or trading approval."""

from __future__ import annotations

from dataclasses import dataclass

from arbitrage_contracts.identity import (
    AssetRef,
    FeeModel,
    PoolDescriptor,
    TokenKey,
    validate_bytes32,
    validate_evm_address,
    validate_positive_integer,
)

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
WETH_ADDRESS = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
DYNAMIC_FEE_FLAG = 0x800000
# Local evidence: 20260908-robinhood-deployments.md and the existing catalog.py
# DEX_FACTORIES snapshot. W1-C authorizes protocol alignment, not live code attestation.
# Tuples keep the audited registry immutable; do not import the legacy parent package.
DEPLOYMENTS: tuple[tuple[int, str, str, str], ...] = (
    (4663, "uniswap-v2", "factory", "0x8bceaa40b9acdfaedf85adf4ff01f5ad6517937f"),
    (4663, "uniswap-v3", "factory", "0x1f7d7550b1b028f7571e69a784071f0205fd2efa"),
    (4663, "up-v3", "factory", "0x1ac9db4a2608ba45d6127b1737949b51bb54b7f3"),
    (4663, "ramses-v3", "factory", "0xe0c4ceb92d08ca985bb70fe0a22feb121a9854a8"),
    (4663, "uniswap-v4", "manager", "0x8366a39cc670b4001a1121b8f6a443a643e40951"),
)


@dataclass(frozen=True)
class VerificationResult:
    """Parameters can match without source authentication, eligibility or executable depth."""

    status: str
    reasons: tuple[str, ...]
    fee_model: FeeModel
    review_status: str = "pending_review"
    can_quote: str = "unknown"
    can_simulate: str = "unknown"
    can_atomic_execute: str = "unsupported"


def currency_asset(chain_id: int, address: str) -> AssetRef:
    """Map the V4 zero currency to native ETH, never to the WETH ERC20 key."""
    validate_positive_integer(chain_id, "chain_id")
    address = validate_evm_address(address).lower()
    if address == ZERO_ADDRESS:
        return AssetRef.native(chain_id, "ETH")
    return AssetRef.erc20(TokenKey(chain_id, address))


def _integer(value: int, name: str, lower: int, upper: int) -> None:
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer")
    if not lower <= value <= upper:
        raise ValueError(f"{name} outside {lower}..{upper}")


def compute_v4_pool_id(
    currency0: str, currency1: str, fee: int, tick_spacing: int, hooks: str
) -> str:
    """Hash the exact ordered PoolKey using Ethereum Keccak and standard ABI encoding."""
    # Definition paths are the exact public eth_abi.encode / eth_utils.keccak objects.
    # Lazy imports preserve the catalog's original zero-network-module import boundary.
    from eth_abi.abi import encode
    from eth_utils.crypto import keccak

    currency0 = validate_evm_address(currency0).lower()
    currency1 = validate_evm_address(currency1).lower()
    hooks = validate_evm_address(hooks).lower()
    if int(currency0, 16) >= int(currency1, 16):
        raise ValueError("V4 requires currency0 < currency1; input is never reordered")
    _integer(fee, "fee", 0, (1 << 24) - 1)
    _integer(tick_spacing, "tick_spacing", 1, (1 << 15) - 1)
    encoded = encode(
        ["address", "address", "uint24", "int24", "address"],
        [currency0, currency1, fee, tick_spacing, hooks],
    )
    return "0x" + keccak(encoded).hex()


def verify_v4_pool_id(
    pool_id: str, currency0: str, currency1: str, fee: int, tick_spacing: int, hooks: str
) -> bool:
    """Reject malformed/truncated IDs and compare every field of the original PoolKey."""
    validate_bytes32(pool_id)
    return pool_id.lower() == compute_v4_pool_id(currency0, currency1, fee, tick_spacing, hooks)


def v3_fee_model(raw_fee: int) -> FeeModel:
    """Convert explicit standard V3 fee units to an exact ratio (100 raw = 1 bps)."""
    _integer(raw_fee, "fee", 0, 999_999)
    return FeeModel.static(
        raw_fee, unit="hundredths_of_bip", numerator=raw_fee, denominator=1_000_000
    )


def _currency_address(asset: AssetRef) -> str:
    if asset.interface_kind == "native":
        if asset.native_identifier != "ETH" or asset.balance_domain_id is not None:
            raise ValueError("Unsupported native currency domain")
        return ZERO_ADDRESS
    if asset.token_key is None or asset.token_key.address == ZERO_ADDRESS:
        raise ValueError("Zero address cannot be ERC20")
    if asset.balance_domain_id is not None:
        raise ValueError("Unsupported balance domain")
    return asset.token_key.address


def verify_pool(pool: PoolDescriptor) -> VerificationResult:
    """Check registered venue and parameters; all discoveries still require source review.

    No symbol, TVL, API label, or declaration of evidence can approve a pool here.
    Fork fee units lacking audited deployment-specific conversion stay unknown.
    """
    key = pool.key
    unknown = FeeModel.unknown()
    if key.protocol_id == "giga-v3":
        return VerificationResult("pending_review", ("AUTH_EVIDENCE_PENDING",), unknown)
    deployment = (key.chain_id, key.protocol_id, key.venue_kind, key.canonical_venue_address)
    if deployment not in DEPLOYMENTS:
        return VerificationResult("rejected", ("UNREGISTERED_PROTOCOL_OR_VENUE",), unknown)
    v4 = key.protocol_id == "uniswap-v4"
    if key.pool_id_kind != ("bytes32" if v4 else "address"):
        return VerificationResult("rejected", ("POOL_ID_KIND_MISMATCH",), unknown)
    try:
        c0, c1 = _currency_address(pool.currency0), _currency_address(pool.currency1)
        if int(c0, 16) >= int(c1, 16):
            raise ValueError("Currency order conflict")
        if not v4 and (c0 == ZERO_ADDRESS or key.canonical_pool_id == ZERO_ADDRESS):
            raise ValueError("V2/V3 require nonzero token and pool contracts")
        fee = pool.fee_model
        if v4:
            if fee.raw_value is None or pool.tick_spacing is None or pool.hooks is None:
                return VerificationResult("pending_review", ("MISSING_V4_PARAMETERS",), unknown)
            if not verify_v4_pool_id(
                key.pool_id, c0, c1, fee.raw_value, pool.tick_spacing, pool.hooks
            ):
                return VerificationResult("rejected", ("POOL_ID_MISMATCH",), unknown)
            if fee.raw_value & DYNAMIC_FEE_FLAG:
                return VerificationResult(
                    "unsupported",
                    ("DYNAMIC_FEE_ADAPTER_REQUIRED",),
                    FeeModel.dynamic(hook_ref=pool.hooks, raw_value=fee.raw_value),
                )
            if pool.hooks.lower() != ZERO_ADDRESS:
                return VerificationResult("unsupported", ("HOOK_ADAPTER_REQUIRED",), unknown)
            _integer(fee.raw_value, "static V4 fee", 0, 1_000_000)
        else:
            if pool.hooks is not None:
                raise ValueError("Hooks are not a V2/V3 parameter")
            if key.protocol_id == "uniswap-v2":
                if pool.tick_spacing is not None:
                    raise ValueError("V2 cannot declare tick spacing")
                if fee.raw_value is not None:
                    _integer(fee.raw_value, "V2 raw fee", 0, (1 << 24) - 1)
                # No generic 30-bps fallback from a DEX name or fee() assumption.
                return VerificationResult("pending_review", ("V2_FEE_EVIDENCE_REQUIRED",), unknown)
            if pool.tick_spacing is None:
                return VerificationResult("pending_review", ("MISSING_TICK_SPACING",), unknown)
            _integer(pool.tick_spacing, "tick_spacing", 1, (1 << 23) - 1)
        if fee.kind == "dynamic":
            return VerificationResult("unsupported", ("DYNAMIC_FEE_ADAPTER_REQUIRED",), fee)
        if fee.kind == "unknown" or fee.raw_value is None:
            return VerificationResult("pending_review", ("FEE_EVIDENCE_REQUIRED",), unknown)
        _integer(fee.raw_value, "fee", 0, 1_000_000 if v4 else 999_999)
        if key.protocol_id in ("up-v3", "ramses-v3"):
            return VerificationResult(
                "pending_review", ("FORK_FEE_UNIT_EVIDENCE_REQUIRED",), unknown
            )
        if fee.unit != "hundredths_of_bip":
            raise ValueError("Fee unit mismatch")
        if fee.hook_ref is not None or fee.model_version is not None:
            raise ValueError("Static fee contains dynamic metadata")
        if (fee.numerator is not None or fee.denominator is not None) and (
            fee.numerator != fee.raw_value or fee.denominator != 1_000_000
        ):
            raise ValueError("Fee ratio mismatch")
        normalized = FeeModel.static(
            fee.raw_value,
            "hundredths_of_bip",
            fee.raw_value,
            1_000_000,
            evidence_ref=fee.evidence_ref,
        )
        if pool.deployment_status != "deployed":
            return VerificationResult("pending_review", ("DEPLOYMENT_UNCONFIRMED",), normalized)
        return VerificationResult(
            "parameters_verified", ("SOURCE_AUTHENTICATION_REQUIRED",), normalized
        )
    except (ValueError, TypeError) as exc:
        return VerificationResult("rejected", (f"INVALID_PARAMETERS: {exc}",), unknown)
