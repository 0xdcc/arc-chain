"""Synthetic small-graph truth, separate from frozen historical catalog evidence."""
import json

import pytest

from research.catalog_topology import HistoricalCatalogTopology

A, B, C = ("0x" + value * 40 for value in ("a", "b", "c"))


def pool(number, token0, token1):
    return dict(address="0x" + f"{number:040x}", dex="test-v3", token0=token0,
                token1=token1, dec0=18, dec1=18, decimals=[18, 18],
                fee_pips=3000, tick_spacing=60)


def build(tmp_path, rows, **kwargs):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(rows))
    return HistoricalCatalogTopology([path], chain_id=4663, base_tokens=[A],
                                     protocol_schemas={"test-v3": "v3"}, **kwargs)


def test_exact_parallel_edges_and_triangles(tmp_path):
    graph = build(tmp_path, [pool(1, A, B), pool(2, A, B), pool(3, B, C), pool(4, C, A)])
    paths = {tuple(int(hop.pool_id, 16) for hop in cycle.hops) for cycle in graph.candidate_cycles}
    assert paths == {(1, 2), (2, 1), (1, 3, 4), (2, 3, 4), (4, 3, 1), (4, 3, 2)}
    assert len(graph.candidate_cycles) == len(paths)
    for cycle in graph.candidate_cycles:
        assert cycle.hops[0].token_in == cycle.hops[-1].token_out == A
        assert len({hop.pool_id for hop in cycle.hops}) == len(cycle.hops)


def test_single_pool_roundtrip_never_counted(tmp_path):
    assert build(tmp_path, [pool(1, A, B)]).candidate_cycles == ()


def test_duplicate_identical_catalog_entry_not_double_counted(tmp_path):
    assert len(build(tmp_path, [pool(1, A, B), pool(1, A, B)]).pools_meta) == 1


def test_conflicting_duplicate_rejected(tmp_path):
    bad = pool(1, A, B)
    bad["fee_pips"] = 500
    with pytest.raises(ValueError, match="Conflicting duplicate"):
        build(tmp_path, [pool(1, A, B), bad])


@pytest.mark.parametrize("field,value", [("chain_id", 1), ("dex", "undeclared"),
                                        ("dec0", None), ("token1", A), ("tick_spacing", 0)])
def test_invalid_metadata_rejected(tmp_path, field, value):
    row = pool(1, A, B)
    row[field] = value
    with pytest.raises(ValueError):
        build(tmp_path, [row])


def test_v4_missing_hook_rejected(tmp_path):
    row = pool(1, A, B)
    row.update(address="0x" + "01" * 32, dex="test-v4")
    path = tmp_path / "v4.json"
    path.write_text(json.dumps([row]))
    with pytest.raises(ValueError):
        HistoricalCatalogTopology([path], chain_id=4663, base_tokens=[A],
                                  protocol_schemas={"test-v4": "v4"})
