"""SQLite transaction intents: wallet-wide pending latch and irreversible loss budget.

RESEARCH ONLY:
- Offline reservation ledger for strategy and cost modeling.
- Cannot sign, broadcast, or invoke RPC endpoints.
- Not connected to live Arc execution registry or real funds authorization.
- `mark_signed` only records caller-synthesized hashes without signing.
- `reconcile` only records caller-verified offline results without asserting
  on-chain authenticity.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Any


class LedgerError(ValueError):
    """Base error for reservation ledger validation and runtime failures."""


class ExecutionLatched(LedgerError):
    """A pending, paused or exhausted wallet cannot reserve another transaction."""


@dataclass(frozen=True)
class IntentIdentity:
    """Frozen identity assertion for atomic intent operations.

    RESEARCH ONLY:
    - Verifies intent state consistency across concurrency boundaries.
    - Not an authorization or cryptographic credential.
    """

    id: str
    chain: int
    wallet: str
    base: str
    nonce: int
    payload: str | dict[str, Any]
    reserve: str
    one_shot: bool

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise LedgerError("Invalid intent identity id")
        _validate_atomic_int(self.chain, "chain", positive=True)
        _validate_atomic_int(self.nonce, "nonce")
        norm_wallet = _validate_hex(self.wallet, 20, "wallet")
        object.__setattr__(self, "wallet", norm_wallet)
        if not isinstance(self.base, str) or not self.base.strip():
            raise LedgerError("Invalid intent identity base")
        if type(self.one_shot) is not bool:
            raise LedgerError("Invalid intent identity one_shot: must be bool")

        res_dec = _validate_decimal(self.reserve, "reserve", positive=True)
        object.__setattr__(self, "reserve", str(res_dec))

        if isinstance(self.payload, dict):
            encoded = json.dumps(self.payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
            object.__setattr__(self, "payload", encoded)
        elif isinstance(self.payload, str):
            if not self.payload:
                raise LedgerError("Empty payload in intent identity")
            try:
                parsed = json.loads(self.payload)
                encoded = json.dumps(parsed, sort_keys=True, separators=(",", ":"), allow_nan=False)
                object.__setattr__(self, "payload", encoded)
            except Exception as exc:
                raise LedgerError("Invalid JSON payload in intent identity") from exc
        else:
            raise LedgerError(f"Invalid payload type: {type(self.payload).__name__}")


class IdentityMismatchError(ExecutionLatched):
    """An operation targets a different persisted reservation identity."""


FundsError = LedgerError


def _validate_atomic_int(value: Any, name: str, *, positive: bool = False) -> int:
    """Validate an unsigned EVM integer quantity."""
    if type(value) is not int or isinstance(value, bool):
        raise LedgerError(f"Invalid {name}: must be an integer")
    if value < (1 if positive else 0) or value >= 2**256:
        raise LedgerError(f"Invalid {name}: integer out of bounds")
    return value


def _validate_hex(value: Any, size_bytes: int, name: str = "hex") -> str:
    """Normalize fixed-width hex addresses and hashes."""
    if isinstance(value, bytes):
        value = "0x" + value.hex()
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]+", value):
        raise LedgerError(f"Invalid {name}: not a valid hex string")
    if len(value) != 2 + size_bytes * 2:
        raise LedgerError(
            f"Invalid {name} length: expected {size_bytes * 2} hex chars, got {len(value) - 2}"
        )
    return value.lower()


def _validate_decimal(
    value: Any,
    name: str,
    *,
    positive: bool = False,
    non_negative: bool = False,
) -> Decimal:
    """Validate Decimal values rejecting bools, NaNs, infinities, and invalid bounds."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise LedgerError(f"Invalid {name}: must be numeric")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise LedgerError(f"Invalid {name}") from exc
    if not result.is_finite():
        raise LedgerError(f"Invalid {name}: must be finite")
    if positive and result <= Decimal(0):
        raise LedgerError(f"Invalid {name}: must be strictly positive")
    if non_negative and result < Decimal(0):
        raise LedgerError(f"Invalid {name}: cannot be negative")
    return result


def _fraction_to_decimal(f: Fraction) -> Decimal:
    """Convert a terminating decimal fraction back to Decimal exact representation.

    Avoids dependency on Decimal context precision / rounding settings.
    """
    if f == 0:
        return Decimal("0")
    sign = "-" if f < 0 else ""
    f = abs(f)
    num, den = f.numerator, f.denominator
    n2 = 0
    temp_den = den
    while temp_den % 2 == 0:
        temp_den //= 2
        n2 += 1
    n5 = 0
    while temp_den % 5 == 0:
        temp_den //= 5
        n5 += 1
    if temp_den != 1:
        raise LedgerError(f"Non-terminating decimal fraction: {f}")
    k = max(n2, n5)
    num = num * (2 ** (k - n2)) * (5 ** (k - n5))
    s = str(num).zfill(k + 1)
    if k == 0:
        res = s
    else:
        res = s[:-k] + "." + s[-k:]
    return Decimal(sign + res)


class FundsLedger:
    """Durable intent state; all writers must use the same configured ledger path.

    The caller supplies an explicit private runtime path, allowed base symbols,
    and loss budget. No production path or historical defaults are embedded.
    BEGIN IMMEDIATE arbitrates threads/processes and FULL synchronous commits
    precede any next pipeline step.
    Persistent policy metadata binds allowed bases and budget to the database.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        allowed_bases: frozenset[str],
        loss_budget_usd: Decimal,
    ) -> None:
        p = Path(path)
        if not p.is_absolute() or p.is_symlink() or not p.parent.is_dir():
            raise LedgerError("Ledger requires an absolute regular runtime path")
        if p.exists() and not p.is_file():
            raise LedgerError("Ledger requires an absolute regular runtime path")
        self._path = p

        if not isinstance(allowed_bases, frozenset):
            raise LedgerError("allowed_bases must be a frozenset[str]")
        if not allowed_bases:
            raise LedgerError("allowed_bases cannot be empty")
        for b in allowed_bases:
            if not isinstance(b, str) or not b.strip() or b != b.strip():
                raise LedgerError(f"Invalid base symbol: {b!r}")
        self._allowed_bases = allowed_bases

        if type(loss_budget_usd) is not Decimal:
            raise LedgerError("loss_budget_usd must be a Decimal")
        if not loss_budget_usd.is_finite() or loss_budget_usd <= Decimal(0):
            raise LedgerError("loss_budget_usd must be positive and finite")
        self._loss_budget_usd = loss_budget_usd
        self._budget_fraction = Fraction(loss_budget_usd)

        self._canonical_allowed_bases_json = json.dumps(
            sorted(self._allowed_bases), separators=(",", ":")
        )
        self._canonical_loss_budget_str = str(_fraction_to_decimal(self._budget_fraction))

        with self._transaction(init=True):
            pass

    @property
    def path(self) -> Path:
        """Ledger SQLite database path."""
        return self._path

    @property
    def allowed_bases(self) -> frozenset[str]:
        """Immutable set of allowed base currency symbols."""
        return self._allowed_bases

    @property
    def loss_budget_usd(self) -> Decimal:
        """Configured global loss budget in USD."""
        return self._loss_budget_usd

    def _ensure_tables_exist(self, db: sqlite3.Connection) -> None:
        schema = """
            CREATE TABLE IF NOT EXISTS metadata (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                schema_version INTEGER NOT NULL,
                allowed_bases TEXT NOT NULL,
                loss_budget_usd TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS wallets (
                chain INTEGER NOT NULL, wallet TEXT NOT NULL,
                spent TEXT NOT NULL DEFAULT '0', paused INTEGER NOT NULL DEFAULT 0,
                pending TEXT, PRIMARY KEY (chain, wallet));
            CREATE TABLE IF NOT EXISTS bases (
                chain INTEGER NOT NULL, wallet TEXT NOT NULL, base TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'PROBE', PRIMARY KEY (chain, wallet, base));
            CREATE TABLE IF NOT EXISTS intents (
                id TEXT PRIMARY KEY, chain INTEGER NOT NULL, wallet TEXT NOT NULL,
                base TEXT NOT NULL, nonce INTEGER NOT NULL, payload TEXT NOT NULL,
                reserve TEXT NOT NULL, state TEXT NOT NULL, hash TEXT,
                net TEXT, gas TEXT, receipt_id TEXT, receipt_status INTEGER,
                one_shot INTEGER NOT NULL,
                UNIQUE (chain, wallet, nonce));
            CREATE TABLE IF NOT EXISTS resume_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chain INTEGER NOT NULL, wallet TEXT NOT NULL,
                reason TEXT NOT NULL, spent_before TEXT NOT NULL,
                budget_before TEXT NOT NULL, timestamp INTEGER NOT NULL);
            """
        for statement in schema.split(";"):
            if statement.strip():
                db.execute(statement)
        columns = {row["name"] for row in db.execute("PRAGMA table_info(intents)")}
        if "receipt_status" not in columns:
            db.execute("ALTER TABLE intents ADD COLUMN receipt_status INTEGER")

    def _verify_policy(self, db: sqlite3.Connection) -> None:
        """Verify that persisted database policy matches immutable instance policy."""
        # Instance self-consistency check (prevents in-memory tampering)
        if (
            type(self._loss_budget_usd) is not Decimal
            or not self._loss_budget_usd.is_finite()
            or self._loss_budget_usd <= Decimal(0)
            or Fraction(self._loss_budget_usd) != self._budget_fraction
            or str(_fraction_to_decimal(self._budget_fraction)) != self._canonical_loss_budget_str
        ):
            raise LedgerError("Tampered instance loss budget state")

        if (
            not isinstance(self._allowed_bases, frozenset)
            or json.dumps(sorted(self._allowed_bases), separators=(",", ":"))
            != self._canonical_allowed_bases_json
        ):
            raise LedgerError("Tampered instance allowed_bases state")

        master_tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        if "metadata" not in master_tables:
            raise LedgerError("Policy metadata table missing from database")

        rows = db.execute("SELECT * FROM metadata").fetchall()
        if len(rows) != 1:
            raise LedgerError(
                f"Corrupt policy metadata: expected exactly one row, got {len(rows)}"
            )

        row = rows[0]
        cols = set(row.keys())
        required_cols = {"id", "schema_version", "allowed_bases", "loss_budget_usd"}
        if not required_cols.issubset(cols):
            raise LedgerError("Corrupt policy metadata: missing required columns")

        if row["id"] != 1:
            raise LedgerError("Corrupt policy metadata: invalid singleton id")

        if type(row["schema_version"]) is not int or row["schema_version"] != 1:
            raise LedgerError(f"Unsupported schema version: {row['schema_version']!r}")

        # Validate allowed_bases format and equivalence
        raw_bases = row["allowed_bases"]
        if not isinstance(raw_bases, str):
            raise LedgerError("Corrupt allowed_bases in metadata: must be string")
        try:
            parsed_bases = json.loads(raw_bases)
        except Exception as exc:
            raise LedgerError("Corrupt allowed_bases in metadata: invalid JSON") from exc

        if not isinstance(parsed_bases, list) or not parsed_bases:
            raise LedgerError("Corrupt allowed_bases in metadata: must be non-empty list")

        for b in parsed_bases:
            if not isinstance(b, str) or not b.strip() or b != b.strip():
                raise LedgerError(f"Corrupt base symbol in metadata: {b!r}")

        bases_set = frozenset(parsed_bases)
        if len(bases_set) != len(parsed_bases):
            raise LedgerError("Corrupt allowed_bases in metadata: duplicate symbols")

        canonical_persisted_bases = json.dumps(sorted(bases_set), separators=(",", ":"))
        if raw_bases != canonical_persisted_bases:
            raise LedgerError("Corrupt allowed_bases in metadata: not canonically formatted")

        if bases_set != self._allowed_bases:
            raise LedgerError(
                "Policy mismatch: allowed_bases does not match persisted ledger policy"
            )

        # Validate loss_budget_usd format and equivalence
        raw_budget = row["loss_budget_usd"]
        if not isinstance(raw_budget, str):
            raise LedgerError("Corrupt loss_budget_usd in metadata: must be string")

        persisted_budget_dec = _validate_decimal(
            raw_budget, "persisted loss_budget_usd", positive=True
        )
        if Fraction(persisted_budget_dec) != self._budget_fraction:
            raise LedgerError(
                "Policy mismatch: loss_budget_usd does not match persisted ledger policy"
            )

    def _init_or_verify_policy(self, db: sqlite3.Connection) -> None:
        """Initialize empty database or verify policy on existing database under lock."""
        existing_tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }

        if "metadata" in existing_tables:
            self._verify_policy(db)
            self._ensure_tables_exist(db)
        else:
            # Check if any user tables already contain data without policy metadata
            for tbl in existing_tables:
                count = db.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                if count > 0:
                    raise LedgerError(
                        f"Existing database table '{tbl}' has data but missing policy metadata"
                    )

            # Fresh empty DB: create tables and bind singleton policy
            self._ensure_tables_exist(db)
            db.execute(
                "INSERT INTO metadata (id, schema_version, allowed_bases, loss_budget_usd) "
                "VALUES (1, 1, ?, ?)",
                (self._canonical_allowed_bases_json, self._canonical_loss_budget_str),
            )

    @contextmanager
    def _transaction(
        self, *, init: bool = False
    ) -> Generator[sqlite3.Connection, None, None]:
        db = sqlite3.connect(self._path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            if init:
                self._init_or_verify_policy(db)
            else:
                self._verify_policy(db)
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def status(self, chain: int, wallet: str, base: str) -> dict[str, Any]:
        """Read persisted latch/budget; absent base is always a fresh probe."""
        chain_id = _validate_atomic_int(chain, "chain", positive=True)
        wallet_addr = _validate_hex(wallet, 20, "wallet")
        if base not in self._allowed_bases:
            raise LedgerError("Unsupported base")
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM wallets WHERE chain=? AND wallet=?", (chain_id, wallet_addr)
            ).fetchone()
            base_row = db.execute(
                "SELECT mode FROM bases WHERE chain=? AND wallet=? AND base=?",
                (chain_id, wallet_addr, base),
            ).fetchone()
            spent = _validate_decimal(row["spent"], "spent") if row else Decimal(0)
            if spent < Decimal(0):
                raise LedgerError("Corrupt negative loss ledger")
            pending = row["pending"] if row else None
            reserved = Decimal(0)
            if pending:
                intent = db.execute(
                    "SELECT reserve FROM intents WHERE id=?", (pending,)
                ).fetchone()
                if intent is None:
                    raise LedgerError("Pending wallet has no reserved intent")
                reserved = _validate_decimal(intent["reserve"], "reserved loss", positive=True)
            remaining_frac = max(
                Fraction(0), self._budget_fraction - Fraction(spent) - Fraction(reserved)
            )
            return {
                "mode": base_row["mode"] if base_row else "PROBE",
                "spent_usd": spent,
                "reserved_usd": reserved,
                "remaining_usd": _fraction_to_decimal(remaining_frac),
                "pending": pending,
                "paused": bool(row["paused"]) if row else False,
            }

    def reserve(
        self,
        *,
        intent_id: str,
        chain: int,
        wallet: str,
        base: str,
        nonce: int,
        payload: dict[str, Any],
        worst_loss_usd: Decimal,
        running: bool,
        auto_execute: bool,
        one_shot: bool,
    ) -> None:
        """Recheck permission and reserve a single wallet-wide intent atomically."""
        if running is not True or auto_execute is not True:
            raise ExecutionLatched("Execution mode/running latch closed")
        chain_id = _validate_atomic_int(chain, "chain", positive=True)
        nonce_val = _validate_atomic_int(nonce, "nonce")
        wallet_addr = _validate_hex(wallet, 20, "wallet")
        if (
            base not in self._allowed_bases
            or not intent_id
            or not isinstance(intent_id, str)
            or type(one_shot) is not bool
        ):
            raise LedgerError("Invalid intent")
        reserve_dec = _validate_decimal(worst_loss_usd, "worst loss", positive=True)
        reserve_frac = Fraction(reserve_dec)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with self._transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO wallets(chain,wallet) VALUES(?,?)",
                (chain_id, wallet_addr),
            )
            db.execute(
                "INSERT OR IGNORE INTO bases(chain,wallet,base) VALUES(?,?,?)",
                (chain_id, wallet_addr, base),
            )
            account = db.execute(
                "SELECT * FROM wallets WHERE chain=? AND wallet=?",
                (chain_id, wallet_addr),
            ).fetchone()
            mode = db.execute(
                "SELECT mode FROM bases WHERE chain=? AND wallet=? AND base=?",
                (chain_id, wallet_addr, base),
            ).fetchone()["mode"]
            if account["paused"] or account["pending"] or mode not in ("PROBE", "NORMAL"):
                raise ExecutionLatched("Wallet/base pending or paused")
            spent_dec = _validate_decimal(account["spent"], "spent")
            spent_frac = Fraction(spent_dec)
            if spent_dec < Decimal(0) or reserve_frac > self._budget_fraction - spent_frac:
                raise ExecutionLatched("Wallet-global loss budget exhausted")
            db.execute(
                "INSERT INTO intents(id,chain,wallet,base,nonce,payload,reserve,state,one_shot) "
                "VALUES(?,?,?,?,?,?,?,'RESERVED',?)",
                (
                    intent_id,
                    chain_id,
                    wallet_addr,
                    base,
                    nonce_val,
                    encoded,
                    str(reserve_dec),
                    int(one_shot),
                ),
            )
            db.execute(
                "UPDATE wallets SET pending=? WHERE chain=? AND wallet=?",
                (intent_id, chain_id, wallet_addr),
            )

    def pending_intent(self, chain: int, wallet: str) -> dict[str, Any] | None:
        """Read the durable intent for read-only receipt recovery after a restart."""
        chain_id = _validate_atomic_int(chain, "chain", positive=True)
        wallet_addr = _validate_hex(wallet, 20, "wallet")
        with self._transaction() as db:
            row = db.execute(
                "SELECT i.* FROM wallets w LEFT JOIN intents i ON i.id=w.pending "
                "WHERE w.chain=? AND w.wallet=? AND w.pending IS NOT NULL",
                (chain_id, wallet_addr),
            ).fetchone()
            if row is None:
                return None
            if row["id"] is None or row["chain"] != chain_id or row["wallet"] != wallet_addr:
                raise LedgerError("Corrupt pending intent")
            result = dict(row)
            result["payload"] = json.loads(result["payload"])
            return result

    def _verify_identity_row(self, row: sqlite3.Row, expected: IntentIdentity) -> None:
        """Verify persisted intent row strictly matches expected identity within active transaction."""
        if row["id"] != expected.id:
            raise IdentityMismatchError(f"Intent id mismatch: {row['id']!r} != {expected.id!r}")
        if row["chain"] != expected.chain:
            raise IdentityMismatchError(f"Intent chain mismatch: {row['chain']} != {expected.chain}")
        if row["wallet"] != expected.wallet:
            raise IdentityMismatchError(f"Intent wallet mismatch: {row['wallet']!r} != {expected.wallet!r}")
        if row["base"] != expected.base:
            raise IdentityMismatchError(f"Intent base mismatch: {row['base']!r} != {expected.base!r}")
        if row["nonce"] != expected.nonce:
            raise IdentityMismatchError(f"Intent nonce mismatch: {row['nonce']} != {expected.nonce}")
        if row["payload"] != expected.payload:
            raise IdentityMismatchError("Intent payload mismatch")
        if Fraction(Decimal(row["reserve"])) != Fraction(Decimal(expected.reserve)):
            raise IdentityMismatchError(f"Intent reserve mismatch: {row['reserve']} != {expected.reserve}")
        if bool(row["one_shot"]) != bool(expected.one_shot):
            raise IdentityMismatchError(f"Intent one_shot mismatch: {row['one_shot']} != {expected.one_shot}")

    def mark_signed(
        self,
        intent_id: str,
        tx_hash: str,
        *,
        expected_identity: IntentIdentity | None = None,
    ) -> None:
        """Persist local signed hash before handing bytes to any RPC transport."""
        if not isinstance(intent_id, str) or not intent_id:
            raise LedgerError("Invalid intent_id")
        if expected_identity is not None and not isinstance(expected_identity, IntentIdentity):
            raise LedgerError("Invalid expected_identity")
        digest = _validate_hex(tx_hash, 32, "tx_hash")
        with self._transaction() as db:
            if expected_identity is not None:
                row = db.execute("SELECT * FROM intents WHERE id=?", (intent_id,)).fetchone()
                if row is None or row["state"] != "RESERVED":
                    raise ExecutionLatched("Intent is not reserved")
                self._verify_identity_row(row, expected_identity)
            changed = db.execute(
                "UPDATE intents SET state='SIGNED', hash=? WHERE id=? AND state='RESERVED'",
                (digest, intent_id),
            ).rowcount
            if changed != 1:
                raise ExecutionLatched("Intent is not reserved")

    def mark_unknown(
        self,
        intent_id: str,
        *,
        expected_identity: IntentIdentity | None = None,
    ) -> None:
        """Broadcast error/receipt timeout keeps the pending latch across restarts."""
        if not isinstance(intent_id, str) or not intent_id:
            raise LedgerError("Invalid intent_id")
        if expected_identity is not None and not isinstance(expected_identity, IntentIdentity):
            raise LedgerError("Invalid expected_identity")
        with self._transaction() as db:
            if expected_identity is not None:
                row = db.execute("SELECT * FROM intents WHERE id=?", (intent_id,)).fetchone()
                if row is None or row["state"] not in ("SIGNED", "UNKNOWN"):
                    raise ExecutionLatched("Unknown broadcast requires a signed intent")
                self._verify_identity_row(row, expected_identity)
            changed = db.execute(
                "UPDATE intents SET state='UNKNOWN' WHERE id=? AND state IN ('SIGNED','UNKNOWN')",
                (intent_id,),
            ).rowcount
            if changed != 1:
                raise ExecutionLatched("Unknown broadcast requires a signed intent")

    def cancel_before_signing(
        self,
        intent_id: str,
        *,
        expected_identity: IntentIdentity | None = None,
    ) -> None:
        """Only a proven pre-sign failure can release a reservation with zero gas."""
        if not isinstance(intent_id, str) or not intent_id:
            raise LedgerError("Invalid intent_id")
        if expected_identity is not None and not isinstance(expected_identity, IntentIdentity):
            raise LedgerError("Invalid expected_identity")
        with self._transaction() as db:
            row = db.execute("SELECT * FROM intents WHERE id=?", (intent_id,)).fetchone()
            if row is None or row["state"] != "RESERVED":
                raise ExecutionLatched("Cannot cancel a potentially broadcast intent")
            if expected_identity is not None:
                self._verify_identity_row(row, expected_identity)
            db.execute("DELETE FROM intents WHERE id=?", (intent_id,))
            db.execute(
                "UPDATE wallets SET pending=NULL WHERE chain=? AND wallet=? AND pending=?",
                (row["chain"], row["wallet"], intent_id),
            )

    def reconcile(
        self,
        intent_id: str,
        *,
        tx_hash: str,
        block_hash: str,
        net_profit_usd: Decimal,
        actual_gas_usd: Decimal,
        receipt_status: int,
        expected_identity: IntentIdentity | None = None,
    ) -> None:
        """Persist independently validated canonical receipt once; never reset loss."""
        if not isinstance(intent_id, str) or not intent_id:
            raise LedgerError("Invalid intent_id")
        if expected_identity is not None and not isinstance(expected_identity, IntentIdentity):
            raise LedgerError("Invalid expected_identity")
        digest = _validate_hex(tx_hash, 32, "tx_hash")
        block_digest = _validate_hex(block_hash, 32, "block_hash")
        receipt_id = digest + ":" + block_digest
        net = _validate_decimal(net_profit_usd, "net profit")
        gas = _validate_decimal(actual_gas_usd, "gas", non_negative=True)
        if type(receipt_status) is not int or receipt_status not in (0, 1):
            raise LedgerError("Unknown receipt status")
        if receipt_status == 0 and Fraction(net) != -Fraction(gas):
            raise LedgerError("Revert must retain exactly its gas loss")
        with self._transaction() as db:
            row = db.execute("SELECT * FROM intents WHERE id=?", (intent_id,)).fetchone()
            if row is None:
                if expected_identity is not None:
                    raise IdentityMismatchError(f"Intent {intent_id!r} not found")
                raise ExecutionLatched("Receipt does not match persisted intent")
            if expected_identity is not None:
                self._verify_identity_row(row, expected_identity)
            if row["hash"] != digest:
                raise ExecutionLatched("Receipt does not match persisted intent")
            if row["state"] == "RECONCILED":
                persisted_net = _validate_decimal(row["net"], "persisted net")
                persisted_gas = _validate_decimal(row["gas"], "persisted gas")
                if (
                    row["receipt_id"] != receipt_id
                    or row["receipt_status"] != receipt_status
                    or Fraction(persisted_net) != Fraction(net)
                    or Fraction(persisted_gas) != Fraction(gas)
                ):
                    raise ExecutionLatched("Conflicting receipt/reorg requires review")
                return
            if row["state"] not in ("SIGNED", "UNKNOWN"):
                raise ExecutionLatched("Intent cannot be reconciled")
            account = db.execute(
                "SELECT * FROM wallets WHERE chain=? AND wallet=?",
                (row["chain"], row["wallet"]),
            ).fetchone()
            if account is None or account["pending"] != intent_id:
                raise ExecutionLatched("Pending intent mismatch")
            spent_dec = _validate_decimal(account["spent"], "spent")
            spent_frac = Fraction(spent_dec)
            loss_frac = max(Fraction(0), -Fraction(net))
            new_spent_frac = spent_frac + loss_frac
            new_spent_dec = _fraction_to_decimal(new_spent_frac)
            paused = (
                receipt_status == 0
                or net <= Decimal(0)
                or bool(row["one_shot"])
                or new_spent_frac >= self._budget_fraction
            )
            db.execute(
                "UPDATE wallets SET spent=?, paused=?, pending=NULL WHERE chain=? AND wallet=?",
                (str(new_spent_dec), int(paused), row["chain"], row["wallet"]),
            )
            base_mode = (
                "RECONCILED_PROFIT" if net > Decimal(0) and receipt_status == 1 else "PAUSED"
            )
            db.execute(
                "UPDATE bases SET mode=? WHERE chain=? AND wallet=? AND base=?",
                (base_mode, row["chain"], row["wallet"], row["base"]),
            )
            db.execute(
                "UPDATE intents SET state='RECONCILED', net=?, gas=?, receipt_id=?, "
                "receipt_status=? WHERE id=?",
                (str(net), str(gas), receipt_id, receipt_status, intent_id),
            )

    def promote(self, chain: int, wallet: str, base: str) -> None:
        """Explicitly upgrade only a reconciled profitable base with an open wallet."""
        chain_id = _validate_atomic_int(chain, "chain", positive=True)
        wallet_addr = _validate_hex(wallet, 20, "wallet")
        if base not in self._allowed_bases:
            raise LedgerError("Unsupported base")
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM wallets WHERE chain=? AND wallet=?", (chain_id, wallet_addr)
            ).fetchone()
            if row is None or row["paused"] or row["pending"]:
                raise ExecutionLatched("Wallet is paused or pending")
            changed = db.execute(
                "UPDATE bases SET mode='NORMAL' WHERE chain=? AND wallet=? AND base=? "
                "AND mode='RECONCILED_PROFIT'",
                (chain_id, wallet_addr, base),
            ).rowcount
            if changed != 1:
                raise ExecutionLatched("Base has no reconciled positive receipt")

    def resume_wallet(
        self,
        chain: int,
        wallet: str,
        *,
        reason: str,
        timestamp: int | None = None,
    ) -> None:
        """Explicitly resume a paused wallet after manual verification without resetting loss.

        Gating rules:
        - reason must be non-empty after stripping whitespace, no default.
        - single BEGIN IMMEDIATE transaction with policy verified.
        - wallet must exist and have non-negative, finite spent amount.
        - loss budget must not be exhausted (remaining budget > 0).
        - no pending or unresolved intents (RESERVED, SIGNED, UNKNOWN) for this wallet.
        - wallet must currently be paused; duplicate resume on active wallet rejected.
        - resets paused=0 on wallet; resets PAUSED bases to PROBE (never automatically NORMAL).
        - spent, metadata budget/bases, intents are never modified or cleared.
        - persists audit row into resume_audit table with pre-resume spent and budget.
        - on any rejection, no audit row is written and no state is mutated.
        """
        if not isinstance(reason, str) or not reason.strip():
            raise LedgerError("Resume reason must be a non-empty string")
        clean_reason = reason.strip()
        chain_id = _validate_atomic_int(chain, "chain", positive=True)
        wallet_addr = _validate_hex(wallet, 20, "wallet")
        if timestamp is not None and (type(timestamp) is not int or timestamp < 0):
            raise LedgerError("Invalid timestamp")
        audit_ts = int(time.time()) if timestamp is None else timestamp

        with self._transaction() as db:
            account = db.execute(
                "SELECT * FROM wallets WHERE chain=? AND wallet=?",
                (chain_id, wallet_addr),
            ).fetchone()
            if account is None:
                raise ExecutionLatched(f"Wallet {wallet_addr} not found")

            # Validate spent format and non-negativity
            spent_dec = _validate_decimal(account["spent"], "spent", non_negative=True)
            spent_frac = Fraction(spent_dec)
            remaining_frac = self._budget_fraction - spent_frac
            if remaining_frac <= 0:
                raise ExecutionLatched("Loss budget exhausted, wallet cannot be resumed")

            # Check for any pending or unresolved intent
            if account["pending"] is not None:
                raise ExecutionLatched(
                    f"Wallet {wallet_addr} has pending intent {account['pending']!r}"
                )
            unresolved = db.execute(
                "SELECT id, state FROM intents WHERE chain=? AND wallet=? "
                "AND state IN ('RESERVED', 'SIGNED', 'UNKNOWN') LIMIT 1",
                (chain_id, wallet_addr),
            ).fetchone()
            if unresolved is not None:
                raise ExecutionLatched(
                    f"Wallet {wallet_addr} has unresolved intent {unresolved['id']!r} in state {unresolved['state']!r}"
                )

            # A recovery must not normalize corrupt state or manufacture budget.
            if type(account["paused"]) is not int or account["paused"] not in (0, 1):
                raise LedgerError("Invalid paused state")
            base_rows = db.execute(
                "SELECT base, mode FROM bases WHERE chain=? AND wallet=?",
                (chain_id, wallet_addr),
            ).fetchall()
            modes = {row["base"]: row["mode"] for row in base_rows}
            if len(modes) != len(base_rows) or not modes:
                raise LedgerError("Inconsistent wallet base records")
            for symbol, mode in modes.items():
                if symbol not in self.allowed_bases or mode not in (
                    "PROBE", "NORMAL", "RECONCILED_PROFIT", "PAUSED"
                ):
                    raise LedgerError("Invalid base state")
            historical_loss = Fraction(0)
            history = db.execute(
                "SELECT * FROM intents WHERE chain=? AND wallet=?",
                (chain_id, wallet_addr),
            ).fetchall()
            for intent in history:
                if intent["state"] != "RECONCILED":
                    raise LedgerError("Invalid or unresolved intent state")
                if intent["base"] not in modes:
                    raise LedgerError("Inconsistent intent base")
                status = intent["receipt_status"]
                if type(status) is not int or status not in (0, 1):
                    raise LedgerError("Invalid reconciled receipt status")
                digest = _validate_hex(intent["hash"], 32, "receipt hash")
                receipt_id = intent["receipt_id"]
                if not isinstance(receipt_id, str):
                    raise LedgerError("Invalid reconciled receipt identity")
                parts = receipt_id.split(":")
                if len(parts) != 2 or parts[0] != digest:
                    raise LedgerError("Inconsistent reconciled receipt identity")
                _validate_hex(parts[1], 32, "receipt block hash")
                net = Fraction(_validate_decimal(intent["net"], "historical net"))
                gas = Fraction(_validate_decimal(intent["gas"], "historical gas", non_negative=True))
                if status == 0 and net != -gas:
                    raise LedgerError("Inconsistent revert loss and gas")
                historical_loss += max(Fraction(0), -net)
            if historical_loss != spent_frac:
                raise LedgerError("Inconsistent spent and reconciled historical loss")

            # Wallet must currently be paused
            if account["paused"] == 0:
                raise ExecutionLatched(f"Wallet {wallet_addr} is not paused")

            # Unpause wallet (spent, metadata, intents are NOT modified)
            db.execute(
                "UPDATE wallets SET paused=0 WHERE chain=? AND wallet=?",
                (chain_id, wallet_addr),
            )

            # Revert PAUSED bases to PROBE (never automatically NORMAL)
            db.execute(
                "UPDATE bases SET mode='PROBE' WHERE chain=? AND wallet=? AND mode='PAUSED'",
                (chain_id, wallet_addr),
            )

            # Persist resume audit record
            db.execute(
                "INSERT INTO resume_audit (chain, wallet, reason, spent_before, budget_before, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    chain_id,
                    wallet_addr,
                    clean_reason,
                    str(spent_dec),
                    self._canonical_loss_budget_str,
                    audit_ts,
                ),
            )

    def resume_audits(self, chain: int, wallet: str) -> list[dict[str, Any]]:
        """Read durable resume audit records for a wallet."""
        chain_id = _validate_atomic_int(chain, "chain", positive=True)
        wallet_addr = _validate_hex(wallet, 20, "wallet")
        with self._transaction() as db:
            rows = db.execute(
                "SELECT id, chain, wallet, reason, spent_before, budget_before, timestamp "
                "FROM resume_audit WHERE chain=? AND wallet=? ORDER BY id ASC",
                (chain_id, wallet_addr),
            ).fetchall()
            return [dict(r) for r in rows]
