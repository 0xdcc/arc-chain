"""Formal regression tests for MarketSnapshot immutability and pool container safety.

Verifies:
1. Isolation from input dictionary mutations (append, overwrite, clear post-construction).
2. Direct mutation rejection on snap.pools (item assignment, deletion, attribute assignment raise TypeError/FrozenInstanceError).
3. Fail-closed constructor rejection of invalid pool entries and non-mapping containers.
4. Lossless to_dict / from_dict roundtrip with standard dict semantics (serialize does not leak MappingProxyType).
5. S1 downstream strategies (spread & triangular) execute cleanly over immutable snapshots without AttributeError crashes.
6. Explicit boundary: snapshot pool mapping is protected via MappingProxyType and defensive shallow copy;
   deep immutability of inner PoolStateSnapshot fields (e.g. raw_response) is NOT claimed in this round.
"""

import dataclasses
import json
from decimal import Decimal
from types import MappingProxyType
from typing import Any

import pytest

from research.market_data.types import (
    MarketSnapshot,
    PoolIdentity,
    PoolStateSnapshot,
    TokenIdentity,
    from_dict,
    to_dict,
)
from research.strategies.spread import find_spread_candidates
from research.strategies.triangular import find_triangular_candidates

_Q96 = Decimal(2**96)


def _price_to_sqrt_price_x96(price_t1_per_t0: float | Decimal, dec0: int, dec1: int) -> int:
    """Convert human price (token1 units per token0 unit) into Uniswap sqrt_price_x96."""
    raw_ratio = Decimal(str(price_t1_per_t0)) * Decimal(10 ** (dec1 - dec0))
    sqrt_ratio = raw_ratio.sqrt()
    return int(sqrt_ratio * _Q96)


@pytest.fixture
def token_usdg() -> TokenIdentity:
    return TokenIdentity(
        chain_id=4663,
        address="0x" + "11" * 20,
        symbol="USDG",
        decimals=6,
    )


@pytest.fixture
def token_weth() -> TokenIdentity:
    return TokenIdentity(
        chain_id=4663,
        address="0x" + "22" * 20,
        symbol="WETH",
        decimals=18,
    )


@pytest.fixture
def token_wbtc() -> TokenIdentity:
    return TokenIdentity(
        chain_id=4663,
        address="0x" + "33" * 20,
        symbol="WBTC",
        decimals=8,
    )


@pytest.fixture
def pool_v3_usdg_weth(token_usdg: TokenIdentity, token_weth: TokenIdentity) -> PoolIdentity:
    return PoolIdentity(
        chain_id=4663,
        protocol="uniswap_v3",
        pool_id="0x" + "aa" * 20,
        token0=token_weth.address,
        token1=token_usdg.address,
        fee_bps=5.0,
        tick_spacing=10,
    )


@pytest.fixture
def pool_v4_usdg_weth(token_usdg: TokenIdentity, token_weth: TokenIdentity) -> PoolIdentity:
    return PoolIdentity(
        chain_id=4663,
        protocol="uniswap_v4",
        pool_id="0x" + "bb" * 20,
        token0=token_weth.address,
        token1=token_usdg.address,
        fee_bps=30.0,
        tick_spacing=60,
    )


@pytest.fixture
def pool_snap_v3(pool_v3_usdg_weth: PoolIdentity, token_usdg: TokenIdentity, token_weth: TokenIdentity) -> PoolStateSnapshot:
    sqrt_p = _price_to_sqrt_price_x96(2000.0, token_weth.decimals, token_usdg.decimals)
    return PoolStateSnapshot(
        pool=pool_v3_usdg_weth,
        block_number=100000,
        block_timestamp=1700000000,
        sqrt_price_x96=sqrt_p,
        liquidity=1_000_000,
        tick=0,
    )


@pytest.fixture
def pool_snap_v4(pool_v4_usdg_weth: PoolIdentity, token_usdg: TokenIdentity, token_weth: TokenIdentity) -> PoolStateSnapshot:
    sqrt_p = _price_to_sqrt_price_x96(2050.0, token_weth.decimals, token_usdg.decimals)
    return PoolStateSnapshot(
        pool=pool_v4_usdg_weth,
        block_number=100000,
        block_timestamp=1700000000,
        sqrt_price_x96=sqrt_p,
        liquidity=2_000_000,
        tick=0,
    )


class TestMarketSnapshotImmutability:
    """Tests validating MarketSnapshot.pools immutability and anti-aliasing contract."""

    def test_input_dict_mutation_isolation(
        self,
        pool_v3_usdg_weth: PoolIdentity,
        pool_v4_usdg_weth: PoolIdentity,
        pool_snap_v3: PoolStateSnapshot,
        pool_snap_v4: PoolStateSnapshot,
    ) -> None:
        """Mutating the input dict after snapshot construction MUST NOT affect snap.pools."""
        input_dict: dict[str, PoolStateSnapshot] = {pool_v3_usdg_weth.pool_id: pool_snap_v3}
        snapshot = MarketSnapshot(
            chain_id=4663,
            block_number=100000,
            captured_at=1700000000.0,
            pools=input_dict,
        )

        # 1. Independent object verification
        assert snapshot.pools is not input_dict
        assert isinstance(snapshot.pools, MappingProxyType)
        assert len(snapshot.pools) == 1
        assert pool_v3_usdg_weth.pool_id in snapshot.pools

        # 2. Append mutation to input_dict
        input_dict[pool_v4_usdg_weth.pool_id] = pool_snap_v4
        assert pool_v4_usdg_weth.pool_id not in snapshot.pools
        assert len(snapshot.pools) == 1

        # 3. Overwrite mutation to input_dict (with invalid entry)
        corrupted_val: Any = "corrupted_string_value"
        input_dict[pool_v3_usdg_weth.pool_id] = corrupted_val
        assert snapshot.pools[pool_v3_usdg_weth.pool_id] is pool_snap_v3
        assert snapshot.pools[pool_v3_usdg_weth.pool_id] != corrupted_val

        # 4. Clear mutation to input_dict
        input_dict.clear()
        assert len(input_dict) == 0
        assert len(snapshot.pools) == 1
        assert pool_v3_usdg_weth.pool_id in snapshot.pools

    def test_direct_mutation_rejection(
        self,
        pool_v3_usdg_weth: PoolIdentity,
        pool_v4_usdg_weth: PoolIdentity,
        pool_snap_v3: PoolStateSnapshot,
        pool_snap_v4: PoolStateSnapshot,
    ) -> None:
        """Direct modification of snap.pools MUST raise TypeError or FrozenInstanceError."""
        snapshot = MarketSnapshot(
            chain_id=4663,
            block_number=100000,
            captured_at=1700000000.0,
            pools={pool_v3_usdg_weth.pool_id: pool_snap_v3},
        )

        # 1. Direct item assignment on mappingproxy
        pools_any: Any = snapshot.pools
        with pytest.raises(TypeError, match="'mappingproxy' object does not support item assignment"):
            pools_any[pool_v4_usdg_weth.pool_id] = pool_snap_v4

        # 2. Direct item deletion on mappingproxy
        with pytest.raises(TypeError, match="'mappingproxy' object does not support item deletion"):
            del pools_any[pool_v3_usdg_weth.pool_id]

        # 3. Direct attribute reassignment (frozen dataclass contract)
        snap_any: Any = snapshot
        with pytest.raises(dataclasses.FrozenInstanceError):
            snap_any.pools = {}

        # 4. Direct attribute deletion (frozen dataclass contract)
        with pytest.raises(dataclasses.FrozenInstanceError):
            del snap_any.pools

    def test_constructor_fail_closed_validation(
        self,
        pool_v3_usdg_weth: PoolIdentity,
        pool_snap_v3: PoolStateSnapshot,
    ) -> None:
        """Constructor MUST fail-closed with TypeError when encountering invalid entries or bad containers."""
        # Bad entry cases
        bad_entries: list[tuple[str, Any]] = [
            ("string_entry", "not_a_pool_snapshot"),
            ("dict_entry", {"pool_id": "0x123", "sqrt_price_x96": 100}),
            ("none_entry", None),
            ("int_entry", 123456789),
        ]

        for label, bad_val in bad_entries:
            with pytest.raises(TypeError, match=rf"pools\['{label}'\] must be PoolStateSnapshot"):
                MarketSnapshot(
                    chain_id=4663,
                    block_number=100000,
                    captured_at=1700000000.0,
                    pools={label: bad_val},
                )

        # Non-mapping pools parameter
        bad_pools_containers: list[Any] = [
            "not_a_dict",
            12345,
            None,
            [pool_snap_v3],
            (pool_snap_v3,),
        ]
        for bad_container in bad_pools_containers:
            with pytest.raises(TypeError, match="pools must be a dict"):
                MarketSnapshot(
                    chain_id=4663,
                    block_number=100000,
                    captured_at=1700000000.0,
                    pools=bad_container,
                )

        # Boolean injection checks (Gate 131: boolean-integer subclass penetration)
        with pytest.raises(ValueError, match="chain_id must be a positive integer"):
            MarketSnapshot(
                chain_id=True,
                block_number=100000,
                captured_at=1700000000.0,
                pools={pool_v3_usdg_weth.pool_id: pool_snap_v3},
            )

        with pytest.raises(ValueError, match="block_number must be non-negative integer"):
            MarketSnapshot(
                chain_id=4663,
                block_number=False,
                captured_at=1700000000.0,
                pools={pool_v3_usdg_weth.pool_id: pool_snap_v3},
            )

    def test_serialization_roundtrip_no_proxy_leakage(
        self,
        pool_v3_usdg_weth: PoolIdentity,
        pool_snap_v3: PoolStateSnapshot,
    ) -> None:
        """to_dict/from_dict roundtrip preserves semantics without leaking MappingProxyType into serialized dict."""
        original = MarketSnapshot(
            chain_id=4663,
            block_number=100000,
            captured_at=1700000000.123,
            pools={pool_v3_usdg_weth.pool_id: pool_snap_v3},
        )

        # 1. Instance method to_dict()
        dict_rep = original.to_dict()
        assert isinstance(dict_rep, dict)
        assert isinstance(dict_rep["pools"], dict)
        # Verify serialize does NOT leak proxy
        assert type(dict_rep["pools"]) is dict

        # 2. JSON compatibility
        json_bytes = json.dumps(dict_rep).encode("utf-8")
        parsed = json.loads(json_bytes.decode("utf-8"))

        # 3. Classmethod from_dict()
        restored = MarketSnapshot.from_dict(parsed)
        assert restored == original
        assert isinstance(restored.pools, MappingProxyType)
        assert restored.pools[pool_v3_usdg_weth.pool_id].sqrt_price_x96 == pool_snap_v3.sqrt_price_x96

        # 4. Standalone to_dict / from_dict helper parity
        helper_dict = to_dict(original)
        assert isinstance(helper_dict, dict)
        assert type(helper_dict["pools"]) is dict

        restored_helper = from_dict(MarketSnapshot, helper_dict)
        assert restored_helper == original
        assert isinstance(restored_helper.pools, MappingProxyType)

    def test_s1_strategies_normal_chain_execution(
        self,
        token_usdg: TokenIdentity,
        token_weth: TokenIdentity,
        token_wbtc: TokenIdentity,
        pool_v3_usdg_weth: PoolIdentity,
        pool_v4_usdg_weth: PoolIdentity,
        pool_snap_v3: PoolStateSnapshot,
        pool_snap_v4: PoolStateSnapshot,
    ) -> None:
        """Downstream S1 strategies (spread and triangular) consume immutable snapshot without crashes."""
        # 1. Spread strategy with two pools on same pair
        orig_pools_dict = {
            pool_v3_usdg_weth.pool_id: pool_snap_v3,
            pool_v4_usdg_weth.pool_id: pool_snap_v4,
        }
        snap_spread = MarketSnapshot(
            chain_id=4663,
            block_number=100000,
            captured_at=1700000000.0,
            pools=orig_pools_dict,
        )

        # Mutate the source dictionary post-construction to simulate external contamination
        orig_pools_any: Any = orig_pools_dict
        orig_pools_any["external_bad_key"] = "bad_value"
        orig_pools_any[pool_v3_usdg_weth.pool_id] = "poisoned_pool"

        # Spread strategy execution: must succeed and find the valid opportunity
        spread_candidates = find_spread_candidates(snap_spread, min_gross_bps=10.0, base_token=token_usdg)
        assert len(spread_candidates) == 1
        cand = spread_candidates[0]
        assert cand.route_type == "two_hop_spread"
        assert cand.base_token == token_usdg
        assert len(cand.hops) == 2

        # 2. Triangular strategy setup
        pool_weth_wbtc = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v3",
            pool_id="0x" + "cc" * 20,
            token0=token_wbtc.address,
            token1=token_weth.address,
            fee_bps=5.0,
            tick_spacing=10,
        )
        pool_wbtc_usdg = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v3",
            pool_id="0x" + "dd" * 20,
            token0=token_wbtc.address,
            token1=token_usdg.address,
            fee_bps=5.0,
            tick_spacing=10,
        )
        snap_weth_wbtc = PoolStateSnapshot(
            pool=pool_weth_wbtc,
            block_number=100000,
            block_timestamp=1700000000,
            sqrt_price_x96=_price_to_sqrt_price_x96(0.05, token_weth.decimals, token_wbtc.decimals),
            liquidity=1_000_000,
            tick=0,
        )
        snap_wbtc_usdg = PoolStateSnapshot(
            pool=pool_wbtc_usdg,
            block_number=100000,
            block_timestamp=1700000000,
            sqrt_price_x96=_price_to_sqrt_price_x96(40000.0, token_wbtc.decimals, token_usdg.decimals),
            liquidity=1_000_000,
            tick=0,
        )

        tri_pools_dict = {
            pool_v3_usdg_weth.pool_id: pool_snap_v3,
            pool_weth_wbtc.pool_id: snap_weth_wbtc,
            pool_wbtc_usdg.pool_id: snap_wbtc_usdg,
        }
        snap_tri = MarketSnapshot(
            chain_id=4663,
            block_number=100000,
            captured_at=1700000000.0,
            pools=tri_pools_dict,
        )

        # Mutate the source dictionary post-construction
        tri_pools_dict.clear()

        # Triangular strategy execution: must succeed without AttributeError
        tri_candidates = find_triangular_candidates(snap_tri, min_gross_bps=1.0, base_token=token_usdg)
        assert isinstance(tri_candidates, list)
        # Strategy executed cleanly across all pool states in snapshot.pools.values()

    def test_shallow_immutability_scope_boundary(
        self,
        pool_v3_usdg_weth: PoolIdentity,
    ) -> None:
        """Documents scope boundary: pools mapping container is immutable, but inner metadata is not deep-frozen."""
        raw_metadata: dict[str, Any] = {"custom_tag": "initial"}
        pool_snap = PoolStateSnapshot(
            pool=pool_v3_usdg_weth,
            block_number=100000,
            block_timestamp=1700000000,
            sqrt_price_x96=100,
            liquidity=1000,
            tick=0,
            raw_response=raw_metadata,
        )
        snapshot = MarketSnapshot(
            chain_id=4663,
            block_number=100000,
            captured_at=1700000000.0,
            pools={pool_v3_usdg_weth.pool_id: pool_snap},
        )

        # Container is immutable
        assert isinstance(snapshot.pools, MappingProxyType)
        pools_any: Any = snapshot.pools
        with pytest.raises(TypeError):
            pools_any["other"] = pool_snap

        # Inner raw_response remains shallowly referenced (explicit scope boundary: deep immutability not claimed)
        assert snapshot.pools[pool_v3_usdg_weth.pool_id].raw_response is raw_metadata
