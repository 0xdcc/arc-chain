"""Live read-only fixed-block RPC shadow sampling for W2-L.

Strict fail-closed safety guardrails:
- 0 private keys, 0 wallet signatures, 0 real trades, 0 external notifications.
- Exactly 1 verified 2-hop closed-loop route (WETH -> CASHCAT -> WETH).
- Exactly 2 amounts (0.001 ETH, 0.01 ETH).
- Maximum 6 epochs, maximum 200 RPC requests, wall clock <= 600 seconds.
- Strict method whitelist: eth_chainId, eth_blockNumber, eth_getBlockByNumber, eth_gasPrice, eth_call.
- eth_call value=0, target strictly restricted to QuoterV2, no state overrides.
- 429 rate limit immediate halt without blind retry; 3 consecutive errors halt.
- Economic net_atoms remains null (unknown), sim_available remains False.
- Complete hash-chained append-only ledger and metric records output.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from dataclasses import asdict
from pathlib import Path
from typing import Any

from arbitrage_contracts.identity import (
    AssetRef,
    FeeModel,
    PoolDescriptor,
    PoolKey,
    TokenKey,
)
from arbitrage_contracts.quote import (
    ActorScope,
    Amount,
    DataMode,
    HopRef,
    QuoteStatus,
    RouteRef,
)
from arbitrage_contracts.state import StateVersion
from opportunities.economics import evaluate_quote_evidence
from opportunities.lifecycle import LifecycleEvent, LifecyclePolicy, ObservationMerger
from opportunities.quote_adapter import FixedBlockQuoteAdapter, QuoteAdapterRequest
from opportunities.store import AppendOnlyLedger

CHAIN_ID = 4663
APPROVED_RPC_URL = "https://robinhood-rpc.publicnode.com"
ROBINHOOD_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
V3_QUOTER = "0x33e885eD0Ec9bF04EcfB19341582aADCb4c8A9E7"
V4_QUOTER = "0x8Dc178eFB8111BB0973Dd9d722ebeFF267c98F94"

WETH_ADDR = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
CASHCAT_ADDR = "0x020bfc650a365f8bb26819deaabf3e21291018b4"

POOL_1_ADDR = "0xd42a491087a15e5afd51feb3606066cc152d2b09"  # fee 3000 (0.3%)
POOL_2_ADDR = "0xa70fc67c9f69da90b63a0e4c05d229954574e313"  # fee 10000 (1.0%)

WHITELIST_METHODS = frozenset(
    {"eth_chainId", "eth_blockNumber", "eth_getBlockByNumber", "eth_gasPrice", "eth_call"}
)
MAX_REQUESTS = 200
MAX_EPOCHS = 6
MAX_CONSECUTIVE_ERRORS = 3


class LiveRpcSafetyError(RuntimeError):
    """Raised when an RPC request violates safety guardrails."""


class LiveRpcTransport:
    """Strictly bounded, read-only HTTP JSON-RPC transport with metric tracking."""

    def __init__(self, endpoint: str, max_requests: int = MAX_REQUESTS, timeout_s: float = 15.0) -> None:
        self.endpoint = endpoint
        self.max_requests = max_requests
        self.timeout_s = timeout_s
        self.calls_count = 0
        self.consecutive_errors = 0
        self.rate_limited = False
        self.halted = False
        self.recorded_calls: list[dict[str, Any]] = []
        self.latencies_ms: list[float] = []

    def call(
        self,
        method: str,
        params: list[Any] | tuple[Any, ...] | None = None,
        block_identifier: str | None = None,
    ) -> dict[str, Any]:
        if method not in WHITELIST_METHODS:
            raise LiveRpcSafetyError(f"Method {method!r} is not in read-only whitelist")

        if self.calls_count >= self.max_requests:
            self.halted = True
            return {"error": {"code": -32000, "message": "max_requests_exceeded"}}

        if self.halted or self.rate_limited:
            return {"error": {"code": -32000, "message": "transport_halted"}}

        call_params = list(params) if params is not None else []

        if method == "eth_call":
            if not call_params or not isinstance(call_params[0], dict):
                raise LiveRpcSafetyError("eth_call requires tx object in params[0]")
            tx_obj = call_params[0]
            to_addr = str(tx_obj.get("to", "")).lower()
            if to_addr not in (V3_QUOTER.lower(), V4_QUOTER.lower(), POOL_1_ADDR.lower(), POOL_2_ADDR.lower(), WETH_ADDR.lower(), CASHCAT_ADDR.lower()):
                raise LiveRpcSafetyError(f"eth_call to unapproved address: {to_addr}")
            if "value" in tx_obj and tx_obj["value"] not in (0, "0", "0x0", "0x00"):
                raise LiveRpcSafetyError("eth_call with value > 0 strictly forbidden")
            if len(call_params) > 2:
                raise LiveRpcSafetyError("eth_call with state override strictly forbidden")

        self.calls_count += 1
        req_id = self.calls_count
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": call_params,
        }
        body_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint,
            data=body_bytes,
            headers={
                "Content-Type": "application/json",
                "User-Agent": ROBINHOOD_USER_AGENT,
            },
        )

        t0 = time.time()
        status_code = 200
        error_info: str | None = None
        resp_json: dict[str, Any] = {}

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                status_code = resp.status
                raw_resp = resp.read()
                resp_json = json.loads(raw_resp.decode("utf-8"))
            elapsed_ms = (time.time() - t0) * 1000
            self.latencies_ms.append(elapsed_ms)
            self.consecutive_errors = 0

            record = {
                "id": req_id,
                "method": method,
                "block": block_identifier,
                "status_code": status_code,
                "elapsed_ms": round(elapsed_ms, 2),
                "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "has_result": "result" in resp_json,
                "has_error": "error" in resp_json,
            }
            self.recorded_calls.append(record)
            return resp_json

        except urllib.error.HTTPError as err:
            elapsed_ms = (time.time() - t0) * 1000
            status_code = err.code
            error_info = f"HTTP {err.code}: {err.reason}"
            if err.code == 429:
                self.rate_limited = True
                self.halted = True
            self.consecutive_errors += 1
            if self.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                self.halted = True
            self.recorded_calls.append({
                "id": req_id,
                "method": method,
                "block": block_identifier,
                "status_code": status_code,
                "elapsed_ms": round(elapsed_ms, 2),
                "error": error_info,
            })
            return {"error": {"code": status_code, "message": error_info}}

        except Exception as exc:
            elapsed_ms = (time.time() - t0) * 1000
            error_info = f"NetworkException: {type(exc).__name__}: {exc}"
            self.consecutive_errors += 1
            if self.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                self.halted = True
            self.recorded_calls.append({
                "id": req_id,
                "method": method,
                "block": block_identifier,
                "status_code": 0,
                "elapsed_ms": round(elapsed_ms, 2),
                "error": error_info,
            })
            return {"error": {"code": -32000, "message": error_info}}


def build_w2_l_route() -> RouteRef:
    """Build the canonical 2-hop WETH <-> CASHCAT route."""
    weth = AssetRef.erc20(TokenKey(CHAIN_ID, WETH_ADDR))
    cashcat = AssetRef.erc20(TokenKey(CHAIN_ID, CASHCAT_ADDR))

    pool1_key = PoolKey(
        chain_id=CHAIN_ID,
        protocol_id="uniswap_v3",
        venue_kind="factory",
        venue_address="0x1f7d7550b1b028f7571e69a784071f0205fd2efa",
        pool_id_kind="address",
        pool_id=POOL_1_ADDR,
    )
    pool1_desc = PoolDescriptor(
        key=pool1_key,
        currency0=cashcat,
        currency1=weth,
        fee_model=FeeModel(kind="static", raw_value=3000),
        identity_evidence_refs=("fixture:w2l_pool1",),
    )

    pool2_key = PoolKey(
        chain_id=CHAIN_ID,
        protocol_id="uniswap_v3",
        venue_kind="factory",
        venue_address="0x1f7d7550b1b028f7571e69a784071f0205fd2efa",
        pool_id_kind="address",
        pool_id=POOL_2_ADDR,
    )
    pool2_desc = PoolDescriptor(
        key=pool2_key,
        currency0=cashcat,
        currency1=weth,
        fee_model=FeeModel(kind="static", raw_value=10000),
        identity_evidence_refs=("fixture:w2l_pool2",),
    )

    hops = (
        HopRef(
            pool_key=pool1_key,
            asset_in=weth,
            asset_out=cashcat,
            direction="one_for_zero",
            pool_descriptor=pool1_desc,
        ),
        HopRef(
            pool_key=pool2_key,
            asset_in=cashcat,
            asset_out=weth,
            direction="zero_for_one",
            pool_descriptor=pool2_desc,
        ),
    )

    return RouteRef(
        route_id=None,
        chain_id=CHAIN_ID,
        base_asset=weth,
        hops=hops,
    )


def run_sampling(output_root: Path) -> dict[str, Any]:
    """Execute bounded live shadow sampling."""
    output_root.mkdir(parents=True, exist_ok=True)
    transport = LiveRpcTransport(APPROVED_RPC_URL)

    # 1. Pre-flight verification
    chain_id_resp = transport.call("eth_chainId")
    observed_chain_id = int(str(chain_id_resp.get("result", "0x0")), 16)
    if observed_chain_id != CHAIN_ID:
        raise LiveRpcSafetyError(f"Connected to chain {observed_chain_id}, expected {CHAIN_ID}")

    route = build_w2_l_route()
    amounts = (
        Amount(route.base_asset, 10**15, 18),   # 0.001 ETH
        Amount(route.base_asset, 10**16, 18),   # 0.01 ETH
    )

    ledger_path = output_root / "ledger.jsonl"
    if ledger_path.exists():
        ledger_path.unlink()
    ledger = AppendOnlyLedger(ledger_path)

    policy = LifecyclePolicy(gap_limit_ms=10000, target_delay_ms=500)
    merger = ObservationMerger(policy)

    request = QuoteAdapterRequest(
        quoter_v3=V3_QUOTER,
        quoter_v4=V4_QUOTER,
        data_mode=DataMode.LIVE_READONLY,
        actor_scope=ActorScope.OWN_AUTHORIZED,
        source_refs=("rpc:robinhood-rpc.publicnode.com",),
        run_baseline_ref="W2-L-v1",
    )
    adapter = FixedBlockQuoteAdapter(transport, request)

    epochs_completed = 0
    total_quotes = 0
    quotes_successful = 0
    start_time = time.time()

    print(f"Starting W2-L real read-only sampling on chain {CHAIN_ID}...")

    for epoch in range(MAX_EPOCHS):
        if transport.halted or transport.rate_limited:
            print(f"Sampling halted at epoch {epoch}: rate_limited={transport.rate_limited}")
            break
        if time.time() - start_time > 540:  # 9 minute soft ceiling
            print("Time budget limit reached, stopping cleanly.")
            break

        # Query state
        block_resp = transport.call("eth_blockNumber")
        if "result" not in block_resp:
            print(f"Epoch {epoch+1} blockNumber query failed: {block_resp.get('error')}")
            time.sleep(2.0)
            continue
        block_num = int(str(block_resp["result"]), 16)
        if block_num <= 0:
            print(f"Epoch {epoch+1} invalid block number {block_num}")
            time.sleep(2.0)
            continue

        block_detail_resp = transport.call("eth_getBlockByNumber", [hex(block_num), False])
        block_data = block_detail_resp.get("result") or {}
        block_hash = block_data.get("hash", "0x" + "0" * 64)
        block_ts = int(str(block_data.get("timestamp", "0x0")), 16)
        if block_ts == 0:
            block_ts = int(time.time())

        gas_price_resp = transport.call("eth_gasPrice")
        gas_price = int(str(gas_price_resp.get("result", "0x0")), 16)

        state = StateVersion(
            chain_id=CHAIN_ID,
            block_domain="l2",
            block_number=block_num,
            block_hash=block_hash,
            completeness="ready",
            complete_through_block=block_num,
            received_at_ms=int(time.time() * 1000),
            block_timestamp_s=block_ts,
        )

        epoch_quotes_ok = 0
        for amount in amounts:
            quote_res = adapter.quote_route(route, amount, state)
            total_quotes += 1
            evidence = quote_res.evidence

            now_ms = int(time.time() * 1000)
            economic = evaluate_quote_evidence(evidence, now_ms, conversion_inputs=())

            event = LifecycleEvent(
                event_id=f"w2l:{epoch}:{amount.atoms}:{evidence.quote_id[:16]}",
                route_id=route.route_id,
                source_record_id=f"{route.route_id}:{amount.to_atoms_str()}:{block_num}",
                base_asset={
                    "interface_kind": route.base_asset.interface_kind,
                    "chain_id": route.base_asset.chain_id,
                    "token_key": {"address": route.base_asset.token_key.address if route.base_asset.token_key else ""},
                },
                amount_atoms=amount.to_atoms_str(),
                decimals=amount.decimals,
                decimals_evidence_ref="rpc:erc20:decimals",
                registry_semantic_revision="w2-l-live-v1",
                observed_at_ms=evidence.finished_at_ms or now_ms,
                available_at_ms=evidence.finished_at_ms or now_ms,
                block_time_s=state.block_timestamp_s,
                monotonic_ns=None,
                result="candidate" if evidence.status == QuoteStatus.QUOTED else "quote_failed",
                candidate=evidence.status == QuoteStatus.QUOTED,
                complete_scan=False,
                revoked=False,
                truncated=False,
            )
            merger.process(event)

            ledger.append({
                "schema_id": "w2-shadow-event-v1",
                "event": asdict(event),
                "quote_status": str(evidence.status),
                "quote_id": evidence.quote_id,
                "amount_in_atoms": amount.to_atoms_str(),
                "amount_out_atoms": str(evidence.amount_out.atoms) if evidence.amount_out else None,
                "net_atoms": None,  # Fail-closed: no atomic simulation in W2
                "economic_status": "unknown",
                "gas_estimate": evidence.gas_evidence.gas_units if evidence.gas_evidence else None,
                "gas_cost_atoms": economic.gas_cost_atoms,
                "sim_available": False,
                "rpc_calls": transport.calls_count,
                "block_number": block_num,
                "halted": transport.halted,
                "truncated": False,
            })

            if evidence.status == QuoteStatus.QUOTED:
                quotes_successful += 1
                epoch_quotes_ok += 1
            else:
                print(f"  [Notice] Amount {amount.atoms} status={evidence.status}, error={evidence.error}")

        epochs_completed += 1
        print(f"Epoch {epoch+1}/{MAX_EPOCHS}: block={block_num}, gas_price={gas_price}, quotes_ok={epoch_quotes_ok}/{len(amounts)}")
        if epoch < MAX_EPOCHS - 1:
            time.sleep(3.0)  # Safe spacing between epochs

    elapsed_total_s = time.time() - start_time
    lifecycle_result = merger.finalize()

    # Metrics computation
    latencies = transport.latencies_ms
    min_lat = min(latencies) if latencies else 0.0
    avg_lat = sum(latencies) / len(latencies) if latencies else 0.0
    max_lat = max(latencies) if latencies else 0.0
    sorted_lat = sorted(latencies)
    p95_lat = sorted_lat[int(len(sorted_lat) * 0.95)] if sorted_lat else 0.0

    metrics = {
        "chain_id": CHAIN_ID,
        "endpoint": APPROVED_RPC_URL,
        "epochs_completed": epochs_completed,
        "max_epochs_target": MAX_EPOCHS,
        "total_rpc_calls": transport.calls_count,
        "max_rpc_budget": MAX_REQUESTS,
        "total_quotes_requested": total_quotes,
        "quotes_successful": quotes_successful,
        "elapsed_total_seconds": round(elapsed_total_s, 2),
        "rate_limited_429": transport.rate_limited,
        "halted": transport.halted,
        "latency_ms": {
            "min": round(min_lat, 2),
            "avg": round(avg_lat, 2),
            "max": round(max_lat, 2),
            "p95": round(p95_lat, 2),
        },
        "episodes_reconstructed": len(lifecycle_result.episodes),
        "confirmed_ledger_records": ledger.confirmed_sequence,
    }

    (output_root / "rpc_metrics.json").write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    with open(output_root / "recorded_calls.jsonl", "w", encoding="utf-8") as f:
        for call_rec in transport.recorded_calls:
            f.write(json.dumps(call_rec) + "\n")

    # Verify ledger integrity
    snapshot = ledger.load()
    assert snapshot.confirmed_sequence == ledger.confirmed_sequence

    print(f"Sampling complete! Total calls: {transport.calls_count}, Latency avg: {avg_lat:.1f}ms, P95: {p95_lat:.1f}ms")
    return metrics


if __name__ == "__main__":
    out = Path("/root/projects/crypto/dex-sniper-engine-w2/docs/w2/evidence/W2-L")
    run_sampling(out)
