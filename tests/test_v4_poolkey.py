"""Tests for Uniswap V4 PoolKey calculation, metadata manifest, and executor calldata alignment."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from eth_abi import encode as abi_encode
from uniswap_universal_router_decoder import RouterCodec
from web3 import Web3

from arbitrage.pool_config import scanned_to_pool_spec
from arbitrage.pool_scanner import ScannedPool
from arbitrage.spread_monitor import PoolSpec
from arbitrage.triangular import SwapLeg, TriangularArbAlert
from arbitrage.v4_reader import (
    V4_POOL_METADATA,
    ZERO_ADDRESS,
    V4PoolSpec,
    compute_v4_pool_id,
    get_v4_pool_metadata,
)
from core.wallet_guard import WalletGuard
from execution.weth_arbitrage_executor import (
    CANONICAL_UNIVERSAL_ROUTER,
    CANONICAL_USDG_ADDRESS,
    CANONICAL_WETH_ADDRESS,
    ArbitrageLeg,
    WethArbitrageExecutor,
    resolve_token_address,
)

# Known canonical pool test vectors
AI_USDG_V4_POOL_ID = "0x7aebd80541bfaaf23dbb6e99ce13d4d31c1a84c91414f971eadbff7db5f85995"
AI_TOKEN_ADDRESS = "0x2E8c31162b855A2ffa90F6F8634643Ad6F111e18"
USDG_TOKEN_ADDRESS = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
PONS_TOKEN_ADDRESS = "0x39dBED3a2bd333467115dE45665cC57F813C4571"
SPY_TOKEN_ADDRESS = "0x117cC2133c37B721F49de2a7a74833232b3B4c0C"
CASHCAT_TOKEN_ADDRESS = "0x020bfC650A365f8Bb26819DEaAbF3e21291018B4"


class TestV4PoolKeyMath:
    """Mathematical verification of keccak256(abi.encode(PoolKey)) == poolId."""

    def test_ai_usdg_023_pool_id_matches_target(self) -> None:
        """Verify AI / USDG 0.23% exactly derives the critical target poolId."""
        computed_id = compute_v4_pool_id(
            currency0=AI_TOKEN_ADDRESS,
            currency1=USDG_TOKEN_ADDRESS,
            fee=2300,
            tick_spacing=23,
            hooks=ZERO_ADDRESS,
        )
        assert computed_id == AI_USDG_V4_POOL_ID.lower()

    def test_pons_usdg_03_pool_id(self) -> None:
        """Verify PONS / USDG 0.3% matches on-chain poolId."""
        target_pid = "0x4be9657ec9002e528f4f17a5c43edc525a07f888f7b180c2afbf75e096c4f38a"
        computed_id = compute_v4_pool_id(
            currency0=PONS_TOKEN_ADDRESS,
            currency1=USDG_TOKEN_ADDRESS,
            fee=3000,
            tick_spacing=60,
            hooks=ZERO_ADDRESS,
        )
        assert computed_id == target_pid.lower()

    def test_pons_usdg_07_pool_id(self) -> None:
        """Verify PONS / USDG 0.7% (tick_spacing=140) matches on-chain poolId."""
        target_pid = "0x13f9ab4e07b7f222cf49ff8ae9d7cdd2f749546eb3f83bdd84b884e27dd5e49d"
        computed_id = compute_v4_pool_id(
            currency0=PONS_TOKEN_ADDRESS,
            currency1=USDG_TOKEN_ADDRESS,
            fee=7000,
            tick_spacing=140,
            hooks=ZERO_ADDRESS,
        )
        assert computed_id == target_pid.lower()

    def test_spy_usdg_03_pool_id(self) -> None:
        """Verify SPY / USDG 0.3% matches on-chain poolId."""
        target_pid = "0xfe2a80bb5618fd14984b92ca6d45bf5ba67443ddb1435e28b2e48df2fc1526cd"
        computed_id = compute_v4_pool_id(
            currency0=SPY_TOKEN_ADDRESS,
            currency1=USDG_TOKEN_ADDRESS,
            fee=3000,
            tick_spacing=60,
            hooks=ZERO_ADDRESS,
        )
        assert computed_id == target_pid.lower()

    def test_cashcat_usdg_0269_pool_id(self) -> None:
        """Verify CASHCAT / USDG 0.269% (tick_spacing=54) matches on-chain poolId."""
        target_pid = "0xa92a3df27a00a276183ff7265fd8affa11df1fe8bb23ddfaf13f6c879a3f818b"
        computed_id = compute_v4_pool_id(
            currency0=CASHCAT_TOKEN_ADDRESS,
            currency1=USDG_TOKEN_ADDRESS,
            fee=2690,
            tick_spacing=54,
            hooks=ZERO_ADDRESS,
        )
        assert computed_id == target_pid.lower()

    def test_weth_usdg_001_native_eth_pool_id(self) -> None:
        """Verify WETH / USDG 0.01% with native ETH (address(0)) matches on-chain poolId."""
        target_pid = "0x24107d152f14a76d292123265ae3f3c71f863fc2f4ef7ba49d64e78d28ea379e"
        computed_id = compute_v4_pool_id(
            currency0=ZERO_ADDRESS,
            currency1=USDG_TOKEN_ADDRESS,
            fee=100,
            tick_spacing=1,
            hooks=ZERO_ADDRESS,
        )
        assert computed_id == target_pid.lower()

    def test_token_ordering_invariance(self) -> None:
        """Ensures passing currency0 and currency1 in reverse order produces identical poolId."""
        pid1 = compute_v4_pool_id(AI_TOKEN_ADDRESS, USDG_TOKEN_ADDRESS, 2300, 23)
        pid2 = compute_v4_pool_id(USDG_TOKEN_ADDRESS, AI_TOKEN_ADDRESS, 2300, 23)
        assert pid1 == pid2 == AI_USDG_V4_POOL_ID.lower()


class TestV4ManifestIntegrity:
    """Verification of the solid manifest dictionary."""

    def test_manifest_contains_high_depth_pools(self) -> None:
        """Ensure V4_POOL_METADATA is loaded and contains > 100 verified pools."""
        assert len(V4_POOL_METADATA) >= 100
        assert AI_USDG_V4_POOL_ID.lower() in V4_POOL_METADATA

    def test_all_manifest_entries_pass_keccak_verification(self) -> None:
        """100% mathematical verification for all pools in V4_POOL_METADATA."""
        for pid, meta in V4_POOL_METADATA.items():
            c0 = meta["currency0"]
            c1 = meta["currency1"]
            fee = meta["fee"]
            ts = meta["tick_spacing"]
            hooks = meta["hooks"]
            computed = compute_v4_pool_id(c0, c1, fee, ts, hooks)
            assert computed == pid.lower(), (
                f"Manifest entry {pid} hash mismatch: {computed} != {pid}"
            )

    def test_get_v4_pool_metadata_case_insensitive(self) -> None:
        """Test lookup helper is case-insensitive."""
        upper_meta = get_v4_pool_metadata(AI_USDG_V4_POOL_ID.upper())
        lower_meta = get_v4_pool_metadata(AI_USDG_V4_POOL_ID.lower())
        assert upper_meta is not None
        assert lower_meta is not None
        assert upper_meta == lower_meta
        assert upper_meta["tick_spacing"] == 23


class TestV4PoolSpec:
    """Unit tests for updated V4PoolSpec dataclass."""

    def test_v4_pool_spec_default_fields(self) -> None:
        """Default tick_spacing is 60 and hooks is ZERO_ADDRESS."""
        spec = V4PoolSpec(
            address=AI_USDG_V4_POOL_ID,
            label="AI/USDG 0.23%",
            fee_bps=23.0,
        )
        assert spec.tick_spacing == 60
        assert spec.hooks == ZERO_ADDRESS

    def test_v4_pool_spec_custom_fields(self) -> None:
        """Custom tick_spacing and hooks are properly set and validated."""
        spec = V4PoolSpec(
            address=AI_USDG_V4_POOL_ID,
            label="AI/USDG 0.23%",
            fee_bps=23.0,
            token0=AI_TOKEN_ADDRESS,
            token1=USDG_TOKEN_ADDRESS,
            tick_spacing=23,
            hooks=ZERO_ADDRESS,
        )
        assert spec.tick_spacing == 23
        assert spec.hooks == ZERO_ADDRESS
        assert spec.compute_pool_id() == AI_USDG_V4_POOL_ID.lower()


class TestPoolConfigConversion:
    """Test scanned_to_pool_spec auto-populates tick_spacing and hooks."""

    def test_scanned_to_pool_spec_matches_v4_metadata(self) -> None:
        """Verify ScannedPool conversion looks up real tick_spacing and hooks."""
        scanned = ScannedPool(
            dex="uniswap-v4",
            name="AI / USDG 0.23%",
            addr=AI_USDG_V4_POOL_ID,
            tvl_usd=279014.0,
            fee_bps=23.0,
            token0_symbol="AI",
            token1_symbol="USDG",
            token0_address=AI_TOKEN_ADDRESS,
            token1_address=USDG_TOKEN_ADDRESS,
        )
        spec = scanned_to_pool_spec(scanned)
        assert isinstance(spec, V4PoolSpec)
        assert spec.tick_spacing == 23
        assert spec.hooks == ZERO_ADDRESS
        assert spec.compute_pool_id() == AI_USDG_V4_POOL_ID.lower()


class TestExecutorV4CalldataAlignment:
    """Verify execution engine builds calldata with exact tickSpacing and hooks."""

    @pytest.fixture
    def executor(self) -> WethArbitrageExecutor:
        guard = WalletGuard(dry_run=True)
        w3_mock = MagicMock()
        w3_mock.eth.gas_price = 1_000_000_000
        return WethArbitrageExecutor(guard=guard, w3=w3_mock)

    def test_plan_from_triangular_alert_populates_v4_leg_metadata(
        self, executor: WethArbitrageExecutor
    ) -> None:
        """Plan generation from triangular alert preserves V4 tick_spacing=23."""
        v4_pool = V4PoolSpec(
            address=AI_USDG_V4_POOL_ID,
            label="AI / USDG 0.23%",
            fee_bps=23.0,
            token0=AI_TOKEN_ADDRESS,
            token1=USDG_TOKEN_ADDRESS,
            tick_spacing=23,
            hooks=ZERO_ADDRESS,
        )
        legs = [
            SwapLeg(
                from_token=CANONICAL_WETH_ADDRESS,
                to_token=AI_TOKEN_ADDRESS,
                pool=PoolSpec(
                    address="0xc4a21f9d6485fc5893dd4a491b320a83daf4da1d",
                    label="AI/WETH 1%",
                    fee_bps=100.0,
                ),
                rate=8442.0,
                fee_bps=100.0,
                effective_rate=8442.0 * (1 - 0.01),
                from_symbol="WETH",
                to_symbol="AI",
            ),
            SwapLeg(
                from_token=AI_TOKEN_ADDRESS,
                to_token=USDG_TOKEN_ADDRESS,
                pool=v4_pool,
                rate=0.2913,
                fee_bps=23.0,
                effective_rate=0.2913 * (1 - 0.0023),
                from_symbol="AI",
                to_symbol="USDG",
            ),
            SwapLeg(
                from_token=USDG_TOKEN_ADDRESS,
                to_token=CANONICAL_WETH_ADDRESS,
                pool=PoolSpec(
                    address="0x52e65b17fb6e5ba00ed806f37afcd2daa50271ca",
                    label="USDG/WETH 0.01%",
                    fee_bps=1.0,
                ),
                rate=0.000406,
                fee_bps=1.0,
                effective_rate=0.000406 * (1 - 0.0001),
                from_symbol="USDG",
                to_symbol="WETH",
            ),
        ]
        alert = TriangularArbAlert(
            start_token="WETH",
            cycle=("WETH", "AI", "USDG", "WETH"),
            legs=legs,
            gross_multiplier=1.02,
            fee_multiplier=0.989,
            expected_multiplier=1.008,
            slippage_buffer_pct=0.5,
            net_multiplier=1.003,
            gross_profit_pct=2.0,
            net_profit_pct=0.3,
            total_fee_pct=1.1,
        )

        plan = executor.plan_from_triangular_alert(
            alert=alert,
            amount_in_weth=10**17,
            amount_usd=250.0,
            slippage_pct=1.0,
        )

        assert len(plan.legs) == 3
        v4_leg = plan.legs[1]
        assert v4_leg.dex == "uniswap-v4"
        assert v4_leg.pool_fee == 2300
        assert v4_leg.tick_spacing == 23
        assert v4_leg.hooks == ZERO_ADDRESS

    def test_universal_router_mixed_calldata_decodes_exact_pool_key(
        self, executor: WethArbitrageExecutor
    ) -> None:
        """Universal Router mixed V3/V4 swap encodes exact PoolKey matching poolId."""
        legs = [
            ArbitrageLeg(
                from_token=CANONICAL_WETH_ADDRESS,
                to_token=AI_TOKEN_ADDRESS,
                pool_fee=10000,
                dex="uniswap-v3",
            ),
            ArbitrageLeg(
                from_token=AI_TOKEN_ADDRESS,
                to_token=USDG_TOKEN_ADDRESS,
                pool_fee=2300,
                dex="uniswap-v4",
                pool_address=AI_USDG_V4_POOL_ID,
                tick_spacing=23,
                hooks=ZERO_ADDRESS,
            ),
            ArbitrageLeg(
                from_token=USDG_TOKEN_ADDRESS,
                to_token=CANONICAL_WETH_ADDRESS,
                pool_fee=100,
                dex="uniswap-v3",
            ),
        ]
        from execution.weth_arbitrage_executor import ArbitragePlan

        plan = ArbitragePlan(
            legs=legs,
            amount_in_weth=10**17,
            expected_amount_out=int(10**17 * 1.02),
            amount_out_min=int(10**17 * 1.01),
            amount_usd=250.0,
            slippage_pct=1.0,
        )

        calldata_info = executor.build_swap_calldata(plan, recipient=CANONICAL_UNIVERSAL_ROUTER)
        calldata = calldata_info["calldata"]

        # Decode calldata with RouterCodec
        codec = RouterCodec()
        decoded_func, decoded_params = codec.decode.function_input(calldata)
        assert decoded_func.fn_name == "execute"

        # Search for SWAP_EXACT_IN_SINGLE in inputs
        v4_inputs = [inp for inp in decoded_params["inputs"] if "V4_SWAP" in str(inp[0])]
        assert len(v4_inputs) >= 1
        actions_dict = v4_inputs[0][1]

        # Extract PoolKey from params
        pool_key_dict: dict[str, Any] | None = None
        for action_fn, param_dict in actions_dict["params"]:
            if "SWAP_EXACT_IN_SINGLE" in str(action_fn):
                pool_key_dict = param_dict["exact_in_single_params"]["PoolKey"]
                break

        assert pool_key_dict is not None, "SWAP_EXACT_IN_SINGLE not found in decoded calldata"
        assert pool_key_dict["fee"] == 2300
        assert pool_key_dict["tickSpacing"] == 23
        assert pool_key_dict["hooks"] == ZERO_ADDRESS

        # Verify keccak256 hash of decoded PoolKey matches AI / USDG poolId 100%
        c0 = pool_key_dict["currency0"]
        c1 = pool_key_dict["currency1"]
        encoded_pool_key = abi_encode(
            ["address", "address", "uint24", "int24", "address"],
            [c0, c1, pool_key_dict["fee"], pool_key_dict["tickSpacing"], pool_key_dict["hooks"]],
        )
        computed_hash = "0x" + Web3.keccak(encoded_pool_key).hex().lower()
        assert computed_hash == AI_USDG_V4_POOL_ID.lower()

    def test_universal_router_all_v4_calldata_decodes_exact_path_keys(
        self, executor: WethArbitrageExecutor
    ) -> None:
        """Universal Router pure V4 swap encodes exact PathKeys with tick_spacing."""
        legs = [
            ArbitrageLeg(
                from_token=CANONICAL_WETH_ADDRESS,
                to_token=USDG_TOKEN_ADDRESS,
                pool_fee=100,
                dex="uniswap-v4",
                tick_spacing=1,
                hooks=ZERO_ADDRESS,
            ),
            ArbitrageLeg(
                from_token=USDG_TOKEN_ADDRESS,
                to_token=AI_TOKEN_ADDRESS,
                pool_fee=2300,
                dex="uniswap-v4",
                tick_spacing=23,
                hooks=ZERO_ADDRESS,
            ),
            ArbitrageLeg(
                from_token=AI_TOKEN_ADDRESS,
                to_token=CANONICAL_WETH_ADDRESS,
                pool_fee=10000,
                dex="uniswap-v4",
                tick_spacing=200,
                hooks=ZERO_ADDRESS,
            ),
        ]
        from execution.weth_arbitrage_executor import ArbitragePlan

        plan = ArbitragePlan(
            legs=legs,
            amount_in_weth=10**17,
            expected_amount_out=int(10**17 * 1.02),
            amount_out_min=int(10**17 * 1.01),
            amount_usd=250.0,
            slippage_pct=1.0,
        )

        calldata_info = executor.build_swap_calldata(plan, recipient=CANONICAL_UNIVERSAL_ROUTER)
        calldata = calldata_info["calldata"]

        codec = RouterCodec()
        decoded_func, decoded_params = codec.decode.function_input(calldata)
        assert decoded_func.fn_name == "execute"

        v4_inputs = [inp for inp in decoded_params["inputs"] if "V4_SWAP" in str(inp[0])]
        assert len(v4_inputs) >= 1
        actions_dict = v4_inputs[0][1]

        path_keys = None
        for action_fn, param_dict in actions_dict["params"]:
            if "SWAP_EXACT_IN" in str(action_fn):
                path_keys = param_dict["params"]["PathKeys"]
                break

        assert path_keys is not None
        assert len(path_keys) == 3
        assert path_keys[0]["tickSpacing"] == 1
        assert path_keys[0]["fee"] == 100
        assert path_keys[1]["tickSpacing"] == 23
        assert path_keys[1]["fee"] == 2300
        assert path_keys[2]["tickSpacing"] == 200
        assert path_keys[2]["fee"] == 10000
