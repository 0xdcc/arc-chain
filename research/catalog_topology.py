"""Read-only historical catalog connectivity, never executable pool identity.

Catalogs without factory/chain attestation cannot create execution PoolDescriptors.
The caller explicitly supplies the historical chain context and base token addresses.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any


def _hex(value: Any, size: int) -> str:
    if (not isinstance(value, str) or len(value) != 2 + size * 2
            or not value.startswith("0x")
            or any(c not in "0123456789abcdefABCDEF" for c in value[2:])):
        raise ValueError("Invalid catalog hex identifier")
    return value.lower()


@dataclass(frozen=True)
class HistoricalHop:
    pool_id: str
    token_in: str
    token_out: str


@dataclass(frozen=True)
class HistoricalCycle:
    base: str
    hops: tuple[HistoricalHop, ...]


class HistoricalCatalogTopology:
    """Enumerate observed 2/3-hop connectivity; no quotes, ledger or transport."""

    def __init__(self, catalog_paths: Sequence[Path], *, chain_id: int,
                 base_tokens: Sequence[str], protocol_schemas: Mapping[str, str]) -> None:
        if type(chain_id) is not int or chain_id <= 0:
            raise ValueError("Explicit positive historical chain_id required")
        schemas = dict(protocol_schemas)
        if not schemas or any(type(key) is not str or not key or value not in ("v3", "v4")
                              for key, value in schemas.items()):
            raise ValueError("Explicit historical protocol schemas required")
        self.chain_id = chain_id
        bases = sorted({_hex(token, 20) for token in base_tokens})
        if not bases:
            raise ValueError("Explicit base tokens required")
        pools: dict[str, dict[str, Any]] = {}
        for path in catalog_paths:
            with path.open(encoding="utf-8") as handle:
                rows = json.load(handle)
            if not isinstance(rows, list):
                raise ValueError("Catalog must contain a list")
            for item in rows:
                if not isinstance(item, dict):
                    raise ValueError("Catalog pool must be an object")
                row = dict(item)
                protocol = row.get("dex")
                if protocol not in schemas:
                    raise ValueError("Unsupported historical protocol")
                addr = _hex(row.get("address"), 32 if schemas[protocol] == "v4" else 20)
                token0, token1 = (_hex(row.get(key), 20) for key in ("token0", "token1"))
                if token0 == token1:
                    raise ValueError("Self-pair is not a pool")
                if "chain_id" in row and (type(row["chain_id"]) is not int or row["chain_id"] != chain_id):
                    raise ValueError("Catalog chain context conflict")
                for key in ("dec0", "dec1"):
                    if type(row.get(key)) is not int or not 0 <= row[key] <= 255:
                        raise ValueError("Explicit token decimals required")
                if row.get("decimals") != [row["dec0"], row["dec1"]]:
                    raise ValueError("Conflicting token decimals")
                if type(row.get("fee_pips")) is not int or not 0 <= row["fee_pips"] < 1 << 24:
                    raise ValueError("Invalid raw fee")
                if type(row.get("tick_spacing")) is not int or not 0 < row["tick_spacing"] < 1 << 23:
                    raise ValueError("Invalid tick spacing")
                if schemas[protocol] == "v4":
                    _hex(row.get("hooks"), 20)
                row.update(address=addr, token0=token0, token1=token1)
                if addr in pools and pools[addr] != row:
                    raise ValueError("Conflicting duplicate pool")
                pools[addr] = row
        self.pools_meta = tuple(MappingProxyType(pools[key]) for key in sorted(pools))
        edges: dict[str, list[HistoricalHop]] = {}
        for pool_meta in self.pools_meta:
            for src, dst in ((pool_meta["token0"], pool_meta["token1"]), (pool_meta["token1"], pool_meta["token0"])):
                edges.setdefault(src, []).append(HistoricalHop(pool_meta["address"], src, dst))
        cycles: list[HistoricalCycle] = []
        for base in bases:
            for first in edges.get(base, []):
                for second in edges.get(first.token_out, []):
                    if second.pool_id == first.pool_id:
                        continue
                    if second.token_out == base:
                        cycles.append(HistoricalCycle(base, (first, second)))
                        continue
                    if second.token_out == first.token_out:
                        continue
                    for third in edges.get(second.token_out, []):
                        if third.token_out == base and third.pool_id not in (first.pool_id, second.pool_id):
                            cycles.append(HistoricalCycle(base, (first, second, third)))
        self.candidate_cycles = tuple(cycles)
