"""Inert public-RPC/receipt fixtures; real admission, route checks, oracle and ledger.

No test body is imported here. Bytes returned by the signing stub are deliberately
not signed EVM transactions. Fixture code identities do not claim live deployments.
"""

import copy
import time
from decimal import Decimal
from fractions import Fraction
from types import SimpleNamespace
from unittest.mock import MagicMock

from core.config import load_safe_config
from core.wallet_guard import WalletGuard
from eth_abi import decode, encode
from execution.funds import BASES, TRANSFER_TOPIC, hex_value
from execution.protocols import PERMIT2, ROUTER, V3_FACTORY, V3_QUOTER
from execution.public_runtime import build_public_runtime
from execution.weth_arbitrage_executor import ArbitrageLeg, ArbitragePlan, WethArbitrageExecutor
from web3 import Web3


def make_receipt_fixture(
    tmp_path,
    monkeypatch,
    *,
    base="USDG",
    gain=None,
    gas_used=100_000,
    triangle=False,
    native_price=Decimal(2500),
    usdg_price=Decimal(1),
    age_seconds=0,
):
    """Build typed public inputs and ABI responses without mocking any admission check."""
    # Keep dataclass factories and all consumers on the same unmodified wall clock.
    observed_at = int(time.time()) - age_seconds
    wallet = "0x" + "11" * 20
    monkeypatch.setattr("eth_account.Account.from_key", lambda _: SimpleNamespace(address=wallet))
    block = {"number": "0xa", "hash": "0x" + "44" * 32, "timestamp": hex(observed_at)}
    token, decimals = BASES[base]
    other = BASES["WETH" if base == "USDG" else "USDG"][0]
    amount = 10_000_000 if base == "USDG" else 4 * 10**15
    gain = (2_500_000 if base == "USDG" else 5 * 10**15) if gain is None else gain
    pools = {100: "0x" + "55" * 20, 500: "0x" + "66" * 20}
    third = "0x" + "bb" * 20
    if triangle:
        pools[3000] = "0x" + "cc" * 20
    legs = [
        ArbitrageLeg(token, other, 100, pool_address=pools[100]),
        ArbitrageLeg(other, token, 500, pool_address=pools[500]),
    ]
    if triangle:
        legs[1] = ArbitrageLeg(other, third, 500, pool_address=pools[500])
        legs.append(ArbitrageLeg(third, token, 3000, pool_address=pools[3000]))
    ctx = SimpleNamespace(
        wallet=wallet,
        token=token,
        gain=gain,
        nonce=1,
        digest=None,
        calls=[],
        block=block,
        gas_used=gas_used,
        receipt_status=1,
        input_amount=amount,
        signed_payloads=[],
        gas_price=10**9,
        native_price=native_price,
        base_price=usdg_price if base == "USDG" else native_price,
    )
    plan = ArbitragePlan(
        legs,
        amount,
        amount + gain,
        max(1, (amount + gain) * 99 // 100),
        float(Decimal(amount) / 10**decimals * ctx.base_price),
        1,
        base_symbol=base,
        base_token=token,
        decimals=decimals,
    )
    feed_addresses = {BASES["WETH"][0]: "0x" + "77" * 20, BASES["USDG"][0]: "0x" + "88" * 20}
    aggregators = {
        value: "0x" + ("99" if index == 0 else "aa") * 20
        for index, value in enumerate(feed_addresses.values())
    }
    feeds = {
        asset: {
            "proxy": proxy,
            "aggregator": aggregators[proxy],
            "decimals": 8,
            "description": "ETH / USD" if asset == BASES["WETH"][0] else "USDG / USD",
            "max_age_seconds": 10,
            "source_url": "https://fixture.invalid/reviewed-feed",
        }
        for asset, proxy in feed_addresses.items()
    }
    code_addresses = {
        ROUTER,
        PERMIT2,
        V3_FACTORY,
        V3_QUOTER,
        token,
        other,
        *pools.values(),
        *feed_addresses.values(),
        *aggregators.values(),
    }
    if triangle:
        code_addresses.add(third)
    code_hash = hex_value(Web3.keccak(b"\x01"), 32)
    profile = {
        "chain_id": 4663,
        "wallet": wallet,
        "router": ROUTER,
        "code_hashes": {address: code_hash for address in code_addresses},
        "transfer_tokens": [token, other, third] if triangle else [token, other],
        "counterparties": [ROUTER],
        "feeds": feeds,
    }

    def packed(types, values):
        return "0x" + encode(types, values).hex()

    def selector(signature):
        return Web3.keccak(text=signature)[:4]

    def request(method, params):
        ctx.calls.append((method, copy.deepcopy(params)))
        if method == "eth_chainId":
            return "0x1237"
        if method == "eth_getBlockByNumber":
            return copy.deepcopy(block)
        if method == "eth_getCode":
            assert params[0].lower() in code_addresses
            assert params[1] == block["number"]
            return "0x01"
        if method == "eth_blockNumber":
            return "0xb"
        if method == "eth_getTransactionReceipt":
            assert params[0] == ctx.digest
            return copy.deepcopy(ctx.receipt)
        if method == "eth_getTransactionByHash":
            assert params[0] == ctx.digest
            return {
                "hash": ctx.digest,
                "chainId": "0x1237",
                "from": wallet,
                "to": ROUTER,
                "value": "0x0",
                "blockNumber": block["number"],
                "blockHash": block["hash"],
            }
        assert method == "eth_call" and len(params) == 2
        assert params[1] == block["number"]
        target = params[0]["to"].lower()
        data = bytes.fromhex(params[0]["data"][2:])
        if target == ROUTER:
            assert params[0]["from"].lower() == wallet
            assert data[:4] in (
                selector("execute(bytes,bytes[])"),
                selector("execute(bytes,bytes[],uint256)"),
            )
            return "0x"
        if target in aggregators:
            feed = next(feed for feed in feeds.values() if feed["proxy"] == target)
            if data[:4] == selector("aggregator()"):
                return packed(["address"], [feed["aggregator"]])
            if data[:4] == selector("decimals()"):
                return packed(["uint8"], [8])
            if data[:4] == selector("description()"):
                return packed(["string"], [feed["description"]])
            assert data[:4] == selector("latestRoundData()")
            price = native_price if feed["description"] == "ETH / USD" else usdg_price
            return packed(
                ["uint80", "int256", "uint256", "uint256", "uint80"],
                [7, int(price * 10**8), observed_at - 1, observed_at, 7],
            )
        if data[:4] == selector("balanceOf(address)"):
            assert decode(["address"], data[4:])[0] == ROUTER
            return packed(["uint256"], [0])
        if data[:4] == selector("allowance(address,address)"):
            assert decode(["address", "address"], data[4:]) == (wallet, PERMIT2)
            return packed(["uint256"], [10**30])
        if data[:4] == selector("allowance(address,address,address)"):
            assert decode(["address"] * 3, data[4:]) == (wallet, token, ROUTER)
            return packed(["uint160", "uint48", "uint48"], [10**30, observed_at + 900, 0])
        if data[:4] == selector("factory()"):
            return packed(["address"], [V3_FACTORY])
        if data[:4] == selector("getPool(address,address,uint24)"):
            _, _, fee = decode(["address", "address", "uint24"], data[4:])
            return packed(["address"], [pools[fee]])
        if data[:4] == selector("fee()"):
            return packed(
                ["uint24"], [next(fee for fee, address in pools.items() if address == target)]
            )
        assert target == V3_QUOTER
        asset_in, asset_out, qty, fee, _ = decode(
            ["(address,address,uint256,uint24,uint160)"], data[4:]
        )[0]
        # Units per USD; the synthetic third asset has six decimals and a $1 rate.
        units = {
            BASES["WETH"][0]: Fraction(10**18) / Fraction(native_price),
            BASES["USDG"][0]: Fraction(10**6) / Fraction(usdg_price),
            third: Fraction(10**6),
        }
        output = int(qty * units[asset_out] / units[asset_in])
        if asset_in == token:
            ctx.input_amount = qty  # verifier visits half input, then exact input
        if asset_out == token:
            output = output * (amount + gain) // amount
        return packed(["uint256", "uint160", "uint32", "uint256"], [output, 2**96, 0, 50_000])

    runtime = build_public_runtime(
        profile,
        request,
        tmp_path / "receipt-ledger.sqlite",
        lambda: (True, True, False),
        Decimal(30),
    )
    w3 = MagicMock()
    w3.eth.gas_price = ctx.gas_price
    w3.eth.get_block.return_value = {"baseFeePerGas": ctx.gas_price}
    w3.eth.estimate_gas.return_value = 100_000
    w3.eth.get_transaction_count.side_effect = lambda *_: ctx.nonce
    w3.eth.get_balance.return_value = 10**18
    w3.eth.contract.return_value.functions.balanceOf.return_value.call.return_value = 10**20
    w3.eth.contract.return_value.functions.allowance.return_value.call.return_value = 10**30
    w3.eth.call.return_value = b""
    w3.eth.account.from_key.return_value = SimpleNamespace(address=wallet)

    def sign_stub(payload, private_key):
        state = executor.funds_runtime.ledger.status(
            4663, wallet, "WETH" if base == "USDG" else "USDG"
        )
        assert state["pending"] and 0 < state["reserved_usd"] <= 1
        assert payload["nonce"] == ctx.nonce
        ctx.signed_payloads.append(copy.deepcopy(payload))
        ctx.raw = f"inert fixture nonce {ctx.nonce}; never signed EVM bytes".encode()
        ctx.digest = hex_value(Web3.keccak(ctx.raw), 32)
        return SimpleNamespace(raw_transaction=ctx.raw)

    def send_stub(raw):
        assert raw == ctx.raw
        logs = []
        for index, (sender, recipient, qty) in enumerate(
            [
                (wallet, ROUTER, ctx.input_amount),
                (ROUTER, wallet, ctx.input_amount + ctx.gain * ctx.input_amount // amount),
            ]
        ):
            logs.append(
                {
                    "address": token,
                    "transactionHash": ctx.digest,
                    "blockHash": block["hash"],
                    "logIndex": hex(index),
                    "topics": [
                        TRANSFER_TOPIC,
                        "0x" + "0" * 24 + sender[2:],
                        "0x" + "0" * 24 + recipient[2:],
                    ],
                    "data": packed(["uint256"], [qty]),
                }
            )
        ctx.receipt = {
            "transactionHash": ctx.digest,
            "blockHash": block["hash"],
            "blockNumber": block["number"],
            "status": hex(ctx.receipt_status),
            "gasUsed": hex(ctx.gas_used),
            "effectiveGasPrice": hex(ctx.gas_price),
            "logs": logs if ctx.receipt_status else [],
        }
        ctx.nonce += 1
        return bytes.fromhex(ctx.digest[2:])

    w3.eth.account.sign_transaction.side_effect = sign_stub
    w3.eth.send_raw_transaction.side_effect = send_stub
    w3.eth.wait_for_transaction_receipt.side_effect = lambda *_args, **_kwargs: ctx.receipt
    config = load_safe_config(
        _env_file=None,
        PRIVATE_KEY="inert; Account.from_key is stubbed",
        DRY_RUN=False,
        UNIVERSAL_ROUTER_ADDRESS=ROUTER,
    )
    executor = WethArbitrageExecutor(
        config=config, guard=WalletGuard(dry_run=False), w3=w3, funds_runtime=runtime
    )
    ctx.executor, ctx.runtime, ctx.ledger, ctx.plan, ctx.w3, ctx.profile = (
        executor,
        runtime,
        runtime.ledger,
        plan,
        w3,
        profile,
    )
    ctx.request = request
    return ctx
