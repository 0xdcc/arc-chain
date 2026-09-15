"""Independent verification test suite for protocol identity whitelist.

Verifies exact dual-specification protocol whitelist:
- V3 whitelist: ('uniswap-v3', 'uniswap_v3')
- V4 whitelist: ('uniswap-v4', 'uniswap_v4')
- Non-whitelisted protocols (forks, case-aliases, other DEXes) are strictly rejected.
- Uniswap V2 strict rejection per C12 is preserved.
"""

from __future__ import annotations

import dataclasses

import pytest

from arbitrage_contracts.identity import (
    PoolDescriptor,
    PoolKey,
)
from arbitrage_contracts.quote import HopRef, RouteRef
from atomic_execution.encoding import (
    UnsupportedProtocolError,
    encode_execution_plan,
)
from atomic_execution.planning import ExecutionPlan
from tests.arc_v3.independent.test_protocol_encoding_increment import make_plan


def _replace_hop_protocol(
    plan: ExecutionPlan, hop_index: int, new_protocol: str
) -> ExecutionPlan:
    """Create a new ExecutionPlan with a specific hop's protocol_id replaced.

    Ensures pool_key, pool_descriptor.key, and route_ref remain fully consistent.
    """
    old_hop = plan.hops[hop_index]
    old_pk = old_hop.pool_key
    new_pk = PoolKey(
        chain_id=old_pk.chain_id,
        protocol_id=new_protocol,
        venue_kind=old_pk.venue_kind,
        venue_address=old_pk.venue_address,
        pool_id_kind=old_pk.pool_id_kind,
        pool_id=old_pk.pool_id,
    )
    old_desc = old_hop.pool_descriptor
    new_desc = None
    if old_desc is not None:
        new_desc = PoolDescriptor(
            key=new_pk,
            currency0=old_desc.currency0,
            currency1=old_desc.currency1,
            fee_model=old_desc.fee_model,
            tick_spacing=old_desc.tick_spacing,
            hooks=old_desc.hooks,
        )
    new_hop = HopRef(
        pool_key=new_pk,
        asset_in=old_hop.asset_in,
        asset_out=old_hop.asset_out,
        direction=old_hop.direction,
        pool_descriptor=new_desc,
    )
    new_hops = list(plan.hops)
    new_hops[hop_index] = new_hop
    new_route = RouteRef(
        chain_id=plan.route_ref.chain_id,
        hops=tuple(new_hops),
        base_asset=plan.route_ref.base_asset,
    )
    return dataclasses.replace(plan, route_ref=new_route)


@pytest.mark.parametrize("protocol", ["uniswap-v3", "uniswap_v3"])
def test_whitelisted_v3_protocols_accepted(protocol: str) -> None:
    """Dual-specification V3 protocols must be accepted and encoded successfully."""
    base_plan = make_plan(["uniswap-v3", "uniswap-v3"])
    plan = _replace_hop_protocol(base_plan, 0, protocol)
    plan = _replace_hop_protocol(plan, 1, protocol)
    encoded = encode_execution_plan(plan)
    assert encoded.calldata_hex.startswith("0x")
    assert len(encoded.calldata_hex) > 2


@pytest.mark.parametrize("protocol", ["uniswap-v4", "uniswap_v4"])
def test_whitelisted_v4_protocols_accepted(protocol: str) -> None:
    """Dual-specification V4 protocols must be accepted and encoded successfully."""
    base_plan = make_plan(["uniswap-v4", "uniswap-v4"])
    plan = _replace_hop_protocol(base_plan, 0, protocol)
    plan = _replace_hop_protocol(plan, 1, protocol)
    encoded = encode_execution_plan(plan)
    assert encoded.calldata_hex.startswith("0x")
    assert len(encoded.calldata_hex) > 2


def test_whitelisted_mixed_v3_v4_accepted() -> None:
    """Mixed paths with valid whitelisted identifiers must encode successfully."""
    base_plan = make_plan(["uniswap-v3", "uniswap-v4", "uniswap-v3"])
    plan = _replace_hop_protocol(base_plan, 0, "uniswap_v3")
    plan = _replace_hop_protocol(plan, 1, "uniswap_v4")
    plan = _replace_hop_protocol(plan, 2, "uniswap-v3")
    encoded = encode_execution_plan(plan)
    assert encoded.calldata_hex.startswith("0x")
    assert len(encoded.calldata_hex) > 2


@pytest.mark.parametrize("fork_protocol", ["sushiswap-v3", "pancakeswap-v3", "giga-v3"])
def test_fork_protocols_rejected(fork_protocol: str) -> None:
    """Fork protocols containing 'v3' must be strictly rejected as unsupported."""
    base_plan = make_plan(["uniswap-v3", "uniswap-v3"])
    plan = _replace_hop_protocol(base_plan, 0, fork_protocol)
    with pytest.raises(UnsupportedProtocolError) as exc_info:
        encode_execution_plan(plan)
    assert fork_protocol in str(exc_info.value)
    assert "unknown protocol" in str(exc_info.value) or "unsupported protocol" in str(exc_info.value)


@pytest.mark.parametrize("alias_protocol", ["UNISWAP-V3", "Uniswap-V3", "uniswap-V3"])
def test_casing_aliases_rejected(alias_protocol: str) -> None:
    """Non-exact casing aliases must be strictly rejected without case-folding."""
    base_plan = make_plan(["uniswap-v3", "uniswap-v3"])
    plan = _replace_hop_protocol(base_plan, 0, alias_protocol)
    with pytest.raises(UnsupportedProtocolError) as exc_info:
        encode_execution_plan(plan)
    assert alias_protocol in str(exc_info.value)
    assert "unknown protocol" in str(exc_info.value) or "unsupported protocol" in str(exc_info.value)


def test_v4_fork_rejected() -> None:
    """V4 fork protocols containing 'v4' must be strictly rejected."""
    base_plan = make_plan(["uniswap-v4", "uniswap-v4"])
    plan = _replace_hop_protocol(base_plan, 0, "pancakeswap-v4")
    with pytest.raises(UnsupportedProtocolError) as exc_info:
        encode_execution_plan(plan)
    assert "pancakeswap-v4" in str(exc_info.value)


def test_v4_alias_rejected() -> None:
    """V4 uppercase alias must be strictly rejected without case-folding."""
    base_plan = make_plan(["uniswap-v4", "uniswap-v4"])
    plan = _replace_hop_protocol(base_plan, 0, "UNISWAP-V4")
    with pytest.raises(UnsupportedProtocolError) as exc_info:
        encode_execution_plan(plan)
    assert "UNISWAP-V4" in str(exc_info.value)


def test_v2_strictly_rejected_c12() -> None:
    """Uniswap V2 protocols must be rejected per C12 policy."""
    base_plan = make_plan(["uniswap-v3", "uniswap-v3"])
    plan = _replace_hop_protocol(base_plan, 0, "uniswap-v2")
    with pytest.raises(UnsupportedProtocolError) as exc_info:
        encode_execution_plan(plan)
    assert "C12" in str(exc_info.value)
