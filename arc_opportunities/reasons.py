"""Arc Shadow Evaluation Rejection and Categorization Taxonomy (T24)

Standardizes rejection and status reasons across:
- Quote / graph capability limits (cross-tick, unsupported hooks, missing snapshots)
- Economic / funds barriers (unknown gas, negative profit, output floor failure)
- Readonly simulation barriers (contract revert, unverified output, rpc error)
- Execution authorization barriers (disabled execution, no private key)
"""

from __future__ import annotations

from enum import StrEnum


class ShadowRejectionReason(StrEnum):
    """Normalized taxonomy of shadow evaluation rejection reasons."""

    # 1. Quote / Graph Layer
    UNSUPPORTED_CROSS_TICK = "UNSUPPORTED_CROSS_TICK"
    UNSUPPORTED_HOOK = "UNSUPPORTED_HOOK"
    UNSUPPORTED_DYNAMIC_FEE = "UNSUPPORTED_DYNAMIC_FEE"
    MISSING_POOL_SNAPSHOT = "MISSING_POOL_SNAPSHOT"
    ZERO_LIQUIDITY = "ZERO_LIQUIDITY"
    CROSS_CHAIN_MISMATCH = "CROSS_CHAIN_MISMATCH"
    L2_DOMAIN_REJECTED = "L2_DOMAIN_REJECTED"

    # 2. Economic / Funds Layer
    UNKNOWN_GAS_EVIDENCE = "UNKNOWN_GAS_EVIDENCE"
    NEGATIVE_NET_PROFIT = "NEGATIVE_NET_PROFIT"
    OUTPUT_FLOOR_NOT_MET = "OUTPUT_FLOOR_NOT_MET"
    EXCESSIVE_TRADE_AMOUNT = "EXCESSIVE_TRADE_AMOUNT"
    SHARED_BALANCE_DOMAIN_CYCLE = "SHARED_BALANCE_DOMAIN_CYCLE"

    # 3. Simulation Layer
    SIMULATION_CONTRACT_REVERT = "SIMULATION_CONTRACT_REVERT"
    SIMULATION_OUTPUT_UNVERIFIED = "SIMULATION_OUTPUT_UNVERIFIED"
    SIMULATION_RPC_ERROR = "SIMULATION_RPC_ERROR"
    SIMULATION_NODE_LIMITATION = "SIMULATION_NODE_LIMITATION"

    # 4. Execution Authorization Layer
    EXECUTION_PERMANENTLY_LOCKED = "EXECUTION_PERMANENTLY_LOCKED"
    NO_EXECUTION_AUTHORIZATION = "NO_EXECUTION_AUTHORIZATION"
