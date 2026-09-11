"""Bounded public read-only metadata probe; never builds a runtime or sends a transaction."""

import argparse
import json
from typing import Any

import requests
from eth_abi import decode
from web3 import Web3

from core.rpc_policy import RpcPolicy

ENDPOINT = "https://rpc.mainnet.chain.robinhood.com"
READS = {"eth_chainId", "eth_getBlockByNumber", "eth_getCode", "eth_call"}
ADDRESSES = {
    "router": "0x8876789976decbfcbbbe364623c63652db8c0904",
    "permit2": "0x000000000022d473030f116ddee9f6b43ac78ba3",
    "pool_manager": "0x8366a39cc670b4001a1121b8f6a443a643e40951",
    "state_view": "0xf3334192d15450cdd385c8b70e03f9a6bd9e673b",
    "v4_quoter": "0x8dc178efb8111bb0973dd9d722ebeff267c98f94",
    "v3_factory": "0x1f7d7550b1b028f7571e69a784071f0205fd2efa",
    "v3_quoter": "0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7",
    "weth": "0x0bd7d308f8e1639fab988df18a8011f41eacad73",
    "usdg": "0x5fc5360d0400a0fd4f2af552add042d716f1d168",
}


class PublicProbe:
    """Use the existing RPC policy plus an exact endpoint/read list and first-error latch."""

    def __init__(self) -> None:
        self.policy = RpcPolicy()
        self.session = requests.Session()
        self.session.trust_env = False
        self.failed = False
        self.records: list[dict[str, Any]] = []

    def request(self, method: str, params: list[Any]) -> Any:
        """Make at most40 total calls, each exactly once, stopping at the first error."""
        if self.failed or len(self.records) >= 40 or method not in READS:
            raise ValueError("Read-only probe closed or method denied")
        self.policy.evaluate(method)
        payload = {
            "jsonrpc": "2.0",
            "id": len(self.records) + 1,
            "method": method,
            "params": params,
        }
        record: dict[str, Any] = {"request": payload}
        self.records.append(record)
        try:
            response = self.session.post(
                ENDPOINT,
                json=payload,
                timeout=15,
                headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"},
                allow_redirects=False,
            )
            response.raise_for_status()
            result = response.json()
            record["response"] = result
            if (
                response.status_code != 200
                or result.get("id") != payload["id"]
                or "error" in result
            ):
                raise ValueError("RPC status, identity or error response")
            return result["result"]
        except Exception as exc:
            self.failed = True
            record["failure"] = f"{type(exc).__name__}: {exc}"
            raise


def collect(probe: PublicProbe, feeds: dict[str, str]) -> dict[str, Any]:
    """Observe code and optional public feed rounds without trusting them automatically."""
    if int(probe.request("eth_chainId", []), 16) != 4663:
        raise ValueError("Wrong public chain")
    block = probe.request("eth_getBlockByNumber", ["latest", False])
    results: dict[str, Any] = {
        "block": block,
        "code": {},
        "feeds": {},
        "state": "OBSERVED_NOT_REVIEWED",
    }
    for role, address in {**ADDRESSES, **feeds}.items():
        code = probe.request("eth_getCode", [address, block["number"]])
        if not isinstance(code, str) or len(code) <= 2:
            raise ValueError(f"Missing deployed code: {role}")
        results["code"][role] = {
            "address": address,
            "keccak": "0x" + Web3.keccak(bytes.fromhex(code[2:])).hex(),
        }
    for role, address in feeds.items():
        facts: dict[str, Any] = {}
        for signature, outputs in (
            ("aggregator()", ["address"]),
            ("decimals()", ["uint8"]),
            ("description()", ["string"]),
            ("latestRoundData()", ["uint80", "int256", "uint256", "uint256", "uint80"]),
        ):
            data = "0x" + Web3.keccak(text=signature)[:4].hex()
            raw = probe.request("eth_call", [{"to": address, "data": data}, block["number"]])
            facts[signature] = decode(outputs, bytes.fromhex(raw[2:]))
        aggregator = facts["aggregator()"][0]
        code = probe.request("eth_getCode", [aggregator, block["number"]])
        if len(code) <= 2:
            raise ValueError("Feed aggregator has no code")
        facts["aggregator_code_keccak"] = "0x" + Web3.keccak(bytes.fromhex(code[2:])).hex()
        results["feeds"][role] = facts
    final = probe.request("eth_getBlockByNumber", [block["number"], False])
    if final["hash"] != block["hash"]:
        raise ValueError("Public probe block reorganized")
    results["missing_feed_identities"] = sorted({"eth_usd_feed", "usdg_usd_feed"} - set(feeds))
    return results


def main() -> None:
    """Emit evidence only; optional feed addresses must first come from public review."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eth-usd-feed")
    parser.add_argument("--usdg-usd-feed")
    args = parser.parse_args()
    feeds: dict[str, str] = {
        name: str(Web3.to_checksum_address(value)) for name, value in vars(args).items() if value
    }
    probe = PublicProbe()
    try:
        result = collect(probe, feeds)
    except Exception as exc:
        print(json.dumps({"state": "FAILED", "error": str(exc), "rpc": probe.records}, indent=2))
        raise SystemExit(1) from exc
    print(json.dumps({"result": result, "rpc": probe.records}, indent=2))


if __name__ == "__main__":
    main()
