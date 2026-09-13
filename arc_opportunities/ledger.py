"""Arc Versioned Opportunity Ledger (T23)

Enforces:
- Formal record typing: RAW, QUOTE, SIM, RECONCILED
- Strict observation_id deterministic provenance
- F04 Hash-chain integrity, checkpointing, and half-tail truncation recovery via AppendOnlyLedger
- Complete retention of negative delta, unknown gas, and failed simulation outcomes
- Single writer isolation with advisory file locking
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from opportunities.store import (
    AppendOnlyLedger,
    _read_snapshot,
)


class RecordType(StrEnum):
    RAW = "raw"
    QUOTE = "quote"
    SIM = "sim"
    RECONCILED = "reconciled"


LEDGER_SCHEMA_VERSION = "arc-opportunity-ledger-v1"


@dataclass(frozen=True, slots=True)
class ArcOpportunityRecord:
    """Canonical persisted opportunity record across its lifecycle."""

    record_type: RecordType
    observation_id: str
    route_id: str
    chain_id: int
    state_ref: str
    amount_in_atoms: int
    amount_out_atoms: int | None
    net_atoms: int | None
    economic_status: str
    quote_status: str
    simulation_status: str | None = None
    reconciled_status: str | None = None
    payload: dict[str, Any] | None = None
    schema_version: str = LEDGER_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.observation_id:
            raise ValueError("observation_id cannot be empty")
        if not self.state_ref:
            raise ValueError("state_ref cannot be empty")
        if self.amount_in_atoms <= 0:
            raise ValueError("amount_in_atoms must be strictly positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "record_type": str(self.record_type),
            "observation_id": self.observation_id,
            "route_id": self.route_id,
            "chain_id": self.chain_id,
            "state_ref": self.state_ref,
            "amount_in_atoms": self.amount_in_atoms,
            "amount_out_atoms": self.amount_out_atoms,
            "net_atoms": self.net_atoms,
            "economic_status": self.economic_status,
            "quote_status": self.quote_status,
            "simulation_status": self.simulation_status,
            "reconciled_status": self.reconciled_status,
            "payload": self.payload or {},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArcOpportunityRecord:
        return cls(
            schema_version=data.get("schema_version", LEDGER_SCHEMA_VERSION),
            record_type=RecordType(data["record_type"]),
            observation_id=data["observation_id"],
            route_id=data["route_id"],
            chain_id=data["chain_id"],
            state_ref=data["state_ref"],
            amount_in_atoms=data["amount_in_atoms"],
            amount_out_atoms=data.get("amount_out_atoms"),
            net_atoms=data.get("net_atoms"),
            economic_status=data["economic_status"],
            quote_status=data["quote_status"],
            simulation_status=data.get("simulation_status"),
            reconciled_status=data.get("reconciled_status"),
            payload=data.get("payload"),
        )


def compute_observation_id(
    route_id: str,
    state_ref: str,
    amount_in_atoms: int,
    timestamp_ms: int,
) -> str:
    """Compute canonical deterministic observation ID."""
    seed = f"{route_id}:{state_ref}:{amount_in_atoms}:{timestamp_ms}"
    return f"obs:{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:24]}"


class ArcOpportunityLedger:
    """Versioned append-only opportunity ledger with F04 hash-chain backing."""

    def __init__(self, path: Path, *, auto_recover: bool = True) -> None:
        self.path = Path(path)
        self._inner = AppendOnlyLedger(self.path, auto_recover=auto_recover)

    def append(self, record: ArcOpportunityRecord) -> int:
        """Append an opportunity record to the ledger under hash-chain guarantees."""
        payload = record.to_dict()
        return self._inner.append(payload)

    def read_all(self) -> tuple[ArcOpportunityRecord, ...]:
        """Read all validated records from the ledger."""
        if not self.path.exists():
            return ()
        snapshot = _read_snapshot(self.path)
        records: list[ArcOpportunityRecord] = []
        for event in snapshot.events:
            records.append(ArcOpportunityRecord.from_dict(event))
        return tuple(records)

    def read_by_type(self, record_type: RecordType) -> tuple[ArcOpportunityRecord, ...]:
        """Read all records matching a specific stage/type."""
        all_records = self.read_all()
        return tuple(r for r in all_records if r.record_type == record_type)

    @property
    def confirmed_sequence(self) -> int:
        """Return the last confirmed sequence acknowledged by this ledger writer."""
        return self._inner.confirmed_sequence

    @property
    def head_hash(self) -> str:
        """Return the current hash-chain head."""
        return self._inner.head_hash
