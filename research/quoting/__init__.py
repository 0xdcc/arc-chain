"""Arbitrage quoting engine and historical replay module."""

from research.quoting.quoter import (
    Quoter,
    build_canonical_route_a,
    build_canonical_route_b,
    classify_rpc_error,
    decode_v3_quoter_result,
    decode_v4_quoter_result,
    encode_v3_quoter_calldata,
    encode_v4_quoter_calldata,
    estimate_usd_profit,
    fee_bps_to_raw,
    is_v3_pool,
    is_v4_pool,
    run_historical_replay,
)
from research.quoting.replay_adapter import ReplayAdapter

__all__ = [
    "Quoter",
    "ReplayAdapter",
    "build_canonical_route_a",
    "build_canonical_route_b",
    "classify_rpc_error",
    "decode_v3_quoter_result",
    "decode_v4_quoter_result",
    "encode_v3_quoter_calldata",
    "encode_v4_quoter_calldata",
    "estimate_usd_profit",
    "fee_bps_to_raw",
    "is_v3_pool",
    "is_v4_pool",
    "run_historical_replay",
]
