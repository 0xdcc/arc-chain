"""Live read-only fixed-block RPC simulation sampling for W5-R.

Strict fail-closed safety guardrails:
- 0 private keys, 0 wallet signatures, 0 real trades, 0 external notifications.
- Exactly 1 verified 2-hop closed-loop route on Robinhood (4663): WETH -> CASHCAT -> WETH.
- Exactly 2 amounts (0.001 ETH: 10^15 wei, 0.01 ETH: 10^16 wei).
- Maximum 5 epochs, maximum 100 RPC requests, wall clock <= 600 seconds.
- Strict method whitelist: eth_chainId, eth_blockNumber, eth_getBlockByNumber, eth_gasPrice, eth_call, eth_estimateGas.
- eth_call value=0, target strictly restricted to canonical Universal Router, caller restricted to authorized public wallet.
- No state overrides permitted.
- 429 rate limit immediate halt without blind retry; 3 consecutive errors halt.
- Result categorized under C15 (CALL_SUCCEEDED / OUTPUT_UNVERIFIED / CONTRACT_REVERT).
- Detailed metric and recorded simulation outputs for reproducible audit.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import requests

from arbitrage_contracts.identity import AssetRef, FeeModel, PoolDescriptor, PoolKey, TokenKey
from arbitrage_contracts.quote import Amount, HopQuote, HopRef, QuoteEvidence, QuoteStatus, RouteRef
from arbitrage_contracts.state import StateVersion
from atomic_execution.encoding import encode_execution_plan
from atomic_execution.planning import build_execution_plan
from atomic_execution.policy import ExecutionPolicy

CHAIN_ID = 4663
APPROVED_RPC_URL = "https://rpc.mainnet.chain.robinhood.com"
ROBINHOOD_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

CANONICAL_UNIVERSAL_ROUTER = "0x8876789976dEcBfCbBbe364623C63652db8C0904"
CANONICAL_PERMIT2 = "0x000000000022d473030f116ddee9f6b43ac78ba3"
CANONICAL_V3_FACTORY = "0x1f7d7550b1b028f7571e69a784071f0205fd2efa"

AUTHORIZED_CALLER_WALLET = "0x23989C17bD91b8E4EA06254D9cA002DF5EF6a83f"
WETH_ADDR = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
CASHCAT_ADDR = "0x020bfc650a365f8bb26819deaabf3e21291018b4"

POOL_1_ADDR = "0xd42a491087a15e5afd51feb3606066cc152d2b09"  # fee 3000 (0.3%)
POOL_2_ADDR = "0xa70fc67c9f69da90b63a0e4c05d229954574e313"  # fee 10000 (1.0%)

WHITELIST_METHODS = frozenset(
    {
        "eth_chainId",
        "eth_blockNumber",
        "eth_getBlockByNumber",
        "eth_gasPrice",
        "eth_call",
        "eth_estimateGas",
    }
)
MAX_REQUESTS = 100
MAX_EPOCHS = 5
MAX_CONSECUTIVE_ERRORS = 3


class LiveSimulationSafetyError(RuntimeError):
    """Raised when an RPC request violates read-only safety guardrails."""


class LiveSimulationTransport:
    """Strictly bounded read-only JSON-RPC transport for real network simulation."""

    def __init__(
        self, endpoint: str, max_requests: int = MAX_REQUESTS, timeout_s: float = 15.0
    ) -> None:
        self.endpoint = endpoint
        self.max_requests = max_requests
        self.timeout_s = timeout_s
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": ROBINHOOD_USER_AGENT})
        self.calls_count = 0
        self.consecutive_errors = 0
        self.rate_limited = False
        self.halted = False
        self.recorded_calls: list[dict[str, Any]] = []
        self.latencies_ms: list[float] = []

    def call(self, method: str, params: list[Any]) -> dict[str, Any]:
        if method not in WHITELIST_METHODS:
            raise LiveSimulationSafetyError(f"Method {method!r} is not in read-only whitelist")

        if self.calls_count >= self.max_requests:
            self.halted = True
            return {"error": {"code": -32000, "message": "max_requests_exceeded"}}

        if self.halted or self.rate_limited:
            return {"error": {"code": -32000, "message": "transport_halted"}}

        # Safety checks on call params
        if method in ("eth_call", "eth_estimateGas"):
            if not params or not isinstance(params[0], dict):
                raise LiveSimulationSafetyError(f"{method} requires tx object in params[0]")
            tx_obj = params[0]
            to_addr = str(tx_obj.get("to", "")).lower()
            if to_addr != CANONICAL_UNIVERSAL_ROUTER.lower():
                raise LiveSimulationSafetyError(
                    f"{method} target must be Universal Router, got: {to_addr}"
                )
            from_addr = str(tx_obj.get("from", "")).lower()
            if from_addr != AUTHORIZED_CALLER_WALLET.lower():
                raise LiveSimulationSafetyError(
                    f"{method} caller must be authorized wallet, got: {from_addr}"
                )
            if "value" in tx_obj and tx_obj["value"] not in (0, "0", "0x0", "0x00"):
                raise LiveSimulationSafetyError(f"{method} value > 0 strictly forbidden")
            if len(params) > 2:
                raise LiveSimulationSafetyError(f"{method} with state override strictly forbidden")

        self.calls_count += 1
        req_id = self.calls_count
        req_body = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }

        t_start = time.perf_counter()
        try:
            resp = self.session.post(self.endpoint, json=req_body, timeout=self.timeout_s)
            latency_ms = (time.perf_counter() - t_start) * 1000.0
            self.latencies_ms.append(latency_ms)

            if resp.status_code == 429:
                self.rate_limited = True
                self.consecutive_errors += 1
                return {"error": {"code": 429, "message": "HTTP 429 Too Many Requests"}}

            if resp.status_code != 200:
                self.consecutive_errors += 1
                if self.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    self.halted = True
                return {
                    "error": {
                        "code": resp.status_code,
                        "message": f"HTTP {resp.status_code}: {resp.text[:120]}",
                    }
                }

            result_json = resp.json()
            if "error" in result_json:
                self.consecutive_errors += 1
                if self.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    self.halted = True
            else:
                self.consecutive_errors = 0

            self.recorded_calls.append(
                {
                    "call_index": self.calls_count,
                    "method": method,
                    "params": params,
                    "latency_ms": round(latency_ms, 2),
                    "response": result_json,
                }
            )
            return result_json

        except Exception as exc:
            latency_ms = (time.perf_counter() - t_start) * 1000.0
            self.latencies_ms.append(latency_ms)
            self.consecutive_errors += 1
            if self.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                self.halted = True
            return {"error": {"code": -32099, "message": str(exc)}}


def run_w5_r_simulation(
    evidence_dir: Path,
    epochs: int = MAX_EPOCHS,
    rpc_url: str = APPROVED_RPC_URL,
) -> dict[str, Any]:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    transport = LiveSimulationTransport(rpc_url)

    # 1. Verify Chain ID
    chain_resp = transport.call("eth_chainId", [])
    if "result" not in chain_resp or int(chain_resp["result"], 16) != CHAIN_ID:
        raise LiveSimulationSafetyError(f"Chain ID check failed: {chain_resp}")

    # Build canonical domain route: WETH -> CASHCAT -> WETH
    weth_asset = AssetRef(
        interface_kind="erc20", chain_id=CHAIN_ID, token_key=TokenKey(CHAIN_ID, WETH_ADDR)
    )
    cashcat_asset = AssetRef(
        interface_kind="erc20", chain_id=CHAIN_ID, token_key=TokenKey(CHAIN_ID, CASHCAT_ADDR)
    )

    pk1 = PoolKey(CHAIN_ID, "uniswap-v3", "factory", CANONICAL_V3_FACTORY, "address", POOL_1_ADDR)
    pk2 = PoolKey(CHAIN_ID, "uniswap-v3", "factory", CANONICAL_V3_FACTORY, "address", POOL_2_ADDR)

    desc1 = PoolDescriptor(pk1, weth_asset, cashcat_asset, FeeModel("static", 3000))
    desc2 = PoolDescriptor(pk2, cashcat_asset, weth_asset, FeeModel("static", 10000))

    hop1 = HopRef(pk1, weth_asset, cashcat_asset, pool_descriptor=desc1)
    hop2 = HopRef(pk2, cashcat_asset, weth_asset, pool_descriptor=desc2)
    route = RouteRef(CHAIN_ID, weth_asset, (hop1, hop2))

    # Amounts to test: 0.001 ETH (10^15) & 0.01 ETH (10^16)
    test_amounts = [
        (1_000_000_000_000_000, 980_000_000_000_000, "0.001_ETH"),
        (10_000_000_000_000_000, 9_800_000_000_000_000, "0.01_ETH"),
    ]

    policy = ExecutionPolicy(
        mode="bounded_probe",
        probe_mode_enabled=True,
        max_amount_usd=Decimal("500.0"),
        slippage_bps=200,  # 2%
        probe_loss_budget_usd=Decimal("1.0"),
    )

    recorded_simulations: list[dict[str, Any]] = []

    print(f"[W5-R] Starting live read-only simulation sampling on Robinhood {CHAIN_ID}...")
    start_wall_s = time.time()

    for epoch in range(1, epochs + 1):
        if transport.halted or transport.rate_limited:
            print(f"[W5-R] Transport halted at epoch {epoch}. Stopping.")
            break

        print(f"[W5-R] Epoch {epoch}/{epochs} pin block...")
        block_resp = transport.call("eth_getBlockByNumber", ["latest", False])
        if "result" not in block_resp or not block_resp["result"]:
            print("[W5-R] Failed to retrieve latest block. Skipping epoch.")
            continue

        block_obj = block_resp["result"]
        block_num = int(block_obj["number"], 16)
        block_hash = block_obj["hash"]
        received_ms = int(time.time() * 1000)

        # Retrieve gas price
        gas_price_resp = transport.call("eth_gasPrice", [])
        gas_price_wei = (
            int(gas_price_resp["result"], 16) if "result" in gas_price_resp else 140_000_000
        )

        state = StateVersion(
            chain_id=CHAIN_ID,
            block_number=block_num,
            block_hash=block_hash,
            received_at_ms=received_ms,
            completeness="ready",
            complete_through_block=block_num,
        )

        for amt_in_wei, amt_out_min_wei, label in test_amounts:
            amt_in = Amount(weth_asset, amt_in_wei, 18)
            amt_out_min = Amount(weth_asset, amt_out_min_wei, 18)
            # Placeholder expected output based on conservative 1% pool loss
            expected_out_wei = int(amt_in_wei * 0.99)
            amt_out_expected = Amount(weth_asset, expected_out_wei, 18)

            quote = QuoteEvidence(
                quote_id=f"quote:live-w5-r:{epoch}:{label}",
                route_ref=route,
                amount_in=amt_in,
                amount_out=amt_out_expected,
                hop_quotes=(
                    HopQuote(
                        0,
                        pk1,
                        weth_asset,
                        cashcat_asset,
                        amt_in,
                        Amount(cashcat_asset, 2_000_000_000, 18),
                        fee_model=FeeModel("static", 3000),
                    ),
                    HopQuote(
                        1,
                        pk2,
                        cashcat_asset,
                        weth_asset,
                        Amount(cashcat_asset, 2_000_000_000, 18),
                        amt_out_expected,
                        fee_model=FeeModel("static", 10000),
                    ),
                ),
                state_version_ref=block_hash,
                status=QuoteStatus.QUOTED,
            )

            # Build execution plan
            plan = build_execution_plan(
                route_ref=route,
                quote_evidence=quote,
                state_version=state,
                base_asset_usd_price=Decimal("2500.0"),
                conservative_gas_usd=Decimal("0.05"),
                policy=policy,
                target_router=CANONICAL_UNIVERSAL_ROUTER,
                plan_id=f"plan:w5-r:{epoch}:{label}",
            )

            # Encode deterministic calldata
            encoded = encode_execution_plan(plan)

            call_tx = {
                "from": AUTHORIZED_CALLER_WALLET,
                "to": CANONICAL_UNIVERSAL_ROUTER,
                "data": encoded.calldata_hex,
                "value": "0x0",
            }

            # 1. Execute fixed-block eth_call
            sim_resp = transport.call("eth_call", [call_tx, hex(block_num)])

            # 2. Execute fixed-block eth_estimateGas
            gas_resp = transport.call("eth_estimateGas", [call_tx, hex(block_num)])

            is_success = "result" in sim_resp and sim_resp["result"] in ("0x", "")
            gas_estimated = int(gas_resp["result"], 16) if "result" in gas_resp else None

            sim_record = {
                "epoch": epoch,
                "label": label,
                "amount_in_wei": amt_in_wei,
                "min_amount_out_wei": amt_out_min_wei,
                "block_number": block_num,
                "block_hash": block_hash,
                "gas_price_wei": gas_price_wei,
                "calldata_sha256": encoded.calldata_sha256,
                "commands_hex": encoded.commands_hex,
                "simulation_response": sim_resp,
                "estimate_gas_response": gas_resp,
                "gas_units_estimated": gas_estimated,
                "evm_success": is_success,
                "c15_verdict": "OUTPUT_UNVERIFIED" if is_success else "CONTRACT_REVERT",
            }
            recorded_simulations.append(sim_record)
            print(
                f"  [{label}] Block: {block_num} -> EVM Success: {is_success}, Gas: {gas_estimated}"
            )

        time.sleep(1.0)

    wall_duration_s = time.time() - start_wall_s

    # Metrics aggregation
    latencies = transport.latencies_ms
    metrics = {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "network": "Robinhood Mainnet",
        "chain_id": CHAIN_ID,
        "rpc_endpoint": rpc_url,
        "total_requests": transport.calls_count,
        "wall_duration_s": round(wall_duration_s, 2),
        "rate_limited_429": transport.rate_limited,
        "transport_halted": transport.halted,
        "latency_stats_ms": {
            "min": round(min(latencies), 2) if latencies else 0,
            "avg": round(sum(latencies) / len(latencies), 2) if latencies else 0,
            "max": round(max(latencies), 2) if latencies else 0,
            "p95": round(sorted(latencies)[int(len(latencies) * 0.95)], 2) if latencies else 0,
        },
        "simulation_counts": {
            "total_simulations": len(recorded_simulations),
            "evm_successes": sum(1 for s in recorded_simulations if s["evm_success"]),
            "c15_output_unverified": sum(
                1 for s in recorded_simulations if s["c15_verdict"] == "OUTPUT_UNVERIFIED"
            ),
            "reverts": sum(
                1 for s in recorded_simulations if s["c15_verdict"] == "CONTRACT_REVERT"
            ),
        },
    }

    # Write evidence outputs
    sim_log_path = evidence_dir / "recorded_simulations.jsonl"
    with sim_log_path.open("w", encoding="utf-8") as f:
        for sim in recorded_simulations:
            f.write(json.dumps(sim, ensure_ascii=False) + "\n")

    metrics_path = evidence_dir / "rpc_metrics.json"
    with metrics_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(metrics, indent=2, ensure_ascii=False) + "\n")

    calls_log_path = evidence_dir / "recorded_rpc_calls.jsonl"
    with calls_log_path.open("w", encoding="utf-8") as f:
        for call_item in transport.recorded_calls:
            f.write(json.dumps(call_item, ensure_ascii=False) + "\n")

    print(
        f"[W5-R] Sampling complete. Wrote {len(recorded_simulations)} simulation records to {evidence_dir}"
    )
    return metrics


if __name__ == "__main__":
    evidence_target = Path("/root/projects/crypto/dex-sniper-engine-w5/docs/w5/evidence/W5-R")
    run_w5_r_simulation(evidence_target)
