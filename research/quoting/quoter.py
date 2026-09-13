"""Unified DEX Quoting Engine for Uniswap V3 and V4.

Provides multi-hop and single-hop chained on-chain quote queries using
Uniswap V3 QuoterV2 and Uniswap V4 Quoter contracts. Integrates directly
with domain data contracts (QuoteResult, QuoteStatus, TokenAmount,
CandidateRoute, RouteHop) and supports both live RPC clients and strict
ReplayAdapter.

Key Architectural Guarantees:
1. Invariant Preservation: Failed quotes strictly return amount_out=None and delta_atoms=None.
2. Error Classification Parity: Precisely classifies CONTRACT_REVERT (including nested
   UnexpectedRevertBytes -> NotEnoughLiquidity), NODE_LIMITATION (archive requests),
   and RPC_ERROR.
3. Fee Non-Duplication: Quoter outputs already account for DEX pool fees and slippage;
   no duplicate fee deduction is ever applied to amount_out or delta.
4. Price Fault Tolerance: Missing token USD prices strictly return None; NEVER default
   or fabricate $1.00 USD.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from web3 import Web3

from research.market_data.types import (
    CandidateRoute,
    PoolIdentity,
    QuoteResult,
    QuoteStatus,
    RouteHop,
    TokenAmount,
    TokenIdentity,
)
from research.quoting.replay_adapter import ReplayAdapter

logger = logging.getLogger(__name__)

# Canonical Robinhood Chain Contract Addresses
DEFAULT_V3_QUOTER: str = "0x33e885eD0Ec9bF04EcfB19341582aADCb4c8A9E7"
DEFAULT_V4_QUOTER: str = "0x8Dc178eFB8111BB0973Dd9d722ebeFF267c98F94"
ZERO_ADDRESS: str = "0x0000000000000000000000000000000000000000"

# Pre-computed 4-byte ABI function selectors
# V3: quoteExactInputSingle((address,address,uint256,uint24,uint160)) -> 0xc6a5026a
V3_QUOTE_SELECTOR: bytes = Web3.keccak(
    text="quoteExactInputSingle((address,address,uint256,uint24,uint160))"
)[:4]

# V4: quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes)) -> 0xaa9d21cb
V4_QUOTE_SELECTOR: bytes = Web3.keccak(
    text="quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))"
)[:4]

# Canonical Robinhood Token Definitions for Route A / B
CANONICAL_WETH = TokenIdentity(
    chain_id=4663,
    address="0x0bd7d308f8e1639fab988df18a8011f41eacad73",
    decimals=18,
    symbol="WETH",
)
CANONICAL_CASHCAT = TokenIdentity(
    chain_id=4663,
    address="0x020bfc650a365f8bb26819deaabf3e21291018b4",
    decimals=18,
    symbol="CASHCAT",
)
CANONICAL_PONS = TokenIdentity(
    chain_id=4663,
    address="0x39dbed3a2bd333467115de45665cc57f813c4571",
    decimals=18,
    symbol="PONS",
)
CANONICAL_USDG = TokenIdentity(
    chain_id=4663,
    address="0x5fc5360d0400a0fd4f2af552add042d716f1d168",
    decimals=6,
    symbol="USDG",
)

# Canonical historical replay tiers
CANONICAL_WETH_TIERS: tuple[int, ...] = (
    10**15,  # 0.001 WETH
    3 * 10**15,  # 0.003 WETH
    10**16,  # 0.010 WETH
    3 * 10**16,  # 0.030 WETH
)
CANONICAL_USDG_TIERS: tuple[int, ...] = (
    3 * 10**6,  # 3 USDG
    10 * 10**6,  # 10 USDG
    30 * 10**6,  # 30 USDG
    100 * 10**6,  # 100 USDG
)


def is_v4_pool(pool: PoolIdentity) -> bool:
    """Determine whether pool is a Uniswap V4 pool."""
    proto = getattr(pool, "protocol", "").lower()
    if "v4" in proto:
        return True
    clean_id = getattr(pool, "pool_id", "").strip().lower()
    return clean_id.startswith("0x") and len(clean_id) == 66


def is_v3_pool(pool: PoolIdentity) -> bool:
    """Determine whether pool is a Uniswap V3 or compatible pool."""
    if is_v4_pool(pool):
        return False
    proto = getattr(pool, "protocol", "").lower()
    if "v3" in proto:
        return True
    clean_id = getattr(pool, "pool_id", "").strip().lower()
    return clean_id.startswith("0x") and len(clean_id) == 42


def fee_bps_to_raw(fee_bps: float) -> int:
    """Convert fee in basis points to integer fee units for contract calls.

    Examples:
        30.0 bps (0.3%) -> 3000
        12.5 bps (0.125%) -> 1250
        26.9 bps (0.269%) -> 2690
        5.0 bps (0.05%) -> 500
    """
    if fee_bps > 1000:
        return int(fee_bps)
    return int(round(fee_bps * 100))


def encode_v3_quoter_calldata(
    token_in: str,
    token_out: str,
    fee: int,
    amount_in: int,
    sqrt_price_limit_x96: int = 0,
) -> str:
    """Encode calldata for Uniswap V3 QuoterV2 quoteExactInputSingle.

    Signature: quoteExactInputSingle((address,address,uint256,uint24,uint160))
    """
    params = (
        Web3.to_checksum_address(token_in),
        Web3.to_checksum_address(token_out),
        int(amount_in),
        int(fee),
        int(sqrt_price_limit_x96),
    )
    encoded = abi_encode(["(address,address,uint256,uint24,uint160)"], [params])
    return "0x" + (V3_QUOTE_SELECTOR + encoded).hex()


def encode_v4_quoter_calldata(
    currency0: str,
    currency1: str,
    fee: int,
    tick_spacing: int,
    hooks: str | None,
    zero_for_one: bool,
    exact_amount: int,
    hook_data: bytes = b"",
) -> str:
    """Encode calldata for Uniswap V4 Quoter quoteExactInputSingle.

    Signature: quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))
    """
    c0 = currency0
    c1 = currency1
    if int(c0, 16) > int(c1, 16):
        c0, c1 = c1, c0

    pool_key = (
        Web3.to_checksum_address(c0),
        Web3.to_checksum_address(c1),
        int(fee),
        int(tick_spacing),
        Web3.to_checksum_address(hooks or ZERO_ADDRESS),
    )
    encoded = abi_encode(
        ["((address,address,uint24,int24,address),bool,uint128,bytes)"],
        [(pool_key, bool(zero_for_one), int(exact_amount), hook_data)],
    )
    return "0x" + (V4_QUOTE_SELECTOR + encoded).hex()


def decode_v3_quoter_result(raw_hex: str) -> tuple[int, int]:
    """Decode Uniswap V3 QuoterV2 output: (amountOut, sqrtPriceX96After, initializedTicksCrossed, gasEstimate).

    Returns:
        (amount_out, gas_estimate)
    """
    clean = raw_hex[2:] if raw_hex.startswith("0x") else raw_hex
    raw_bytes = bytes.fromhex(clean)
    amount_out, _, _, gas_est = abi_decode(
        ["uint256", "uint160", "uint32", "uint256"], raw_bytes
    )
    return int(amount_out), int(gas_est)


def decode_v4_quoter_result(raw_hex: str) -> tuple[int, int]:
    """Decode Uniswap V4 Quoter output: (amountOut, gasEstimate).

    Returns:
        (amount_out, gas_estimate)
    """
    clean = raw_hex[2:] if raw_hex.startswith("0x") else raw_hex
    raw_bytes = bytes.fromhex(clean)
    amount_out, gas_est = abi_decode(["uint256", "uint256"], raw_bytes)
    return int(amount_out), int(gas_est)


def classify_rpc_error(res: dict[str, Any]) -> dict[str, Any]:
    """Classify JSON-RPC error response strictly following M1 precision rules.

    Differentiates:
    - CONTRACT_REVERT (code 3 or valid revert hex data, decoding UnexpectedRevertBytes
      and inner NotEnoughLiquidity pool_id)
    - NODE_LIMITATION (code -32602 with archive/historical block limitation message)
    - Rejects ambiguous string matching and validates bytes32 lengths.

    Raises:
        ValueError: If error payload is malformed, unrecognized, or truncated.
        TypeError: If error field is not a dictionary.
    """
    if not isinstance(res, dict) or "error" not in res:
        raise ValueError("Invalid RPC error response structure: missing 'error' key")

    err = res.get("error")
    if not isinstance(err, dict):
        raise TypeError(f"Malformed RPC error payload: expected dict, got {type(err)}")

    code = err.get("code")
    msg = str(err.get("message", ""))
    data = err.get("data")

    # 1. Node limitation check (-32602 archive limit)
    if code == -32602:
        if "archive" in msg.lower() or "historical block" in msg.lower():
            return {
                "category": QuoteStatus.NODE_LIMITATION,
                "code": code,
                "message": msg,
                "selector": None,
                "nested_selector": None,
                "pool_id": None,
                "revert_reason": None,
                "data": data,
            }
        raise ValueError(f"RPC invalid params error (-32602): {msg}")

    # 2. Contract revert check
    is_code_3 = code == 3
    has_hex_data = isinstance(data, str) and data.startswith("0x") and len(data) >= 10

    if is_code_3 or has_hex_data:
        if has_hex_data:
            assert isinstance(data, str)
            sel = data[2:10].lower()
            if sel == "6190b2b0":  # UnexpectedRevertBytes(bytes)
                try:
                    raw_payload = bytes.fromhex(data[10:])
                    inner_bytes = abi_decode(["bytes"], raw_payload)[0]
                except Exception as exc:
                    raise ValueError(
                        f"Truncated or corrupted UnexpectedRevertBytes ABI: {exc}"
                    ) from exc

                inner_hex = inner_bytes.hex()
                if len(inner_hex) < 8:
                    raise ValueError(f"Truncated inner revert data: length {len(inner_hex)} < 8")

                nested_sel = inner_hex[:8].lower()
                if nested_sel == "7a5ed734":  # NotEnoughLiquidity(bytes32 poolId)
                    pool_id_hex = inner_hex[8:]
                    if len(pool_id_hex) < 64:
                        raise ValueError(
                            f"Truncated bytes32 for NotEnoughLiquidity pool_id: "
                            f"got {len(pool_id_hex)} hex chars, expected 64"
                        )
                    pool_id = "0x" + pool_id_hex[:64]
                    return {
                        "category": QuoteStatus.CONTRACT_REVERT,
                        "code": code,
                        "message": msg,
                        "selector": "0x6190b2b0",
                        "nested_selector": "0x7a5ed734",
                        "pool_id": pool_id,
                        "revert_reason": "NotEnoughLiquidity",
                        "data": data,
                    }
                return {
                    "category": QuoteStatus.CONTRACT_REVERT,
                    "code": code,
                    "message": msg,
                    "selector": "0x6190b2b0",
                    "nested_selector": "0x" + nested_sel,
                    "pool_id": None,
                    "revert_reason": "UnexpectedRevertBytes",
                    "data": data,
                }
            return {
                "category": QuoteStatus.CONTRACT_REVERT,
                "code": code,
                "message": msg,
                "selector": "0x" + sel,
                "nested_selector": None,
                "pool_id": None,
                "revert_reason": None,
                "data": data,
            }
        return {
            "category": QuoteStatus.CONTRACT_REVERT,
            "code": code,
            "message": msg,
            "selector": None,
            "nested_selector": None,
            "pool_id": None,
            "revert_reason": None,
            "data": data,
        }

    raise ValueError(f"Unknown or unhandled RPC error: code={code}, message='{msg}'")


def estimate_usd_profit(
    quote_result: QuoteResult,
    token_usd_price: Decimal | float | int | str | None,
) -> Decimal | None:
    """Calculate USD profit from QuoteResult without float precision loss.

    Strict Safety Red Line:
    When token_usd_price is None, NEVER fabricate or fallback to $1.00 USD.
    Strictly return None.
    """
    if token_usd_price is None:
        return None
    if quote_result.status != QuoteStatus.QUOTED:
        return None
    if quote_result.delta_atoms is None:
        return None

    price_dec = Decimal(str(token_usd_price))
    decimals = quote_result.amount_in.token.decimals
    delta_dec = Decimal(quote_result.delta_atoms) / (Decimal(10) ** decimals)
    return delta_dec * price_dec


def build_canonical_route_a() -> CandidateRoute:
    """Build canonical 3-hop Route A: WETH -> CASHCAT -> PONS -> WETH."""
    p_weth_cc = PoolIdentity(
        chain_id=4663,
        protocol="uniswap_v3",
        pool_id="0xd42a491087a15e5afd51feb3606066cc152d2b09",
        token0=CANONICAL_CASHCAT.address,
        token1=CANONICAL_WETH.address,
        fee_bps=30.0,
        tick_spacing=60,
    )
    p_cc_pons = PoolIdentity(
        chain_id=4663,
        protocol="uniswap_v4",
        pool_id="0x3ee7a201318a8b5dfeafdb8a6d1e80d8a1909858d9d2ce10c79ea3b315731f32",
        token0=CANONICAL_CASHCAT.address,
        token1=CANONICAL_PONS.address,
        fee_bps=12.5,
        tick_spacing=13,
        hooks=ZERO_ADDRESS,
    )
    p_pons_weth = PoolIdentity(
        chain_id=4663,
        protocol="uniswap_v3",
        pool_id="0xed50bdeea8adc232f159486192a4157281d722ff",
        token0=CANONICAL_WETH.address,
        token1=CANONICAL_PONS.address,
        fee_bps=30.0,
        tick_spacing=60,
    )
    return CandidateRoute(
        candidate_id="route_a_weth_cycle",
        route_type="triangular",
        base_token=CANONICAL_WETH,
        hops=(
            RouteHop(pool=p_weth_cc, token_in=CANONICAL_WETH, token_out=CANONICAL_CASHCAT),
            RouteHop(pool=p_cc_pons, token_in=CANONICAL_CASHCAT, token_out=CANONICAL_PONS),
            RouteHop(pool=p_pons_weth, token_in=CANONICAL_PONS, token_out=CANONICAL_WETH),
        ),
        observed_gross_bps=606.0,
        snapshot_block=58239319,
        created_at=0.0,
    )


def build_canonical_route_b() -> CandidateRoute:
    """Build canonical 3-hop Route B: USDG -> CASHCAT -> PONS -> USDG."""
    p_usdg_cc = PoolIdentity(
        chain_id=4663,
        protocol="uniswap_v4",
        pool_id="0xa92a3df27a00a276183ff7265fd8affa11df1fe8bb23ddfaf13f6c879a3f818b",
        token0=CANONICAL_CASHCAT.address,
        token1=CANONICAL_USDG.address,
        fee_bps=26.9,
        tick_spacing=54,
        hooks=ZERO_ADDRESS,
    )
    p_cc_pons = PoolIdentity(
        chain_id=4663,
        protocol="uniswap_v4",
        pool_id="0x3ee7a201318a8b5dfeafdb8a6d1e80d8a1909858d9d2ce10c79ea3b315731f32",
        token0=CANONICAL_CASHCAT.address,
        token1=CANONICAL_PONS.address,
        fee_bps=12.5,
        tick_spacing=13,
        hooks=ZERO_ADDRESS,
    )
    p_pons_usdg = PoolIdentity(
        chain_id=4663,
        protocol="uniswap_v4",
        pool_id="0x4be9657ec9002e528f4f17a5c43edc525a07f888f7b180c2afbf75e096c4f38a",
        token0=CANONICAL_PONS.address,
        token1=CANONICAL_USDG.address,
        fee_bps=30.0,
        tick_spacing=60,
        hooks=ZERO_ADDRESS,
    )
    return CandidateRoute(
        candidate_id="route_b_usdg_cycle",
        route_type="triangular",
        base_token=CANONICAL_USDG,
        hops=(
            RouteHop(pool=p_usdg_cc, token_in=CANONICAL_USDG, token_out=CANONICAL_CASHCAT),
            RouteHop(pool=p_cc_pons, token_in=CANONICAL_CASHCAT, token_out=CANONICAL_PONS),
            RouteHop(pool=p_pons_usdg, token_in=CANONICAL_PONS, token_out=CANONICAL_USDG),
        ),
        observed_gross_bps=589.0,
        snapshot_block=58239319,
        created_at=0.0,
    )


class Quoter:
    """Public DEX Quoting Engine for Uniswap V3 and V4."""

    def __init__(
        self,
        rpc_client: Any,
        v3_quoter: str = DEFAULT_V3_QUOTER,
        v4_quoter: str = DEFAULT_V4_QUOTER,
    ) -> None:
        """Initialize Quoter with RPC provider and quoter addresses.

        Args:
            rpc_client: RPC client implementing `.call(method, params, block_identifier)`.
            v3_quoter: Address of Uniswap V3 QuoterV2 contract.
            v4_quoter: Address of Uniswap V4 Quoter contract.
        """
        self.rpc: Any = rpc_client
        self.v3_quoter: str = Web3.to_checksum_address(v3_quoter)
        self.v4_quoter: str = Web3.to_checksum_address(v4_quoter)

    def quote_hop(
        self,
        hop: RouteHop,
        amount_in: TokenAmount | int,
        block_identifier: str | int | None = None,
        quote_mode: str = "live",
    ) -> QuoteResult:
        """Execute on-chain quote for a single swap hop.

        Args:
            hop: RouteHop containing pool identity, token_in, and token_out.
            amount_in: Input amount as TokenAmount or integer atoms.
            block_identifier: Target block number or hex identifier.
            quote_mode: Quote execution mode ('live', 'historical_replay', 'synthetic').

        Returns:
            QuoteResult with status QUOTED on success, or classified failure status.
        """
        if isinstance(amount_in, int):
            in_token_amount = TokenAmount(token=hop.token_in, atoms=amount_in)
        elif isinstance(amount_in, TokenAmount):
            if amount_in.token.address.lower() != hop.token_in.address.lower():
                raise ValueError(
                    f"amount_in token {amount_in.token.address} does not match "
                    f"hop token_in {hop.token_in.address}"
                )
            in_token_amount = amount_in
        else:
            raise TypeError(
                f"amount_in must be TokenAmount or int, got {type(amount_in).__name__}"
            )

        block_num: int | None = None
        if block_identifier is not None:
            if isinstance(block_identifier, str) and block_identifier.startswith("0x"):
                try:
                    block_num = int(block_identifier, 16)
                except ValueError:
                    pass
            elif isinstance(block_identifier, int):
                block_num = block_identifier
                block_identifier = hex(block_identifier)

        # 1. Calldata encoding
        fee_raw = fee_bps_to_raw(hop.pool.fee_bps)
        if is_v4_pool(hop.pool):
            c0 = hop.pool.token0
            c1 = hop.pool.token1
            if int(c0, 16) > int(c1, 16):
                c0, c1 = c1, c0
            zero_for_one = hop.token_in.address.lower() == c0.lower()
            calldata = encode_v4_quoter_calldata(
                currency0=c0,
                currency1=c1,
                fee=fee_raw,
                tick_spacing=hop.pool.tick_spacing,
                hooks=hop.pool.hooks,
                zero_for_one=zero_for_one,
                exact_amount=in_token_amount.atoms,
            )
            target = self.v4_quoter
        else:
            calldata = encode_v3_quoter_calldata(
                token_in=hop.token_in.address,
                token_out=hop.token_out.address,
                fee=fee_raw,
                amount_in=in_token_amount.atoms,
            )
            target = self.v3_quoter

        # 2. Call RPC
        call_obj: dict[str, str] = {"to": target, "data": calldata}
        call_params = (
            [call_obj, block_identifier] if block_identifier is not None else [call_obj]
        )

        try:
            res = self.rpc.call(
                "eth_call",
                call_params,
                block_identifier=block_identifier,
            )
        except RuntimeError:
            # Propagate ReplayAdapter parity errors
            raise
        except Exception as exc:
            return QuoteResult(
                status=QuoteStatus.RPC_ERROR,
                amount_in=in_token_amount,
                amount_out=None,
                delta_atoms=None,
                error_message=str(exc),
                block_number=block_num,
                quote_mode=quote_mode,
            )

        # 3. Process RPC response
        if not isinstance(res, dict):
            return QuoteResult(
                status=QuoteStatus.INVALID_RESPONSE,
                amount_in=in_token_amount,
                amount_out=None,
                delta_atoms=None,
                error_message=f"Non-dict response received from RPC: {res}",
                block_number=block_num,
                quote_mode=quote_mode,
            )

        if "error" in res:
            try:
                diag = classify_rpc_error(res)
                return QuoteResult(
                    status=QuoteStatus(diag["category"]),
                    amount_in=in_token_amount,
                    amount_out=None,
                    delta_atoms=None,
                    error_code=diag.get("code"),
                    error_message=diag.get("message"),
                    raw_revert_data=diag.get("data"),
                    block_number=block_num,
                    quote_mode=quote_mode,
                )
            except ValueError as val_err:
                return QuoteResult(
                    status=QuoteStatus.RPC_ERROR,
                    amount_in=in_token_amount,
                    amount_out=None,
                    delta_atoms=None,
                    error_message=str(val_err),
                    block_number=block_num,
                    quote_mode=quote_mode,
                )

        if (
            "result" not in res
            or not isinstance(res["result"], str)
            or not res["result"].startswith("0x")
        ):
            return QuoteResult(
                status=QuoteStatus.INVALID_RESPONSE,
                amount_in=in_token_amount,
                amount_out=None,
                delta_atoms=None,
                error_message=f"Missing or invalid result hex payload: {res}",
                block_number=block_num,
                quote_mode=quote_mode,
            )

        # 4. Decode successful output
        try:
            if is_v4_pool(hop.pool):
                out_atoms, gas_est = decode_v4_quoter_result(res["result"])
            else:
                out_atoms, gas_est = decode_v3_quoter_result(res["result"])
        except Exception as exc:
            return QuoteResult(
                status=QuoteStatus.INVALID_RESPONSE,
                amount_in=in_token_amount,
                amount_out=None,
                delta_atoms=None,
                error_message=f"Failed to decode quote result: {exc}",
                block_number=block_num,
                quote_mode=quote_mode,
            )

        out_token_amount = TokenAmount(token=hop.token_out, atoms=out_atoms)
        delta: int | None = None
        if hop.token_out.address.lower() == hop.token_in.address.lower():
            delta = out_atoms - in_token_amount.atoms

        return QuoteResult(
            status=QuoteStatus.QUOTED,
            amount_in=in_token_amount,
            amount_out=out_token_amount,
            delta_atoms=delta,
            gas_estimate=gas_est,
            block_number=block_num,
            quote_mode=quote_mode,
        )

    def quote_route(
        self,
        route: CandidateRoute,
        amount_in: TokenAmount | int,
        block_identifier: str | int | None = None,
        quote_mode: str = "live",
    ) -> QuoteResult:
        """Execute sequential on-chain quote chaining across all hops in a route.

        Chaining Rule:
        Output amount of hop i is directly passed as input amount to hop i+1.
        If any hop fails (reverts or RPC error), execution terminates immediately
        and returns a failed QuoteResult preserving the error classification.

        Fee Non-Duplication:
        DEX swap fees are already subtracted inside the Quoter simulation.
        No additional fee reduction is ever applied.

        Args:
            route: CandidateRoute composed of chained RouteHops.
            amount_in: Initial input amount as TokenAmount or integer atoms.
            block_identifier: Target block number or hex identifier.
            quote_mode: Quote execution mode ('live', 'historical_replay', 'synthetic').

        Returns:
            QuoteResult for the entire route.
        """
        if isinstance(amount_in, int):
            initial_in = TokenAmount(token=route.base_token, atoms=amount_in)
        elif isinstance(amount_in, TokenAmount):
            if amount_in.token.address.lower() != route.base_token.address.lower():
                raise ValueError(
                    f"amount_in token {amount_in.token.address} does not match "
                    f"route base_token {route.base_token.address}"
                )
            initial_in = amount_in
        else:
            raise TypeError(
                f"amount_in must be TokenAmount or int, got {type(amount_in).__name__}"
            )

        block_num: int | None = None
        if block_identifier is not None:
            if isinstance(block_identifier, str) and block_identifier.startswith("0x"):
                try:
                    block_num = int(block_identifier, 16)
                except ValueError:
                    pass
            elif isinstance(block_identifier, int):
                block_num = block_identifier
                block_identifier = hex(block_identifier)
        elif route.snapshot_block:
            block_num = route.snapshot_block

        current_amount: TokenAmount = initial_in
        total_gas: int = 0

        for _hop_idx, hop in enumerate(route.hops):
            hop_res = self.quote_hop(
                hop=hop,
                amount_in=current_amount,
                block_identifier=block_identifier,
                quote_mode=quote_mode,
            )

            if hop_res.status != QuoteStatus.QUOTED:
                # Terminate chaining immediately on failure
                return QuoteResult(
                    status=hop_res.status,
                    amount_in=initial_in,
                    amount_out=None,
                    delta_atoms=None,
                    gas_estimate=None,
                    error_code=hop_res.error_code,
                    error_message=hop_res.error_message,
                    raw_revert_data=hop_res.raw_revert_data,
                    block_number=block_num,
                    quote_mode=quote_mode,
                )

            assert hop_res.amount_out is not None
            current_amount = hop_res.amount_out
            if hop_res.gas_estimate:
                total_gas += hop_res.gas_estimate

        # All hops succeeded: final token amount is current_amount
        final_out = TokenAmount(token=route.base_token, atoms=current_amount.atoms)
        delta = final_out.atoms - initial_in.atoms

        return QuoteResult(
            status=QuoteStatus.QUOTED,
            amount_in=initial_in,
            amount_out=final_out,
            delta_atoms=delta,
            gas_estimate=total_gas if total_gas > 0 else None,
            block_number=block_num,
            quote_mode=quote_mode,
        )

    def replay_historical_session(
        self,
        adapter: ReplayAdapter | None = None,
    ) -> list[QuoteResult]:
        """Execute the exact 30-record historical replay sequence and verify full consumption.

        Sequence:
        - Records 0-3: Chain ID, block number, block header, gas price.
        - Records 4-13: 5 pool slot0 & liquidity state reads.
        - Records 14-21: Route A WETH triangular cycle across 4 tiers.
        - Records 22-29: Route B USDG triangular cycle across 4 tiers.

        Returns:
            List of 8 QuoteResults (4 Route A + 4 Route B).
        """
        active_adapter = adapter if adapter is not None else self.rpc
        if not hasattr(active_adapter, "call"):
            raise TypeError("Replay adapter must have a 'call' method")

        # 1. Pinned block acquisition (records 0 to 3)
        active_adapter.call("eth_chainId")
        block_num_res = active_adapter.call("eth_blockNumber")
        block_num = int(block_num_res["result"], 16)
        block_ident = hex(block_num)

        active_adapter.call("eth_getBlockByNumber", [block_ident, False])
        active_adapter.call("eth_gasPrice")

        # 2. Stage 1: 5 pools slot0 & liquidity queries (records 4 to 13)
        state_view = "0xF3334192D15450CdD385c8B70e03f9A6bD9E673b"
        pool_cashcat_weth = "0xd42A491087a15E5afd51FEb3606066Cc152d2b09"
        pool_pons_weth = "0xEd50bDeeA8aDC232f159486192a4157281D722ff"

        p_cc_pons = "3ee7a201318a8b5dfeafdb8a6d1e80d8a1909858d9d2ce10c79ea3b315731f32"
        p_cc_usdg = "a92a3df27a00a276183ff7265fd8affa11df1fe8bb23ddfaf13f6c879a3f818b"
        p_pons_usdg = "4be9657ec9002e528f4f17a5c43edc525a07f888f7b180c2afbf75e096c4f38a"

        pool_queries = [
            (state_view, "0xc815641c" + p_cc_pons),
            (state_view, "0xfa6793d5" + p_cc_pons),
            (pool_cashcat_weth, "0x3850c7bd"),
            (pool_cashcat_weth, "0x1a686502"),
            (pool_pons_weth, "0x3850c7bd"),
            (pool_pons_weth, "0x1a686502"),
            (state_view, "0xc815641c" + p_cc_usdg),
            (state_view, "0xfa6793d5" + p_cc_usdg),
            (state_view, "0xc815641c" + p_pons_usdg),
            (state_view, "0xfa6793d5" + p_pons_usdg),
        ]
        for target, calldata in pool_queries:
            active_adapter.call(
                "eth_call",
                [{"to": target, "data": calldata}, block_ident],
                block_identifier=block_ident,
            )

        # 3. Stage 2: Quoting Route A (records 14 to 21) & Route B (records 22 to 29)
        quoter_exec = (
            self
            if self.rpc is active_adapter
            else Quoter(
                rpc_client=active_adapter,
                v3_quoter=self.v3_quoter,
                v4_quoter=self.v4_quoter,
            )
        )

        route_a = build_canonical_route_a()
        route_b = build_canonical_route_b()

        results: list[QuoteResult] = []
        for amt in CANONICAL_WETH_TIERS:
            q_res = quoter_exec.quote_route(
                route_a,
                amount_in=amt,
                block_identifier=block_ident,
                quote_mode="historical_replay",
            )
            results.append(q_res)

        for amt in CANONICAL_USDG_TIERS:
            q_res = quoter_exec.quote_route(
                route_b,
                amount_in=amt,
                block_identifier=block_ident,
                quote_mode="historical_replay",
            )
            results.append(q_res)

        # 4. Strict completion check: verify all 30 records were consumed
        if hasattr(active_adapter, "verify_complete"):
            active_adapter.verify_complete()

        return results


def run_historical_replay(
    adapter: ReplayAdapter,
    quoter: Quoter | None = None,
) -> list[QuoteResult]:
    """Execute complete 30-record historical replay sequence and verify full consumption."""
    q = quoter if quoter is not None else Quoter(rpc_client=adapter)
    return q.replay_historical_session(adapter=adapter)
