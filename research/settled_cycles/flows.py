"""Reconstruct action graphs and multi-subject balance vectors from evidence."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from arbitrage_contracts.identity import (
    AssetRef,
    PoolIdKind,
    PoolKey,
    TokenKey,
    validate_evm_address,
)
from research.settled_cycles.decoders import (
    DecodedSwapV3,
    DecodedSwapV4,
    DecodedTransfer,
    DecodedUnwrap,
    DecodedWrap,
    UnsupportedLog,
    decode_log,
)
from research.settled_cycles.evidence import EvidenceBundle
from research.settled_cycles.models import ActionKind, CycleAction, SubjectBalanceDelta

_NATIVE = "native"
_PROTOCOL_ID = "research-w3"


def _erc20(chain_id: int, address: str) -> AssetRef:
    """Create a chain-scoped ERC-20 asset reference."""
    return AssetRef.erc20(TokenKey(chain_id, address))


def _native(chain_id: int) -> AssetRef:
    """Create the chain's native asset reference."""
    return AssetRef.native(chain_id, _NATIVE)


def _pool_key_v3(chain_id: int, pool_address: str) -> PoolKey:
    """Create a V3 pool key from the emitted pool address."""
    return PoolKey(chain_id, _PROTOCOL_ID, "factory", pool_address, PoolIdKind.ADDRESS, pool_address)


def _pool_key_v4(chain_id: int, manager_address: str, pool_id: str) -> PoolKey:
    """Create a V4 pool key from the manager address and bytes32 pool id."""
    return PoolKey(chain_id, _PROTOCOL_ID, "manager", manager_address, PoolIdKind.BYTES32, pool_id)


def _log_key(log: Mapping[str, Any]) -> tuple[int, str, str, int] | None:
    """Return a receipt-log identity key suitable for trace correlation."""
    raw_index = log.get("logIndex")
    raw_index = int(raw_index, 16) if type(raw_index) is str else raw_index
    topics = log.get("topics")
    address = log.get("address")
    data = log.get("data", "0x")
    if (
        type(raw_index) is not int
        or isinstance(raw_index, bool)
        or not isinstance(topics, list)
        or type(address) is not str
        or type(data) is not str
    ):
        return None
    return len(topics), address.lower(), data.lower(), raw_index


def _collect_trace(
    frame: Mapping[str, Any],
    parent_is_reverted: bool,
    trace_address: str,
    native_flows: list[tuple[str, str, int, str]],
) -> dict[tuple[int, str, str, int], bool]:
    """Walk a callTracer frame, prune failed subtrees, and correlate emitted logs."""
    is_reverted = parent_is_reverted or frame.get("error") is not None
    frame_type = frame.get("type")
    raw_value = frame.get("value")
    value = int(raw_value, 16) if type(raw_value) is str else raw_value
    raw_from = frame.get("from")
    raw_to = frame.get("to")
    if (
        not is_reverted
        and frame_type in ("CALL", "CREATE", "CREATE2")
        and type(value) is int
        and value > 0
        and isinstance(raw_from, str)
        and isinstance(raw_to, str)
    ):
        native_flows.append(
            (
                validate_evm_address(raw_from),
                validate_evm_address(raw_to),
                value,
                trace_address,
            )
        )

    ownership: dict[tuple[int, str, str, int], bool] = {}
    prefix = f"{trace_address}/" if trace_address else ""
    calls = frame.get("calls", [])
    if isinstance(calls, list):
        for child_index, child in enumerate(calls):
            if isinstance(child, Mapping):
                ownership.update(_collect_trace(child, is_reverted, f"{prefix}{child_index}", native_flows))

    logs = frame.get("logs", [])
    if isinstance(logs, list):
        for frame_log in logs:
            if isinstance(frame_log, Mapping):
                key = _log_key(frame_log)
                if key is not None:
                    ownership[key] = not is_reverted
    return ownership


def _trace_evidence(bundle: EvidenceBundle) -> tuple[dict[tuple[int, str, str, int], bool], list[tuple[str, str, int, str]]]:
    """Return log ownership and successful native-value flows from an available trace."""
    native_flows: list[tuple[str, str, int, str]] = []
    if not bundle.trace_available or not isinstance(bundle.trace, Mapping):
        return {}, native_flows
    return _collect_trace(bundle.trace, False, "", native_flows), native_flows


def _action(
    step_id: int,
    action_kind: ActionKind,
    *,
    pool_key: PoolKey | None = None,
    asset_in: AssetRef | None = None,
    asset_out: AssetRef | None = None,
    amount_in: int | None = None,
    amount_out: int | None = None,
    direction: str | None = None,
    log_index: int | None,
    trace_address: str | None,
    execution_status: str,
    evidence_refs: tuple[str, ...],
) -> CycleAction:
    """Construct one validated action with common parentage fields."""
    return CycleAction(
        step_id=step_id,
        action_kind=action_kind,
        pool_key=pool_key,
        asset_in=asset_in,
        asset_out=asset_out,
        amount_in_atoms=amount_in,
        amount_out_atoms=amount_out,
        direction=direction,
        log_index=log_index,
        trace_address=trace_address,
        parent_step_id=None,
        execution_status=execution_status,
        evidence_refs=evidence_refs,
    )


def reconstruct_actions_and_flows(
    bundle: EvidenceBundle, pool_registry: Mapping[str, Any] | None = None
) -> tuple[tuple[CycleAction, ...], tuple[SubjectBalanceDelta, ...]]:
    """Reconstruct decoded actions and successful, trace-validated balance deltas."""
    del pool_registry
    chain_id = bundle.chain_id
    tx_status = bundle.receipt.get("status")
    raw_tx_origin = bundle.transaction.get("from")
    if not isinstance(raw_tx_origin, str):
        raise ValueError("bundle transaction missing 'from' address")
    tx_origin = validate_evm_address(raw_tx_origin)
    log_ownership, native_flows = _trace_evidence(bundle)
    actions: list[CycleAction] = []
    deltas: dict[tuple[str, AssetRef], int] = {}

    def add_delta(subject: str, asset: AssetRef, amount: int) -> None:
        deltas[(subject, asset)] = deltas.get((subject, asset), 0) + amount

    for sender, recipient, value, trace_address in native_flows:
        native_asset = _native(chain_id)
        actions.append(
            _action(
                len(actions) + 1,
                ActionKind.TRANSFER,
                asset_in=native_asset,
                asset_out=native_asset,
                amount_in=value,
                amount_out=value,
                direction=f"{sender}->{recipient}",
                log_index=None,
                trace_address=trace_address or None,
                execution_status="success" if tx_status == 1 else "reverted",
                evidence_refs=("trace",),
            )
        )
        if tx_status == 1:
            add_delta(sender, native_asset, -value)
            add_delta(recipient, native_asset, value)

    for raw_log in bundle.receipt.get("logs", []):
        if not isinstance(raw_log, Mapping) or raw_log.get("removed") is True:
            continue
        key = _log_key(raw_log)
        log_is_reverted = key is not None and key in log_ownership and not log_ownership[key]
        if key is not None and log_ownership.get(key) is True:
            log_ownership.pop(key, None)
        decoded = decode_log(raw_log)

        execution_status = "reverted" if tx_status == 0 or log_is_reverted else "success"
        evidence_refs = (
            (f"log:{decoded.log_index}",) if decoded.log_index is not None else ("log",)
        )
        common: dict[str, Any] = {
            "log_index": decoded.log_index,
            "trace_address": None,
            "execution_status": execution_status,
            "evidence_refs": evidence_refs,
        }

        if isinstance(decoded, DecodedTransfer):
            token = _erc20(chain_id, decoded.token_address)
            actions.append(
                _action(
                    len(actions) + 1,
                    ActionKind.TRANSFER,
                    asset_in=token,
                    asset_out=token,
                    amount_in=decoded.value_atoms,
                    amount_out=decoded.value_atoms,
                    direction=f"{decoded.from_address}->{decoded.to_address}",
                    **common,
                )
            )
            if execution_status == "success":
                add_delta(decoded.from_address, token, -decoded.value_atoms)
                add_delta(decoded.to_address, token, decoded.value_atoms)
        elif isinstance(decoded, DecodedWrap):
            weth = _erc20(chain_id, decoded.token_address)
            actions.append(
                _action(
                    len(actions) + 1,
                    ActionKind.WRAP,
                    asset_in=_native(chain_id),
                    asset_out=weth,
                    amount_in=decoded.wad_atoms,
                    amount_out=decoded.wad_atoms,
                    direction=f"{tx_origin}->{decoded.dst}",
                    **common,
                )
            )
            if execution_status == "success":
                add_delta(decoded.dst, _native(chain_id), -decoded.wad_atoms)
                add_delta(decoded.dst, weth, decoded.wad_atoms)
        elif isinstance(decoded, DecodedUnwrap):
            weth = _erc20(chain_id, decoded.token_address)
            actions.append(
                _action(
                    len(actions) + 1,
                    ActionKind.UNWRAP,
                    asset_in=weth,
                    asset_out=_native(chain_id),
                    amount_in=decoded.wad_atoms,
                    amount_out=decoded.wad_atoms,
                    direction=f"{decoded.src}->{tx_origin}",
                    **common,
                )
            )
            if execution_status == "success":
                add_delta(decoded.src, weth, -decoded.wad_atoms)
                add_delta(decoded.src, _native(chain_id), decoded.wad_atoms)
        elif isinstance(decoded, (DecodedSwapV3, DecodedSwapV4)):
            pool_key = (
                _pool_key_v3(chain_id, decoded.pool_address)
                if isinstance(decoded, DecodedSwapV3)
                else _pool_key_v4(chain_id, decoded.manager_address, decoded.pool_id)
            )
            actions.append(
                _action(
                    len(actions) + 1,
                    ActionKind.SWAP,
                    pool_key=pool_key,
                    amount_in=decoded.amount0_atoms,
                    amount_out=decoded.amount1_atoms,
                    direction=f"{decoded.sender}->{decoded.sender}",
                    **common,
                )
            )
        else:
            reason = decoded.reason if isinstance(decoded, UnsupportedLog) else "unknown"
            actions.append(
                _action(
                    len(actions) + 1,
                    ActionKind.UNKNOWN,
                    direction=reason,
                    **common,
                )
            )

    if tx_status == 0:
        return tuple(actions), ()

    ordered = sorted(
        deltas.items(),
        key=lambda item: (
            item[0][0].lower(),
            item[0][1].interface_kind,
            item[0][1].native_identifier or "",
            item[0][1].token_key.address if item[0][1].token_key else "",
        ),
    )
    basis = "mixed" if native_flows else "events"
    return tuple(actions), tuple(
        SubjectBalanceDelta(
            subject_address=subject,
            asset=asset,
            delta_atoms=amount,
            basis=basis,
            completeness="complete",
            reconciliation_diff_atoms=0,
        )
        for (subject, asset), amount in ordered
    )
